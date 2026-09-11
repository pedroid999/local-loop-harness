## Phase: REPAIR (final verification failed)

Every selected criterion has passed its own TDD cycle, but the runner's final verification
of the whole selection failed. The failures are in "Latest runner feedback" below.

- Fix the production code (or project configuration) so the complete suite, the quality
  checks and the smoke check pass.
- Tests are frozen: only formatting-only changes under `$test_dirs` are kept; any other
  test change is reverted.
- Do not add new behavior. Then stop.

Quality checks:
$quality_commands
