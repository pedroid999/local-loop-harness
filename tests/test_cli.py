import subprocess
import sys
from pathlib import Path

import pytest

from ralph.cli import main
from ralph.state import RunStore, latest_run_id

REPO = Path(__file__).resolve().parents[1]
FAKE_AGENT = REPO / "tests" / "fixtures" / "fake_agent.py"
SPEC = """\
---
id: calc
package: calc
---
# Calc

## TASK-001: Addition

### Acceptance criteria
- AC-001: add(2, 3) returns 5.
"""


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "harness"
    (root / "specs").mkdir(parents=True)
    (root / "specs" / "calc.md").write_text(SPEC)
    py = sys.executable
    (root / "ralph.toml").write_text(
        f"""
[harness]
prompts_dir = "{REPO / "prompts"}"

[runner]
default = "command"

[runner.command]
argv = ["{py}", "{FAKE_AGENT}", "{{prompt_file}}"]
prompt_via = "file"

[verify]
test_command = ["{py}", "-m", "pytest"]
quality_commands = []
setup_commands = []
"""
    )
    return root


def cli(root: Path, *args: str) -> int:
    return main(["--config", str(root / "ralph.toml"), *args])


def test_end_to_end_setup_red_green_refactor_success_with_the_command_runner(root, capsys):
    code = cli(root, "--spec", str(root / "specs/calc.md"))

    assert code == 0
    out = capsys.readouterr().out
    for phase in ("SETUP", "RED AC-001", "GREEN AC-001", "REFACTOR AC-001"):
        assert phase in out
    assert "SUCCESS" in out
    run_id = latest_run_id(root / ".ralph")
    assert run_id is not None
    state = RunStore(root / ".ralph", run_id).load()
    assert state.iteration == 4 and state.acceptance["AC-001"].status == "done"

    assert cli(root, "--spec", str(root / "specs/calc.md"), "--verify-only") == 0
    assert "VERIFIED COMPLETE" in capsys.readouterr().out
    assert cli(root, "--resume", run_id) == 0  # nothing left to do: no new invocations
    assert RunStore(root / ".ralph", run_id).load().iteration == 4


def test_verify_only_without_evidence_is_not_complete(root, capsys):
    assert cli(root, "--spec", str(root / "specs/calc.md"), "--verify-only") == 1
    assert "NOT COMPLETE" in capsys.readouterr().out


def test_iteration_budget_exhaustion_is_a_nonzero_exit(root, capsys):
    assert cli(root, "--spec", str(root / "specs/calc.md"), "--max-iterations", "2") == 10
    assert "NOT complete" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["0", "-3", "abc"])
def test_max_iterations_must_be_a_positive_integer(root, value):
    with pytest.raises(SystemExit) as exc:
        cli(root, "--spec", str(root / "specs/calc.md"), "--max-iterations", value)
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "args",
    [[], ["--task", "TASK-001"], ["--resume", "x", "--spec", "y"], ["--spec", "x", "--reconcile"]],
)
def test_invalid_argument_combinations_are_rejected(root, args):
    with pytest.raises(SystemExit) as exc:
        cli(root, *args)
    assert exc.value.code == 2


def test_unknown_task_is_rejected(root, capsys):
    assert cli(root, "--spec", str(root / "specs/calc.md"), "--task", "TASK-404") == 2
    assert "Unknown task" in capsys.readouterr().err


@pytest.mark.parametrize("workspace", [".", "specs"])
def test_workspace_overlapping_the_harness_is_refused(root, capsys, workspace):
    code = cli(root, "--spec", str(root / "specs/calc.md"), "--workspace", str(root / workspace))
    assert code == 2
    assert "Workspace" in capsys.readouterr().err


def test_entry_point_runs_under_uv():
    result = subprocess.run(
        [sys.executable, str(REPO / "ralph.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and "--max-iterations" in result.stdout
