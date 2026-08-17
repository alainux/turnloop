from __future__ import annotations

import importlib
import importlib.metadata
from pathlib import Path


def test_pin_package_layout_uses_the_canonical_turn_tree():
    package_root = Path(__file__).resolve().parents[1]
    for module_name in (
        "turn.core",
        "turn.db.store",
        "turn.runner.runner",
        "turn.server.api",
        "turn.domain.schemas",
    ):
        module = importlib.import_module(module_name)
        assert Path(module.__file__).resolve().is_relative_to(package_root)

    entry_point = next(
        entry_point
        for entry_point in importlib.metadata.entry_points(group="console_scripts")
        if entry_point.name == "turn"
    )
    assert entry_point.value == "turn.__main__:main"


def test_pin_no_duplicate_python_implementation_tree_at_repository_root():
    repository_root = Path(__file__).resolve().parents[2]
    assert not any(
        (repository_root / name).is_file()
        for name in ("core.py", "db/store.py", "runner/runner.py", "server/api.py")
    )
