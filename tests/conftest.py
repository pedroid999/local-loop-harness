"""Shared fixtures: a throwaway harness root with a tiny workspace and a scripted fake agent."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ralph.config import Config, LoopConfig, VerifyConfig
from ralph.loop import create_run, open_run, run_loop
from ralph.runners import FakeCall, FakeRunner, InvocationResult
from ralph.state import Status
from ralph.tdd import Run

REPO = Path(__file__).resolve().parents[1]

CALC_SPEC = """\
---
id: calc
package: calc
---
# Calc

## TASK-001: Addition

### Acceptance criteria
- AC-001: add(2, 3) returns 5.

## TASK-002: Subtraction

Depends on: TASK-001

### Acceptance criteria
- AC-002: subtract(5, 3) returns 2.

## TASK-003: Unrelated multiplication

### Acceptance criteria
- AC-003: multiply(2, 4) returns 8 (UNRELATED-MARKER).
"""

TESTS = {
    "AC-001": "from calc import add\n\n\ndef test_ac_001_adds():\n    assert add(2, 3) == 5\n",
    "AC-002": (
        "from calc import subtract\n\n\ndef test_ac_002_subtracts():\n"
        "    assert subtract(5, 3) == 2\n"
    ),
    "AC-003": (
        "from calc import multiply\n\n\ndef test_ac_003_multiplies():\n"
        "    assert multiply(2, 4) == 8\n"
    ),
}
IMPL = {
    "AC-001": "def add(a, b):\n    return a + b\n",
    "AC-002": "def subtract(a, b):\n    return a - b\n",
    "AC-003": "def multiply(a, b):\n    return a * b\n",
}


RUNAWAY_CALLS = 40


def guarded(script: Callable[[FakeCall], str | InvocationResult | None]):
    """Unlimited-mode tests must not hang forever if the harness regresses."""

    def wrapper(call: FakeCall) -> str | InvocationResult | None:
        if call.index >= RUNAWAY_CALLS:
            raise AssertionError(f"runaway loop: {RUNAWAY_CALLS} invocations without stopping")
        return script(call)

    return wrapper


def phase_of(prompt: str) -> tuple[str, str | None]:
    phase = re.search(r"## Phase: (\w+)", prompt)
    target = re.search(r"^Target: (\S+)", prompt, re.MULTILINE)
    return (phase.group(1) if phase else "?", target.group(1) if target else None)


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class GoodAgent:
    """Behaves like a disciplined TDD agent for the calc spec."""

    def __init__(self) -> None:
        self.phases: list[str] = []

    def __call__(self, call: FakeCall) -> str | InvocationResult | None:
        phase, target = phase_of(call.prompt)
        self.phases.append(f"{phase}:{target}")
        return self.act(call, phase, target)

    def act(self, call: FakeCall, phase: str, target: str | None) -> str | InvocationResult | None:
        ws = call.workspace
        if phase == "RED" and target:
            write(ws, f"tests/test_{target.lower().replace('-', '_')}.py", TESTS[target])
        elif phase == "GREEN" and target:
            init = ws / "calc/__init__.py"
            if IMPL[target] not in init.read_text():
                init.write_text(init.read_text() + IMPL[target])
        elif phase == "REFACTOR":
            return "No refactor needed: the implementation is minimal."
        return f"finished {phase}"


@dataclass
class Harness:
    root: Path
    workspace: Path
    spec_path: Path
    config: Config
    output: list[str] = field(default_factory=list)

    def echo(self, line: str) -> None:
        self.output.append(line)

    def start(
        self,
        script: Callable[[FakeCall], str | InvocationResult | None],
        *,
        tasks: list[str] | None = None,
        max_iterations: int | None = None,
    ) -> tuple[Status, Run, FakeRunner]:
        runner = FakeRunner(guarded(script))
        run = create_run(
            self.config,
            self.spec_path,
            tasks,
            runner,
            max_iterations=max_iterations,
            echo=self.echo,
        )
        return run_loop(run), run, runner

    def resume(
        self,
        run_id: str,
        script: Callable[[FakeCall], str | InvocationResult | None],
        *,
        max_iterations: int | None = None,
        reconcile: bool = False,
    ) -> tuple[Status, Run, FakeRunner]:
        runner = FakeRunner(guarded(script))
        run = open_run(
            self.config,
            run_id,
            lambda _name: runner,
            max_iterations=max_iterations,
            echo=self.echo,
            reconcile=reconcile,
        )
        return run_loop(run), run, runner


def make_harness(tmp_path: Path, spec: str = CALC_SPEC) -> Harness:
    root = tmp_path / "harness"
    workspace = root / "app"
    write(
        workspace,
        "pyproject.toml",
        '[project]\nname = "calc"\nversion = "0"\n\n'
        '[tool.pytest.ini_options]\npythonpath = ["."]\n',
    )
    write(workspace, "calc/__init__.py", "")
    (workspace / "tests").mkdir()
    spec_path = root / "specs" / "calc.md"
    write(spec_path.parent, spec_path.name, spec)
    config = Config(
        root=root,
        workspace=workspace,
        state_dir=root / ".ralph",
        prompts_dir=REPO / "prompts",
        verify=VerifyConfig(
            test_command=[sys.executable, "-m", "pytest"],
            quality_commands=[],
            setup_commands=[],
            test_timeout_s=60,
        ),
        loop=LoopConfig(invocation_timeout_s=30),
    )
    return Harness(root, workspace, spec_path, config)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return make_harness(tmp_path)
