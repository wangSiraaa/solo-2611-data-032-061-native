"""Contract tests: rejection semantics, determinism, CRS, output artifacts."""

from __future__ import annotations

import csv
import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from energyalloc import ValidationError, allocate, compute, explain_parcel, load_inputs  # noqa: E402
from energyalloc.grid import allocate_wh  # noqa: E402


PLAN = {
    "crs": "EPSG:3857",
    "coordinate_unit": "metre",
    "priority_rule": "higher numeric priority wins, then ascending zone_id",
    "precision_grid": 0.01,
    "area_tolerance_m2": 1e-05,
    "unassigned_id": "UNASSIGNED",
    "wh_remainder_tie_break": "ascending zone_id then canonical fragment geometry",
}


def rect(x0, y0, x1, y1):
    return {
        "type": "Polygon",
        "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]],
    }


def write_inputs(tmp_path, parcels, zones, plan=None, parcels_crs=None, zones_crs=None):
    plan_path = tmp_path / "plan.json"
    parcels_path = tmp_path / "parcels.geojson"
    zones_path = tmp_path / "zones.geojson"
    plan_path.write_text(json.dumps(plan or PLAN), encoding="utf-8")
    pdoc = {"type": "FeatureCollection", "features": parcels}
    zdoc = {"type": "FeatureCollection", "features": zones}
    if parcels_crs is not None:
        pdoc["crs"] = parcels_crs
    if zones_crs is not None:
        zdoc["crs"] = zones_crs
    parcels_path.write_text(json.dumps(pdoc), encoding="utf-8")
    zones_path.write_text(json.dumps(zdoc), encoding="utf-8")
    return plan_path, parcels_path, zones_path


def p(pid, energy, geom):
    return {
        "type": "Feature",
        "properties": {"parcel_id": pid, "energy_wh": energy},
        "geometry": geom,
    }


def z(zid, priority, geom):
    return {
        "type": "Feature",
        "properties": {"zone_id": zid, "priority": priority},
        "geometry": geom,
    }


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------

def test_duplicate_parcel_id_is_fatal(tmp_path):
    paths = write_inputs(
        tmp_path,
        [p("P", 10, rect(0, 0, 1, 1)), p("P", 20, rect(2, 2, 3, 3))],
        [z("A", 1, rect(0, 0, 3, 3))],
    )
    with pytest.raises(ValidationError) as excinfo:
        load_inputs(*paths)
    assert excinfo.value.code == "duplicate_parcel_id"


def test_duplicate_zone_id_is_fatal(tmp_path):
    paths = write_inputs(
        tmp_path,
        [p("P", 10, rect(0, 0, 1, 1))],
        [z("A", 1, rect(0, 0, 3, 3)), z("A", 2, rect(0, 0, 1, 1))],
    )
    with pytest.raises(ValidationError) as excinfo:
        load_inputs(*paths)
    assert excinfo.value.code == "duplicate_zone_id"


def test_crs_mismatch_is_fatal(tmp_path):
    paths = write_inputs(
        tmp_path,
        [p("P", 10, rect(0, 0, 1, 1))],
        [z("A", 1, rect(0, 0, 3, 3))],
        parcels_crs={"type": "name", "properties": {"name": "EPSG:4326"}},
    )
    with pytest.raises(ValidationError) as excinfo:
        load_inputs(*paths)
    assert excinfo.value.code == "crs_mismatch"


def test_matching_crs_spelling_accepted(tmp_path):
    paths = write_inputs(
        tmp_path,
        [p("P", 10, rect(0, 0, 1, 1))],
        [z("A", 1, rect(0, 0, 3, 3))],
        parcels_crs="urn:ogc:def:crs:EPSG::3857",
    )
    scenario = load_inputs(*paths)
    assert len(scenario.parcels) == 1


def test_invalid_polygon_rejected_not_repaired(tmp_path):
    bowtie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]],
    }
    paths = write_inputs(
        tmp_path,
        [p("BAD", 7, bowtie), p("OK", 3, rect(10, 10, 12, 12))],
        [z("A", 1, rect(0, 0, 20, 20))],
    )
    scenario = load_inputs(*paths)
    assert [x.parcel_id for x in scenario.parcels] == ["OK"]
    assert scenario.rejected_parcels[0]["parcel_id"] == "BAD"
    assert scenario.rejected_parcels[0]["code"] == "invalid_geometry"
    # Energy stays with the rejected parcel.
    assert scenario.rejected_parcels[0]["energy_wh_reported"] == 7
    result = compute(scenario)
    assert result.parcels[0].allocated_wh == 3
    assert result.stats["energy_reported_with_rejected_parcels_wh"] == 7


def test_null_geometry_rejected(tmp_path):
    paths = write_inputs(
        tmp_path,
        [
            {
                "type": "Feature",
                "properties": {"parcel_id": "N", "energy_wh": 1},
                "geometry": None,
            }
        ],
        [z("A", 1, rect(0, 0, 1, 1))],
    )
    scenario = load_inputs(*paths)
    assert scenario.rejected_parcels[0]["code"] == "invalid_geometry"


def test_zero_area_input_reported_and_not_reassigned(tmp_path):
    # An empty polygon is a valid-but-zero-area shape.  It is rejected and
    # reported separately, and its energy is never moved to another parcel.
    empty_poly = {"type": "Polygon", "coordinates": []}
    paths = write_inputs(
        tmp_path,
        [p("ZERO", 42, empty_poly), p("OK", 5, rect(0, 0, 2, 2))],
        [z("A", 1, rect(0, 0, 5, 5))],
    )
    scenario = load_inputs(*paths)
    codes = {r["parcel_id"]: r["code"] for r in scenario.rejected_parcels}
    assert codes["ZERO"] in ("empty_geometry", "zero_area")
    result = compute(scenario)
    assert result.parcels[0].parcel_id == "OK"
    assert result.stats["energy_reported_with_rejected_parcels_wh"] == 42
    assert result.stats["energy_allocated_wh"] == 5
    assert result.stats["energy_input_wh_accepted_parcels"] == 5


def test_tiny_positive_area_parcel_is_kept_and_allocated(tmp_path):
    # A valid 1 mm square has positive area, so it is a real parcel even
    # though it is far below the precision grid.  It is kept, not rejected.
    paths = write_inputs(
        tmp_path,
        [p("TINY", 3, rect(0, 0, 0.001, 0.001))],
        [z("A", 1, rect(0, 0, 1, 1))],
    )
    result = compute(load_inputs(*paths))
    tiny = result.parcels[0]
    assert tiny.area_m2 == pytest.approx(1e-6)
    assert tiny.allocated_wh == 3
    assert tiny.energy_checks["passed"]


def test_negative_and_fractional_energy_rejected(tmp_path):
    paths = write_inputs(
        tmp_path,
        [p("NEG", -1, rect(0, 0, 1, 1)), p("FRAC", 1.5, rect(2, 0, 3, 1))],
        [z("A", 1, rect(0, 0, 5, 5))],
    )
    scenario = load_inputs(*paths)
    codes = {r["parcel_id"]: r["code"] for r in scenario.rejected_parcels}
    assert codes == {"NEG": "negative_energy", "FRAC": "invalid_energy"}


def test_plan_unit_and_tolerance_validation(tmp_path):
    bad_plan = dict(PLAN, coordinate_unit="degree")
    paths = write_inputs(
        tmp_path, [p("P", 1, rect(0, 0, 1, 1))], [z("A", 1, rect(0, 0, 2, 2))],
        plan=bad_plan,
    )
    with pytest.raises(ValidationError) as excinfo:
        load_inputs(*paths)
    assert excinfo.value.code == "plan_unit"

    bad_plan2 = dict(PLAN, area_tolerance_m2=-1)
    paths = write_inputs(
        tmp_path, [p("P", 1, rect(0, 0, 1, 1))], [z("A", 1, rect(0, 0, 2, 2))],
        plan=bad_plan2,
    )
    with pytest.raises(ValidationError) as excinfo:
        load_inputs(*paths)
    assert excinfo.value.code == "plan_area_tolerance"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_input_order_does_not_change_result(tmp_path):
    parcels = [
        p("P1", 1001, rect(0, 0, 10, 10)),
        p("P2", 809, rect(10, 0, 20, 10)),
        p("P3", 997, rect(20, 0, 30, 10)),
    ]
    zones = [
        z("A", 20, rect(-1, -1, 7, 11)),
        z("B", 10, rect(4, -1, 14, 11)),
        z("C", 20, rect(6, 1, 12, 9)),
    ]
    a_dir = tmp_path / "a"
    a_dir.mkdir()
    base = compute(load_inputs(*write_inputs(a_dir, parcels, zones)))
    base_view = _view(base)

    rng = random.Random(12345)
    for i in range(5):
        p_shuffled = parcels[:]
        z_shuffled = zones[:]
        rng.shuffle(p_shuffled)
        rng.shuffle(z_shuffled)
        d = tmp_path / f"shuffle{i}"
        d.mkdir()
        shuffled = compute(load_inputs(*write_inputs(d, p_shuffled, z_shuffled)))
        assert _view(shuffled) == base_view


def _view(result):
    return [
        (
            r.parcel_id,
            r.allocated_wh,
            tuple(
                sorted(
                    (f.owner_id, f.geometry_key, f.wh) for f in r.fragments
                )
            ),
            tuple(sorted((c.zone_id, c.accepted, c.reason) for c in r.claims)),
        )
        for r in result.parcels
    ]


def test_remainder_tie_break_is_stable(tmp_path):
    parcels = [p("T", 2, rect(0, 0, 3, 1))]
    zones = [
        z("C", 10, rect(2, 0, 3, 1)),
        z("A", 10, rect(0, 0, 1, 1)),
        z("B", 10, rect(1, 0, 2, 1)),
    ]
    result = compute(load_inputs(*write_inputs(tmp_path, parcels, zones)))
    by_owner = {f.owner_id: f.wh for f in result.parcels[0].fragments}
    # Exact 2/3 each; two units awarded. Ascending zone_id: A then B.
    assert by_owner == {"A": 1, "B": 1, "C": 0}


# ---------------------------------------------------------------------------
# Integer apportionment unit tests
# ---------------------------------------------------------------------------

def test_allocate_wh_conserves_and_respects_ties():
    allocations, details = allocate_wh(1, [1, 1, 1], ["A", "B", "C"])
    assert allocations == [1, 0, 0]
    assert [d.rank for d in details] == [1, 2, 3]

    allocations, _ = allocate_wh(10, [1, 1, 1, 1], ["A", "B", "C", "D"])
    assert allocations == [3, 3, 2, 2]

    allocations, _ = allocate_wh(0, [1, 2, 3], ["A", "B", "C"])
    assert allocations == [0, 0, 0]

    allocations, _ = allocate_wh(7, [0, 0], ["A", "B"])
    assert allocations == [0, 0]


def test_allocate_wh_large_values():
    allocations, details = allocate_wh(
        10**12 + 1, [1, 2, 3], ["A", "B", "C"]
    )
    assert sum(allocations) == 10**12 + 1
    assert sum(d.floor_wh for d in details) + sum(
        d.remainder_wh for d in details
    ) == 10**12 + 1


# ---------------------------------------------------------------------------
# Output artifacts
# ---------------------------------------------------------------------------

def test_output_files_are_independently_readable(tmp_path):
    parcels = [p("P", 100, rect(0, 0, 10, 10))]
    zones = [z("A", 1, rect(0, 0, 4, 10)), z("B", 2, rect(2, 0, 10, 10))]
    scenario = load_inputs(*write_inputs(tmp_path, parcels, zones))
    out = tmp_path / "out"
    result = allocate(scenario, out)

    assert (out / "fragments.geojson").exists()
    assert (out / "allocation.csv").exists()
    assert (out / "summary.json").exists()
    assert (out / "evidence" / "parcel_P.json").exists()

    with (out / "fragments.geojson").open() as fh:
        doc = json.load(fh)
    assert doc["type"] == "FeatureCollection"
    assert len(doc["features"]) == len(result.fragments)
    for feature in doc["features"]:
        assert feature["geometry"]["type"] == "Polygon"
        assert {"fragment_id", "parcel_id", "wh", "area_m2"} <= set(
            feature["properties"]
        )

    with (out / "allocation.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(result.fragments)
    assert sum(int(r["wh"]) for r in rows) == 100

    summary = json.loads((out / "summary.json").read_text())
    assert summary["checks"]["passed"] is True
    assert summary["stats"]["candidate_pairs"] == 2


def test_explain_parcel_evidence_contents(tmp_path):
    parcels = [p("P", 100, rect(0, 0, 10, 10))]
    zones = [z("A", 1, rect(0, 0, 4, 10)), z("B", 2, rect(2, 0, 10, 10))]
    scenario = load_inputs(*write_inputs(tmp_path, parcels, zones))
    doc = explain_parcel(scenario, "P")

    assert doc["parcel_id"] == "P"
    assert doc["energy_wh"] == 100
    assert doc["original_geometry"]["type"] == "Polygon"
    assert {c["zone_id"] for c in doc["candidates"]} == {"A", "B"}
    # B has higher priority and adjudicates first.
    assert doc["adjudication"][0]["zone_id"] == "B"
    assert doc["adjudication"][0]["adjudication_rank"] == 1
    for frag in doc["fragments"]:
        assert frag["wh_source"]["total_wh"] == frag["wh"]
        assert frag["wh_source"]["exact_share"] > 0


def test_explain_unknown_and_rejected(tmp_path):
    bowtie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]],
    }
    scenario = load_inputs(
        *write_inputs(
            tmp_path,
            [p("BAD", 7, bowtie), p("OK", 3, rect(0, 0, 1, 1))],
            [z("A", 1, rect(0, 0, 2, 2))],
        )
    )
    rejected_doc = explain_parcel(scenario, "BAD")
    assert rejected_doc["status"] == "rejected"
    with pytest.raises(ValidationError) as excinfo:
        explain_parcel(scenario, "MISSING")
    assert excinfo.value.code == "unknown_parcel"


def test_geometry_and_energy_closure_are_separate(tmp_path):
    # Deliberately create a parcel with no covering zones: geometry must pass
    # (UNASSIGNED fragment) and energy must pass through that fragment.
    parcels = [p("P", 17, rect(0, 0, 3, 2))]
    scenario = load_inputs(*write_inputs(tmp_path, parcels, []))
    result = compute(scenario)
    only = result.parcels[0]
    assert only.geometry_checks["passed"]
    assert only.energy_checks["passed"]
    assert len(only.fragments) == 1
    assert only.fragments[0].kind == "unassigned"
    assert only.fragments[0].wh == 17
    assert only.unassigned_area_m2 == pytest.approx(6.0)


def test_multi_polygon_and_holes_preserved(tmp_path):
    with_hole = {
        "type": "Polygon",
        "coordinates": [
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
            [[3, 3], [3, 7], [7, 7], [7, 3], [3, 3]],
        ],
    }
    multipoly = {
        "type": "MultiPolygon",
        "coordinates": [
            rect(0, 0, 2, 2)["coordinates"],
            rect(5, 0, 7, 2)["coordinates"],
        ],
    }
    parcels = [p("HOLE", 64, with_hole), p("MULTI", 8, multipoly)]
    zones = [
        # B takes a left strip; A covers the rest, keeping the hole interior.
        z("A", 1, rect(0, 0, 10, 10)),
        z("B", 2, rect(0, 0, 2, 10)),
    ]
    result = compute(load_inputs(*write_inputs(tmp_path, parcels, zones)))
    hole = next(r for r in result.parcels if r.parcel_id == "HOLE")
    assert hole.geometry_checks["passed"]
    assert hole.area_m2 == pytest.approx(100 - 16)
    a_frag = next(f for f in hole.fragments if f.owner_id == "A")
    assert len(a_frag.geometry.interiors) == 1

    multi = next(r for r in result.parcels if r.parcel_id == "MULTI")
    assert multi.geometry_checks["passed"]
    assert sum(f.area_m2 for f in multi.fragments) == pytest.approx(8.0)
    assert len(multi.fragments) == 2


def test_line_and_point_contact_produce_no_area(tmp_path):
    parcels = [p("P", 10, rect(0, 0, 10, 10))]
    zones = [
        # shares only the x=10 edge -> line contact, no area
        z("EDGE", 10, rect(10, 0, 20, 10)),
        # touches only the corner (10,10) -> point contact, no area
        z("CORNER", 10, rect(10, 10, 20, 20)),
    ]
    result = compute(load_inputs(*write_inputs(tmp_path, parcels, zones)))
    only = result.parcels[0]
    assert only.status == "ok"
    reasons = {c.zone_id: c.reason for c in only.claims}
    assert reasons["EDGE"] == "line_or_point_contact_only"
    assert reasons["CORNER"] == "line_or_point_contact_only"
    assert [f.kind for f in only.fragments] == ["unassigned"]
    assert only.fragments[0].wh == 10
    assert only.geometry_checks["passed"]


def test_polygon_intersection_is_not_normalized(tmp_path):
    # A covers 0..6, B covers 4..10 in a 10x10 parcel.  Independent
    # intersection-and-normalize would give A=B=50.  Adjudication must give
    # A=60 and B=40, so the 20 m2 double-covered strip is counted once.
    parcels = [p("P", 100, rect(0, 0, 10, 10))]
    zones = [z("A", 10, rect(0, 0, 6, 10)), z("B", 5, rect(4, 0, 10, 10))]
    result = compute(load_inputs(*write_inputs(tmp_path, parcels, zones)))
    areas = {f.owner_id: f.area_m2 for f in result.parcels[0].fragments}
    assert areas == {"A": 60.0, "B": 40.0}
    assert sum(areas.values()) == 100.0
