"""Coding-CLI adapters. All CLI-specific behavior lives here, outside the loop.

A runner performs exactly one fresh, non-interactive agent invocation per call and
reports what the CLI did. It never decides whether work is complete.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ralph import proc
from ralph.config import CommandConfig, ConfigError, OpenCodeConfig

Echo = Callable[[str], None]
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CONTINUATION_FLAGS = ("-c", "--continue", "-s", "--session", "--fork", "--auto")


@dataclass
class InvocationResult:
    exit_code: int | None = 0
    timed_out: bool = False
    duration_s: float = 0.0
    final_text: str = ""
    session_id: str | None = None
    tool_calls: int = 0
    tool_errors: list[str] = field(default_factory=list)
    permission_denials: list[str] = field(default_factory=list)
    cli_error: str | None = None
    fatal: bool = False  # the CLI cannot run at all (e.g. binary missing)

    @property
    def ok(self) -> bool:
        return (
            self.exit_code == 0 and not self.timed_out and self.cli_error is None and not self.fatal
        )

    def summary(self) -> str:
        state = "ok" if self.ok else "FAILED"
        if self.timed_out:
            state = "TIMED OUT"
        parts = [
            f"{state} exit={self.exit_code} {self.duration_s:.1f}s",
            f"tools={self.tool_calls} tool_errors={len(self.tool_errors)}",
            f"permission_denials={len(self.permission_denials)}",
        ]
        if self.session_id:
            parts.append(f"session={self.session_id}")
        if self.cli_error:
            parts.append(f"cli_error={self.cli_error[:200]}")
        return " ".join(parts)


class Runner(Protocol):
    name: str

    def invoke(
        self, prompt: str, workspace: Path, log_path: Path, timeout_s: float | None
    ) -> InvocationResult: ...


def _write_prompt(prompt: str, log_path: Path) -> Path:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_file = log_path.parent / "prompt.md"
    prompt_file.write_text(prompt, encoding="utf-8")
    return prompt_file


class OpenCodeRunner:
    """`opencode run --agent A --model M --format json <prompt>` in a brand-new session."""

    name = "opencode"

    def __init__(self, config: OpenCodeConfig, echo: Echo = print) -> None:
        for arg in config.extra_args:
            if arg.split("=", 1)[0] in CONTINUATION_FLAGS:
                raise ConfigError(
                    f"[runner.opencode] extra_args may not contain {arg!r}: every iteration "
                    "must be a fresh session, and blanket --auto approval is not allowed "
                    "(use [runner.opencode.permission] for scoped permissions)"
                )
        self.config = config
        self.echo = echo

    def argv(self, prompt: str, workspace: Path) -> list[str]:
        # The prompt is one argv element (no shell). A leading "-" would parse as a flag.
        safe_prompt = prompt if not prompt.startswith("-") else "\n" + prompt
        c = self.config
        return [
            c.binary,
            "run",
            "--agent",
            c.agent,
            "--model",
            c.model,
            "--format",
            "json",
            "--dir",
            str(workspace),
            *c.extra_args,
            safe_prompt,
        ]

    def env(self, workspace: Path) -> dict[str, str]:
        # OpenCode takes its project directory from PWD, not the process cwd. Without this
        # it treats the directory ralph was launched from (the harness) as the project.
        extra: dict[str, str] = {"PWD": str(workspace)}
        if self.config.permission:
            inline: dict[str, Any] = {}
            if existing := os.environ.get("OPENCODE_CONFIG_CONTENT"):
                try:
                    inline = json.loads(existing)
                except json.JSONDecodeError:
                    inline = {}
            agents = inline.setdefault("agent", {})
            agents.setdefault(self.config.agent, {})["permission"] = self.config.permission
            extra["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline)
        return proc.child_env(extra)

    def invoke(
        self, prompt: str, workspace: Path, log_path: Path, timeout_s: float | None
    ) -> InvocationResult:
        _write_prompt(prompt, log_path)
        result = InvocationResult()
        texts: list[str] = []

        def on_stdout(line: str) -> None:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return
            if isinstance(event, dict):
                self._handle_event(event, result, texts)

        def on_stderr(line: str) -> None:
            clean = ANSI.sub("", line).strip()
            if "permission requested" in clean and "reject" in clean:
                result.permission_denials.append(clean.lstrip("! "))
                self.echo(f"    ! {clean}")

        try:
            outcome = proc.run(
                self.argv(prompt, workspace),
                workspace,
                timeout_s,
                env=self.env(workspace),
                on_stdout=on_stdout,
                on_stderr=on_stderr,
                log_path=log_path,
            )
        except (FileNotFoundError, PermissionError) as exc:
            result.exit_code, result.fatal = None, True
            result.cli_error = f"cannot start {self.config.binary!r}: {exc}"
            return result

        result.exit_code = outcome.returncode
        result.timed_out = outcome.timed_out
        result.duration_s = outcome.duration_s
        result.final_text = "".join(texts).strip()[-8000:]
        if outcome.returncode not in (0, None) and result.cli_error is None:
            tail = outcome.stderr.strip()[-500:]
            result.cli_error = f"opencode exited with code {outcome.returncode}: {tail}"
        return result

    def _handle_event(
        self, event: dict[str, Any], result: InvocationResult, texts: list[str]
    ) -> None:
        # Parsed defensively: unknown event types and missing fields are ignored.
        if event.get("sessionID") and not result.session_id:
            result.session_id = str(event["sessionID"])
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        assert isinstance(part, dict)
        kind = event.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
            first = part["text"].strip().splitlines()
            if first:
                self.echo(f"    > {first[0][:160]}")
        elif kind == "tool_use":
            result.tool_calls += 1
            tool = str(part.get("tool", "?"))
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            assert isinstance(state, dict)
            status = str(state.get("status", "?"))
            error = str(state.get("error") or "")
            self.echo(f"    · {tool} {_tool_target(state)} [{status}]")
            if status == "error":
                if "rejected permission" in error or "prevents you from using" in error:
                    result.permission_denials.append(f"{tool}: {error[:300]}")
                else:
                    result.tool_errors.append(f"{tool}: {error[:300]}")
        elif kind == "error":
            result.cli_error = (
                f"opencode error event: {json.dumps(event.get('error', event))[:500]}"
            )
            self.echo(f"    ! {result.cli_error}")


def _tool_target(state: dict[str, Any]) -> str:
    data = state.get("input")
    if not isinstance(data, dict):
        return ""
    for key in ("filePath", "path", "command", "pattern"):
        if isinstance(data.get(key), str):
            return str(data[key])[:100]
    return ""


class CommandRunner:
    """Generic adapter for any CLI.

    Contract: `argv` is an explicit list. `{prompt_file}` and `{workspace}` placeholders
    are substituted. With prompt_via="stdin" the prompt is written to stdin. The process
    runs with cwd=workspace and env RALPH_PROMPT_FILE / RALPH_WORKSPACE. Exit code 0
    means the invocation itself worked (not that the work is done); stdout is the
    model output shown to the next iteration only through runner feedback.
    """

    name = "command"

    def __init__(self, config: CommandConfig, echo: Echo = print) -> None:
        if not config.argv:
            raise ConfigError("[runner.command] argv must be a non-empty list")
        self.config = config
        self.echo = echo

    def invoke(
        self, prompt: str, workspace: Path, log_path: Path, timeout_s: float | None
    ) -> InvocationResult:
        prompt_file = _write_prompt(prompt, log_path)
        values = {"prompt_file": str(prompt_file), "workspace": str(workspace)}
        argv = [arg.format(**values) for arg in self.config.argv]
        env = proc.child_env(
            {"RALPH_PROMPT_FILE": str(prompt_file), "RALPH_WORKSPACE": str(workspace)}
        )
        try:
            outcome = proc.run(
                argv,
                workspace,
                timeout_s,
                env=env,
                stdin_text=prompt if self.config.prompt_via == "stdin" else None,
                on_stdout=lambda line: self.echo(f"    > {line[:160]}"),
                log_path=log_path,
            )
        except (FileNotFoundError, PermissionError) as exc:
            return InvocationResult(exit_code=None, fatal=True, cli_error=f"cannot start: {exc}")
        return InvocationResult(
            exit_code=outcome.returncode,
            timed_out=outcome.timed_out,
            duration_s=outcome.duration_s,
            final_text=outcome.stdout.strip()[-8000:],
            cli_error=None
            if outcome.returncode in (0, None)
            else f"command exited with code {outcome.returncode}: {outcome.stderr.strip()[-500:]}",
        )


@dataclass
class FakeCall:
    index: int
    prompt: str
    workspace: Path


FakeAction = Callable[[FakeCall], "str | InvocationResult | None"]


class FakeRunner:
    """Deterministic runner for tests: `script(call)` edits files and returns text or a result."""

    name = "fake"

    def __init__(self, script: FakeAction) -> None:
        self.script = script
        self.calls: list[FakeCall] = []

    def invoke(
        self, prompt: str, workspace: Path, log_path: Path, timeout_s: float | None
    ) -> InvocationResult:
        _write_prompt(prompt, log_path)
        call = FakeCall(len(self.calls), prompt, workspace)
        self.calls.append(call)
        outcome = self.script(call)
        if isinstance(outcome, InvocationResult):
            return outcome
        return InvocationResult(final_text=outcome or "")


def make_runner(name: str, opencode: OpenCodeConfig, command: CommandConfig, echo: Echo) -> Runner:
    if name == "opencode":
        return OpenCodeRunner(opencode, echo)
    if name == "command":
        return CommandRunner(command, echo)
    raise ConfigError(f"Unknown runner {name!r}")
