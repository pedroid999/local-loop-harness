# Specs

Put one Markdown file per feature in this directory and run it with
`uv run ralph.py --spec specs/<file>.md`. Copy [`_template.md`](_template.md) to start,
or copy the complete example: `cp examples/specs/task-manager.md specs/`.

You write the spec. The agent derives the tests from it using strict TDD, and the runner
verifies every step. You do not need to write tests first (but you may, see below).

## Format

```text
---                     optional YAML front matter
id / title / package / stack / acceptance_tests / smoke_command
---
# Title
Overview prose.

## TASK-001: Title       one level-2 heading per task; IDs are stable, uppercase, with a hyphen
Type: feature            feature | refactor | docs   (optional, default feature)
Depends on: TASK-000     comma-separated task IDs or "none"

### Requirements         free text, shown to the agent
### Acceptance criteria  REQUIRED: one list item per criterion
- AC-001: text           ID first; "[AC-001] text" also works
- AC-002 (manual): text  cannot be automated; needs --accept-manual AC-002
### Verification         free text, shown to the agent
```

Rules enforced by the parser:

- Task and acceptance IDs are unique within the spec; dependencies must exist and be acyclic.
- Every task has at least one acceptance criterion; every list item in
  `### Acceptance criteria` starts with an ID.
- Level-2 headings that are not `## ID: Title` (e.g. `## Notes`) end the previous task.

## Selecting work

- `--spec specs/x.md` selects every task in the spec.
- `--task TASK-003` selects TASK-003 plus its transitive dependencies, nothing else.
  Unknown IDs are rejected; matching is case-insensitive only when unambiguous.

## How acceptance criteria map to tests

Each criterion is one TDD unit. The runner maps tests to criteria by **test function
name**: `AC-001` maps to functions named `test_ac_001` or `test_ac_001_<anything>`
(lowercase, non-alphanumerics become `_`). Tests live under `tests/` in the workspace.

A criterion is complete only when the runner itself has recorded RED (a genuine failure
against unchanged production code), GREEN (the same frozen test passing), a passing
REFACTOR check, and the whole selected suite plus quality checks pass at the end.

- `Type: refactor` tasks use characterization tests: they must pass *before* and after.
- `Type: docs` tasks may only change documentation files (`*.md`, `docs/**`) in GREEN.
- `(manual)` criteria are never auto-completed; the run ends `BLOCKED` until you accept them.

## Your own acceptance tests (optional)

List them in `acceptance_tests` (workspace-relative globs). The agent can never modify
them, and if a test for the current criterion already exists the runner uses it for RED
without asking the agent to write one.

## Editing a spec during a run

The spec is protected while a run is active. If you change it, resume with
`--resume <RUN_ID> --reconcile`: criteria whose text changed lose their evidence and are
redone; unchanged criteria keep theirs.
