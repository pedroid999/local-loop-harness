---
# All front matter keys are optional.
id: my-feature                 # defaults to the file name
title: My feature
package: my_app                # Python package the agent creates/uses (enables clean RED on a missing module)
stack: >                       # extra guidance appended to the default Python/uv/pytest/Ruff/mypy stack
  FastAPI only if an HTTP API is needed; server-rendered Jinja2 for HTML.
acceptance_tests: []           # workspace globs of YOUR tests, e.g. ["tests/acceptance/**"]; never editable by the agent
smoke_command: []              # e.g. ["uv", "run", "python", "-c", "import my_app"]; run once at final verification
---

# My feature

One or two paragraphs describing the goal and any global constraints.

## TASK-001: Short task title

Type: feature                  <!-- feature (default) | refactor | docs -->
Depends on: none               <!-- or a comma-separated list: TASK-001, TASK-002 -->

### Requirements
- Concrete, observable behavior. Name modules, functions or endpoints when you care.

### Acceptance criteria
- AC-001: One observable, testable outcome per line.
- AC-002: Another outcome. Continuation lines must be indented
  like this one.
- AC-003 (manual): Subjective criteria are marked manual; a human accepts them with --accept-manual.

### Verification
- Optional notes on how the behavior should be exercised (unit test, API test, CLI run...).
