"""The TDD gatekeeper: runner-controlled phase transitions, guards and final verification.

Phases per acceptance criterion (AC):

    feature:  RED (test fails for missing behavior) -> GREEN (same test passes) -> REFACTOR -> done
    docs:     RED -> GREEN (documentation files only, quality checks) -> done
    refactor: RED (characterization tests PASS on current code) -> REFACTOR -> done

The runner restores guarded files after every invocation (production files in RED,
test files in GREEN/REFACTOR/REPAIR, protected files always) and then decides the
outcome from test runs it performs itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ralph.config import Config
from ralph.runners import Runner
from ralph.specs import Acceptance, Spec, Task
from ralph.state import AcceptanceState, RunState, RunStore, Status, now
from ralph.verify import (
    CheckResult,
    SuiteResult,
    TestRef,
    classify_failure,
    discover_tests,
    local_modules,
    run_checks,
    run_suite,
    same_ast,
    skeleton_problems,
    test_hash,
    triviality_problems,
)
from ralph.workspace import (
    Changes,
    PathRules,
    Snapshot,
    copy_files,
    diff,
    file_hash,
    fresh_copy,
    restore,
    snapshot,
    tree_hash,
)


def tail(text: str, limit: int = 3000) -> str:
    text = text.strip()
    return text if len(text) <= limit else "[...]\n" + text[-limit:]


@dataclass
class Run:
    """Everything one run needs: configuration, selection, state and the runner."""

    cfg: Config
    spec: Spec
    tasks: list[Task]
    state: RunState
    store: RunStore
    runner: Runner
    rules: PathRules
    echo: Callable[[str], None] = print
    setup_checked: bool = False
    verdict_cache: dict[str, Verdict] = field(default_factory=dict)

    @property
    def workspace(self) -> Path:
        return Path(self.state.workspace)

    def acceptance(self) -> list[Acceptance]:
        return [ac for task in self.tasks for ac in task.acceptance]

    def automated(self) -> list[Acceptance]:
        return [ac for ac in self.acceptance() if not ac.manual]

    def status_of(self, ac: Acceptance) -> AcceptanceState:
        return self.state.acceptance[ac.id]

    def task_of(self, ac: Acceptance) -> Task:
        return next(t for t in self.tasks if t.id == ac.task_id)

    def production(self, rel: str) -> bool:
        return self.rules.is_production(rel) and not self.rules.is_protected(rel)

    def mapped_tests(self, ac: Acceptance) -> list[TestRef]:
        found = discover_tests(self.workspace, self.cfg.verify.test_dirs)
        return [ref for ref in found.values() if ac.matches_test(ref.function)]

    def suite(self) -> SuiteResult:
        v = self.cfg.verify
        return run_suite(
            self.workspace,
            v.test_command,
            v.test_dirs,
            self.store.path("junit.xml"),
            v.test_timeout_s,
        )

    def quality(self) -> list[CheckResult]:
        return run_checks(
            self.cfg.verify.quality_commands, self.workspace, self.cfg.verify.test_timeout_s
        )

    def smoke_command(self) -> list[str] | None:
        return self.spec.smoke_command or self.cfg.verify.smoke_command or None

    def note(self, text: str) -> None:
        self.echo(f"[ralph] {text}")
        self.store.append_progress(f"## Runner ({now()})\n{text}")

    def evidence(self, outcome: str, by: str = "agent", **extra: Any) -> dict[str, Any]:
        return {
            "iteration": self.state.iteration,
            "at": now(),
            "outcome": outcome,
            "by": by,
            **extra,
        }


@dataclass
class Step:
    phase: str  # setup | red | green | refactor | repair
    ac: Acceptance | None = None
    task: Task | None = None

    @property
    def key(self) -> str:
        return f"{self.phase}:{self.ac.id if self.ac else '-'}"

    def label(self) -> str:
        target = f" {self.ac.id} ({self.ac.task_id})" if self.ac else ""
        return f"{self.phase.upper()}{target}"


@dataclass
class Verification:
    ok: bool
    summary: str
    feedback: str = ""
    violations: list[str] = field(default_factory=list)
    changes: str = ""
    stop: Status | None = None
    stop_reason: str = ""


@dataclass
class Verdict:
    ok: bool
    problems: list[str] = field(default_factory=list)
    needs_human: list[str] = field(default_factory=list)
    report: list[str] = field(default_factory=list)
    feedback: str = ""


def _result(ok: bool, summary: str, violations: list[str], details: str = "") -> Verification:
    full = summary + (f" [{'; '.join(violations)}]" if violations else "")
    feedback = "" if ok else "\n".join([summary, *(f"- {v}" for v in violations), details]).strip()
    return Verification(ok, full, feedback, list(violations))


def _passed(suite: SuiteResult, base_nodeid: str) -> bool:
    cases = suite.for_base(base_nodeid)
    return bool(cases) and all(c.outcome == "passed" for c in cases)


def _describe(suite: SuiteResult, base_nodeid: str) -> str:
    cases = suite.for_base(base_nodeid)
    if not cases:
        file = base_nodeid.split("::", 1)[0]
        error = suite.collection_errors.get(file)
        return f"not collected ({error})" if error else "not collected"
    bad = next(c for c in cases if c.outcome != "passed")
    return f"{bad.outcome}: {bad.message[:300]}"


# --------------------------------------------------------------------------- phase selection


def next_open(run: Run) -> Acceptance | None:
    return next((ac for ac in run.automated() if run.status_of(ac).status != "done"), None)


def current_step(run: Run) -> Step:
    if not run.state.setup_done:
        return Step("setup")
    ac = next_open(run)
    if ac is None:
        return Step("repair")
    status = run.status_of(ac).status
    return Step("red" if status == "pending" else status, ac, run.task_of(ac))


# ------------------------------------------------------------------- runner-only transitions


def setup_check(run: Run) -> tuple[bool, str]:
    ws = run.workspace
    if not (ws / "pyproject.toml").is_file():
        return False, "The workspace has no pyproject.toml yet (no uv project)."
    for check in run_checks(run.cfg.verify.setup_commands, ws, run.cfg.verify.test_timeout_s):
        if not check.ok:
            return False, f"Setup command failed: {check.name}\n{tail(check.output)}"
    suite = run.suite()
    # Existing tests that import not-yet-implemented workspace code are fine here; only
    # infrastructure problems (pytest missing, syntax errors, third-party imports) are not.
    local = local_modules(ws, run.spec.package)
    broken = {f: r for f, e in suite.collection_errors.items() if (r := classify_failure(e, local))}
    if (
        broken
        or suite.timed_out
        or suite.exit_code in (None, 3, 4)
        or (suite.exit_code == 2 and not suite.collection_errors)
    ):
        details = "\n".join(f"{f}: {r}" for f, r in broken.items())
        header = f"pytest cannot run the suite (exit {suite.exit_code}):"
        return False, f"{header}\n{details}\n{tail(suite.output)}"
    for check in run.quality():
        if not check.ok:
            return False, f"Quality check failed: {check.name}\n{tail(check.output)}"
    return True, "pyproject present, setup commands pass, pytest collects, quality checks pass"


def take_baseline(run: Run, name: str) -> Snapshot:
    base = snapshot(run.workspace, only=run.production)
    fresh_copy(run.workspace, run.store.path(f"baselines/{name}"), base)
    run.store.save_snapshot(f"baselines/{name}", base)
    return base


def enter_red(run: Run, ac: Acceptance) -> None:
    """Preserve the pre-change production snapshot for this criterion."""
    status = run.status_of(ac)
    status.status = "red"
    status.baseline_hash = tree_hash(take_baseline(run, ac.id))


def settle(run: Run) -> None:
    """Advance through transitions that need no agent: setup already satisfied,
    or tests for the next criterion that already exist (e.g. written by you)."""
    state = run.state
    if not state.setup_done:
        if run.setup_checked:
            return
        run.setup_checked = True
        ok, detail = setup_check(run)
        if not ok:
            state.feedback = state.feedback or detail
            return
        state.setup_done = True
        run.note(f"Setup verified by the runner without an agent call: {detail}")
    while (ac := next_open(run)) is not None:
        status = run.status_of(ac)
        if status.status != "pending":
            return
        enter_red(run, ac)
        if status.stale_tests or not run.mapped_tests(ac):
            return
        verification = evaluate_red(run, ac, [], by="runner")
        run.note(f"Runner-only RED check for {ac.id} using existing tests: {verification.summary}")
        if not verification.ok:
            state.feedback = verification.feedback
            return
        if status.status != "done":
            return


# --------------------------------------------------------------------------- guards


@dataclass
class PreInvocation:
    workspace: Snapshot
    harness: Snapshot


def harness_snapshot(run: Run) -> Snapshot:
    """Every harness file outside the workspace and the state directory, plus the spec.

    Any change during an invocation stops the run: the agent must only touch the workspace.
    """
    root = run.cfg.root.resolve()
    excluded = [
        p.relative_to(root).as_posix()
        for p in (run.workspace.resolve(), run.cfg.state_dir.resolve())
        if p.is_relative_to(root)
    ]
    snap = snapshot(
        root, only=lambda rel: not any(rel == e or rel.startswith(e + "/") for e in excluded)
    )
    spec = Path(run.state.spec_path).resolve()
    if spec.is_file() and not spec.is_relative_to(root):
        snap[str(spec)] = file_hash(spec)
    return snap


def capture_pre(run: Run) -> PreInvocation:
    ws_snap = snapshot(run.workspace)
    protected = {p: h for p, h in ws_snap.items() if run.rules.is_protected(p)}
    fresh_copy(run.workspace, run.store.path("pre"), protected)
    return PreInvocation(ws_snap, harness_snapshot(run))


def enforce_guards(
    run: Run, step: Step, pre: PreInvocation
) -> tuple[list[str], Changes, Verification | None]:
    harness_changes = diff(pre.harness, harness_snapshot(run))
    if harness_changes:
        reason = (
            f"Protected harness files changed during the invocation: {harness_changes.describe()}. "
            "Inspect them (e.g. git diff) before resuming. If you edited the spec on purpose, "
            "resume with --reconcile."
        )
        return (
            [],
            Changes(),
            Verification(
                False,
                "protected harness files changed",
                reason,
                stop=Status.BLOCKED,
                stop_reason=reason,
            ),
        )
    if run.store.tampered():
        reason = f"{run.store.state_path} was modified outside the runner."
        return (
            [],
            Changes(),
            Verification(
                False, "run state tampered", reason, stop=Status.BLOCKED, stop_reason=reason
            ),
        )

    ws = run.workspace
    changes = diff(pre.workspace, snapshot(ws))
    violations: list[str] = []
    protected = changes.only(run.rules.is_protected)
    if protected:
        pre_protected = {p: h for p, h in pre.workspace.items() if run.rules.is_protected(p)}
        restore(ws, run.store.path("pre"), pre_protected, protected.paths)
        violations.append(f"protected workspace files reverted: {protected.describe()}")

    if step.phase == "setup":
        violations += _guard_setup(run, changes)
    elif step.phase == "red" and step.ac:
        violations += _guard_red(run, step.ac)
    else:
        violations += _guard_locked_tests(run)
        if step.phase == "green" and step.task and step.task.type == "docs" and step.ac:
            violations += _guard_docs(run, step.ac)
    return violations, changes, None


def _restore_from_baseline(run: Run, name: str, paths: list[str]) -> None:
    restore(
        run.workspace,
        run.store.path(f"baselines/{name}"),
        run.store.load_snapshot(f"baselines/{name}"),
        paths,
    )


def _guard_setup(run: Run, changes: Changes) -> list[str]:
    bad = [
        p
        for p in [*changes.added, *changes.modified]
        if run.production(p) and skeleton_problems(run.workspace / p)
    ]
    if not bad:
        return []
    _restore_from_baseline(run, "_setup", bad)
    return [f"behavior in SETUP reverted (functions/classes are not scaffolding): {', '.join(bad)}"]


def _guard_red(run: Run, ac: Acceptance) -> list[str]:
    violations = []
    base = run.store.load_snapshot(f"baselines/{ac.id}")
    current = snapshot(run.workspace, only=run.production)
    changed = diff(base, current).only(lambda p: not run.rules.is_red_allowed(p))
    if changed:
        _restore_from_baseline(run, ac.id, changed.paths)
        violations.append(
            f"production changes reverted (RED allows only tests): {changed.describe()}"
        )
    broken = sorted(
        {
            nodeid.split("::", 1)[0]
            for status in run.state.acceptance.values()
            for nodeid, digest in status.tests.items()
            if test_hash(run.workspace, nodeid) != digest
        }
    )
    if broken:
        restore(
            run.workspace, run.store.path("tests_ref"), run.store.load_snapshot("tests_ref"), broken
        )
        violations.append(f"frozen tests restored: {', '.join(broken)}")
    return violations


def _guard_locked_tests(run: Run) -> list[str]:
    if not run.store.path("tests_ref.json").exists():
        return []
    ref, ref_dir = run.store.load_snapshot("tests_ref"), run.store.path("tests_ref")
    changed = diff(ref, snapshot(run.workspace, only=run.rules.is_test))
    if not changed:
        return []
    # Formatting-only edits are kept (lint/format checks also cover tests) and become the
    # new reference; anything that changes a test file's AST is reverted.
    formatting = [p for p in changed.modified if same_ast(ref_dir / p, run.workspace / p)]
    if formatting:
        copy_files(run.workspace, ref_dir, formatting)
        ref.update({p: file_hash(run.workspace / p) for p in formatting})
        run.store.save_snapshot("tests_ref", ref)
    rejected = [p for p in changed.paths if p not in formatting]
    if not rejected:
        return []
    restore(run.workspace, ref_dir, ref, rejected)
    return [
        "test changes reverted (tests are frozen in this phase; only formatting may change): "
        + ", ".join(rejected)
    ]


def _guard_docs(run: Run, ac: Acceptance) -> list[str]:
    base = run.store.load_snapshot(f"baselines/{ac.id}")
    current = snapshot(run.workspace, only=run.production)
    changed = diff(base, current).only(lambda p: not run.rules.is_docs(p))
    if not changed:
        return []
    _restore_from_baseline(run, ac.id, changed.paths)
    return [f"non-documentation changes reverted (docs task): {changed.describe()}"]


# --------------------------------------------------------------------------- phase gates


def freeze(run: Run, ac: Acceptance, refs: list[TestRef]) -> None:
    status = run.status_of(ac)
    status.tests = {r.nodeid: test_hash(run.workspace, r.nodeid) or "" for r in refs}
    status.stale_tests = False
    tests = snapshot(run.workspace, only=run.rules.is_test)
    fresh_copy(run.workspace, run.store.path("tests_ref"), tests)
    run.store.save_snapshot("tests_ref", tests)


def evaluate_red(run: Run, ac: Acceptance, violations: list[str], by: str) -> Verification:
    status, task = run.status_of(ac), run.task_of(ac)
    refs = run.mapped_tests(ac)
    dirs = ", ".join(run.cfg.verify.test_dirs)
    if not refs:
        suite = run.suite()
        details = f"No test named {ac.test_prefix}_* was found under {dirs}."
        if suite.collection_error:
            errors = "; ".join(f"{f}: {e}" for f, e in suite.collection_errors.items())
            details += f"\nPytest could not collect the suite: {errors}\n{tail(suite.output, 1500)}"
        return _result(
            False,
            f"RED not established for {ac.id}: no test named {ac.test_prefix}_*",
            violations,
            details,
        )
    problems = [p for r in refs for p in triviality_problems(run.workspace, r.nodeid)]
    if problems:
        return _result(
            False,
            f"RED not established for {ac.id}: trivial or unconditional test",
            violations,
            "\n".join(problems),
        )

    suite = run.suite()
    outcomes: dict[str, tuple[str, str]] = {}
    for ref in refs:
        cases = suite.for_base(ref.nodeid)
        if cases:
            bad = next((c for c in cases if c.outcome in ("error", "skipped")), None)
            failed = next((c for c in cases if c.outcome == "failed"), None)
            chosen = bad or failed
            outcomes[ref.nodeid] = (chosen.outcome, chosen.message) if chosen else ("passed", "")
        elif ref.file in suite.collection_errors:
            outcomes[ref.nodeid] = ("failed", suite.collection_errors[ref.file])
        else:
            outcomes[ref.nodeid] = ("missing", "not collected by pytest")
    invalid = [
        f"{n}: {o} ({m[:200]})" for n, (o, m) in outcomes.items() if o != "passed" and o != "failed"
    ]
    if invalid:
        return _result(
            False,
            f"RED not established for {ac.id}: tests errored, were skipped or not collected",
            violations,
            "\n".join(invalid) + "\n" + tail(suite.output, 1500),
        )
    failures = {n: m for n, (o, m) in outcomes.items() if o == "failed"}

    if task.type == "refactor":
        if failures:
            return _result(
                False,
                f"Characterization tests for {ac.id} must pass on the current code "
                "before refactoring",
                violations,
                "\n".join(f"{n}: {m[:300]}" for n, m in failures.items()),
            )
        freeze(run, ac, refs)
        status.kind, status.status = "characterization", "refactor"
        status.red = run.evidence("passed", by, tests=sorted(status.tests))
        _extend_baseline(run, list(status.tests))
        return _result(
            True,
            f"Characterization verified for {ac.id}: {len(refs)} test(s) pass on current code",
            violations,
        )

    if not failures:
        freeze(run, ac, refs)
        status.kind, status.status = "pre-existing", "done"
        status.red = run.evidence(
            "passed",
            by,
            tests=sorted(status.tests),
            note="behavior already present; no production change was made",
        )
        _extend_baseline(run, list(status.tests))
        return _result(
            True,
            f"{ac.id} already satisfied: its test passes on unchanged code "
            "(recorded as pre-existing, no code was broken to fake RED)",
            violations,
        )

    local = local_modules(run.workspace, run.spec.package)
    infra = {n: reason for n, m in failures.items() if (reason := classify_failure(m, local))}
    if infra:
        return _result(
            False,
            f"RED not established for {ac.id}: the failure is not caused by missing behavior",
            violations,
            "\n".join(f"{n}: {r}" for n, r in infra.items()),
        )
    freeze(run, ac, refs)
    status.status = "green"
    status.red = run.evidence(
        "failed",
        by,
        baseline_hash=status.baseline_hash,
        tests={n: m[:300] for n, m in failures.items()},
    )
    first = next(iter(failures.values()))[:160]
    return _result(
        True,
        f"RED verified for {ac.id}: {len(failures)} test(s) fail on unchanged code ({first})",
        violations,
    )


def _extend_baseline(run: Run, nodeids: list[str]) -> None:
    run.state.passing_baseline = sorted(set(run.state.passing_baseline) | set(nodeids))


def evaluate_tests(
    run: Run, ac: Acceptance, violations: list[str], phase: str, need_quality: bool
) -> Verification:
    """GREEN/REFACTOR gate: frozen tests pass unchanged, no regressions, optional quality."""
    status = run.status_of(ac)
    suite = run.suite()
    problems: list[str] = []
    if suite.collection_errors:
        problems.append(
            "pytest could not collect: "
            + "; ".join(f"{f}: {e}" for f, e in suite.collection_errors.items())
        )
    elif suite.collection_error:
        problems.append(f"pytest did not complete (exit {suite.exit_code})")
    for nodeid, digest in status.tests.items():
        if test_hash(run.workspace, nodeid) != digest:
            problems.append(f"frozen test changed or missing: {nodeid}")
        elif not _passed(suite, nodeid):
            problems.append(f"frozen test not passing: {nodeid}: {_describe(suite, nodeid)}")
    regressions = [
        n for n in run.state.passing_baseline if n not in status.tests and not _passed(suite, n)
    ]
    if regressions:
        problems.append("previously passing tests now fail: " + ", ".join(regressions))
    failed_checks = [c for c in (run.quality() if need_quality else []) if not c.ok]
    problems += [f"quality check failed: {c.name}" for c in failed_checks]
    if problems:
        details = tail(suite.output) + "".join(
            f"\n\n$ {c.name}\n{tail(c.output, 1500)}" for c in failed_checks
        )
        return _result(
            False,
            f"{phase.upper()} rejected for {ac.id}: {problems[0]}",
            violations,
            "\n".join(f"- {p}" for p in problems[1:]) + "\n\n" + details,
        )
    passing = {c.base_nodeid for c in suite.cases.values() if _passed(suite, c.base_nodeid)}
    _extend_baseline(run, sorted(passing))
    quality = " and quality checks" if need_quality else ""
    return _result(
        True,
        f"{phase.upper()} verified for {ac.id}: {len(status.tests)} frozen test(s), "
        f"{len(passing)} passing test(s){quality}",
        violations,
    )


def evaluate(run: Run, step: Step, violations: list[str], changes: Changes) -> Verification:
    if step.phase == "setup":
        ok, detail = setup_check(run)
        if ok:
            run.state.setup_done = True
        return _result(
            ok,
            f"SETUP {'verified' if ok else 'not verified'}: {detail.splitlines()[0]}",
            violations,
            detail,
        )
    if step.phase == "repair":
        verdict = final_verdict(run)
        ok = not verdict.problems
        return _result(
            ok,
            "REPAIR verified: final checks pass"
            if ok
            else f"REPAIR rejected: {verdict.problems[0]}",
            violations,
            verdict.feedback,
        )
    assert step.ac is not None and step.task is not None
    ac, status = step.ac, run.status_of(step.ac)
    if step.phase == "red":
        return evaluate_red(run, ac, violations, by="agent")
    if step.phase == "green":
        docs = step.task.type == "docs"
        result = evaluate_tests(run, ac, violations, "green", need_quality=docs)
        if result.ok:
            status.green = run.evidence("passed", tests=sorted(status.tests))
            status.status = "done" if docs else "refactor"
        return result
    result = evaluate_tests(run, ac, violations, "refactor", need_quality=True)
    if result.ok:
        touched = [p for p in changes.paths if run.production(p)]
        status.refactor = run.evidence("passed", no_change=not touched, changed=touched)
        status.status = "done"
    return result


# --------------------------------------------------------------------------- completion


def _missing_evidence(status: AcceptanceState, task: Task) -> list[str]:
    need = {"pre-existing": ["red"], "characterization": ["red", "refactor"], "manual": []}.get(
        status.kind, ["red", "green"] if task.type == "docs" else ["red", "green", "refactor"]
    )
    return [phase for phase in need if not getattr(status, phase)]


def final_verdict(run: Run, report_only: bool = False) -> Verdict:
    """Independent completion check. The agent's words, exit codes and plans count for nothing.

    report_only runs every check even when criteria are still open (for --verify-only).
    """
    state = run.state
    problems: list[str] = []
    if not state.setup_done:
        problems.append("project setup has not been verified by the runner")
    open_ids = [ac.id for ac in run.automated() if run.status_of(ac).status != "done"]
    if open_ids:
        problems.append(f"criteria without complete TDD evidence: {', '.join(open_ids)}")
    if problems and not report_only:
        return Verdict(False, problems)
    manual = [ac.id for ac in run.acceptance() if ac.manual and run.status_of(ac).status != "done"]
    key = (
        tree_hash(snapshot(run.workspace))
        + "|"
        + ",".join(manual)
        + f"|{len(state.passing_baseline)}"
    )
    if key in run.verdict_cache and not report_only:
        return run.verdict_cache[key]

    report: list[str] = []
    suite = run.suite()
    if suite.collection_error:
        problems.append(f"pytest did not collect/complete cleanly (exit {suite.exit_code})")
    if not suite.cases:
        problems.append("zero tests collected: an empty suite is never success")
    found = discover_tests(run.workspace, run.cfg.verify.test_dirs)
    for ac in run.automated():
        status = run.status_of(ac)
        mapped = [r for r in found.values() if ac.matches_test(r.function)]
        if not mapped:
            problems.append(f"{ac.id}: no test mapped (expected {ac.test_prefix}_*)")
            continue
        if missing := _missing_evidence(status, run.task_of(ac)):
            problems.append(f"{ac.id}: missing runner evidence for {', '.join(missing)}")
        problems += [
            f"{ac.id}: frozen test changed: {n}"
            for n, d in status.tests.items()
            if test_hash(run.workspace, n) != d
        ]
        failing = [r.nodeid for r in mapped if not _passed(suite, r.nodeid)]
        problems += [f"{ac.id}: {n} {_describe(suite, n)}" for n in failing]
        report.append(
            f"{ac.id}: {len(mapped)} test(s), evidence={status.kind}, "
            f"{'FAILING' if failing else 'passing'}"
        )
    problems += [
        f"regression: {n} {_describe(suite, n)}"
        for n in state.passing_baseline
        if not _passed(suite, n)
    ]
    outputs = ""
    for check in run.quality():
        report.append(f"quality {'ok' if check.ok else 'FAILED'}: {check.name}")
        if not check.ok:
            problems.append(f"quality check failed: {check.name}")
            outputs += f"\n\n$ {check.name}\n{tail(check.output, 1500)}"
    if smoke := run.smoke_command():
        (check,) = run_checks([smoke], run.workspace, run.cfg.verify.test_timeout_s)
        report.append(f"smoke {'ok' if check.ok else 'FAILED'}: {check.name}")
        if not check.ok:
            problems.append(f"smoke check failed: {check.name}")
            outputs += f"\n\n$ {check.name}\n{tail(check.output, 1500)}"
    report += [f"{m}: manual criterion awaiting human acceptance" for m in manual]
    feedback = ""
    if problems:
        feedback = "Final verification failed:\n" + "\n".join(f"- {p}" for p in problems)
        feedback += "\n\n" + tail(suite.output, 2500) + outputs
    verdict = Verdict(
        ok=not problems and not manual,
        problems=problems,
        needs_human=manual if not problems else [],
        report=report,
        feedback=feedback,
    )
    run.verdict_cache[key] = verdict
    return verdict
