from pathlib import Path

import pytest

from ralph.specs import SpecError, parse_spec, parse_spec_text, select_tasks

ROOT = Path(__file__).resolve().parents[1]

DEMO = """\
---
id: demo
title: Demo
package: calc
smoke_command: ["python", "-c", "import calc"]
---

# Demo spec

Some overview.

## TASK-001: Addition

Type: feature
Depends on: none

### Requirements
- Provide add(a, b).

### Acceptance criteria
- AC-001: add(2, 3) returns 5.
- AC-002: add works with negative numbers,
  including add(-1, -1) == -2.

## TASK-002: Subtraction

Depends on: TASK-001

### Acceptance criteria
- [AC-003] subtract(5, 3) returns 2.
- AC-004 (manual): The README reads well.

## TASK-003: Unrelated docs

Type: docs
Depends on: none

### Acceptance criteria
- AC-005: README mentions usage.

## Notes

Not a task.
"""


def demo(text: str = DEMO):
    return parse_spec_text(text, Path("specs/demo.md"))


def test_parses_front_matter_tasks_and_acceptance_criteria():
    spec = demo()

    assert spec.id == "demo"
    assert spec.package == "calc"
    assert spec.smoke_command == ["python", "-c", "import calc"]
    assert [t.id for t in spec.tasks] == ["TASK-001", "TASK-002", "TASK-003"]
    t1, t2, t3 = spec.tasks
    assert t1.type == "feature" and t2.type == "feature" and t3.type == "docs"
    assert t2.depends_on == ("TASK-001",)
    assert [a.id for a in t1.acceptance] == ["AC-001", "AC-002"]
    assert "add(-1, -1) == -2" in t1.acceptance[1].text
    assert t2.acceptance[1].manual is True
    assert t2.acceptance[0].manual is False
    assert "Not a task" not in t3.body


def test_task_selection_includes_dependencies_only():
    spec = demo()

    selected = select_tasks(spec, ["TASK-002"])

    assert [t.id for t in selected] == ["TASK-001", "TASK-002"]


def test_whole_spec_selected_without_task():
    assert [t.id for t in select_tasks(demo(), None)] == ["TASK-001", "TASK-002", "TASK-003"]


def test_unknown_task_rejected_and_case_insensitive_unique_match_accepted():
    spec = demo()
    with pytest.raises(SpecError, match="Unknown task"):
        select_tasks(spec, ["TASK-999"])
    assert [t.id for t in select_tasks(spec, ["task-003"])] == ["TASK-003"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda s: s.replace("AC-003", "AC-001"), "Duplicate acceptance"),
        (lambda s: s.replace("Depends on: TASK-001", "Depends on: TASK-042"), "unknown task"),
        (
            lambda s: s.replace("Type: feature\nDepends on: none", "Depends on: TASK-002"),
            "cycle",
        ),
        (lambda s: s.replace("- AC-005: README mentions usage.", ""), "no acceptance"),
        (lambda s: s.replace("AC-002", "AC-001-2"), "ambiguous"),
        (lambda s: s.replace("- AC-005: README", "- README"), "without an ID"),
        (lambda s: s.replace("Type: docs", "Type: poetry"), "Unknown task type"),
        (lambda s: s.replace("id: demo", "id: [unclosed"), "front matter"),
    ],
)
def test_invalid_specs_are_rejected(mutation, message):
    with pytest.raises(SpecError, match=message):
        demo(mutation(DEMO))


def test_front_matter_is_optional():
    spec = demo(DEMO.split("---\n", 2)[2])
    assert spec.id == "demo"  # falls back to the file stem
    assert spec.package is None


def test_acceptance_test_naming_convention():
    ac = demo().tasks[0].acceptance[0]
    assert ac.test_prefix == "test_ac_001"
    assert ac.matches_test("test_ac_001_adds_numbers")
    assert ac.matches_test("test_ac_001[param]")
    assert not ac.matches_test("test_ac_0011_other")
    assert not ac.matches_test("test_other")


def test_hashes_change_only_for_edited_task():
    before = demo()
    after = demo(DEMO.replace("subtract(5, 3) returns 2", "subtract(5, 3) returns 2 exactly"))

    assert before.tasks[0].content_hash == after.tasks[0].content_hash
    assert before.tasks[1].content_hash != after.tasks[1].content_hash
    assert before.tasks[1].acceptance[0].text_hash != after.tasks[1].acceptance[0].text_hash
    assert before.tasks[1].acceptance[1].text_hash == after.tasks[1].acceptance[1].text_hash


@pytest.mark.parametrize("path", ["examples/specs/task-manager.md", "specs/_template.md"])
def test_shipped_specs_are_valid(path):
    spec = parse_spec(ROOT / path)
    assert spec.tasks
    assert all(t.acceptance for t in spec.tasks)
