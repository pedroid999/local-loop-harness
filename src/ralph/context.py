"""Fresh, bounded prompt for one iteration: rules, phase, selected work, recent evidence.

Only the selected tasks, the current phase, runner-verified progress and the latest
runner feedback are included. Agent conversation output is never replayed.
"""

from __future__ import annotations

from string import Template

from ralph.tdd import Run, Step
from ralph.workspace import iter_files

DEFAULT_STACK = (
    "Python 3.12+ managed with uv; pytest for tests; Ruff for lint and formatting; mypy "
    "for types. Use FastAPI only when the spec needs an HTTP backend; for HTML prefer "
    "server-rendered Jinja2 templates (HTMX optional). Keep the solution as simple as "
    "the spec allows."
)
MARKS = {"done": "[x]", "pending": "[ ]"}


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n[... truncated ...]"


def render_prompt(run: Run, step: Step) -> str:
    cfg, spec = run.cfg, run.spec
    ac, task = step.ac, step.task
    status = run.status_of(ac) if ac else None
    values = {
        "workspace": str(run.workspace),
        "test_dirs": ", ".join(f"{d}/" for d in cfg.verify.test_dirs),
        "stack": DEFAULT_STACK + (f"\nSpec guidance: {spec.stack}" if spec.stack else ""),
        "package_line": f"Application package: `{spec.package}`." if spec.package else "",
        "package": spec.package or "<the application package>",
        "quality_commands": "\n".join(f"  - `{' '.join(c)}`" for c in cfg.verify.quality_commands)
        or "  - (none configured)",
        "ac_id": ac.id if ac else "",
        "ac_text": ac.text if ac else "",
        "task_id": task.id if task else "",
        "task_title": task.title if task else "",
        "test_prefix": ac.test_prefix if ac else "test",
        "frozen_tests": "\n".join(f"  - `{n}`" for n in (status.tests if status else {}))
        or "  - (none)",
        "docs_rule": "- This is a documentation task: only documentation files may change."
        if task and task.type == "docs"
        else "",
    }
    parts = [_template(run, "system.md", values), _template(run, f"{step.phase}.md", values)]
    if step.phase == "red" and status and status.stale_tests and ac:
        parts.append(
            f"Note: existing `{ac.test_prefix}_*` tests may be outdated (the criterion changed "
            "or RED was restarted). Update them so they express the criterion exactly."
        )
    if step.phase == "refactor" and task and task.type == "refactor":
        parts.append("This task IS a refactoring: perform the restructuring the task describes.")
    parts += [_selected_work(run, step), _progress(run)]
    if run.state.feedback:
        parts.append(
            "## Latest runner feedback\n" + clip(run.state.feedback, cfg.context.max_feedback_chars)
        )
    parts.append(_listing(run))
    prompt = "\n\n".join(p.strip() for p in parts if p.strip()) + "\n"
    return clip(prompt, cfg.context.max_prompt_chars)


def _template(run: Run, name: str, values: dict[str, str]) -> str:
    text = (run.cfg.prompts_dir / name).read_text(encoding="utf-8")
    return Template(text).safe_substitute(values)


def _selected_work(run: Run, step: Step) -> str:
    lines = [f"## Selected work: {run.spec.title}"]
    if run.spec.overview:
        lines.append(clip(run.spec.overview, 1500))
    for task in run.tasks:
        deps = ", ".join(task.depends_on) or "none"
        lines.append(f"\n### {task.id}: {task.title} (type: {task.type}; depends on: {deps})")
        if step.task and task.id == step.task.id:
            body = task.body.split("\n", 1)[1] if "\n" in task.body else ""
            lines.append(clip(body, 4000))
            continue
        for ac in task.acceptance:
            state = run.status_of(ac).status
            lines.append(f"- {MARKS.get(state, '[~]')} {ac.id}: {clip(ac.text, 300)} ({state})")
    if step.task:
        lines.append("\nCriteria status in the current task:")
        for ac in step.task.acceptance:
            state = run.status_of(ac).status
            here = " <- current" if step.ac and ac.id == step.ac.id else ""
            lines.append(f"- {MARKS.get(state, '[~]')} {ac.id} ({state}){here}")
    return "\n".join(lines)


def _progress(run: Run) -> str:
    history = run.state.history[-run.cfg.context.max_history_entries :]
    if not history:
        return ""
    lines = ["## Recent progress (runner-verified)"]
    for entry in history:
        mark = "ok" if entry.get("ok") else "rejected"
        target = f" {entry['ac']}" if entry.get("ac") else ""
        lines.append(
            f"- iteration {entry['iteration']} {entry['phase'].upper()}{target} "
            f"[{mark}]: {clip(entry['summary'], 300)}"
        )
    return "\n".join(lines)


def _listing(run: Run) -> str:
    limit = run.cfg.context.max_listing_entries
    files = list(iter_files(run.workspace))
    shown = "\n".join(f"- {f}" for f in files[:limit]) or "- (empty)"
    more = f"\n- ... ({len(files) - limit} more)" if len(files) > limit else ""
    return f"## Workspace files\n{shown}{more}"
