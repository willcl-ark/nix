"""Shared Ralph page colors, controls and persisted theme preference."""

from pathlib import Path

_ROOT = Path(__file__).parent
STYLE = (_ROOT / "theme.css").read_text(encoding="utf-8")
SCRIPT = (_ROOT / "theme.js").read_text(encoding="utf-8")
BUTTON = '<button type="button" data-theme-toggle hidden>Switch theme</button>'


def save_assets(report_dir):
    from .report import _write_public_file

    report_dir = Path(report_dir)
    report_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    _write_public_file(report_dir / "theme.css", STYLE)
    _write_public_file(report_dir / "theme.js", SCRIPT)
