"""Guards the README architecture diagram against silently breaking.

The SVG once lost its ``xmlns`` declaration, which makes GitHub render it as
a wall of raw CSS instead of a picture. The PNG is what the README actually
shows, so both assets are checked here.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SVG = REPO_ROOT / "docs" / "architecture.svg"
PNG = REPO_ROOT / "docs" / "architecture.png"
README = REPO_ROOT / "README.md"
SVG_NS = 'xmlns="http://www.w3.org/2000/svg"'


def test_svg_root_element_declares_the_svg_namespace() -> None:
    root_tag = re.search(r"<svg\b[^>]*>", SVG.read_text(encoding="utf-8"))
    assert root_tag is not None
    assert SVG_NS in root_tag.group(0)


def test_readme_references_the_png() -> None:
    assert "docs/architecture.png" in README.read_text(encoding="utf-8")


def test_png_exists_and_is_not_empty() -> None:
    assert PNG.exists()
    assert PNG.stat().st_size > 10_000
