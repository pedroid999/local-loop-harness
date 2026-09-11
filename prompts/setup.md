## Phase: SETUP (project skeleton, no behavior)

Prepare the workspace so the runner can test it. Create ONLY scaffolding:
- `pyproject.toml` for a uv project named after the spec, requiring Python >= 3.12, with
  the runtime dependencies the spec needs and a dev dependency group containing pytest,
  ruff and mypy (use `uv add` / `uv add --dev`). Create `pyproject.toml` FIRST, in the
  current directory (or with `uv init --bare --no-workspace`), so `uv` never modifies a
  parent project.
- `[tool.pytest.ini_options]` with `pythonpath = ["."]` (or `["src"]` for a src layout)
  so tests can import the package.
- The package directory `$package` with an empty `__init__.py` (a docstring is fine).
- An empty `tests/` directory (a `tests/__init__.py` is optional).

Do NOT write functions, classes or tests yet: Python files containing `def` or `class`
are reverted by the runner in this phase.

The runner will then check: `uv sync` works, pytest can collect (zero tests is fine now),
and the quality checks pass:
$quality_commands
