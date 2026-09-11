"""Behavior of the Ralph loop driven by a deterministic fake agent."""

import sys

import pytest
from conftest import IMPL, TESTS, GoodAgent, make_harness, write

from ralph.loop import ReconcileRequired, accept_manual, open_run, run_loop
from ralph.runners import FakeRunner, InvocationResult
from ralph.state import EXIT_CODES, RunStore, Status

ONE_TASK = ["TASK-001"]


def ac(run, ac_id):
    return run.state.acceptance[ac_id]


def test_red_green_refactor_reaches_verified_success(harness):
    agent = GoodAgent()

    status, run, runner = harness.start(agent, tasks=["TASK-002"])

    assert status is Status.SUCCESS
    assert agent.phases == [
        "RED:AC-001",
        "GREEN:AC-001",
        "REFACTOR:AC-001",
        "RED:AC-002",
        "GREEN:AC-002",
        "REFACTOR:AC-002",
    ]
    first = ac(run, "AC-001")
    assert first.status == "done" and first.kind == "tdd"
    assert first.red["outcome"] == "failed" and first.baseline_hash
    assert first.green["outcome"] == "passed" and first.refactor["outcome"] == "passed"
    assert list(first.tests) == ["tests/test_ac_001.py::test_ac_001_adds"]
    assert run.state.iteration == len(runner.calls) == 6
    assert RunStore(harness.config.state_dir, run.state.run_id).load().status == Status.SUCCESS
    assert (run.store.dir / "progress.md").read_text().count("## Iteration") == 6


def test_unlimited_mode_keeps_looping_past_repeated_failures_until_success(harness):
    greens = []

    class SlowAgent(GoodAgent):
        def act(self, call, phase, target):
            if phase == "GREEN":
                greens.append(1)
                if len(greens) <= 7:
                    return "I think it works now. DONE."
            return super().act(call, phase, target)

    status, _, runner = harness.start(SlowAgent(), tasks=ONE_TASK)

    assert status is Status.SUCCESS
    assert len(runner.calls) == 10  # RED + 7 failed GREEN + GREEN + REFACTOR
    assert any("same phase has failed" in line for line in harness.output)


def test_finite_limit_counts_every_invocation(harness):
    status, run, runner = harness.start(lambda call: "DONE", tasks=ONE_TASK, max_iterations=4)

    assert status is Status.MAX_ITERATIONS_REACHED
    assert len(runner.calls) == 4 and run.state.iteration == 4
    assert EXIT_CODES[status] != 0


def test_success_stops_early_before_the_limit(harness):
    status, _, runner = harness.start(GoodAgent(), tasks=ONE_TASK, max_iterations=50)

    assert status is Status.SUCCESS
    assert len(runner.calls) == 3


def test_each_iteration_is_a_fresh_prompt_scoped_to_the_selection(harness):
    def chatty(call):
        GoodAgent()(call)
        return f"private chatter {call.index}"

    status, _, runner = harness.start(chatty, tasks=ONE_TASK)

    assert status is Status.SUCCESS
    for call in runner.calls:
        assert "FRESH session" in call.prompt
        assert "TASK-002" not in call.prompt and "UNRELATED-MARKER" not in call.prompt
        assert "private chatter" not in call.prompt  # no conversation replay


def test_red_reverts_production_changes_and_verifies_against_unchanged_code(harness):
    def eager(call):
        write(call.workspace, "tests/test_add.py", TESTS["AC-001"])
        write(call.workspace, "calc/__init__.py", IMPL["AC-001"])  # not allowed in RED
        return "wrote test and implementation"

    status, run, _ = harness.start(eager, tasks=ONE_TASK, max_iterations=1)

    assert status is Status.MAX_ITERATIONS_REACHED
    assert (harness.workspace / "calc/__init__.py").read_text() == ""
    assert ac(run, "AC-001").status == "green"
    assert ac(run, "AC-001").red["outcome"] == "failed"
    assert "production changes reverted" in run.state.history[-1]["summary"]


@pytest.mark.parametrize(
    ("test_body", "reason"),
    [
        ("def test_ac_001_x():\n    assert False\n", "constant"),
        ("def test_ac_001_x():\n    pass\n", "no assertion"),
        ("import pytest\n\n@pytest.mark.skip\ndef test_ac_001_x():\n    assert 1 == 2\n", "skip"),
        (
            "import missing_pkg\n\ndef test_ac_001_x():\n    assert missing_pkg.x == 1\n",
            "not a workspace",
        ),
        ("def test_ac_001_x(:\n", "collect"),
        ("def test_something_else():\n    assert 1 == 2\n", "no test named test_ac_001"),
    ],
)
def test_fake_red_evidence_is_rejected(harness, test_body, reason):
    def agent(call):
        write(call.workspace, "tests/test_x.py", test_body)

    status, run, _ = harness.start(agent, tasks=ONE_TASK, max_iterations=1)

    assert status is Status.MAX_ITERATIONS_REACHED
    assert ac(run, "AC-001").status == "red"
    assert reason in run.state.feedback


def test_false_done_and_empty_suite_never_succeed(harness):
    status, run, _ = harness.start(
        lambda call: "All tests pass. DONE. <promise>COMPLETE</promise>",
        tasks=ONE_TASK,
        max_iterations=3,
    )

    assert status is Status.MAX_ITERATIONS_REACHED
    assert ac(run, "AC-001").status == "red"
    assert not any(a.green for a in run.state.acceptance.values())


def test_frozen_red_test_must_pass_unchanged_in_green(harness):
    class Cheater(GoodAgent):
        def act(self, call, phase, target):
            if phase == "GREEN":
                write(
                    call.workspace,
                    "tests/test_ac_001.py",
                    "def test_ac_001_adds():\n    assert True\n",
                )
                return "weakened the test"
            return super().act(call, phase, target)

    status, run, _ = harness.start(Cheater(), tasks=ONE_TASK, max_iterations=2)

    assert status is Status.MAX_ITERATIONS_REACHED
    assert (harness.workspace / "tests/test_ac_001.py").read_text() == TESTS["AC-001"]
    assert ac(run, "AC-001").status == "green"
    assert "test changes reverted" in run.state.feedback


def test_already_passing_test_is_recorded_as_pre_existing_without_breaking_code(harness):
    write(harness.workspace, "calc/__init__.py", IMPL["AC-001"])

    status, run, runner = harness.start(GoodAgent(), tasks=ONE_TASK)

    assert status is Status.SUCCESS
    assert len(runner.calls) == 1  # only RED; nothing to implement
    assert ac(run, "AC-001").kind == "pre-existing"
    assert ac(run, "AC-001").red["outcome"] == "passed"


def test_existing_failing_test_establishes_red_without_an_agent_call(harness):
    write(harness.workspace, "tests/test_ac_001.py", TESTS["AC-001"])
    agent = GoodAgent()

    status, run, _ = harness.start(agent, tasks=ONE_TASK)

    assert status is Status.SUCCESS
    assert agent.phases == ["GREEN:AC-001", "REFACTOR:AC-001"]
    assert ac(run, "AC-001").red["by"] == "runner"


def test_resume_preserves_iteration_count_and_tdd_phase(harness):
    status, run, _ = harness.start(GoodAgent(), tasks=ONE_TASK, max_iterations=2)
    assert status is Status.MAX_ITERATIONS_REACHED
    assert ac(run, "AC-001").status == "refactor"

    agent = GoodAgent()
    status, resumed, _ = harness.resume(run.state.run_id, agent)

    assert status is Status.SUCCESS
    assert agent.phases == ["REFACTOR:AC-001"]
    assert resumed.state.iteration == 3


def test_resume_with_a_total_limit_counts_already_consumed_iterations(harness):
    _, run, _ = harness.start(lambda call: "nothing", tasks=ONE_TASK, max_iterations=2)

    status, resumed, runner = harness.resume(
        run.state.run_id, lambda call: "nothing", max_iterations=3
    )

    assert status is Status.MAX_ITERATIONS_REACHED
    assert len(runner.calls) == 1 and resumed.state.iteration == 3


def test_repeated_cli_failures_stop_with_runner_error(harness):
    failing = InvocationResult(exit_code=1, cli_error="provider unreachable")

    status, run, runner = harness.start(lambda call: failing, tasks=ONE_TASK)

    assert status is Status.RUNNER_ERROR
    assert len(runner.calls) == harness.config.loop.max_consecutive_runner_failures
    assert "provider unreachable" in run.state.stop_reason


def test_missing_cli_is_an_immediate_runner_error(harness):
    fatal = InvocationResult(exit_code=None, fatal=True, cli_error="cannot start 'opencode'")

    status, _, runner = harness.start(lambda call: fatal, tasks=ONE_TASK)

    assert status is Status.RUNNER_ERROR and len(runner.calls) == 1


def test_timeouts_become_feedback_and_the_loop_recovers(harness):
    calls = []

    def flaky(call):
        calls.append(1)
        if len(calls) == 1:
            return InvocationResult(exit_code=None, timed_out=True, duration_s=30)
        return GoodAgent()(call)

    status, run, _ = harness.start(flaky, tasks=ONE_TASK)

    assert status is Status.SUCCESS
    assert "TIMED OUT" in run.state.history[0]["runner"]


def test_ctrl_c_cancels_and_preserves_state(harness):
    def interrupt(call):
        if call.index == 1:
            raise KeyboardInterrupt
        return GoodAgent()(call)

    status, run, _ = harness.start(interrupt, tasks=ONE_TASK)

    assert status is Status.CANCELLED
    saved = RunStore(harness.config.state_dir, run.state.run_id).load()
    assert saved.status == Status.CANCELLED and saved.iteration == 2
    assert saved.acceptance["AC-001"].status == "green"
    assert not (harness.config.state_dir / "run.lock").exists()


def test_editing_a_protected_harness_file_blocks_the_run(harness):
    def vandal(call):
        harness.spec_path.write_text(harness.spec_path.read_text() + "\n- AC-999: sneaky\n")

    status, run, runner = harness.start(vandal, tasks=ONE_TASK)

    assert status is Status.BLOCKED and len(runner.calls) == 1
    assert "specs/calc.md" in run.state.stop_reason


def test_writing_anywhere_in_the_harness_outside_the_workspace_blocks_the_run(harness):
    def lost_agent(call):
        write(harness.root, "greeting/pyproject.toml", "[project]\n")  # e.g. wrong cwd

    status, run, runner = harness.start(lost_agent, tasks=ONE_TASK)

    assert status is Status.BLOCKED and len(runner.calls) == 1
    assert "greeting/pyproject.toml" in run.state.stop_reason


def test_workspace_agent_config_is_protected(harness):
    class Escalator(GoodAgent):
        def act(self, call, phase, target):
            write(call.workspace, "opencode.json", '{"permission": {"bash": "allow"}}')
            return super().act(call, phase, target)

    status, run, _ = harness.start(Escalator(), tasks=ONE_TASK)

    assert status is Status.SUCCESS
    assert not (harness.workspace / "opencode.json").exists()
    assert "protected workspace files reverted" in run.state.history[0]["summary"]


def test_regressions_are_rejected_in_green(harness):
    class Breaker(GoodAgent):
        def act(self, call, phase, target):
            if phase == "GREEN" and target == "AC-002":
                write(call.workspace, "calc/__init__.py", IMPL["AC-002"])  # drops add()
                return "implemented subtract"
            return super().act(call, phase, target)

    status, run, _ = harness.start(Breaker(), tasks=["TASK-002"], max_iterations=5)

    assert status is Status.MAX_ITERATIONS_REACHED
    assert ac(run, "AC-002").status == "green"
    assert "test_ac_001_adds" in run.state.feedback


def test_refactor_requires_quality_checks(harness):
    lint = [
        sys.executable,
        "-c",
        "import pathlib, sys; sys.exit('LINT' in pathlib.Path('calc/__init__.py').read_text())",
    ]
    harness.config.verify.quality_commands = [lint]

    class Sloppy(GoodAgent):
        def act(self, call, phase, target):
            init = call.workspace / "calc/__init__.py"
            if phase == "RED":
                return super().act(call, phase, target)
            if phase == "GREEN":
                init.write_text(IMPL["AC-001"] + "# LINT\n")
                return "done"
            if (
                phase == "REFACTOR"
                and "LINT" in call.prompt
                and self.phases.count("REFACTOR:AC-001") > 1
            ):
                init.write_text(IMPL["AC-001"])
                return "fixed lint"
            return "no refactor needed"

    status, run, runner = harness.start(Sloppy(), tasks=ONE_TASK)

    assert status is Status.SUCCESS
    assert len(runner.calls) == 4  # RED, GREEN, REFACTOR (quality fails), REFACTOR
    assert ac(run, "AC-001").refactor["outcome"] == "passed"


def test_frozen_tests_may_be_reformatted_but_not_changed_after_red(harness):
    # Found by the real-model smoke test: a lint error inside a frozen test file made
    # REFACTOR impossible, because the quality checks also cover tests.
    no_trailing_ws = (
        "import pathlib, sys; sys.exit(any(line != line.rstrip() "
        "for p in pathlib.Path('tests').glob('*.py') for line in p.read_text().splitlines()))"
    )
    harness.config.verify.quality_commands = [[sys.executable, "-c", no_trailing_ws]]
    sloppy_test = TESTS["AC-001"].replace("\n\n\ndef", "\n    \n\ndef")

    class Tidy(GoodAgent):
        def act(self, call, phase, target):
            path = call.workspace / "tests/test_ac_001.py"
            if phase == "RED":
                write(call.workspace, "tests/test_ac_001.py", sloppy_test)
                return "test written"
            if phase == "REFACTOR":
                if self.phases.count("REFACTOR:AC-001") == 1:
                    path.write_text(path.read_text().replace("== 5", ">= 0"))  # weakening
                    return "made the test easier"
                path.write_text("\n".join(x.rstrip() for x in path.read_text().splitlines()) + "\n")
                return "stripped trailing whitespace"
            return super().act(call, phase, target)

    status, _, runner = harness.start(Tidy(), tasks=ONE_TASK)

    assert status is Status.SUCCESS
    assert len(runner.calls) == 4  # RED, GREEN, REFACTOR (weakening reverted), REFACTOR
    final = (harness.workspace / "tests/test_ac_001.py").read_text()
    assert "== 5" in final and "    \n" not in final


def test_final_verification_failure_triggers_repair(tmp_path):
    smoke = f'["{sys.executable}", "-c", "import calc; calc.main()"]'
    spec = f"---\nid: calc\npackage: calc\nsmoke_command: {smoke}\n---\n"
    spec += "# Calc\n\n## TASK-001: Addition\n\n### Acceptance criteria\n"
    spec += "- AC-001: add(2, 3) returns 5.\n"
    harness = make_harness(tmp_path, spec)

    class Repairer(GoodAgent):
        def act(self, call, phase, target):
            if phase == "REPAIR":
                init = call.workspace / "calc/__init__.py"
                init.write_text(init.read_text() + "\ndef main():\n    return 0\n")
                return "added main"
            return super().act(call, phase, target)

    agent = Repairer()
    status, _, _ = harness.start(agent)

    assert status is Status.SUCCESS
    assert agent.phases[-1] == "REPAIR:None"


def test_changed_spec_requires_explicit_reconciliation(harness):
    _, run, _ = harness.start(GoodAgent(), tasks=ONE_TASK)
    assert ac(run, "AC-001").status == "done"
    harness.spec_path.write_text(harness.spec_path.read_text().replace("returns 5", "returns five"))

    with pytest.raises(ReconcileRequired, match="AC-001"):
        open_run(
            harness.config, run.state.run_id, lambda n: FakeRunner(GoodAgent()), echo=harness.echo
        )

    status, resumed, runner = harness.resume(run.state.run_id, GoodAgent(), reconcile=True)
    assert status is Status.SUCCESS
    assert ac(resumed, "AC-001").status == "done" and len(runner.calls) >= 1


def test_manual_criteria_block_until_a_human_accepts_them(tmp_path):
    spec = (
        "---\nid: calc\npackage: calc\n---\n# Calc\n\n## TASK-001: Addition\n\n"
        "### Acceptance criteria\n- AC-001: add(2, 3) returns 5.\n"
        "- AC-002 (manual): The code reads well.\n"
    )
    harness = make_harness(tmp_path, spec)

    status, run, _ = harness.start(GoodAgent())
    assert status is Status.BLOCKED and "AC-002" in run.state.stop_reason

    resumed = open_run(
        harness.config, run.state.run_id, lambda n: FakeRunner(GoodAgent()), echo=harness.echo
    )
    accept_manual(resumed, ["AC-002"])
    assert run_loop(resumed) is Status.SUCCESS
    assert ac(resumed, "AC-002").kind == "manual"
