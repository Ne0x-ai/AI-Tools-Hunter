"""Guards the README architecture diagram against silently breaking.

The SVG once lost its ``xmlns`` declaration, which makes GitHub render it as
a wall of raw CSS instead of a picture. The README shows that SVG directly,
so the asset and the reference have to stay in step: a stale PNG used to sit
in the repo showing a diagram that no longer matched the code.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SVG = REPO_ROOT / "docs" / "architecture.svg"
JSON = REPO_ROOT / "docs" / "architecture.json"
README = REPO_ROOT / "README.md"
README_ZH = REPO_ROOT / "README.zh-CN.md"
SVG_NS = 'xmlns="http://www.w3.org/2000/svg"'


def test_svg_root_element_declares_the_svg_namespace() -> None:
    root_tag = re.search(r"<svg\b[^>]*>", SVG.read_text(encoding="utf-8"))
    assert root_tag is not None
    assert SVG_NS in root_tag.group(0)


def test_readme_references_the_svg() -> None:
    assert "docs/architecture.svg" in README.read_text(encoding="utf-8")


def test_readme_does_not_point_at_a_removed_raster() -> None:
    """A PNG cannot be regenerated without the external tool that made it.

    If someone re-adds the reference without re-adding a matching asset, the
    README image 404s on GitHub — and a stale PNG is worse than none, because
    it silently shows an architecture the code no longer has.
    """
    for readme in (README, README_ZH):
        text = readme.read_text(encoding="utf-8")
        assert "docs/architecture.png" not in text
        assert "docs/architecture.html" not in text


def test_svg_is_well_formed_and_has_a_viewbox() -> None:
    """GitHub renders SVG through a strict parser; one stray tag blanks it."""
    root = ET.parse(SVG).getroot()
    assert root.get("viewBox"), "without a viewBox the diagram cannot scale"
    assert root.find("{http://www.w3.org/2000/svg}title") is not None


def test_diagram_source_lists_the_write_boundary() -> None:
    """The JSON is the source of record, so the two regions must stay named.

    The diagram's whole point is that ``start`` owns every ingest table and
    ``serve`` does not. Losing those labels turns it back into a generic
    pipeline drawing.
    """
    import json

    spec = json.loads(JSON.read_text(encoding="utf-8"))
    labels = [boundary["label"] for boundary in spec["boundaries"]]
    assert any("start" in label and "sole writer" in label for label in labels)
    assert any("serve" in label and "never writes ingest" in label for label in labels)

    ids = {component["id"] for component in spec["components"]}
    for boundary in spec["boundaries"]:
        assert set(boundary["wraps"]) <= ids, "a region wraps an unknown component"
    for connection in spec["connections"]:
        assert connection["from"] in ids and connection["to"] in ids
