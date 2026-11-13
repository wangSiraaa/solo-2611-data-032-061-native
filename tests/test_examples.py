"""End-to-end tests against the hand-computable rectangular examples.

Every expected number here was computed by hand from rectangle areas; the
tests are independent of the allocation code's own arithmetic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from energyalloc import compute, load_inputs  # noqa: E402
from cases import CASES  # noqa: E402


def _write_case(tmp_path: Path, case: dict) -> tuple[Path, Path, Path]:
    plan = tmp_path / "plan.json"
    parcels = tmp_path / "parcels.geojson"
    zones = tmp_path / "zones.geojson"
    plan.write_text(json.dumps(case["plan"]), encoding="utf-8")
    parcels.write_text(
        json.dumps({"type": "FeatureCollection", "features": case["parcels"]}),
        encoding="utf-8",
    )
    zones.write_text(
        json.dumps({"type": "FeatureCollection", "features": case["zones"]}),
        encoding="utf-8",
    )
    return plan, parcels, zones


@pytest.fixture(params=[c["name"] for c in CASES])
def case(request):
    return next(c for c in CASES if c["name"] == request.param)


def test_example_expected_fragments(case, tmp_path):
    plan, parcels, zones = _write_case(tmp_path, case)
    result = compute(load_inputs(plan, parcels, zones))
    expected = case["expected"]

    accepted = [p.parcel_id for p in result.parcels]
    assert accepted == expected["accepted"]

    for parcel_result in result.parcels:
        assert parcel_result.geometry_checks["passed"], parcel_result.notes
        assert parcel_result.energy_checks["passed"]
        assert parcel_result.allocated_wh == parcel_result.energy_wh == expected["energy_wh"]

        got = sorted(
            (
                f["owner_id"],
                round(f["area_m2"], 9),
                f["wh"],
            )
            for f in (
                {
                    "owner_id": frag.owner_id,
                    "area_m2": frag.area_m2,
                    "wh": frag.wh,
                }
                for frag in parcel_result.fragments
            )
        )
        want = sorted(
            (f["owner_id"], round(f["area_m2"], 9), f["wh"])
            for f in expected["fragments"]
        )
        assert got == want, (parcel_result.parcel_id, got, want)

        assert parcel_result.unassigned_area_m2 == pytest.approx(
            expected["unassigned_area_m2"], abs=1e-9
        )


def test_example_specifics(case, tmp_path):
    plan, parcels, zones = _write_case(tmp_path, case)
    result = compute(load_inputs(plan, parcels, zones))
    expected = case["expected"]
    name = case["name"]

    if name == "02_equal_priority":
        # Z must still be recorded as preempted evidence.
        p = result.parcels[0]
        z = next(c for c in p.claims if c.zone_id == "Z")
        assert z.accepted is False
        assert z.reason == "fully_preempted_by_higher_priority"
        assert z.preempted_area_m2 == pytest.approx(expected["preempted"]["Z"])

    if name == "03_hole":
        p = result.parcels[0]
        a_frag = next(f for f in p.fragments if f.owner_id == "A")
        # The 10x10 hole must survive inside A's fragment as an interior ring.
        assert len(a_frag.geometry.interiors) == expected["interior_rings"]["A"]

    if name == "05_remainder_tie":
        p = result.parcels[0]
        by_owner = {f.owner_id: f for f in p.fragments}
        assert by_owner["A"].share.rank == 1
        assert by_owner["B"].share.rank == 2
        assert by_owner["C"].share.rank == 3
        assert by_owner["A"].wh == 1 and by_owner["B"].wh == 0 and by_owner["C"].wh == 0

    if name == "07_tiny_fragment":
        p = result.parcels[0]
        tiny = [f for f in p.fragments if f.tiny]
        assert len(tiny) == 1
        assert tiny[0].owner_id == "B"
        assert tiny[0].area_m2 == pytest.approx(1e-4, rel=1e-9)
        assert tiny[0].wh == 1

    if name == "08_candidate_filter":
        assert result.stats["candidate_pairs"] == expected["candidate_pairs"]
        assert (
            result.stats["zone_parcel_pairs_total"]
            == expected["zone_parcel_pairs_total"]
        )
        assert result.stats["pairs_avoided_by_spatial_filter"] == 2

    if name == "09_rejections":
        codes = {
            r["parcel_id"]: r["code"] for r in result.rejected_parcels
        }
        for pid, code in expected["rejected"].items():
            assert codes[pid] == code, (pid, codes.get(pid))
        # Rejected energy is reported with the parcel, not reassigned.
        assert result.stats["energy_reported_with_rejected_parcels_wh"] == 3
        assert result.stats["energy_allocated_wh"] == 10
