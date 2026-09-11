# Ralph harness

A small, classic **Ralph loop** for a coding agent, with **strict TDD enforced by the
runner** and a **pluggable coding CLI**. You write Markdown specs; the harness repeatedly
starts a fresh agent session (OpenCode with a local Qwen model by default) to implement
them, verifies every step itself, and stops successfully only when the whole selection
is independently verified.

Claude built this harness; it is not a runtime dependency or a fallback. The agent that
does the work is whatever CLI you configure.

## Quick start

```bash
uv sync                                         # harness dependencies (Python 3.12+)
cp examples/specs/task-manager.md specs/        # or write your own from specs/_template.md

uv run ralph.py --spec specs/task-manager.md                          # loop until verified
uv run ralph.py --spec specs/task-manager.md --max-iterations 20      # at most 20 invocations
uv run ralph.py --spec specs/task-manager.md --task TASK-003 --max-iterations 10
uv run ralph.py --spec specs/task-manager.md --runner opencode
uv run ralph.py --resume RUN_ID                                       # or --resume latest
uv run ralph.py --spec specs/task-manager.md --verify-only            # checks only, no agent
```

The application is built in `app/` (change with `--workspace` or `[harness] workspace`).
The workspace may not be the harness root, contain it, or sit inside `src/`, `specs/`,
`tests/`, `prompts/`, `.ralph/` or `.git/`.

## The loop

```python
iteration = 0  # restored from state on --resume
while max_iterations is None or iteration < max_iterations:
    if independently_verified_complete(selected_work):
        return SUCCESS
    iteration += 1
    context = build_fresh_context(selected_work, persisted_progress)
    result = run_one_iteration(context)  # ONE fresh agent invocation
    verification = independently_verify(result)  # runner guards + runner-run tests
    persist_progress_and_feedback(verification)
    if independently_verified_complete(selected_work):
        return SUCCESS
return MAX_ITERATIONS_REACHED
```

That is `run_loop` in [`src/ralph/loop.py`](src/ralph/loop.py). Every agent invocation —
setup, RED, GREEN, refactor or repair — consumes exactly one iteration. There is no hidden
iteration cap and no total time limit. Without `--max-iterations` the loop ends only on
verified success, Ctrl-C, or an unrecoverable operational error (the CLI cannot start, or
`max_consecutive_runner_failures` invocations fail in a row).

One sequential agent, one criterion per iteration, a fresh session every time, and
file-based memory: the prompt contains the rules, the current phase, the selected tasks
only, runner-verified progress and the latest runner feedback — never the agent's
previous conversation or full logs.

## Strict TDD, controlled by the runner

The TDD unit is one **acceptance criterion** (`AC-001`). Tests map to criteria by name:
`AC-001` ↔ `test_ac_001_<anything>` under `tests/`.

| Phase | The agent is asked to | The runner then |
|---|---|---|
| SETUP (once) | create a uv project skeleton, no behavior | reverts any Python file with `def`/`class`; checks `pyproject.toml`, setup commands, pytest collection and quality checks |
| RED | write the smallest failing test | reverts production changes against the criterion's preserved baseline; rejects missing, trivial (`assert True/False`, no assertion), skipped, `pytest.fail`, syntax/`NameError` and missing-third-party failures; runs the test on unchanged code; freezes the test (per-function source hash) |
| GREEN | implement the minimum to pass | reverts any test change; requires the frozen tests to pass **unchanged** and every previously passing test to stay green |
| REFACTOR | improve structure or explain why not | reverts test changes; requires all tests and quality checks (Ruff, format, mypy) to pass; records whether production changed |
| REPAIR | fix a failing final verification | tests stay frozen; re-runs the final verification |

Other rules:

- A new test that already passes is **not** broken on purpose: the criterion is recorded
  as `pre-existing` and flagged in the evidence.
- If tests for the next criterion already exist (e.g. you wrote them), the runner checks
  RED itself without an agent call.
- `Type: refactor` tasks use characterization tests that must pass before and after;
  `Type: docs` tasks may only change documentation in GREEN.
- Future criteria's tests may be failing during intermediate slices; the full selected
  suite must be green at the end.
- If the agent believes a frozen test is wrong it can print `RALPH-TEST-DISPUTE: ...`; you
  decide. `--resume RUN_ID --restart-red AC-003` restores production to that criterion's
  baseline and invalidates its and all later criteria's evidence.

Frozen means *semantically* frozen: a test's identity is the hash of its function AST, so
formatting-only edits (whitespace, wrapping, comments) are kept — lint and format checks
cover tests too — while any change to what a test does is reverted.

Evidence per criterion (in `state.json` and `plan.md`): frozen test IDs and hashes,
pre-change production hash, RED/GREEN/REFACTOR results with iteration and time. The
agent's claims are never evidence.

### When is the work complete?

`SUCCESS` requires, checked by the runner on the current files:

- setup verified and every selected automated criterion through its TDD cycle;
- every criterion mapped to at least one collected test, all passing, frozen hashes intact;
- zero collected tests is never success; skipped required tests do not count;
- every previously passing test still passing; quality checks and the smoke command
  (spec `smoke_command`) passing;
- `(manual)` criteria accepted by you with `--resume RUN_ID --accept-manual AC-ID`.

A completion token, an exit code, a checked plan or the model saying DONE changes nothing.
Agent-written tests prove behavior only as well as they express the criterion: read the
tests of criteria you care about, and mark subjective criteria as `(manual)`.

## Exit codes

| Status | Code | Meaning |
|---|---|---|
| SUCCESS | 0 | independently verified complete |
| MAX_ITERATIONS_REACHED | 10 | budget used up; the work is not complete |
| BLOCKED | 11 | needs you: protected file changed, spec changed (`--reconcile`), manual criteria, or opt-in stall stop |
| RUNNER_ERROR | 12 | the coding CLI cannot run or keeps failing |
| CANCELLED | 130 | Ctrl-C; state saved, resume later |
| — | 2 | usage or configuration error; `--verify-only` returns 1 when not complete |

## State, resume and spec changes

Everything lives in `.ralph/runs/<RUN_ID>/` (git-ignored): `state.json` (atomic writes),
`plan.md`, `progress.md`, `feedback.md`, baselines, frozen-test copies and one folder per
iteration with `prompt.md`, `agent.log` (raw CLI output) and `verify.md`. A lock file
allows one active run per state directory.

`--resume` keeps the consumed iteration count and the TDD phase. `--max-iterations N` on
resume is a **total** for the run (already consumed iterations count); without it the
resumed run is unlimited. If the selected spec changed, resume stops with `BLOCKED` until
you pass `--reconcile`: changed criteria lose their evidence and are redone, unchanged ones
keep theirs. Editing the spec *during* a run stops it (the spec is a protected file).

## OpenCode runner

Tested with **OpenCode 1.18.30**, agent `local-qwen`, model `llama.cpp/qwen3.5-9b-local`
(llama-server context 131072; the harness keeps prompts far smaller — capacity is not a
target). Each iteration runs:

```bash
opencode run --agent local-qwen --model llama.cpp/qwen3.5-9b-local --format json \
  --dir <workspace> "<prompt>"
```

with `cwd` = workspace **and `PWD` = workspace** (OpenCode derives its project directory
from `PWD`, not the process cwd — without it, the real-model smoke test showed the agent
treating the harness as its project), `shell=False`, stdin closed, and a new session every time
(`--continue`, `--session`, `--fork` and `--auto` are refused in `extra_args`). JSON events
(`step_start`, `text`, `tool_use`, `step_finish`, `error`) are parsed defensively and
streamed to the console; raw output goes to `agent.log`. CLI failures (cannot start,
non-zero exit, `error` events, timeouts), tool failures, permission denials, model text
and verifier results are reported separately.

**Permissions.** Your `local-qwen` agent uses `"ask"` for edit and bash, and
`opencode run` **auto-rejects** every `"ask"` request (it logs
`permission requested: ...; auto-rejecting` and still exits 0). So `ralph.toml` ships a
scoped override in `[runner.opencode.permission]` — edits allowed, `external_directory`
denied, bash denied except `uv`/`ls`/`cat`/`grep`/`find`/`mkdir`/`python -m pytest`. It is
passed only to the child process through `OPENCODE_CONFIG_CONTENT`; your global OpenCode
configuration is never modified. Delete the section to use the agent's own permissions.
Note that allowing `uv run *` means the agent can run arbitrary Python.

If denials happen anyway, the runner prints them with a hint and feeds them back.
`extra_args = ["--pure"]` skips global OpenCode plugins if they get in the way of a small
local model.

## Adding another coding CLI

Option 1 — no code: the generic `command` runner.

```toml
[runner]
default = "command"

[runner.command]
argv = ["my-agent", "--non-interactive", "--prompt-file", "{prompt_file}"]
prompt_via = "file"        # or "stdin" (default)
```

Contract: argv is a list (no shell); `{prompt_file}` and `{workspace}` are substituted;
the process runs in the workspace with `RALPH_PROMPT_FILE` and `RALPH_WORKSPACE` set and
must start a fresh, non-interactive session; exit code 0 means the invocation worked (not
that the work is done); stdout is logged. `tests/fixtures/fake_agent.py` is a complete
example.

Option 2 — an adapter class in [`src/ralph/runners.py`](src/ralph/runners.py) that
implements `invoke(prompt, workspace, log_path, timeout_s) -> InvocationResult`, registered
in `make_runner`. Specs, state, TDD and verification do not change.

## Safety model (read this)

The agent runs with **your user's privileges**. The harness uses argv lists, an explicit
workspace `cwd`, scoped CLI permissions, per-invocation and per-test timeouts with
process-group termination, and after every invocation it:

- hash-checks every harness file outside the workspace and `.ralph/` (plus the spec) and
  stops `BLOCKED` if anything changed or appeared;
- reverts protected workspace files (`opencode.json`, `.opencode/`, your acceptance tests);
- reverts production changes in RED and test changes in GREEN/REFACTOR/REPAIR;
- runs pytest with `-o addopts=` so configuration cannot deselect tests;
- runs tests and quality checks as `uv run python -m <tool>` with the harness venv removed
  from `PATH`, so they come from the application's own environment — never from the
  harness or a global install (a real-model run once passed SETUP that way without the
  app declaring pytest, ruff or mypy).

These checks enforce the accepted TDD transitions. They are **not a sandbox**: a prompt or
a post-execution hash check cannot prevent arbitrary shell side effects outside the
tracked files (network access, other directories, installed packages). For stronger
isolation put the workspace outside the harness in its own git repository, and consider a
container or VM (a possible later option; not required in v1). Nothing is ever pushed or
deployed by the harness.

## Developing the harness

```bash
uv run pytest            # includes a fake-runner end-to-end RED -> GREEN -> SUCCESS run
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

Layout: `ralph.py` (entry point), `src/ralph/` (`cli`, `config`, `specs`, `loop`, `tdd`,
`verify`, `workspace`, `runners`, `context`, `state`, `proc`), `prompts/` (runtime prompt
templates), `specs/` (your specs, format guide and template), `examples/specs/`, `app/`
(generated application), `tests/` (harness tests), `.ralph/` (run state).
