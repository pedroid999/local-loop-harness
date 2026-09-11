"""Markdown spec parsing, validation and task selection.

Format (see specs/README.md): optional YAML front matter, then one
`## TASK-ID: Title` section per task with `Type:`, `Depends on:` and a
`### Acceptance criteria` list whose items start with an acceptance ID.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

ID = r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+"
TASK_HEADING = re.compile(rf"^##\s+(?P<id>{ID})\s*[:\-—]\s*(?P<title>.+?)\s*$")
H2 = re.compile(r"^##\s+")
H3 = re.compile(r"^###\s+(?P<title>.+?)\s*$")
TYPE_LINE = re.compile(r"^type:\s*(?P<value>\S+)\s*$", re.IGNORECASE)
DEPENDS_LINE = re.compile(r"^depends on:\s*(?P<value>.*?)\s*$", re.IGNORECASE)
LIST_ITEM = re.compile(r"^\s*[-*]\s+(?P<rest>.*)$")
AC_ITEM = re.compile(
    rf"^\[?(?P<id>{ID})\]?\s*(?:\((?P<flag>manual)\))?\s*[:.\-—]?\s*(?P<text>.*)$",
    re.IGNORECASE,
)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
TASK_TYPES = ("feature", "refactor", "docs")


class SpecError(ValueError):
    """The spec file is invalid or a selection cannot be resolved."""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def slugify(identifier: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", identifier.lower()).strip("_")


@dataclass(frozen=True)
class Acceptance:
    id: str
    text: str
    task_id: str
    manual: bool = False

    @property
    def test_prefix(self) -> str:
        return f"test_{slugify(self.id)}"

    @property
    def text_hash(self) -> str:
        return sha256(f"{self.id}|{self.manual}|{self.text}")

    def matches_test(self, test_name: str) -> bool:
        """True when a pytest function name belongs to this criterion."""
        name = test_name.split("[", 1)[0]
        return name == self.test_prefix or name.startswith(self.test_prefix + "_")


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    type: str
    depends_on: tuple[str, ...]
    acceptance: tuple[Acceptance, ...]
    body: str

    @property
    def content_hash(self) -> str:
        return sha256(self.body)


@dataclass(frozen=True)
class Spec:
    path: Path
    id: str
    title: str
    overview: str
    tasks: tuple[Task, ...]
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def package(self) -> str | None:
        value = self.meta.get("package")
        return str(value) if value else None

    @property
    def stack(self) -> str:
        return str(self.meta.get("stack") or "").strip()

    @property
    def acceptance_tests(self) -> list[str]:
        return [str(g) for g in self.meta.get("acceptance_tests") or []]

    @property
    def smoke_command(self) -> list[str] | None:
        value = self.meta.get("smoke_command")
        return [str(v) for v in value] if value else None

    def task(self, task_id: str) -> Task:
        return next(t for t in self.tasks if t.id == task_id)

    def acceptance(self) -> list[Acceptance]:
        return [ac for task in self.tasks for ac in task.acceptance]


def parse_spec(path: Path) -> Spec:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(f"Cannot read spec {path}: {exc}") from exc
    return parse_spec_text(text, path)


def parse_spec_text(text: str, path: Path) -> Spec:
    meta, body = _split_front_matter(text, path)
    body = HTML_COMMENT.sub("", body)
    lines = body.splitlines()

    title = str(meta.get("title") or "")
    overview: list[str] = []
    sections: list[tuple[str, str, list[str]]] = []
    current: tuple[str, str, list[str]] | None = None
    for line in lines:
        heading = TASK_HEADING.match(line)
        if heading:
            current = (heading["id"], heading["title"], [])
            sections.append(current)
        elif H2.match(line):
            current = None  # a non-task level-2 heading ends the task section
        elif current is not None:
            current[2].append(line)
        elif line.startswith("# ") and not title:
            title = line[2:].strip()
        elif not line.startswith("# "):
            overview.append(line)

    tasks = tuple(_parse_task(tid, ttitle, tlines, path) for tid, ttitle, tlines in sections)
    spec = Spec(
        path=path,
        id=str(meta.get("id") or path.stem),
        title=title or path.stem,
        overview="\n".join(overview).strip(),
        tasks=tasks,
        meta=meta,
    )
    _validate(spec)
    return spec


def _split_front_matter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        raise SpecError(f"{path}: unterminated YAML front matter")
    try:
        meta = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as exc:
        raise SpecError(f"{path}: invalid YAML front matter: {exc}") from exc
    if not isinstance(meta, dict):
        raise SpecError(f"{path}: YAML front matter must be a mapping")
    return meta, text[end + 5 :]


def _parse_task(task_id: str, title: str, lines: list[str], path: Path) -> Task:
    task_type = "feature"
    depends: tuple[str, ...] = ()
    acceptance: list[list[Any]] = []  # [id, manual, text_parts]
    subsection = ""
    for line in lines:
        h3 = H3.match(line)
        if h3:
            subsection = h3["title"].lower()
            continue
        if not subsection and (m := TYPE_LINE.match(line.strip())):
            task_type = m["value"].lower()
            if task_type not in TASK_TYPES:
                raise SpecError(f"{path}: {task_id}: Unknown task type {m['value']!r}")
        elif not subsection and (m := DEPENDS_LINE.match(line.strip())):
            value = m["value"].strip()
            if value.lower() not in ("", "none", "-"):
                depends = tuple(d.strip() for d in value.split(",") if d.strip())
        elif subsection.startswith("acceptance"):
            if item := LIST_ITEM.match(line):
                ac = AC_ITEM.match(item["rest"].strip())
                if not ac:
                    raise SpecError(
                        f"{path}: {task_id}: acceptance criterion without an ID: {line.strip()!r}"
                    )
                acceptance.append([ac["id"].upper(), bool(ac["flag"]), [ac["text"].strip()]])
            elif line.startswith((" ", "\t")) and line.strip() and acceptance:
                acceptance[-1][2].append(line.strip())

    body = "\n".join(line.rstrip() for line in lines).strip()
    return Task(
        id=task_id,
        title=title,
        type=task_type,
        depends_on=depends,
        acceptance=tuple(
            Acceptance(id=aid, text=" ".join(parts), task_id=task_id, manual=manual)
            for aid, manual, parts in acceptance
        ),
        body=f"## {task_id}: {title}\n{body}",
    )


def _validate(spec: Spec) -> None:
    task_ids = [t.id for t in spec.tasks]
    if not task_ids:
        raise SpecError(f"{spec.path}: no tasks found (expected '## TASK-001: Title' headings)")
    if dup := _duplicates(task_ids):
        raise SpecError(f"{spec.path}: Duplicate task IDs: {', '.join(dup)}")
    ac_ids = [ac.id for ac in spec.acceptance()]
    if dup := _duplicates(ac_ids):
        raise SpecError(f"{spec.path}: Duplicate acceptance IDs: {', '.join(dup)}")
    for task in spec.tasks:
        if not task.acceptance:
            raise SpecError(f"{spec.path}: {task.id} has no acceptance criteria")
        for dep in task.depends_on:
            if dep not in task_ids:
                raise SpecError(f"{spec.path}: {task.id} depends on unknown task {dep}")
    slugs = sorted(slugify(a) for a in ac_ids)
    for a, b in pairwise(slugs):
        if b.startswith(a + "_"):
            raise SpecError(
                f"{spec.path}: acceptance IDs are ambiguous as test prefixes: "
                f"test_{a}_* would also match test_{b}_*"
            )
    _topological(spec, task_ids)  # raises on cycles


def _duplicates(items: Sequence[str]) -> list[str]:
    return sorted({i for i in items if items.count(i) > 1})


def _topological(spec: Spec, roots: Sequence[str]) -> list[Task]:
    """Tasks reachable from roots, dependencies first, stable by spec order."""
    order: list[Task] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(task_id: str, trail: tuple[str, ...]) -> None:
        if task_id in done:
            return
        if task_id in visiting:
            raise SpecError(f"{spec.path}: dependency cycle: {' -> '.join((*trail, task_id))}")
        visiting.add(task_id)
        for dep in spec.task(task_id).depends_on:
            visit(dep, (*trail, task_id))
        visiting.discard(task_id)
        done.add(task_id)
        order.append(spec.task(task_id))

    position = {t.id: i for i, t in enumerate(spec.tasks)}
    for root in sorted(roots, key=lambda r: position[r]):
        visit(root, ())
    return order  # depth-first post-order: dependencies first, otherwise spec order


def resolve_task_id(spec: Spec, requested: str) -> str:
    ids = [t.id for t in spec.tasks]
    if requested in ids:
        return requested
    matches = [i for i in ids if i.lower() == requested.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SpecError(f"Ambiguous task ID {requested!r}: {', '.join(matches)}")
    raise SpecError(f"Unknown task {requested!r}. Known tasks: {', '.join(ids)}")


def select_tasks(spec: Spec, requested: Sequence[str] | None) -> list[Task]:
    """Selected tasks plus their transitive dependencies, dependencies first."""
    if not requested:
        return _topological(spec, [t.id for t in spec.tasks])
    return _topological(spec, [resolve_task_id(spec, r) for r in requested])


def selection_hash(tasks: Sequence[Task], spec: Spec) -> str:
    relevant = {k: spec.meta.get(k) for k in ("package", "acceptance_tests", "smoke_command")}
    return sha256(repr(relevant) + "".join(t.content_hash for t in tasks))
