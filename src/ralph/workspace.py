"""File snapshots, diffs, restores and path rules for the application workspace.

These checks enforce accepted TDD transitions after each agent invocation. They are
NOT an OS sandbox: the agent runs with your user's privileges and can have arbitrary
side effects; the runner only detects and reverts changes to the files it tracks.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

DEFAULT_IGNORE = frozenset(
    {
        ".venv",
        "venv",
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "node_modules",
        ".ralph",
        ".idea",
        ".vscode",
        "build",
        "dist",
        ".DS_Store",
        ".tox",
        ".nox",
        "htmlcov",
        ".coverage",
    }
)

Snapshot = dict[str, str]  # posix relative path -> sha256


class WorkspaceError(ValueError):
    """The workspace location is unsafe."""


def iter_files(root: Path, ignore: frozenset[str] = DEFAULT_IGNORE) -> Iterator[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ignore and not d.endswith(".egg-info"))
        base = Path(dirpath)
        for name in sorted(filenames):
            if name in ignore or name.endswith((".pyc", ".pyo")):
                continue
            yield (base / name).relative_to(root).as_posix()


def file_hash(path: Path) -> str:
    if path.is_symlink():
        return hashlib.sha256(f"symlink:{os.readlink(path)}".encode()).hexdigest()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(
    root: Path,
    only: Callable[[str], bool] | None = None,
    ignore: frozenset[str] = DEFAULT_IGNORE,
) -> Snapshot:
    if not root.exists():
        return {}
    return {
        rel: file_hash(root / rel) for rel in iter_files(root, ignore) if only is None or only(rel)
    }


def tree_hash(snap: Snapshot) -> str:
    digest = hashlib.sha256()
    for rel in sorted(snap):
        digest.update(f"{rel}\0{snap[rel]}\n".encode())
    return digest.hexdigest()


@dataclass(frozen=True)
class Changes:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return sorted({*self.added, *self.modified, *self.deleted})

    def __bool__(self) -> bool:
        return bool(self.added or self.modified or self.deleted)

    def describe(self) -> str:
        parts = [f"+{p}" for p in self.added] + [f"~{p}" for p in self.modified]
        parts += [f"-{p}" for p in self.deleted]
        return ", ".join(parts) or "no file changes"

    def only(self, keep: Callable[[str], bool]) -> Changes:
        return Changes(
            [p for p in self.added if keep(p)],
            [p for p in self.modified if keep(p)],
            [p for p in self.deleted if keep(p)],
        )


def diff(before: Snapshot, after: Snapshot) -> Changes:
    return Changes(
        added=sorted(set(after) - set(before)),
        modified=sorted(p for p in set(before) & set(after) if before[p] != after[p]),
        deleted=sorted(set(before) - set(after)),
    )


def copy_files(root: Path, dest: Path, rels: Iterable[str]) -> None:
    for rel in rels:
        source, target = root / rel, dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


def fresh_copy(root: Path, dest: Path, snap: Snapshot) -> None:
    """Replace dest with a copy of the files listed in snap."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    copy_files(root, dest, snap)


def restore(root: Path, ref_dir: Path, ref_snapshot: Snapshot, rels: Iterable[str]) -> None:
    """Make each rel match the reference copy: restore it, or delete it if it was absent."""
    for rel in rels:
        target = root / rel
        if rel in ref_snapshot:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.exists():
                target.unlink()
            shutil.copy2(ref_dir / rel, target, follow_symlinks=False)
        elif target.is_symlink() or target.exists():
            target.unlink()


def _matches(rel: str, pattern: str) -> bool:
    pattern = pattern.rstrip("/")
    return rel == pattern or rel.startswith(pattern + "/") or fnmatch(rel, pattern)


@dataclass(frozen=True)
class PathRules:
    """Classifies workspace paths as tests, docs, protected or RED-allowed files."""

    test_dirs: Sequence[str] = ("tests",)
    docs_globs: Sequence[str] = ("*.md", "docs/**")
    protected: Sequence[str] = ()
    red_allowed: Sequence[str] = ()

    def is_test(self, rel: str) -> bool:
        return PurePosixPath(rel).name == "conftest.py" or any(
            _matches(rel, d) for d in self.test_dirs
        )

    def is_docs(self, rel: str) -> bool:
        return any(fnmatch(rel, g) for g in self.docs_globs)

    def is_protected(self, rel: str) -> bool:
        return any(_matches(rel, p) for p in self.protected)

    def is_red_allowed(self, rel: str) -> bool:
        return rel in self.red_allowed

    def is_production(self, rel: str) -> bool:
        return not self.is_test(rel)


def check_workspace_location(
    workspace: Path, harness_root: Path, protected_paths: Sequence[str], state_dir: Path
) -> None:
    ws = workspace.resolve()
    harness = harness_root.resolve()
    if ws == harness or harness.is_relative_to(ws):
        raise WorkspaceError(
            f"Workspace {ws} is the harness root or contains it; use a separate directory "
            "such as ./app"
        )
    for name in [*protected_paths, ".git", str(state_dir)]:
        guarded = (harness / name).resolve()
        if ws == guarded or ws.is_relative_to(guarded):
            raise WorkspaceError(f"Workspace {ws} overlaps protected harness path {guarded}")
