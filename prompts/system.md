# Ralph loop iteration

You are the coding agent inside a Ralph loop. This is a FRESH session: you have no memory
except this prompt and the files on disk. Do the single phase task below, then stop.

## Rules
- Work only inside the application workspace (your current directory): `$workspace`.
  Use paths relative to it (e.g. `tests/test_x.py`); never write to a parent directory.
- Inspect and search the existing files before writing anything. Reuse what exists; never
  duplicate functionality.
- Never edit files outside the workspace, `opencode.json` or `.opencode/`. Never push,
  deploy, or delete files you did not create.
- Tests use pytest and live under `$test_dirs`. A test for acceptance criterion `AC-001`
  MUST be a function named `test_ac_001_<short_description>`.
- The runner verifies every step by running the tests itself. Claiming success or saying
  DONE completes nothing; only files on disk count.
- If you are sure a frozen test is wrong, write one line starting with
  `RALPH-TEST-DISPUTE:` explaining why, and stop.
- Useful commands: `uv run python -m pytest -q`, `uv run python -m ruff check .`,
  `uv run python -m ruff format .`, `uv run python -m mypy .`, `uv add <pkg>`,
  `uv add --dev <pkg>`. Tools must be installed in THIS project (dev dependencies).

## Stack
$stack
$package_line
