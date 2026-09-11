"""Subprocess execution: argv lists, shell=False, explicit cwd, process-group timeouts."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

LineCallback = Callable[[str], None]


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for child processes: the harness virtualenv must not leak into the app."""
    env = dict(os.environ)
    # `uv run ralph.py` puts the harness venv first on PATH; tools found there must never
    # satisfy the application's checks (or be used by the agent in its place).
    harness_bins = {str(Path(sys.prefix) / "bin")}
    if venv := env.pop("VIRTUAL_ENV", None):
        harness_bins.add(str(Path(venv) / "bin"))
    env["PATH"] = os.pathsep.join(
        p for p in env.get("PATH", "").split(os.pathsep) if p and p not in harness_bins
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        env.update(extra)
    return env


class Tail:
    """Keeps the last max_chars characters of a stream of lines."""

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        self.lines: list[str] = []
        self.size = 0
        self.dropped = False

    def add(self, line: str) -> None:
        self.lines.append(line)
        self.size += len(line)
        while self.size > self.max_chars and len(self.lines) > 1:
            self.size -= len(self.lines.pop(0))
            self.dropped = True

    def text(self) -> str:
        body = "".join(self.lines)
        return ("[... earlier output truncated ...]\n" + body) if self.dropped else body


@dataclass
class ProcResult:
    argv: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        text = self.stdout
        if self.stderr.strip():
            text += ("\n" if text and not text.endswith("\n") else "") + self.stderr
        if self.timed_out:
            text += f"\n[timed out after {self.duration_s:.0f}s; process group terminated]"
        return text


def terminate_group(proc: subprocess.Popen[str], grace_s: float = 5.0) -> None:
    """SIGTERM the whole process group, then SIGKILL whatever survives."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=grace_s)
            # The leader exited; make sure no member of the group survives it.
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except subprocess.TimeoutExpired:
            continue
        except (ProcessLookupError, PermissionError):
            return


def run(
    argv: Sequence[str],
    cwd: Path,
    timeout_s: float | None,
    *,
    env: Mapping[str, str] | None = None,
    stdin_text: str | None = None,
    on_stdout: LineCallback | None = None,
    on_stderr: LineCallback | None = None,
    log_path: Path | None = None,
    max_chars: int = 100_000,
) -> ProcResult:
    """Run argv, streaming lines to callbacks and an optional log file.

    Raises FileNotFoundError when the executable does not exist. KeyboardInterrupt
    terminates the child process group before propagating.
    """
    start = time.monotonic()
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else child_env(),
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        start_new_session=True,
    )
    log = log_path.open("a", encoding="utf-8") if log_path else None
    lock = threading.Lock()
    tails = {"stdout": Tail(max_chars), "stderr": Tail(max_chars)}

    def pump(stream: IO[str], name: str, callback: LineCallback | None) -> None:
        for line in stream:
            tails[name].add(line)
            if log:
                with lock:
                    log.write(line if name == "stdout" else f"[stderr] {line}")
                    log.flush()
            if callback:
                callback(line.rstrip("\n"))

    assert proc.stdout is not None and proc.stderr is not None
    threads = [
        threading.Thread(target=pump, args=(proc.stdout, "stdout", on_stdout), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, "stderr", on_stderr), daemon=True),
    ]
    if stdin_text is not None:
        threads.append(threading.Thread(target=_feed, args=(proc, stdin_text), daemon=True))
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_group(proc)
    except BaseException:
        terminate_group(proc)
        raise
    finally:
        for thread in threads:
            thread.join(timeout=5)
        if log:
            log.close()

    return ProcResult(
        argv=list(argv),
        returncode=None if timed_out else proc.returncode,
        stdout=tails["stdout"].text(),
        stderr=tails["stderr"].text(),
        timed_out=timed_out,
        duration_s=time.monotonic() - start,
    )


def _feed(proc: subprocess.Popen[str], text: str) -> None:
    assert proc.stdin is not None
    try:
        proc.stdin.write(text)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
