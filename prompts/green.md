## Phase: GREEN (make the frozen test pass)

Target: $ac_id ($task_id: $task_title)
Criterion: $ac_text

These tests are FROZEN. Make them pass without changing them:
$frozen_tests

- Implement the smallest complete production change that satisfies the criterion.
- Do NOT change any file under `$test_dirs` (or any `conftest.py`): only formatting-only
  edits are kept; any other test change is reverted by the runner.
- Keep previously passing tests green.
$docs_rule
- Run `uv run python -m pytest -q` until the frozen tests pass. Then stop.
