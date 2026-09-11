---
id: task-manager
title: Task Manager
package: task_manager
stack: >
  Python 3.12+ managed with uv; pytest, Ruff and mypy as dev dependencies.
  FastAPI for the JSON API (tests use fastapi.testclient.TestClient, which needs httpx).
  Server-rendered HTML with Jinja2 for the web page. In-memory storage only, no database.
smoke_command: ["uv", "run", "python", "-c", "from task_manager.api import app; assert app.title"]
---

# Task Manager

A tiny task manager: a domain model with an in-memory repository, a JSON API to
create, list, complete and delete tasks, and one server-rendered HTML page.

All code lives in the `task_manager` package. Each test must build its own app with
`task_manager.api.create_app()` so tests never share state.

## TASK-001: Task model and in-memory repository

Type: feature
Depends on: none

### Requirements
- `task_manager.repository.Task` has an integer `id`, a non-empty `title`
  (surrounding whitespace removed) and a boolean `done` that defaults to false.
- `task_manager.repository.TaskRepository` stores tasks in memory and assigns
  incrementing ids starting at 1. Methods: `add(title) -> Task`, `list() -> list[Task]`.

### Acceptance criteria
- AC-001: `TaskRepository().add("  Buy milk  ")` returns a task with id 1,
  title "Buy milk" and done false.
- AC-002: Adding a task whose title is empty or only whitespace raises `ValueError`.
- AC-003: `list()` returns all added tasks in creation order.

### Verification
Unit tests against `task_manager.repository`.

## TASK-002: HTTP API to create and list tasks

Type: feature
Depends on: TASK-001

### Requirements
- `task_manager.api.create_app()` returns a new FastAPI application with its own
  empty repository; `task_manager.api.app` is a module-level instance for serving.
- Tasks are serialized as JSON objects `{"id": int, "title": str, "done": bool}`.

### Acceptance criteria
- AC-004: `POST /tasks` with JSON `{"title": "Write spec"}` returns 201 and
  `{"id": 1, "title": "Write spec", "done": false}`.
- AC-005: `POST /tasks` with an empty or whitespace-only title returns 422.
- AC-006: `GET /tasks` returns 200 and a JSON list of the created tasks in creation order.

### Verification
API tests with `TestClient(create_app())`.

## TASK-003: Complete and delete tasks

Type: feature
Depends on: TASK-002

### Requirements
- Completing a task sets `done` to true. Deleting a task removes it permanently.

### Acceptance criteria
- AC-007: `POST /tasks/{id}/complete` returns 200 and the task with `done` true.
- AC-008: `DELETE /tasks/{id}` returns 204 and the task no longer appears in `GET /tasks`.
- AC-009: Completing or deleting an unknown id returns 404.

### Verification
API tests with `TestClient(create_app())`.

## TASK-004: HTML task list page

Type: feature
Depends on: TASK-002

### Requirements
- `GET /` renders a Jinja2 template listing every task. No JavaScript required.

### Acceptance criteria
- AC-010: `GET /` returns 200 with an HTML content type and shows each task title
  inside an `<li>` element.
- AC-011: `GET /` shows the text "No tasks yet" when there are no tasks.

### Verification
API tests with `TestClient(create_app())` asserting on the response body.
