"""The checked-in browser contract must be reproducible from server schemas."""
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "ui" / "src" / "generated" / "domain.ts"
GENERATOR = ROOT / "scripts" / "generate_ui_contract.py"


def test_generated_ui_contract_matches_shared_schema():
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    generated = Path(OUTPUT).read_text()
    assert "export interface Agent" in generated
    assert "export interface Planner" in generated
    assert "export interface Executor" in generated
    assert "export interface GraphNodeView" in generated
    assert "export interface GraphView" in generated
