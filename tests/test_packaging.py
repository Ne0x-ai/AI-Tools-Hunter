"""Packaging guards: data that must ride along with the code.

Both files checked here are *not* importable code, so no ordinary test would
notice them going missing. They fail only after ``pip install``, in a fresh
environment, at startup — the worst possible place to find out.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "ai_tools_before_launch"


def _package_data() -> dict[str, list[str]]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["setuptools"]["package-data"]


def _covered(package: str, relative: str) -> bool:
    patterns = _package_data().get(package, [])
    return any(fnmatch.fnmatch(relative, pattern) for pattern in patterns)


def test_every_static_asset_ships_in_the_wheel() -> None:
    """A .py-only wheel makes ``create_app`` raise before it returns.

    ``StaticFiles(directory=...)`` refuses a missing directory, so the whole
    console — not just one page — dies at import time on an installed wheel.
    Pointing ``package-data`` at ``static/assets/*`` instead of the exact
    hashed filenames keeps this working across frontend rebuilds.
    """
    static = PACKAGE / "static"
    files = [
        path.relative_to(PACKAGE).as_posix()
        for path in sorted(static.rglob("*"))
        if path.is_file()
    ]
    assert files, "the frontend build output is missing; run npm run build"

    unshipped = [name for name in files if not _covered("ai_tools_before_launch", name)]
    assert unshipped == [], f"not covered by package-data: {unshipped}"


def test_rdap_bootstrap_table_ships_in_the_wheel() -> None:
    """Without it every non-curated TLD silently loses its age gate."""
    assert _covered(
        "ai_tools_before_launch.filter", "rdap_bootstrap.json"
    ), "the IANA snapshot must stay in package-data"


def test_the_console_entrypoint_files_exist_in_the_source_tree() -> None:
    """Belt and braces for the two paths ``create_app`` hardcodes."""
    from ai_tools_before_launch.api.console import STATIC_DIR

    assert (STATIC_DIR / "index.html").is_file()
    assert (STATIC_DIR / "assets").is_dir()
