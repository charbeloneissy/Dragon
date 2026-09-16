"""Single-source live dashboard loader for the Dragon arbitrage engine.

The canonical dashboard lives at the repository root so the deployed engine
and the directly served dashboard cannot silently diverge.
"""
from pathlib import Path


_DASHBOARD_PATH = Path(__file__).resolve().parents[2] / "dashboard.html"

if not _DASHBOARD_PATH.is_file():
    raise RuntimeError(f"Dragon dashboard file not found: {_DASHBOARD_PATH}")

HTML = _DASHBOARD_PATH.read_text(encoding="utf-8")
