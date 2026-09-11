"""Persistent run state: atomic JSON, a run lock, and the per-run evidence layout.

.ralph/
  run.lock                      one active run per state directory
  latest                        id of the most recent run
  runs/<run_id>/
    state.json                  RunState (atomic writes)
    plan.md  progress.md        concise, human-readable plan and log
    feedback.md                 latest runner feedback for the next iteration
    baseline/  baseline.json    production files before the current RED (restore source)
    tests_ref/ tests_ref.json   test files at the last freeze (restore source)
    pre/                        protected workspace files before the current invocation
    iterations/NNNN/            prompt.md, agent.log, junit.xml, verify.md
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ralph.specs import slugify


class Status(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    MAX_ITERATIONS_REACHED = "MAX_ITERATIONS_REACHED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    RUNNER_ERROR = "RUNNER_ERROR"


EXIT_CODES = {
    Status.SUCCESS: 0,
    Status.RUNNING: 1,
    Status.MAX_ITERATIONS_REACHED: 10,
    Status.BLOCKED: 11,
    Status.RUNNER_ERROR: 12,
    Status.CANCELLED: 130,
}


class LockError(RuntimeError):
    """Another run holds the lock."""


class StateError(RuntimeError):
    """State is missing, corrupt or was modified outside the runner."""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class AcceptanceState:
    id: str
    task_id: str
    text_hash: str
    status: str = "pending"  # pending | red | green | refactor | done
    kind: str = "tdd"  # tdd | pre-existing | characterization | manual
    tests: dict[str, str] = field(default_factory=dict)  # frozen nodeid -> source sha256
    baseline_hash: str | None = None  # pre-change production tree hash
    red: dict[str, Any] | None = None
    green: dict[str, Any] | None = None
    refactor: dict[str, Any] | None = None
    # Existing tests for this criterion may be outdated (spec reconciled or RED restarted):
    # the agent must revisit them in RED instead of the runner reusing them.
    stale_tests: bool = False

    def reset(self) -> None:
        self.status, self.kind, self.tests, self.baseline_hash = "pending", "tdd", {}, None
        self.red = self.green = self.refactor = None
        self.stale_tests = True


@dataclass
class RunState:
    run_id: str
    spec_path: str
    spec_id: str
    requested_tasks: list[str]
    selected_tasks: list[str]
    selection_hash: str
    workspace: str
    runner: str
    max_iterations: int | None
    acceptance: dict[str, AcceptanceState]
    task_hashes: dict[str, str] = field(default_factory=dict)
    iteration: int = 0
    status: str = Status.RUNNING
    stop_reason: str = ""
    setup_done: bool = False
    passing_baseline: list[str] = field(default_factory=list)
    feedback: str = ""
    repair_needed: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)
    stall_key: str = ""
    stall_count: int = 0
    consecutive_runner_failures: int = 0
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=False)

    @classmethod
    def from_json(cls, text: str) -> RunState:
        data = json.loads(text)
        data["acceptance"] = {k: AcceptanceState(**v) for k, v in data["acceptance"].items()}
        return cls(**data)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def new_run_id(spec_id: str) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{slugify(spec_id)[:40]}"


class RunStore:
    def __init__(self, state_dir: Path, run_id: str) -> None:
        self.state_dir = state_dir
        self.dir = state_dir / "runs" / run_id
        self._written_hash: str | None = None

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    def path(self, name: str) -> Path:
        return self.dir / name

    def iteration_dir(self, iteration: int) -> Path:
        path = self.dir / "iterations" / f"{iteration:04d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def exists(self) -> bool:
        return self.state_path.exists()

    def save(self, state: RunState) -> None:
        state.updated_at = now()
        text = state.to_json()
        atomic_write(self.state_path, text)
        atomic_write(self.state_dir / "latest", state.run_id + "\n")
        self._written_hash = hashlib.sha256(text.encode()).hexdigest()

    def load(self) -> RunState:
        if not self.exists():
            raise StateError(f"No run state at {self.state_path}")
        text = self.state_path.read_text(encoding="utf-8")
        try:
            state = RunState.from_json(text)
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            raise StateError(f"Corrupt run state {self.state_path}: {exc}") from exc
        self._written_hash = hashlib.sha256(text.encode()).hexdigest()
        return state

    def tampered(self) -> bool:
        """True when state.json no longer matches what the runner last wrote."""
        if self._written_hash is None or not self.exists():
            return False
        current = hashlib.sha256(self.state_path.read_bytes()).hexdigest()
        return current != self._written_hash

    def write(self, name: str, text: str) -> None:
        atomic_write(self.path(name), text)

    def append_progress(self, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.path("progress.md").open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n\n")

    def save_snapshot(self, name: str, snap: dict[str, str]) -> None:
        atomic_write(self.path(f"{name}.json"), json.dumps(snap, indent=1, sort_keys=True))

    def load_snapshot(self, name: str) -> dict[str, str]:
        path = self.path(f"{name}.json")
        return dict(json.loads(path.read_text())) if path.exists() else {}


def latest_run_id(state_dir: Path) -> str | None:
    path = state_dir / "latest"
    return path.read_text().strip() or None if path.exists() else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def run_lock(state_dir: Path, run_id: str) -> Iterator[None]:
    """Exclusive lock per state directory; stale locks from dead processes are replaced."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / "run.lock"
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                pid_text, _, owner = lock.read_text().partition(" ")
                pid = int(pid_text)
            except (OSError, ValueError):
                pid, owner = -1, "?"
            if pid > 0 and _pid_alive(pid):
                raise LockError(
                    f"Run {owner.strip()} (pid {pid}) is active; lock file {lock}"
                ) from None
            lock.unlink(missing_ok=True)  # stale lock from a dead process
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(f"{os.getpid()} {run_id}\n")
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)
        return
    raise LockError(f"Could not acquire {lock}")
