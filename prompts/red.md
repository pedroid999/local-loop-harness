## Phase: RED (write a failing test first)

Target: $ac_id ($task_id: $task_title)
Criterion: $ac_text

- Write the smallest meaningful pytest test(s) for THIS criterion only, named
  `${test_prefix}_<short_description>`, in a file under `$test_dirs`.
- Assert the observable behavior the criterion describes. No `assert True`,
  `assert False`, `pytest.fail`, skips or xfails.
- Do NOT create or modify production code and do not modify existing tests. Only test
  files (and `pyproject.toml`/`uv.lock` to add a missing test dependency) may change.
  The runner reverts any production change made in this phase.
- The test must fail now because the behavior is missing or wrong — not because of a
  syntax error, a missing third-party package or a broken fixture.
- Import production code from the package as it should exist once implemented.
- Run `uv run python -m ruff format tests` and `uv run python -m ruff check --fix tests`
  so the test is clean: after RED the test is frozen and only formatting may change.
- You may run `uv run python -m pytest -q` to see it fail. Then stop.
