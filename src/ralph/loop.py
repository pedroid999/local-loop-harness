"""The classic Ralph loop: one fresh agent invocation per iteration, runner verification,
file-based memory, and success only when the runner independently verifies completion.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path

from ralph.config import Config
from ralph.context import render_prompt
from ralph.runners import InvocationResult, Runner
from ralph.specs import Spec, Task, parse_spec, resolve_task_id, select_tasks, selection_hash
from ralph.state import (
    AcceptanceState,
    RunState,
    RunStore,
    StateError,
    Status,
    latest_run_id,
    new_run_id,
    now,
    run_lock,
)
from ralph.tdd import (
    PreInvocation,
    Run,
    Step,
    Verdict,
    Verification,
    capture_pre,
    current_step,
    enforce_guards,
    evaluate,
    final_verdict,
    settle,
    take_baseline,
)
from ralph.workspace import PathRules, check_workspace_location, diff, restore, snapshot

Echo = Callable[[str], None]


class ReconcileRequired(RuntimeError):
    """The selected spec changed since the run started."""


# --------------------------------------------------------------------------- the loop


def run_loop(run: Run) -> Status:
    state = run.state
    with run_lock(run.cfg.state_dir, state.run_id):
        step: Step | None = None
        pre: PreInvocation | None = None
        try:
            settle(run)
            while state.max_iterations is None or state.iteration < state.max_iterations:
                verdict = independently_verified_complete(run)
                if verdict.ok:
                    return finish(run, Status.SUCCESS)
                if verdict.needs_human:
                    return finish(run, Status.BLOCKED, _manual_reason(verdict))

                state.iteration += 1
                step = current_step(run)
                context = build_fresh_context(run, step)
                pre = capture_pre(run)
                result = run_one_iteration(run, step, context)
                verification = independently_verify(run, step, result, pre)
                pre = None
                persist_progress_and_feedback(run, step, result, verification)
                if verification.stop is not None:
                    return finish(run, verification.stop, verification.stop_reason)

                if independently_verified_complete(run).ok:
                    return finish(run, Status.SUCCESS)

            if independently_verified_complete(run).ok:
                return finish(run, Status.SUCCESS)
            return finish(run, Status.MAX_ITERATIONS_REACHED, _budget_reason(run))
        except KeyboardInterrupt:
            if step is not None and pre is not None:
                # Best effort: revert disallowed changes made by the interrupted invocation.
                with contextlib.suppress(Exception):
                    enforce_guards(run, step, pre)
            return finish(run, Status.CANCELLED, "Interrupted (Ctrl-C). Resume with --resume.")


def independently_verified_complete(run: Run) -> Verdict:
    verdict = final_verdict(run)
    all_done = all(run.status_of(ac).status == "done" for ac in run.automated())
    run.state.repair_needed = all_done and run.state.setup_done and bool(verdict.problems)
    if run.state.repair_needed:
        run.state.feedback = verdict.feedback
    return verdict


def build_fresh_context(run: Run, step: Step) -> str:
    return render_prompt(run, step)


def run_one_iteration(run: Run, step: Step, prompt: str) -> InvocationResult:
    limit = run.state.max_iterations or "∞"
    run.echo(
        f"[ralph] iteration {run.state.iteration}/{limit} · {step.label()} · "
        f"invoking {run.runner.name}"
    )
    log = run.store.iteration_dir(run.state.iteration) / "agent.log"
    result = run.runner.invoke(prompt, run.workspace, log, run.cfg.loop.invocation_timeout_s)
    run.echo(f"[ralph]   runner: {result.summary()}")
    return result


def independently_verify(
    run: Run, step: Step, result: InvocationResult, pre: PreInvocation
) -> Verification:
    violations, changes, stop = enforce_guards(run, step, pre)
    if stop is not None:
        return stop
    verification = evaluate(run, step, violations, changes)
    verification.changes = changes.describe()
    if result.permission_denials:
        hint = (
            "The CLI auto-rejected permission requests: "
            + "; ".join(result.permission_denials[:5])
            + ". Grant scoped permissions in [runner.opencode.permission] (ralph.toml)."
        )
        verification.feedback = (verification.feedback + "\n" + hint).strip()
        run.echo(f"[ralph]   warning: {hint}")
    return verification


def persist_progress_and_feedback(
    run: Run, step: Step, result: InvocationResult, verification: Verification
) -> None:
    state, cfg = run.state, run.cfg
    mark = "✓" if verification.ok else "✗"
    run.echo(f"[ralph]   {mark} {verification.summary}")

    if result.fatal:
        verification.stop = Status.RUNNER_ERROR
        verification.stop_reason = f"The coding CLI cannot run: {result.cli_error}"
    elif not result.ok and not verification.ok:
        state.consecutive_runner_failures += 1
        if state.consecutive_runner_failures >= cfg.loop.max_consecutive_runner_failures:
            cause = result.cli_error or ("timed out" if result.timed_out else "failed")
            verification.stop = Status.RUNNER_ERROR
            verification.stop_reason = (
                f"{state.consecutive_runner_failures} consecutive failed invocations; last: {cause}"
            )
    else:
        state.consecutive_runner_failures = 0

    if verification.ok:
        state.stall_key, state.stall_count = "", 0
    else:
        same = state.stall_key == step.key
        state.stall_key, state.stall_count = step.key, (state.stall_count + 1 if same else 1)
        if state.stall_count >= cfg.loop.warn_after_stalls:
            run.echo(
                f"[ralph]   warning: the same phase has failed {state.stall_count} times in a row "
                f"({step.label()}). Consider clarifying the spec, --restart-red, or reading "
                f"{run.store.iteration_dir(state.iteration)}/agent.log"
            )
        stop_after = cfg.loop.stop_after_stalls
        if stop_after and state.stall_count >= stop_after and verification.stop is None:
            verification.stop = Status.BLOCKED
            verification.stop_reason = (
                f"No progress for {state.stall_count} iterations on {step.label()} "
                "(opt-in stop_after_stalls)."
            )

    dispute = next(
        (line for line in result.final_text.splitlines() if "RALPH-TEST-DISPUTE:" in line), ""
    )
    if dispute:
        run.echo(
            f"[ralph]   agent disputes a frozen test: {dispute.strip()} "
            f"(if right: --resume {state.run_id} --restart-red {step.ac.id if step.ac else '<AC>'})"
        )

    state.feedback = "" if verification.ok else verification.feedback
    state.history.append(
        {
            "iteration": state.iteration,
            "at": now(),
            "phase": step.phase,
            "ac": step.ac.id if step.ac else None,
            "ok": verification.ok,
            "summary": verification.summary,
            "runner": result.summary(),
            "changes": verification.changes,
        }
    )
    state.history = state.history[-100:]
    iteration_dir = run.store.iteration_dir(state.iteration)
    (iteration_dir / "verify.md").write_text(
        f"{verification.summary}\n\n{verification.feedback}\n", encoding="utf-8"
    )
    run.store.append_progress(
        f"## Iteration {state.iteration} — {step.label()} — {now()}\n"
        f"- runner: {result.summary()}\n- changes: {verification.changes}\n"
        f"- verdict: {mark} {verification.summary}"
        + (f"\n- agent dispute: {dispute.strip()}" if dispute else "")
    )
    if verification.stop is None:
        settle(run)
    save(run)


def finish(run: Run, status: Status, reason: str = "") -> Status:
    state = run.state
    state.status, state.stop_reason = status, reason
    save(run)
    run.store.append_progress(f"## Run finished: {status} — {now()}\n{reason}")
    done = sum(run.status_of(ac).status == "done" for ac in run.acceptance())
    run.echo(
        f"[ralph] {status}: {done}/{len(run.acceptance())} criteria verified "
        f"after {state.iteration} iteration(s). {reason}".rstrip()
    )
    run.echo(f"[ralph] evidence and logs: {run.store.dir}")
    if status is not Status.SUCCESS:
        run.echo(f"[ralph] resume with: uv run ralph.py --resume {state.run_id}")
    return status


def save(run: Run) -> None:
    run.store.save(run.state)
    run.store.write("plan.md", render_plan(run))
    run.store.write("feedback.md", run.state.feedback or "(no pending feedback)\n")


def render_plan(run: Run) -> str:
    state = run.state
    lines = [
        f"# Plan: {run.spec.title} (run {state.run_id})",
        "",
        f"Spec: {state.spec_path}  ",
        f"Selected tasks: {', '.join(state.selected_tasks)}  ",
        f"Iterations consumed: {state.iteration} (limit: {state.max_iterations or 'none'})  ",
        f"Status: {state.status}",
        "",
        "| Criterion | Task | Status | Evidence | Tests |",
        "|---|---|---|---|---|",
    ]
    for ac in run.acceptance():
        s = run.status_of(ac)
        evidence = "+".join(p for p in ("red", "green", "refactor") if getattr(s, p)) or "-"
        kind = "manual" if ac.manual and s.kind != "manual" else s.kind
        lines.append(
            f"| {ac.id} | {ac.task_id} | {s.status} | {kind}: {evidence} | {len(s.tests)} |"
        )
    return "\n".join(lines) + "\n"


def _manual_reason(verdict: Verdict) -> str:
    ids = " ".join(f"--accept-manual {i}" for i in verdict.needs_human)
    return (
        f"All automated criteria are verified. Manual criteria need a human: "
        f"{', '.join(verdict.needs_human)}. Review them, then resume with {ids}."
    )


def _budget_reason(run: Run) -> str:
    done = sum(run.status_of(ac).status == "done" for ac in run.acceptance())
    return (
        f"Iteration budget of {run.state.max_iterations} exhausted with {done}/"
        f"{len(run.acceptance())} criteria verified. The work is NOT complete."
    )


# --------------------------------------------------------------------------- run setup


def _rules(cfg: Config, spec: Spec) -> PathRules:
    return PathRules(
        test_dirs=cfg.verify.test_dirs,
        docs_globs=cfg.verify.docs_globs,
        protected=[*cfg.verify.protected_workspace_paths, *spec.acceptance_tests],
        red_allowed=cfg.verify.red_allowed_paths,
    )


def create_run(
    cfg: Config,
    spec_path: Path,
    requested: list[str] | None,
    runner: Runner,
    *,
    max_iterations: int | None = None,
    echo: Echo = print,
) -> Run:
    spec = parse_spec(spec_path)
    tasks = select_tasks(spec, requested)
    check_workspace_location(cfg.workspace, cfg.root, cfg.protected_paths, cfg.state_dir)
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    run_id = new_run_id(spec.id)
    while RunStore(cfg.state_dir, run_id).exists():
        run_id += "x"
    state = RunState(
        run_id=run_id,
        spec_path=str(spec_path.resolve()),
        spec_id=spec.id,
        requested_tasks=[resolve_task_id(spec, r) for r in requested or []],
        selected_tasks=[t.id for t in tasks],
        selection_hash=selection_hash(tasks, spec),
        task_hashes={t.id: t.content_hash for t in tasks},
        workspace=str(cfg.workspace.resolve()),
        runner=runner.name,
        max_iterations=max_iterations,
        acceptance=_acceptance_states(tasks),
    )
    run = Run(
        cfg, spec, tasks, state, RunStore(cfg.state_dir, run_id), runner, _rules(cfg, spec), echo
    )
    take_baseline(run, "_setup")
    save(run)
    tasks_text = ", ".join(state.selected_tasks)
    run.store.append_progress(
        f"# Progress for run {run_id}\nSpec {spec_path} · tasks {tasks_text} · "
        f"runner {runner.name} · limit {max_iterations or 'none'}"
    )
    echo(f"[ralph] run {run_id}: {len(run.acceptance())} criteria in {tasks_text}")
    return run


def _acceptance_states(tasks: list[Task]) -> dict[str, AcceptanceState]:
    return {
        ac.id: AcceptanceState(ac.id, ac.task_id, ac.text_hash)
        for t in tasks
        for ac in t.acceptance
    }


def open_run(
    cfg: Config,
    run_id: str,
    make_runner: Callable[[str], Runner],
    *,
    max_iterations: int | None = None,
    echo: Echo = print,
    reconcile: bool = False,
    runner_name: str | None = None,
) -> Run:
    """Load a run for resume. The consumed iteration count and TDD phases are kept."""
    if run_id == "latest":
        run_id = latest_run_id(cfg.state_dir) or ""
        if not run_id:
            raise StateError(f"No runs found in {cfg.state_dir}")
    store = RunStore(cfg.state_dir, run_id)
    state = store.load()
    spec = parse_spec(Path(state.spec_path))
    tasks = select_tasks(spec, state.requested_tasks or None)
    check_workspace_location(Path(state.workspace), cfg.root, cfg.protected_paths, cfg.state_dir)
    runner = make_runner(runner_name or state.runner)
    run = Run(cfg, spec, tasks, state, store, runner, _rules(cfg, spec), echo)

    if selection_hash(tasks, spec) != state.selection_hash:
        changes = describe_spec_changes(state, tasks)
        if not reconcile:
            raise ReconcileRequired(
                f"The selected spec changed since run {run_id} started:\n{changes}\n"
                "Stale completion evidence is not reused silently. Resume with --reconcile to "
                "redo changed criteria (unchanged ones keep their evidence), or start a new run."
            )
        _reconcile(run, changes)

    state.max_iterations = max_iterations
    state.status, state.stop_reason, state.runner = Status.RUNNING, "", runner.name
    save(run)
    done = sum(run.status_of(ac).status == "done" for ac in run.acceptance())
    echo(
        f"[ralph] resuming {run_id} at iteration {state.iteration}: {done}/"
        f"{len(run.acceptance())} criteria verified"
    )
    return run


def describe_spec_changes(state: RunState, tasks: list[Task]) -> str:
    current = {ac.id: ac for t in tasks for ac in t.acceptance}
    lines = []
    for ac_id, ac in current.items():
        if ac_id not in state.acceptance:
            lines.append(f"- {ac_id}: new criterion")
        elif state.acceptance[ac_id].text_hash != ac.text_hash:
            lines.append(f"- {ac_id}: criterion text changed")
    lines += [f"- {a}: criterion removed" for a in state.acceptance if a not in current]
    for task in tasks:
        if state.task_hashes.get(task.id) not in (None, task.content_hash):
            lines.append(f"- {task.id}: task text changed")
    return (
        "\n".join(lines) or "- spec metadata changed (package, acceptance_tests or smoke_command)"
    )


def _reconcile(run: Run, changes: str) -> None:
    state = run.state
    current = {ac.id: ac for t in run.tasks for ac in t.acceptance}
    fresh = _acceptance_states(run.tasks)
    for ac_id, ac in current.items():
        old = state.acceptance.get(ac_id)
        if old is None or old.text_hash != ac.text_hash:
            if old is not None:
                state.passing_baseline = [n for n in state.passing_baseline if n not in old.tests]
            fresh[ac_id].stale_tests = old is not None
        else:
            fresh[ac_id] = old
    state.acceptance = fresh
    state.selected_tasks = [t.id for t in run.tasks]
    state.selection_hash = selection_hash(run.tasks, run.spec)
    state.task_hashes = {t.id: t.content_hash for t in run.tasks}
    state.repair_needed = False
    state.feedback = ""
    run.note(f"Spec reconciled on explicit request:\n{changes}")


def accept_manual(run: Run, ac_ids: list[str]) -> None:
    manual = {ac.id: ac for ac in run.acceptance() if ac.manual}
    for ac_id in ac_ids:
        if ac_id not in manual:
            raise StateError(
                f"{ac_id} is not a manual criterion of this run ({', '.join(manual) or 'none'})"
            )
        status = run.state.acceptance[ac_id]
        status.status, status.kind = "done", "manual"
        status.red = {"outcome": "accepted", "by": "human", "at": now()}
        run.note(f"{ac_id} accepted manually by the user.")
    save(run)


def restart_red(run: Run, ac_id: str) -> None:
    """Explicitly restart RED for a criterion whose frozen test is wrong: restore production
    files to that criterion's preserved baseline and invalidate its downstream evidence."""
    order = [ac.id for ac in run.automated()]
    if ac_id not in order:
        raise StateError(f"{ac_id} is not an automated criterion of this run")
    name = f"baselines/{ac_id}"
    if not run.store.path(f"{name}.json").exists():
        raise StateError(f"{ac_id} has no preserved baseline yet (it never entered RED)")
    base = run.store.load_snapshot(name)
    current = snapshot(run.workspace, only=run.production)
    restore(run.workspace, run.store.path(name), base, diff(base, current).paths)
    invalidated = []
    for later in order[order.index(ac_id) :]:
        status = run.state.acceptance[later]
        if status.status != "pending" or later == ac_id:
            run.state.passing_baseline = [
                n for n in run.state.passing_baseline if n not in status.tests
            ]
            status.reset()
            invalidated.append(later)
    run.state.repair_needed, run.state.feedback = False, ""
    run.note(
        f"RED restarted for {ac_id} by the user; production restored to its baseline; "
        f"evidence invalidated for {', '.join(invalidated)}."
    )
    save(run)
