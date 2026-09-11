import sys
from pathlib import Path

import pytest

from ralph.verify import (
    classify_failure,
    local_modules,
    run_checks,
    run_suite,
    skeleton_problems,
    test_hash,
    triviality_problems,
)

PYTEST = [sys.executable, "-m", "pytest"]


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def ws(tmp_path):
    write(tmp_path, "pyproject.toml", '[tool.pytest.ini_options]\npythonpath = ["."]\n')
    write(tmp_path, "calc/__init__.py", "def add(a, b):\n    return a + b\n")
    return tmp_path


def suite(ws: Path):
    return run_suite(ws, PYTEST, ["tests"], ws.parent / "junit.xml", timeout_s=120)


def test_run_suite_reports_each_outcome_by_node_id(ws):
    write(
        ws,
        "tests/test_calc.py",
        "import pytest\nfrom calc import add\n\n"
        "def test_ac_001_adds():\n    assert add(2, 3) == 5\n\n"
        "def test_ac_002_fails():\n    assert add(1, 1) == 3\n\n"
        "@pytest.mark.skip(reason='later')\n"
        "def test_ac_003_skipped():\n    assert add(1, 1) == 2\n\n"
        "@pytest.fixture\ndef broken():\n    raise RuntimeError('boom')\n\n"
        "def test_ac_004_errors(broken):\n    assert True\n\n"
        "class TestGroup:\n    @pytest.mark.parametrize('n', [1, 2])\n"
        "    def test_ac_005_param(self, n):\n        assert add(n, 0) == n\n",
    )

    result = suite(ws)

    outcomes = {nodeid: case.outcome for nodeid, case in result.cases.items()}
    assert outcomes == {
        "tests/test_calc.py::test_ac_001_adds": "passed",
        "tests/test_calc.py::test_ac_002_fails": "failed",
        "tests/test_calc.py::test_ac_003_skipped": "skipped",
        "tests/test_calc.py::test_ac_004_errors": "error",
        "tests/test_calc.py::TestGroup::test_ac_005_param[1]": "passed",
        "tests/test_calc.py::TestGroup::test_ac_005_param[2]": "passed",
    }
    assert "assert 2 == 3" in result.cases["tests/test_calc.py::test_ac_002_fails"].message
    assert not result.collection_error


def test_addopts_cannot_deselect_tests(ws):
    write(
        ws,
        "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["."]\naddopts = "-k nothing"\n',
    )
    write(ws, "tests/test_calc.py", "def test_ac_001_x():\n    assert 1 + 1 == 2\n")

    assert list(suite(ws).cases) == ["tests/test_calc.py::test_ac_001_x"]


def test_empty_suite_and_collection_errors_are_detected(ws):
    assert suite(ws).cases == {}
    write(ws, "tests/test_bad.py", "def test_ac_001_x(:\n    pass\n")
    result = suite(ws)
    assert result.collection_error
    assert "SyntaxError" in result.output or "invalid syntax" in result.output


@pytest.mark.parametrize(
    ("message", "genuine"),
    [
        ("AssertionError: assert 404 == 201", True),
        ("TypeError: add() takes 2 positional arguments", True),
        ("AttributeError: module 'calc' has no attribute 'sub'", True),
        ("ModuleNotFoundError: No module named 'calc.api'", True),
        ("ImportError: cannot import name 'sub' from 'calc' (/x/calc/__init__.py)", True),
        ("ModuleNotFoundError: No module named 'fastapi'", False),
        ("ImportError: cannot import name 'x' from 'requests'", False),
        ("NameError: name 'undefined_thing' is not defined", False),
        ("SyntaxError: invalid syntax", False),
        ("Failed: pytest.fail called", True),
    ],
)
def test_classify_failure_separates_behavior_from_infrastructure(message, genuine):
    reason = classify_failure(message, {"calc"})
    assert (reason is None) is genuine


def test_local_modules_include_existing_packages_and_declared_package(ws):
    write(ws, "src/other/__init__.py", "")
    write(ws, "script.py", "")
    assert {"calc", "other", "script", "future_pkg"} <= local_modules(ws, "future_pkg")
    assert "tests" not in local_modules(ws, None)


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("    assert True\n", "constant"),
        ("    x = 1\n", "no assertion"),
        ("    pytest.fail('todo')\n", "unconditional"),
        ("    raise NotImplementedError\n", "unconditional"),
        ("    assert False, 'red'\n", "constant"),
    ],
)
def test_trivial_or_unconditional_tests_are_rejected(ws, body, problem):
    write(ws, "tests/test_t.py", f"import pytest\n\ndef test_ac_001_x():\n{body}")
    problems = triviality_problems(ws, "tests/test_t.py::test_ac_001_x")
    assert any(problem in p for p in problems)


def test_skip_markers_are_rejected_and_real_tests_accepted(ws):
    write(
        ws,
        "tests/test_t.py",
        "import pytest\nfrom calc import add\n\n"
        "@pytest.mark.skip\ndef test_ac_001_skip():\n    assert add(1, 1) == 2\n\n"
        "def test_ac_002_ok():\n    assert add(1, 1) == 2\n\n"
        "def test_ac_003_raises():\n    with pytest.raises(TypeError):\n        add(1)\n",
    )
    assert any("skip" in p for p in triviality_problems(ws, "tests/test_t.py::test_ac_001_skip"))
    assert triviality_problems(ws, "tests/test_t.py::test_ac_002_ok") == []
    assert triviality_problems(ws, "tests/test_t.py::test_ac_003_raises") == []


def test_test_hash_tracks_the_function_source_only(ws):
    write(ws, "tests/test_t.py", "def test_ac_001_x():\n    assert 1 == 1\n")
    first = test_hash(ws, "tests/test_t.py::test_ac_001_x")
    write(
        ws,
        "tests/test_t.py",
        "def test_ac_001_x():\n    assert 1 == 1\n\n\ndef test_other():\n    assert 2\n",
    )
    same = test_hash(ws, "tests/test_t.py::test_ac_001_x[param]")
    write(ws, "tests/test_t.py", "def test_ac_001_x():\n    assert 1 == 1 or True\n")
    changed = test_hash(ws, "tests/test_t.py::test_ac_001_x")

    assert first is not None and first == same
    assert changed != first
    assert test_hash(ws, "tests/test_t.py::test_missing") is None


def test_test_hash_ignores_formatting_but_not_semantics(ws):
    write(
        ws,
        "tests/test_t.py",
        "def test_ac_001_x():\n    value = 1  \n    \n    assert value == 1\n",
    )
    original = test_hash(ws, "tests/test_t.py::test_ac_001_x")
    write(
        ws,
        "tests/test_t.py",
        "def test_ac_001_x():\n    value = 1  # tidy\n\n    assert value == 1\n",
    )
    reformatted = test_hash(ws, "tests/test_t.py::test_ac_001_x")
    write(ws, "tests/test_t.py", "def test_ac_001_x():\n    value = 1\n\n    assert value >= 0\n")
    weakened = test_hash(ws, "tests/test_t.py::test_ac_001_x")

    assert original == reformatted
    assert weakened != original


def test_skeleton_problems_flag_behavior_in_setup_files(ws):
    write(ws, "pkg/__init__.py", '"""Package."""\n__all__: list[str] = []\n')
    write(ws, "pkg/logic.py", "def f():\n    return 1\n")
    assert skeleton_problems(ws / "pkg/__init__.py") == []
    assert skeleton_problems(ws / "pkg/logic.py")


def test_run_checks_reports_success_failure_and_timeout(ws):
    ok, bad, slow = run_checks(
        [
            [sys.executable, "-c", "print('fine')"],
            [sys.executable, "-c", "import sys; print('lint error'); sys.exit(1)"],
            [sys.executable, "-c", "import time; time.sleep(30)"],
        ],
        ws,
        timeout_s=2,
    )
    assert ok.ok and "fine" in ok.output
    assert not bad.ok and "lint error" in bad.output
    assert not slow.ok and "timed out" in slow.output
