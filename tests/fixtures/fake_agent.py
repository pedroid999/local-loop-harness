"""A deterministic stand-in coding CLI for the generic `command` runner.

Usage (argv from ralph.toml): python fake_agent.py {prompt_file}
It reads the phase from the prompt and edits the current directory (the workspace),
producing a tiny calculator app through SETUP -> RED -> GREEN -> REFACTOR.
"""

import re
import sys
from pathlib import Path

prompt = Path(sys.argv[1]).read_text(encoding="utf-8")
phase = re.search(r"## Phase: (\w+)", prompt)
target = re.search(r"^Target: (\S+)", prompt, re.MULTILINE)
ws = Path.cwd()
name = phase.group(1) if phase else "?"

if name == "SETUP":
    (ws / "pyproject.toml").write_text(
        '[project]\nname = "calc"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n\n'
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
    )
    (ws / "calc").mkdir(exist_ok=True)
    (ws / "calc" / "__init__.py").write_text('"""Calculator."""\n')
    (ws / "tests").mkdir(exist_ok=True)
elif name == "RED" and target and target.group(1) == "AC-001":
    (ws / "tests" / "test_add.py").write_text(
        "from calc import add\n\n\ndef test_ac_001_adds_two_numbers():\n    assert add(2, 3) == 5\n"
    )
elif name == "GREEN":
    init = ws / "calc" / "__init__.py"
    init.write_text(init.read_text() + "\n\ndef add(a: int, b: int) -> int:\n    return a + b\n")
elif name == "REFACTOR":
    print("No refactor needed: the implementation is already minimal.")

print(f"fake agent handled {name}")
