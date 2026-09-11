"""Command-line interface. Exit codes: 0 SUCCESS, 10 MAX_ITERATIONS_REACHED, 11 BLOCKED,
12 RUNNER_ERROR, 130 CANCELLED, 2 usage/configuration error, 1 --verify-only not complete."""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from ralph.config import Config, ConfigError, load_config
from ralph.loop import (
    ReconcileRequired,
    _acceptance_states,
    _rules,
    accept_manual,
    create_run,
    open_run,
    restart_red,
    run_loop,
)
from ralph.runners import InvocationResult, Runner, make_runner
from ralph.specs import Spec, SpecError, parse_spec, resolve_task_id, select_tasks, selection_hash
from ralph.state import EXIT_CODES, LockError, RunState, RunStore, StateError, Status, run_lock
from ralph.tdd import Run, final_verdict
from ralph.workspace import WorkspaceError, check_workspace_location

RunnerFactory = Callable[[str, Config], Runner]


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ralph",
        description="Classic Ralph loop with runner-enforced strict TDD. Without "
        "--max-iterations it loops until the runner verifies completion, you press Ctrl-C, "
        "or an unrecoverable operational error occurs.",
    )
    parser.add_argument("--spec", type=Path, help="Markdown spec to implement")
    parser.add_argument(
        "--task",
        action="append",
        metavar="ID",
        help="task ID to select (plus its dependencies); repeatable",
    )
    parser.add_argument(
        "--max-iterations",
        type=positive_int,
        metavar="N",
        help="total agent invocations for the run (default: unlimited)",
    )
    parser.add_argument("--runner", choices=["opencode", "command"], help="coding CLI adapter")
    parser.add_argument("--workspace", type=Path, help="application directory (default: app/)")
    parser.add_argument("--config", type=Path, default=Path("ralph.toml"))
    parser.add_argument("--resume", metavar="RUN_ID", help="resume a run ('latest' works)")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="run the independent completion checks without invoking the agent",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="with --resume: accept spec changes, redoing changed criteria",
    )
    parser.add_argument(
        "--restart-red",
        metavar="AC_ID",
        help="with --resume: restart RED for a criterion whose frozen test is wrong",
    )
    parser.add_argument(
        "--accept-manual",
        action="append",
        metavar="AC_ID",
        help="with --resume: record your acceptance of a (manual) criterion",
    )
    parser.add_argument(
        "--stop-after-stalls",
        type=positive_int,
        metavar="N",
        help="opt-in: stop BLOCKED after N consecutive failed iterations on one phase",
    )
    return parser


def main(argv: Sequence[str] | None = None, runner_factory: RunnerFactory | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if bool(args.spec) == bool(args.resume):
        parser.error("use exactly one of --spec or --resume")
    if args.task and not args.spec:
        parser.error("--task requires --spec")
    if (args.reconcile or args.restart_red or args.accept_manual) and not args.resume:
        parser.error("--reconcile, --restart-red and --accept-manual require --resume")

    def echo(line: str) -> None:
        print(line, flush=True)

    try:
        cfg = load_config(
            args.config if args.config.exists() else None,
            root=args.config.parent if args.config.exists() else Path.cwd(),
        )
        if args.workspace:
            cfg.workspace = args.workspace.resolve()
        if args.stop_after_stalls:
            cfg.loop.stop_after_stalls = args.stop_after_stalls

        def factory(name: str) -> Runner:
            if runner_factory:
                return runner_factory(name, cfg)
            return make_runner(name, cfg.opencode, cfg.command, echo)

        if args.verify_only:
            return verify_only(cfg, args, echo)
        if args.spec:
            run = create_run(
                cfg,
                args.spec,
                args.task,
                factory(args.runner or cfg.runner),
                max_iterations=args.max_iterations,
                echo=echo,
            )
        else:
            run = open_run(
                cfg,
                args.resume,
                factory,
                max_iterations=args.max_iterations,
                echo=echo,
                reconcile=args.reconcile,
                runner_name=args.runner,
            )
            if args.accept_manual:
                accept_manual(run, [a.upper() for a in args.accept_manual])
            if args.restart_red:
                restart_red(run, args.restart_red.upper())
        return EXIT_CODES[run_loop(run)]
    except ReconcileRequired as exc:
        print(f"ralph: {exc}", file=sys.stderr)
        return EXIT_CODES[Status.BLOCKED]
    except (ConfigError, SpecError, WorkspaceError, StateError, LockError) as exc:
        print(f"ralph: error: {exc}", file=sys.stderr)
        return 2


def _find_run(cfg: Config, spec_path: Path, tasks: list[str]) -> RunStore | None:
    runs = cfg.state_dir / "runs"
    for state_file in sorted(runs.glob("*/state.json"), reverse=True) if runs.exists() else []:
        try:
            state = RunState.from_json(state_file.read_text())
        except (ValueError, TypeError, KeyError):
            continue
        if state.spec_path == str(spec_path.resolve()) and sorted(state.requested_tasks) == sorted(
            tasks
        ):
            return RunStore(cfg.state_dir, state.run_id)
    return None


def verify_only(cfg: Config, args: argparse.Namespace, echo: Callable[[str], None]) -> int:
    """Report the independent completion checks. Never invokes the agent or changes state."""
    with run_lock(cfg.state_dir, "verify-only"), tempfile.TemporaryDirectory() as scratch:
        if args.resume:
            store = RunStore(cfg.state_dir, args.resume)
            state = store.load()
            spec = parse_spec(Path(state.spec_path))
        else:
            spec = parse_spec(args.spec)
            requested = [resolve_task_id(spec, t) for t in args.task or []]
            found = _find_run(cfg, args.spec, requested)
            state = found.load() if found else _blank_state(cfg, spec, args.spec, requested)
        tasks = select_tasks(spec, state.requested_tasks or None)
        check_workspace_location(
            Path(state.workspace), cfg.root, cfg.protected_paths, cfg.state_dir
        )
        run = Run(
            cfg,
            spec,
            tasks,
            state,
            RunStore(Path(scratch), "verify"),
            _NoRunner(),
            _rules(cfg, spec),
            echo,
        )
        stale = selection_hash(tasks, spec) != state.selection_hash
        verdict = final_verdict(run, report_only=True)
        echo(f"[ralph] verify-only: {spec.path} (evidence from run: {state.run_id or 'none'})")
        for line in verdict.report:
            echo(f"  {line}")
        for problem in verdict.problems:
            echo(f"  PROBLEM: {problem}")
        if stale:
            echo("  PROBLEM: the spec changed since that run; its evidence is stale (--reconcile)")
        complete = verdict.ok and not stale
        echo(f"[ralph] {'VERIFIED COMPLETE' if complete else 'NOT COMPLETE'}")
        return 0 if complete else 1


def _blank_state(cfg: Config, spec: Spec, spec_path: Path, requested: list[str]) -> RunState:
    """State with no evidence, so --verify-only can report on a spec that was never run."""
    tasks = select_tasks(spec, requested or None)
    return RunState(
        run_id="",
        spec_path=str(spec_path.resolve()),
        spec_id=spec.id,
        requested_tasks=requested,
        selected_tasks=[t.id for t in tasks],
        selection_hash=selection_hash(tasks, spec),
        workspace=str(cfg.workspace.resolve()),
        runner="none",
        max_iterations=None,
        acceptance=_acceptance_states(tasks),
    )


class _NoRunner:
    name = "none"

    def invoke(
        self, prompt: str, workspace: Path, log_path: Path, timeout_s: float | None
    ) -> InvocationResult:
        raise RuntimeError("--verify-only never invokes the agent")


if __name__ == "__main__":
    raise SystemExit(main())
