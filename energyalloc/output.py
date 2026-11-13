"""Output writers: fragment GeoJSON, integer allocation CSV, evidence JSON.

All files are plain GeoJSON / CSV / JSON and are readable by independent
consumers without importing this package.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .engine import AllocationResult, Fragment, ParcelResult
from .model import Scenario

FRAGMENT_COLUMNS = [
    "parcel_id",
    "fragment_id",
    "owner_id",
    "zone_id",
    "kind",
    "priority",
    "adjudication_rank",
    "area_m2",
    "wh",
    "parcel_energy_wh",
    "tiny_fragment",
    "geometry_key",
]

ALLOCATION_COLUMNS = [
    "parcel_id",
    "fragment_id",
    "owner_id",
    "zone_id",
    "kind",
    "priority",
    "adjudication_rank",
    "area_m2",
    "wh",
    "parcel_energy_wh",
    "exact_share",
    "fractional_remainder",
    "remainder_rank",
    "tiny_fragment",
]


def _coords(geom) -> Any:
    """Raw GeoJSON coordinate arrays; floats round-trip exactly in JSON."""
    return geom.__geo_interface__["coordinates"]


def fragment_feature(frag: Fragment, parcel_energy_wh: int) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {
            "fragment_id": frag.fragment_id,
            "parcel_id": frag.parcel_id,
            "owner_id": frag.owner_id,
            "zone_id": frag.zone_id,
            "kind": frag.kind,
            "priority": frag.priority,
            "adjudication_rank": frag.adjudication_rank,
            "area_m2": frag.area_m2,
            "wh": frag.wh,
            "parcel_energy_wh": parcel_energy_wh,
            "tiny_fragment": frag.tiny,
            "geometry_key": frag.geometry_key,
        },
        "geometry": {
            "type": frag.geometry.geom_type,
            "coordinates": _coords(frag.geometry),
        },
    }


def write_fragments_geojson(result: AllocationResult, path: Path) -> None:
    features = []
    for p in result.parcels:
        for frag in p.fragments:
            features.append(fragment_feature(frag, p.energy_wh))
    doc = {
        "type": "FeatureCollection",
        "name": "energy_allocation_fragments",
        "crs_declaration": {
            "crs": result.scenario.plan.crs,
            "coordinate_unit": result.scenario.plan.coordinate_unit,
            "note": "GeoJSON has no CRS member; coordinates are in the CRS "
            "declared by plan.json and repeated here for consumers.",
        },
        "features": features,
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def write_allocation_csv(result: AllocationResult, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=ALLOCATION_COLUMNS)
        writer.writeheader()
        for p in result.parcels:
            for frag in p.fragments:
                writer.writerow(
                    {
                        "parcel_id": frag.parcel_id,
                        "fragment_id": frag.fragment_id,
                        "owner_id": frag.owner_id,
                        "zone_id": frag.zone_id if frag.zone_id is not None else "",
                        "kind": frag.kind,
                        "priority": (
                            frag.priority if frag.priority is not None else ""
                        ),
                        "adjudication_rank": (
                            frag.adjudication_rank
                            if frag.adjudication_rank is not None
                            else ""
                        ),
                        "area_m2": repr(frag.area_m2),
                        "wh": frag.wh,
                        "parcel_energy_wh": p.energy_wh,
                        "exact_share": (
                            repr(float(frag.share.exact_share))
                            if frag.share
                            else ""
                        ),
                        "fractional_remainder": (
                            repr(float(frag.share.remainder_fraction))
                            if frag.share
                            else ""
                        ),
                        "remainder_rank": (
                            frag.share.rank if frag.share and frag.share.rank is not None else ""
                        ),
                        "tiny_fragment": frag.tiny,
                    }
                )


def parcel_evidence(
    result: AllocationResult, parcel_result: ParcelResult
) -> dict[str, Any]:
    scenario = result.scenario
    parcel = scenario.parcel_by_id(parcel_result.parcel_id)
    if parcel is None:  # pragma: no cover - rejected parcels have no result
        raise KeyError(parcel_result.parcel_id)
    zone_lookup = {z.zone_id: z for z in scenario.zones}

    candidates = []
    for zone_id in parcel_result.candidates:
        zone = zone_lookup[zone_id]
        claim = next(
            (c for c in parcel_result.claims if c.zone_id == zone_id), None
        )
        candidates.append(
            {
                "zone_id": zone.zone_id,
                "priority": zone.priority,
                "adjudication_rank": claim.adjudication_rank if claim else None,
                "raw_intersection_area_m2": (
                    claim.raw_intersection_area_m2 if claim else 0.0
                ),
                "preempted_area_m2": claim.preempted_area_m2 if claim else 0.0,
                "net_fragment_area_m2": (
                    claim.net_fragment_area_m2 if claim else 0.0
                ),
                "accepted": claim.accepted if claim else False,
                "reason": claim.reason if claim else "not_evaluated",
            }
        )

    adjudication = []
    for claim in sorted(parcel_result.claims, key=lambda c: c.adjudication_rank):
        adjudication.append(claim.as_dict())

    fragments = []
    for frag in parcel_result.fragments:
        fragments.append(
            {
                "fragment_id": frag.fragment_id,
                "owner_id": frag.owner_id,
                "kind": frag.kind,
                "zone_id": frag.zone_id,
                "priority": frag.priority,
                "adjudication_rank": frag.adjudication_rank,
                "geometry": {
                    "type": frag.geometry.geom_type,
                    "coordinates": _coords(frag.geometry),
                },
                "geometry_key": frag.geometry_key,
                "area_m2": frag.area_m2,
                "tiny_fragment": frag.tiny,
                "wh": frag.wh,
                "wh_source": frag.share.as_dict() if frag.share else None,
            }
        )

    return {
        "parcel_id": parcel_result.parcel_id,
        "status": parcel_result.status,
        "energy_wh": parcel_result.energy_wh,
        "original_geometry": {
            "type": parcel.geometry.geom_type,
            "coordinates": _coords(parcel.geometry),
            "note": "source geometry, used unmodified for all area and allocation maths",
        },
        "area_m2": parcel_result.area_m2,
        "candidate_zone_count": len(parcel_result.candidates),
        "candidates": candidates,
        "adjudication": adjudication,
        "fragments": fragments,
        "unassigned_area_m2": parcel_result.unassigned_area_m2,
        "allocated_wh": parcel_result.allocated_wh,
        "unallocated_wh": parcel_result.unallocated_wh,
        "geometry_checks": parcel_result.geometry_checks,
        "energy_checks": parcel_result.energy_checks,
        "notes": parcel_result.notes,
        "wh_rule": {
            "method": "largest remainder (Hamilton) over fragment area",
            "tie_break": scenario.plan.wh_remainder_tie_break,
            "exact_arithmetic": "fragment areas and quotas as exact rationals",
        },
    }


def rejected_evidence(scenario: Scenario) -> list[dict[str, Any]]:
    return list(scenario.rejected_parcels)


def write_evidence(
    result: AllocationResult, outdir: Path
) -> dict[str, Any]:
    evidence_dir = outdir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for p in result.parcels:
        doc = parcel_evidence(result, p)
        fname = f"parcel_{_safe_name(p.parcel_id)}.json"
        (evidence_dir / fname).write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        index.append(
            {
                "parcel_id": p.parcel_id,
                "status": p.status,
                "file": f"evidence/{fname}",
                "fragments": len(p.fragments),
                "energy_wh": p.energy_wh,
                "allocated_wh": p.allocated_wh,
            }
        )
    for rejected in result.rejected_parcels:
        pid = rejected.get("parcel_id") or f"index_{rejected.get('source_index')}"
        fname = f"rejected_{_safe_name(str(pid))}.json"
        (evidence_dir / fname).write_text(
            json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        index.append(
            {
                "parcel_id": pid,
                "status": "rejected",
                "file": f"evidence/{fname}",
                "code": rejected.get("code"),
                "energy_wh_reported": rejected.get("energy_wh_reported"),
            }
        )
    return {"parcels": index}


def _safe_name(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in value)


def write_summary(result: AllocationResult, path: Path) -> None:
    plan = result.scenario.plan
    summary = {
        "plan": {
            "crs": plan.crs,
            "coordinate_unit": plan.coordinate_unit,
            "priority_rule": plan.priority_rule,
            "precision_grid": plan.precision_grid,
            "area_tolerance_m2": plan.area_tolerance_m2,
            "unassigned_id": plan.unassigned_id,
            "wh_remainder_tie_break": plan.wh_remainder_tie_break,
        },
        "stats": result.stats,
        "checks": result.checks,
        "rejected_parcels": result.rejected_parcels,
        "zero_area_parcels": [
            {
                "parcel_id": p.parcel_id,
                "energy_wh": p.energy_wh,
                "unallocated_wh": p.unallocated_wh,
                "notes": p.notes,
            }
            for p in result.parcels
            if p.status == "zero_area"
        ],
        "precision_boundary": {
            "grid": plan.precision_grid,
            "area_tolerance_m2": plan.area_tolerance_m2,
            "tiny_threshold_m2": max(
                plan.area_tolerance_m2, plan.precision_grid ** 2
            ),
            "statement": (
                "Source coordinates are used unmodified: no snapping, repair, "
                "or reprojection. precision_grid sets the canonical-key "
                "rounding and, together with area_tolerance_m2, the "
                "tiny-fragment flag threshold max(area_tolerance, grid^2). "
                "Fragments at or below that threshold are kept and allocated "
                "their exact area share; they are never dropped. Closure "
                "checks use the unmodified source geometry."
            ),
        },
        "precision_input_report": result.scenario.precision_report,
    }
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def write_all(result: AllocationResult, outdir: str | Path) -> Path:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    write_fragments_geojson(result, out / "fragments.geojson")
    write_allocation_csv(result, out / "allocation.csv")
    write_evidence(result, out)
    write_summary(result, out / "summary.json")
    return out
