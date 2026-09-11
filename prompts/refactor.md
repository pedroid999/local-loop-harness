## Phase: REFACTOR (improve structure, keep behavior)

Target: $ac_id ($task_id: $task_title)
Criterion: $ac_text

Tests for this criterion (frozen):
$frozen_tests

- If the code for this criterion would clearly benefit (duplication, naming, types,
  structure), improve it without changing behavior. Otherwise change nothing and reply
  with one line explaining why no refactor is needed. Do not make cosmetic churn.
- Do not add behavior: new behavior needs its own test-first cycle.
- Tests are frozen: you may only REFORMAT files under `$test_dirs` (e.g.
  `uv run python -m ruff format tests`, `uv run python -m ruff check --fix tests`); any change
  to what a test does is reverted by the runner.
- The runner requires all tests AND these quality checks to pass:
$quality_commands
