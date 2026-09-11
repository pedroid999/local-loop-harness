"""Verification must run in the application's own environment, never the harness's.

Found by a real-model smoke run: the app declared no pytest/ruff/mypy, yet SETUP passed,
because `uv run pytest` inside the app fell back to the harness's tools via PATH.
"""

import os
import sys
from pathlib import Path

from ralph.config import VerifyConfig
from ralph.proc import child_env


def test_child_env_hides_the_harness_virtualenv(monkeypatch):
    harness_bin = str(Path(sys.prefix) / "bin")
    monkeypatch.setenv("PATH", os.pathsep.join([harness_bin, "/usr/bin"]))
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)

    env = child_env()

    assert harness_bin not in env["PATH"].split(os.pathsep)
    assert "/usr/bin" in env["PATH"].split(os.pathsep)
    assert "VIRTUAL_ENV" not in env


def test_default_checks_import_tools_from_the_app_environment():
    # `python -m tool` resolves `python` to the app's venv, so the tool must be installed
    # there; a bare `uv run tool` silently falls back to any tool found on PATH.
    verify = VerifyConfig()
    commands = [verify.test_command, *verify.quality_commands]
    assert all(command[:4] == ["uv", "run", "python", "-m"] for command in commands)


def test_shipped_ralph_toml_uses_the_same_isolated_commands():
    from ralph.config import load_config

    root = Path(__file__).resolve().parents[1]
    verify = load_config(root / "ralph.toml").verify
    commands = [verify.test_command, *verify.quality_commands]
    assert all(command[:4] == ["uv", "run", "python", "-m"] for command in commands)
