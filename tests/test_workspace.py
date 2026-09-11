from pathlib import Path

import pytest

from ralph.workspace import (
    PathRules,
    WorkspaceError,
    check_workspace_location,
    copy_files,
    diff,
    restore,
    snapshot,
)


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_snapshot_ignores_caches_and_diff_classifies_changes(tmp_path):
    write(tmp_path, "pkg/a.py", "a = 1\n")
    write(tmp_path, "pkg/b.py", "b = 1\n")
    write(tmp_path, ".venv/lib/x.py", "ignored\n")
    write(tmp_path, "pkg/__pycache__/a.pyc", "ignored\n")
    before = snapshot(tmp_path)

    write(tmp_path, "pkg/a.py", "a = 2\n")
    (tmp_path / "pkg/b.py").unlink()
    write(tmp_path, "pkg/c.py", "c = 1\n")
    changes = diff(before, snapshot(tmp_path))

    assert set(before) == {"pkg/a.py", "pkg/b.py"}
    assert changes.modified == ["pkg/a.py"]
    assert changes.deleted == ["pkg/b.py"]
    assert changes.added == ["pkg/c.py"]
    assert changes.paths == ["pkg/a.py", "pkg/b.py", "pkg/c.py"]


def test_restore_reverts_modified_and_deleted_and_removes_added(tmp_path):
    ws, ref = tmp_path / "ws", tmp_path / "ref"
    write(ws, "a.py", "original\n")
    write(ws, "b.py", "keep me\n")
    ref_snapshot = snapshot(ws)
    copy_files(ws, ref, ref_snapshot)

    write(ws, "a.py", "changed\n")
    (ws / "b.py").unlink()
    write(ws, "new/c.py", "added\n")
    restore(ws, ref, ref_snapshot, ["a.py", "b.py", "new/c.py"])

    assert (ws / "a.py").read_text() == "original\n"
    assert (ws / "b.py").read_text() == "keep me\n"
    assert not (ws / "new/c.py").exists()
    assert snapshot(ws) == ref_snapshot


def test_path_rules_classify_tests_docs_and_protected_paths():
    rules = PathRules(
        test_dirs=["tests"],
        docs_globs=["*.md", "docs/**"],
        protected=["opencode.json", ".opencode", "tests/acceptance/**"],
    )
    assert rules.is_test("tests/test_x.py")
    assert rules.is_test("conftest.py")
    assert not rules.is_test("pkg/tests.py")
    assert rules.is_docs("README.md") and rules.is_docs("docs/guide/x.txt")
    assert not rules.is_docs("pkg/a.py")
    assert rules.is_protected("opencode.json")
    assert rules.is_protected(".opencode/agents/x.md")
    assert rules.is_protected("tests/acceptance/test_a.py")
    assert not rules.is_protected("tests/test_a.py")


@pytest.mark.parametrize(
    ("workspace", "ok"),
    [
        ("app", True),
        ("../elsewhere", True),
        (".", False),
        ("..", False),
        ("src/ralph/app", False),
        ("specs", False),
        (".ralph/app", False),
    ],
)
def test_workspace_must_not_overlap_the_harness(tmp_path, workspace, ok):
    harness = tmp_path / "harness"
    harness.mkdir()
    target = (harness / workspace).resolve()
    protected = ["src", "specs", "tests", "prompts", "ralph.py"]

    if ok:
        check_workspace_location(target, harness, protected, harness / ".ralph")
    else:
        with pytest.raises(WorkspaceError):
            check_workspace_location(target, harness, protected, harness / ".ralph")
