---
id: greeting-smoke
package: greeting
---
# Greeting (smoke test)

A one-function package used to smoke-test the harness with the real local model.

## TASK-001: Greeting function

Type: feature
Depends on: none

### Requirements
- `greeting.greet(name: str) -> str` lives in the `greeting` package.

### Acceptance criteria
- AC-001: `greet("Ada")` returns `"Hello, Ada!"`.
