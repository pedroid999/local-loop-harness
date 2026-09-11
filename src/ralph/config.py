"""Configuration: ralph.toml merged over defaults. Unknown keys are errors."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Invalid configuration or command-line usage."""


@dataclass
class OpenCodeConfig:
    binary: str = "opencode"
    agent: str = "local-qwen"
    model: str = "llama.cpp/qwen3.5-9b-local"
    # Extra CLI arguments inserted before the prompt (e.g. ["--pure"]).
    extra_args: list[str] = field(default_factory=list)
    # Agent permission override injected ONLY into the child process through
    # OPENCODE_CONFIG_CONTENT. Your global OpenCode config is never modified.
    permission: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandConfig:
    # argv list; placeholders {prompt_file} and {workspace} are substituted.
    argv: list[str] = field(default_factory=list)
    prompt_via: str = "stdin"  # "stdin" or "file"


@dataclass
class VerifyConfig:
    # `python -m tool` runs the tool from the app's own environment: a bare `uv run tool`
    # would silently fall back to any tool found on PATH (harness venv, global installs).
    test_command: list[str] = field(default_factory=lambda: ["uv", "run", "python", "-m", "pytest"])
    test_dirs: list[str] = field(default_factory=lambda: ["tests"])
    quality_commands: list[list[str]] = field(
        default_factory=lambda: [
            ["uv", "run", "python", "-m", "ruff", "check", "."],
            ["uv", "run", "python", "-m", "ruff", "format", "--check", "."],
            ["uv", "run", "python", "-m", "mypy", "."],
        ]
    )
    # Commands run before the runner-only setup check (e.g. install dependencies).
    setup_commands: list[list[str]] = field(default_factory=lambda: [["uv", "sync"]])
    smoke_command: list[str] = field(default_factory=list)  # a spec's smoke_command wins
    # Non-test files the agent may change during RED (to add a test dependency).
    red_allowed_paths: list[str] = field(default_factory=lambda: ["pyproject.toml", "uv.lock"])
    docs_globs: list[str] = field(default_factory=lambda: ["*.md", "*.rst", "*.txt", "docs/**"])
    # Workspace paths the agent may never change (reverted after every invocation).
    protected_workspace_paths: list[str] = field(
        default_factory=lambda: ["opencode.json", "opencode.jsonc", ".opencode"]
    )
    test_timeout_s: float = 900


@dataclass
class LoopConfig:
    invocation_timeout_s: float = 1800
    # Consecutive failed invocations (CLI error or timeout, with no accepted progress)
    # treated as an unrecoverable operational error. Not an iteration limit.
    max_consecutive_runner_failures: int = 3
    warn_after_stalls: int = 3
    stop_after_stalls: int | None = None  # opt-in; default is to keep looping


@dataclass
class ContextConfig:
    max_feedback_chars: int = 6000
    max_history_entries: int = 8
    max_listing_entries: int = 150
    max_prompt_chars: int = 40_000


@dataclass
class Config:
    root: Path
    workspace: Path
    state_dir: Path
    prompts_dir: Path
    runner: str = "opencode"
    protected_paths: list[str] = field(
        default_factory=lambda: [
            "ralph.py",
            "ralph.toml",
            "pyproject.toml",
            "uv.lock",
            "src",
            "prompts",
            "specs",
            "tests",
            "examples",
        ]
    )
    opencode: OpenCodeConfig = field(default_factory=OpenCodeConfig)
    command: CommandConfig = field(default_factory=CommandConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    context: ContextConfig = field(default_factory=ContextConfig)


def _build(cls: type, values: dict[str, Any], where: str) -> Any:
    known = {f.name for f in fields(cls)}
    unknown = set(values) - known
    if unknown:
        raise ConfigError(f"Unknown keys in [{where}]: {', '.join(sorted(unknown))}")
    return cls(**values)


def load_config(path: Path | None, root: Path | None = None) -> Config:
    """Load ralph.toml (optional) relative to the harness root."""
    data: dict[str, Any] = {}
    if path is not None and path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
    base = (root or (path.parent if path else Path.cwd())).resolve()

    harness = dict(data.get("harness", {}))
    runner_section = dict(data.get("runner", {}))
    unknown_top = set(data) - {"harness", "runner", "verify", "loop", "context"}
    if unknown_top:
        raise ConfigError(f"Unknown sections: {', '.join(sorted(unknown_top))}")

    allowed_harness = {"workspace", "state_dir", "prompts_dir", "protected_paths"}
    if unknown := set(harness) - allowed_harness:
        raise ConfigError(f"Unknown keys in [harness]: {', '.join(sorted(unknown))}")
    default_runner = runner_section.pop("default", "opencode")
    opencode = _build(OpenCodeConfig, runner_section.pop("opencode", {}), "runner.opencode")
    command = _build(CommandConfig, runner_section.pop("command", {}), "runner.command")
    if runner_section:
        raise ConfigError(f"Unknown keys in [runner]: {', '.join(sorted(runner_section))}")

    config = Config(
        root=base,
        workspace=(base / harness.get("workspace", "app")).resolve(),
        state_dir=(base / harness.get("state_dir", ".ralph")).resolve(),
        prompts_dir=(base / harness.get("prompts_dir", "prompts")).resolve(),
        runner=default_runner,
        opencode=opencode,
        command=command,
        verify=_build(VerifyConfig, data.get("verify", {}), "verify"),
        loop=_build(LoopConfig, data.get("loop", {}), "loop"),
        context=_build(ContextConfig, data.get("context", {}), "context"),
    )
    if "protected_paths" in harness:
        config.protected_paths = list(harness["protected_paths"])
    validate(config)
    return config


def validate(config: Config) -> None:
    if config.runner not in ("opencode", "command"):
        raise ConfigError(f"Unknown runner {config.runner!r}; expected 'opencode' or 'command'")
    if config.command.prompt_via not in ("stdin", "file"):
        raise ConfigError("[runner.command] prompt_via must be 'stdin' or 'file'")
    if not config.verify.test_command:
        raise ConfigError("[verify] test_command must be a non-empty argv list")
    if config.loop.max_consecutive_runner_failures < 1:
        raise ConfigError("[loop] max_consecutive_runner_failures must be >= 1")
    if config.loop.stop_after_stalls is not None and config.loop.stop_after_stalls < 1:
        raise ConfigError("[loop] stop_after_stalls must be a positive integer")
