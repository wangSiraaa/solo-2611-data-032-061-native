"""The calculation-linkbase subset is rejected before any conclusion is drawn."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from xbrlcheck.taxonomy import load_taxonomy

TAXONOMY_DIR = Path(__file__).resolve().parents[1] / "examples" / "taxonomy"


def _build(tmp_path: Path, linkbase_xml: str) -> Path:
    """Copy the minimal schemas and write a custom calculation linkbase."""
    for name in ("xbrli-min.xsd", "xbrldt-min.xsd"):
        shutil.copy(TAXONOMY_DIR / name, tmp_path / name)
    schema = (TAXONOMY_DIR / "example.xsd").read_text()
    schema = schema.replace(
        'xlink:href="calculation.xml"', 'xlink:href="custom-calculation.xml"'
    )
    (tmp_path / "example.xsd").write_text(schema)
    (tmp_path / "custom-calculation.xml").write_text(linkbase_xml)
    return tmp_path / "example.xsd"


def _linkbase(*arcs: str, role: str = "http://www.xbrl.org/2003/role/link") -> str:
    locs = "".join(
        '<link:loc xlink:type="locator" xlink:href="example.xsd#%s" xlink:label="%s"/>' % (n, n)
        for n in ("Total", "PartA", "PartB")
    )
    return (
        '<?xml version="1.0"?>'
        '<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<link:calculationLink xlink:type="extended" xlink:role="%s">%s%s'
        "</link:calculationLink></link:linkbase>" % (role, locs, "".join(arcs))
    )


def _arc(frm="Total", to="PartA", extra="") -> str:
    return (
        '<link:calculationArc xlink:type="arc" '
        'xlink:arcrole="http://www.xbrl.org/2003/arcrole/summation-item" '
        'xlink:from="%s" xlink:to="%s" weight="1"%s/>' % (frm, to, extra)
    )


def test_priority_rejected(tmp_path):
    schema = _build(tmp_path, _linkbase(_arc(extra=' priority="2"')))
    taxonomy = load_taxonomy([schema])
    assert any(d.code == "unsupported-priority" for d in taxonomy.diagnostics)


def test_prohibited_rejected(tmp_path):
    schema = _build(tmp_path, _linkbase(_arc(extra=' use="prohibited"')))
    taxonomy = load_taxonomy([schema])
    assert any(d.code == "unsupported-prohibited" for d in taxonomy.diagnostics)


def test_multiple_roles_rejected(tmp_path):
    schema = _build(tmp_path, _linkbase(_arc()))
    schema2 = tmp_path / "second.xsd"
    schema2.write_text(schema.read_text().replace("custom-calculation.xml", "second-calculation.xml"))
    (tmp_path / "second-calculation.xml").write_text(
        _linkbase(_arc("Total", "PartB"), role="urn:role:other")
    )
    # load both schemas -> two roles
    taxonomy = load_taxonomy([schema, schema2])
    assert any(d.code == "multiple-calculation-roles" for d in taxonomy.diagnostics)


def test_cycle_rejected(tmp_path):
    schema = _build(tmp_path, _linkbase(_arc("Total", "PartA"), _arc("PartA", "Total")))
    taxonomy = load_taxonomy([schema])
    assert any(d.code == "cyclic-calculation" for d in taxonomy.diagnostics)


def test_well_formed_linkbase_has_no_errors(tmp_path):
    schema = _build(tmp_path, _linkbase(_arc("Total", "PartA"), _arc("Total", "PartB")))
    taxonomy = load_taxonomy([schema])
    assert not taxonomy.has_errors
    assert len(taxonomy.calc_relations) == 1
    assert len(taxonomy.calc_relations[0].arcs) == 2
