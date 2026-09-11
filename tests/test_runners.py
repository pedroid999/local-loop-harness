import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from ralph.config import CommandConfig, ConfigError, OpenCodeConfig
from ralph.runners import CommandRunner, OpenCodeRunner

# A stand-in `opencode` binary that replays the event shapes observed from the real
# `opencode run --format json` (OpenCode 1.18.30) and records how it was invoked.
FAKE_OPENCODE = r"""
import json, os, subprocess, sys, time
out = os.environ["FAKE_OUT"]
with open(out, "w") as f:
    json.dump({"argv": sys.argv[1:], "config": os.environ.get("OPENCODE_CONFIG_CONTENT"),
               "cwd": os.getcwd(), "pwd": os.environ.get("PWD"),
               "virtual_env": os.environ.get("VIRTUAL_ENV")}, f)
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(out + ".child", "w").write(str(child.pid))
    time.sleep(60)
events = [
    {"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}},
    {"type": "tool_use", "sessionID": "ses_1", "part": {"type": "tool", "tool": "write",
     "state": {"status": "error",
               "error": "The user rejected permission to use this specific tool call."}}},
    {"type": "tool_use", "sessionID": "ses_1", "part": {"type": "tool", "tool": "bash",
     "state": {"status": "error", "error": "exit code 1: boom"}}},
    {"type": "tool_use", "sessionID": "ses_1", "part": {"type": "tool", "tool": "read",
     "state": {"status": "completed"}}},
    {"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "All done."}},
    {"type": "step_finish", "sessionID": "ses_1", "part": {"reason": "stop"}},
]
print("not json noise")
for event in events:
    print(json.dumps(event), flush=True)
print("\x1b[93m! \x1b[0mpermission requested: edit (x.py); auto-rejecting", file=sys.stderr)
if mode == "error_event":
    print(json.dumps({"type": "error", "error": {"name": "APIError",
                      "data": {"message": "connection refused"}}}))
sys.exit(3 if mode == "fail" else 0)
"""

PERMISSION = {"edit": "allow", "bash": {"*": "deny", "uv run *": "allow"}}


@pytest.fixture
def fake_opencode(tmp_path, monkeypatch):
    script = tmp_path / "opencode"
    script.write_text(f"#!{sys.executable}\n{FAKE_OPENCODE}")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    out = tmp_path / "invocation.json"
    monkeypatch.setenv("FAKE_OUT", str(out))
    monkeypatch.setenv("VIRTUAL_ENV", "/harness/.venv")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runner = OpenCodeRunner(
        OpenCodeConfig(binary=str(script), permission=PERMISSION), echo=lambda _: None
    )
    return runner, workspace, out


def test_argv_uses_agent_model_and_json_events_and_never_continues_a_session():
    runner = OpenCodeRunner(OpenCodeConfig(), echo=lambda _: None)

    argv = runner.argv("# Ralph iteration", Path("/work/app"))

    assert argv == [
        "opencode",
        "run",
        "--agent",
        "local-qwen",
        "--model",
        "llama.cpp/qwen3.5-9b-local",
        "--format",
        "json",
        "--dir",
        "/work/app",
        "# Ralph iteration",
    ]
    assert not {"-c", "--continue", "-s", "--session", "--fork"} & set(argv)


@pytest.mark.parametrize("flag", ["--continue", "-c", "-s", "--session=abc", "--fork", "--auto"])
def test_session_continuation_and_blanket_auto_approval_are_rejected(flag):
    with pytest.raises(ConfigError):
        OpenCodeRunner(OpenCodeConfig(extra_args=[flag]), echo=lambda _: None)


def test_parses_events_and_scopes_permissions_to_the_subprocess(fake_opencode, tmp_path):
    runner, workspace, out = fake_opencode
    log = tmp_path / "agent.log"

    result = runner.invoke("# prompt", workspace, log, timeout_s=30)

    assert result.ok and result.exit_code == 0
    assert result.session_id == "ses_1"
    assert result.final_text == "All done."
    assert result.tool_calls == 3
    assert result.tool_errors == ["bash: exit code 1: boom"]
    assert any("write" in d for d in result.permission_denials)
    assert any("edit (x.py)" in d for d in result.permission_denials)
    invocation = json.loads(out.read_text())
    assert json.loads(invocation["config"]) == {"agent": {"local-qwen": {"permission": PERMISSION}}}
    assert Path(invocation["cwd"]).resolve() == workspace.resolve()
    # OpenCode resolves its project directory from PWD: it must match the workspace,
    # not the harness directory the user launched ralph from.
    assert Path(invocation["pwd"]).resolve() == workspace.resolve()
    assert invocation["virtual_env"] is None
    assert '"type": "text"' in log.read_text()
    assert "OPENCODE_CONFIG_CONTENT" not in os.environ


@pytest.mark.parametrize(
    ("mode", "error"), [("error_event", "connection refused"), ("fail", "exit")]
)
def test_error_events_and_nonzero_exit_are_cli_failures(
    fake_opencode, tmp_path, monkeypatch, mode, error
):
    runner, workspace, _ = fake_opencode
    monkeypatch.setenv("FAKE_MODE", mode)

    result = runner.invoke("# prompt", workspace, tmp_path / "agent.log", timeout_s=30)

    assert not result.ok
    assert error in (result.cli_error or "")


def test_timeout_terminates_the_whole_process_group(fake_opencode, tmp_path, monkeypatch):
    runner, workspace, out = fake_opencode
    monkeypatch.setenv("FAKE_MODE", "sleep")

    result = runner.invoke("# prompt", workspace, tmp_path / "agent.log", timeout_s=1.5)

    assert result.timed_out and not result.ok
    child = int(Path(str(out) + ".child").read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("child process survived the timeout")


def test_missing_binary_is_a_fatal_runner_error(tmp_path):
    runner = OpenCodeRunner(OpenCodeConfig(binary=str(tmp_path / "missing")), echo=lambda _: None)
    result = runner.invoke("# prompt", tmp_path, tmp_path / "agent.log", timeout_s=5)
    assert result.fatal and not result.ok


def test_command_runner_sends_the_prompt_on_stdin(tmp_path):
    runner = CommandRunner(
        CommandConfig(
            argv=[sys.executable, "-c", "import sys; print('got', len(sys.stdin.read()))"]
        ),
        echo=lambda _: None,
    )
    result = runner.invoke("12345", tmp_path, tmp_path / "agent.log", timeout_s=30)
    assert result.ok and "got 5" in result.final_text


def test_command_runner_can_pass_a_prompt_file_and_workspace(tmp_path):
    script = "import os, sys; print(open(sys.argv[1]).read(), os.environ['RALPH_WORKSPACE'])"
    runner = CommandRunner(
        CommandConfig(argv=[sys.executable, "-c", script, "{prompt_file}"], prompt_via="file"),
        echo=lambda _: None,
    )
    result = runner.invoke("hello prompt", tmp_path, tmp_path / "it" / "agent.log", timeout_s=30)
    assert result.ok
    assert "hello prompt" in result.final_text and str(tmp_path) in result.final_text


def test_command_runner_requires_an_argv_list():
    with pytest.raises(ConfigError):
        CommandRunner(CommandConfig(argv=[]), echo=lambda _: None)
