from __future__ import annotations

import ast
from pathlib import Path


PRODUCTION_ROOT = Path(__file__).resolve().parents[1]


def _python_files(package: str) -> list[Path]:
    return sorted((PRODUCTION_ROOT / package).rglob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module if node.level == 0 else node.module)
    return names


def _assert_package_does_not_import(package: str, forbidden: tuple[str, ...]) -> None:
    violations = []
    for path in _python_files(package):
        for imported in _imports(path):
            if any(imported == prefix or imported.startswith(f"{prefix}.") for prefix in forbidden):
                violations.append(f"{path.relative_to(PRODUCTION_ROOT)} imports {imported}")
    assert not violations, "\n".join(violations)


def test_domain_and_graph_dependencies_point_inward():
    _assert_package_does_not_import(
        "domain", ("turn.skills", "turn.db", "turn.runner", "turn.server", "turn.workers", "turn.mcp")
    )
    _assert_package_does_not_import("graph", ("turn.db", "turn.runner", "turn.server", "turn.workers"))
    _assert_package_does_not_import("db", ("turn.runner", "turn.server"))
    _assert_package_does_not_import("runner", ("turn.server",))


def test_production_does_not_import_test_packages():
    violations = []
    for path in PRODUCTION_ROOT.rglob("*.py"):
        if "tests" in path.parts:
            continue
        for imported in _imports(path):
            if imported == "turn.tests" or imported.startswith("turn.tests."):
                violations.append(f"{path.relative_to(PRODUCTION_ROOT)} imports {imported}")
    assert not violations, "\n".join(violations)


def test_private_store_and_runner_members_stay_inside_their_owners():
    violations = []
    for path in PRODUCTION_ROOT.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr == "_save_node" and path.parent.name != "db":
                violations.append(f"{path.relative_to(PRODUCTION_ROOT)} calls Store._save_node")
            if path.name == "core.py" and node.attr in {"_running", "_schedule_project"}:
                violations.append(f"core.py accesses Runner.{node.attr}")
    assert not violations, "\n".join(violations)
