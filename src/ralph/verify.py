"""Runner-side verification: pytest execution, test discovery and hashing, RED
classification, trivial-test detection, quality and smoke checks.

Nothing here trusts the agent: results come from processes the runner starts itself.
"""

from __future__ import annotations

import ast
import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ralph import proc
from ralph.workspace import DEFAULT_IGNORE

INFRA_EXCEPTIONS = {"SyntaxError", "IndentationError", "TabError", "NameError"}
IMPORT_EXCEPTIONS = {"ModuleNotFoundError", "ImportError"}


@dataclass
class TestCase:
    nodeid: str
    outcome: str  # passed | failed | error | skipped
    message: str = ""
    details: str = ""

    @property
    def base_nodeid(self) -> str:
        return self.nodeid.split("[", 1)[0]

    @property
    def function(self) -> str:
        return self.base_nodeid.rsplit("::", 1)[-1]


@dataclass
class SuiteResult:
    exit_code: int | None
    cases: dict[str, TestCase] = field(default_factory=dict)
    output: str = ""
    timed_out: bool = False
    collection_errors: dict[str, str] = field(default_factory=dict)  # file -> exception line

    @property
    def collection_error(self) -> bool:
        return bool(self.collection_errors) or self.timed_out or self.exit_code in (None, 2, 3, 4)

    def for_base(self, base_nodeid: str) -> list[TestCase]:
        return [c for c in self.cases.values() if c.base_nodeid == base_nodeid]


@dataclass
class TestRef:
    nodeid: str  # without parametrization
    file: str
    qualname: tuple[str, ...]

    @property
    def function(self) -> str:
        return self.qualname[-1]


@dataclass
class CheckResult:
    command: list[str]
    ok: bool
    output: str

    @property
    def name(self) -> str:
        return " ".join(self.command)


# --------------------------------------------------------------------------- pytest


def run_suite(
    workspace: Path,
    test_command: Sequence[str],
    test_dirs: Sequence[str],
    junit_path: Path,
    timeout_s: float | None,
    max_chars: int = 20_000,
) -> SuiteResult:
    """Run the workspace test suite with settings the agent cannot override via addopts."""
    targets = [d for d in test_dirs if (workspace / d).exists()]
    if not targets:
        return SuiteResult(exit_code=5, output="No test directory exists yet.")
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    junit_path.unlink(missing_ok=True)
    argv = [
        *test_command,
        "-o",
        "addopts=",
        "-o",
        "junit_family=xunit1",
        "-p",
        "no:cacheprovider",
        f"--rootdir={workspace}",
        f"--junitxml={junit_path}",
        "--tb=short",
        "-q",
        *targets,
    ]
    try:
        result = proc.run(argv, workspace, timeout_s, max_chars=max_chars)
    except FileNotFoundError as exc:
        return SuiteResult(exit_code=None, output=f"Test command not found: {exc}")
    cases, collection = _parse_junit(junit_path) if junit_path.exists() else ({}, {})
    return SuiteResult(
        exit_code=result.returncode,
        cases=cases,
        output=result.output,
        timed_out=result.timed_out,
        collection_errors=collection,
    )


def _parse_junit(path: Path) -> tuple[dict[str, TestCase], dict[str, str]]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {}, {}
    cases: dict[str, TestCase] = {}
    collection: dict[str, str] = {}
    for tc in root.iter("testcase"):
        name, classname, file = tc.get("name", ""), tc.get("classname", ""), tc.get("file")
        outcome, message, details = "passed", "", ""
        for tag, label in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            child = tc.find(tag)
            if child is not None:
                outcome, message, details = label, child.get("message", ""), child.text or ""
                break
        if outcome == "error" and "collection failure" in message:
            collected_file = file or _module_to_path(f"{classname}.{name}".strip("."))
            collection[collected_file] = _exception_line(details) or message
            continue
        if not file:
            continue
        module = file.removesuffix(".py").replace("/", ".")
        inner = classname[len(module) + 1 :] if classname.startswith(module + ".") else ""
        nodeid = "::".join([file, *([p for p in inner.split(".") if p]), name])
        cases[nodeid] = TestCase(nodeid, outcome, message.strip(), details[-4000:])
    return cases, collection


def _module_to_path(dotted: str) -> str:
    return dotted.replace(".", "/") + ".py"


def _exception_line(details: str) -> str:
    """Last `E   SomeError: ...` line of a pytest traceback."""
    lines = [m.group(1) for m in re.finditer(r"^E\s+(.+)$", details, re.MULTILINE)]
    errors = [line for line in lines if re.match(r"^[\w.]+(Error|Exception)\b", line)]
    return (errors or lines or [""])[-1].strip()


# --------------------------------------------------------------------------- RED


def classify_failure(message: str, local: set[str]) -> str | None:
    """None when the failure reflects missing/incorrect behavior, else why it does not."""
    first = (message.strip().splitlines() or [""])[0]
    match = re.match(r"^([A-Za-z_][\w.]*)\s*(?::|$)", first)
    exc = match.group(1).rsplit(".", 1)[-1] if match else ""
    if exc in INFRA_EXCEPTIONS:
        return f"{exc} is a broken test, not missing behavior: {first}"
    if exc in IMPORT_EXCEPTIONS:
        module = re.search(r"No module named '([^']+)'", first) or re.search(
            r"from '([^']+)'", first
        )
        if not module:
            return f"Import failure outside the workspace code: {first}"
        top = module.group(1).split(".")[0]
        if top not in local:
            return (
                f"'{top}' is not a workspace module (missing third-party dependency or "
                f"wrong import?): {first}"
            )
    return None


def local_modules(workspace: Path, declared: str | None) -> set[str]:
    names = {declared} if declared else set()
    for base in (workspace, workspace / "src"):
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if entry.name.startswith(".") or entry.name in DEFAULT_IGNORE:
                continue
            if entry.name in ("tests", "test", "src"):
                continue
            if entry.is_dir() and any(entry.glob("*.py")):
                names.add(entry.name)
            elif entry.suffix == ".py":
                names.add(entry.stem)
    return names


# --------------------------------------------------------------------------- static test analysis


def discover_tests(workspace: Path, test_dirs: Sequence[str]) -> dict[str, TestRef]:
    """Tests found statically with pytest's default naming rules (independent of pytest config)."""
    found: dict[str, TestRef] = {}
    for test_dir in test_dirs:
        root = workspace / test_dir
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if any(part in DEFAULT_IGNORE for part in path.relative_to(workspace).parts):
                continue
            if not (path.name.startswith("test_") or path.name.endswith("_test.py")):
                continue
            rel = path.relative_to(workspace).as_posix()
            tree = _parse(path)
            if tree is None:
                continue
            for node in tree.body:
                if _is_test_function(node):
                    assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                    found[f"{rel}::{node.name}"] = TestRef(f"{rel}::{node.name}", rel, (node.name,))
                elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                    for item in node.body:
                        if _is_test_function(item):
                            assert isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                            nodeid = f"{rel}::{node.name}::{item.name}"
                            found[nodeid] = TestRef(nodeid, rel, (node.name, item.name))
    return found


def _is_test_function(node: ast.stmt) -> bool:
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None


def _find(
    workspace: Path, nodeid: str
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, str] | None:
    file, *qualname = nodeid.split("[", 1)[0].split("::")
    path = workspace / file
    tree = _parse(path) if path.is_file() else None
    if tree is None or not qualname:
        return None
    body: list[ast.stmt] = tree.body
    for index, name in enumerate(qualname):
        last = index == len(qualname) - 1
        node = next(
            (
                n
                for n in body
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and n.name == name
            ),
            None,
        )
        if node is None:
            return None
        if last:
            if isinstance(node, ast.ClassDef):
                return None
            return node, path.read_text(encoding="utf-8")
        if not isinstance(node, ast.ClassDef):
            return None
        body = node.body
    return None


def test_hash(workspace: Path, nodeid: str) -> str | None:
    """Semantic identity of a test: the AST of the function, decorators included.

    Formatting-only edits (whitespace, line wrapping, comments) keep the identity, so
    frozen tests can satisfy lint/format checks; any behavioral change breaks it.
    """
    found = _find(workspace, nodeid)
    if found is None:
        return None
    return hashlib.sha256(ast.dump(found[0]).encode()).hexdigest()


def same_ast(a: Path, b: Path) -> bool:
    """True when two Python files differ only in formatting and comments."""
    if a.suffix != ".py" or b.suffix != ".py" or not (a.is_file() and b.is_file()):
        return False
    tree_a, tree_b = _parse(a), _parse(b)
    return tree_a is not None and tree_b is not None and ast.dump(tree_a) == ast.dump(tree_b)


test_hash.__test__ = False  # type: ignore[attr-defined]  # not a pytest test


def triviality_problems(workspace: Path, nodeid: str) -> list[str]:
    found = _find(workspace, nodeid)
    if found is None:
        return [f"{nodeid}: test function not found"]
    node, _ = found
    problems: list[str] = []
    for decorator in node.decorator_list:
        if re.search(r"\b(skip|skipif|xfail)\b", ast.unparse(decorator)):
            problems.append(f"{nodeid}: skip/xfail markers are not allowed on acceptance tests")
    body = [s for s in node.body if not _is_docstring(s)]
    for stmt in body:
        if isinstance(stmt, ast.Raise) or _is_pytest_call(stmt, {"fail", "skip", "xfail"}):
            problems.append(f"{nodeid}: unconditional failure or skip ({ast.unparse(stmt)[:60]})")
    asserts = [n for n in ast.walk(node) if isinstance(n, ast.Assert)]
    raises_ctx = any(
        isinstance(n, ast.With | ast.AsyncWith)
        and any(_call_name(item.context_expr) in {"raises", "warns"} for item in n.items)
        for n in ast.walk(node)
    )
    assert_calls = any(
        isinstance(n, ast.Call) and _call_name(n).startswith("assert") for n in ast.walk(node)
    )
    if not asserts and not raises_ctx and not assert_calls:
        problems.append(f"{nodeid}: no assertion")
    elif (
        asserts
        and all(isinstance(a.test, ast.Constant) for a in asserts)
        and not (raises_ctx or assert_calls)
    ):
        problems.append(f"{nodeid}: only constant assertions (assert True/False prove nothing)")
    return problems


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _call_name(expr: ast.expr) -> str:
    if isinstance(expr, ast.Call):
        expr = expr.func
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Name):
        return expr.id
    return ""


def _is_pytest_call(stmt: ast.stmt, names: set[str]) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Attribute)
        and isinstance(stmt.value.func.value, ast.Name)
        and stmt.value.func.value.id == "pytest"
        and stmt.value.func.attr in names
    )


def skeleton_problems(path: Path) -> list[str]:
    """Setup may only add scaffolding: Python files without functions or classes."""
    if path.suffix != ".py" or not path.is_file():
        return []
    tree = _parse(path)
    if tree is None:
        return [f"{path.name}: not valid Python"]
    return [
        f"{path.name}: defines {type(n).__name__} {getattr(n, 'name', '')} (no behavior in SETUP)"
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda)
    ]


# --------------------------------------------------------------------------- quality / smoke


def run_checks(
    commands: Sequence[Sequence[str]], workspace: Path, timeout_s: float | None
) -> list[CheckResult]:
    results = []
    for command in commands:
        try:
            outcome = proc.run(command, workspace, timeout_s, max_chars=8_000)
            results.append(CheckResult(list(command), outcome.ok, outcome.output))
        except FileNotFoundError as exc:
            results.append(CheckResult(list(command), False, f"command not found: {exc}"))
    return results
