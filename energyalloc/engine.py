"""Allocation engine: adjudicate coverage, tile parcels, apportion Wh.

Core invariant: for every accepted parcel, the accepted zone fragments and the
UNASSIGNED fragment form a pairwise-disjoint tiling of the parcel (up to the
declared area tolerance), and their integer Wh sum to the parcel's energy.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from shapely import make_valid
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from .grid import ShareDetail, allocate_wh, canonical_key
from .model import Parcel, Scenario, Zone

POLYGON_TYPES = (Polygon, MultiPolygon)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _polygonal(geom: BaseGeometry) -> BaseGeometry:
    """Keep only polygonal components; lines and points carry no area."""
    if geom.is_empty:
        return MultiPolygon()
    if isinstance(geom, Polygon):
        return geom
    if isinstance(geom, MultiPolygon):
        return geom
    if geom.geom_type == "GeometryCollection":
        parts: list[Polygon] = []
        for g in geom.geoms:
            if isinstance(g, Polygon) and not g.is_empty:
                parts.append(g)
            elif isinstance(g, MultiPolygon):
                parts.extend(p for p in g.geoms if not p.is_empty)
        if not parts:
            return MultiPolygon()
        return parts[0] if len(parts) == 1 else MultiPolygon(parts)
    return MultiPolygon()


def _as_fragment(geom: BaseGeometry) -> Polygon | MultiPolygon | None:
    geom = _polygonal(geom)
    if geom.is_empty:
        return None
    if isinstance(geom, POLYGON_TYPES):
        return geom
    return None


def _robust(op: str, a: BaseGeometry, b: BaseGeometry) -> BaseGeometry:
    """Apply an overlay op; make_valid only on the *result*, never on inputs."""
    if op == "intersection":
        result = a.intersection(b)
    elif op == "difference":
        result = a.difference(b)
    elif op == "union":
        result = a.union(b)
    else:  # pragma: no cover - programming error
        raise ValueError(op)
    if not result.is_valid:
        result = make_valid(result)
    return result


def _atoms(geom: Polygon | MultiPolygon | None) -> list[Polygon]:
    """Split a fragment geometry into connected polygonal atoms.

    Connected pieces are kept as separate fragments so that a tiny island is
    visible in the output rather than hidden inside one MultiPolygon.  Holes
    stay with their containing polygon.
    """
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom] if geom.area > 0 else []
    atoms: list[Polygon] = []
    for part in geom.geoms:
        if isinstance(part, Polygon) and part.area > 0:
            atoms.append(part)
    return atoms


def _tiny_threshold(tol: float, grid: float) -> float:
    """Area at or below which a fragment is flagged ``tiny_fragment``.

    A fragment cannot be smaller than roughly one precision-grid cell
    (``grid ** 2``).  Using ``max(area_tolerance, grid ** 2)`` therefore
    catches both noise-sized and single-cell fragments.  Flagging never
    removes a fragment: tiny pieces are retained and allocated their exact
    share, as required.
    """
    return max(tol, grid * grid)


def _is_tiny(area: float, threshold: float) -> bool:
    # Overlay arithmetic can return a cell area a few ulps above grid**2, so
    # compare with a relative slack large enough for double rounding and far
    # smaller than any real geometric distinction.
    return area <= threshold * (1.0 + 1e-9) + 1e-15


def _stable_fragment_id(parcel_id: str, owner_id: str, geom_key: str) -> str:
    digest = hashlib.sha256(
        f"{parcel_id}\x1f{owner_id}\x1f{geom_key}".encode("utf-8")
    ).hexdigest()[:16]
    return f"F-{digest}"


def _union_area(geoms: Iterable[BaseGeometry]) -> float:
    from shapely import union_all

    listed = [g for g in geoms if not g.is_empty]
    if not listed:
        return 0.0
    return float(union_all(listed).area)


def _pairwise_overlap_area(geoms: list[BaseGeometry]) -> float:
    if len(geoms) < 2:
        return 0.0
    tree = STRtree(geoms)
    total = 0.0
    for i, g in enumerate(geoms):
        for j in tree.query(g):
            j = int(j)
            if j <= i:
                continue
            inter = g.intersection(geoms[j])
            if not inter.is_empty:
                total += float(_polygonal(inter).area)
    return total


# ---------------------------------------------------------------------------
# Result structures
# ---------------------------------------------------------------------------

@dataclass
class ClaimEvidence:
    zone_id: str
    priority: int
    adjudication_rank: int
    raw_intersection_area_m2: float
    preempted_area_m2: float
    net_fragment_area_m2: float
    accepted: bool
    reason: str
    fragment_count: int = 0
    exact_intersection_computed: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "priority": self.priority,
            "adjudication_rank": self.adjudication_rank,
            "raw_intersection_area_m2": self.raw_intersection_area_m2,
            "preempted_area_m2": self.preempted_area_m2,
            "net_fragment_area_m2": self.net_fragment_area_m2,
            "accepted": self.accepted,
            "reason": self.reason,
            "fragment_count": self.fragment_count,
            "exact_intersection_computed": self.exact_intersection_computed,
        }


@dataclass
class Fragment:
    fragment_id: str
    parcel_id: str
    owner_id: str
    zone_id: str | None
    priority: int | None
    kind: str  # "zone" | "unassigned"
    geometry: Polygon
    geometry_key: str
    area_m2: float
    adjudication_rank: int | None
    wh: int = 0
    share: ShareDetail | None = None
    tiny: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "fragment_id": self.fragment_id,
            "parcel_id": self.parcel_id,
            "owner_id": self.owner_id,
            "zone_id": self.zone_id,
            "priority": self.priority,
            "kind": self.kind,
            "geometry_key": self.geometry_key,
            "area_m2": self.area_m2,
            "adjudication_rank": self.adjudication_rank,
            "wh": self.wh,
            "tiny_fragment": self.tiny,
            "wh_share": self.share.as_dict() if self.share else None,
        }


@dataclass
class ParcelResult:
    parcel_id: str
    energy_wh: int
    status: str
    area_m2: float
    candidates: list[str]
    claims: list[ClaimEvidence]
    fragments: list[Fragment]
    unassigned_area_m2: float
    allocated_wh: int
    unallocated_wh: int
    geometry_checks: dict[str, Any]
    energy_checks: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.status == "ok"
            and self.geometry_checks.get("passed", False)
            and self.energy_checks.get("passed", False)
        )


@dataclass
class AllocationResult:
    scenario: Scenario
    parcels: list[ParcelResult]
    rejected_parcels: list[dict[str, Any]]
    stats: dict[str, Any]
    checks: dict[str, Any]

    @property
    def fragments(self) -> list[Fragment]:
        return [f for p in self.parcels for f in p.fragments]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ZoneIndex:
    def __init__(self, zones: Iterable[Zone]):
        # Adjudication order: higher numeric priority first, then zone_id.
        self.zones = sorted(zones, key=lambda z: z.sort_key)
        self.geoms = [z.geometry for z in self.zones]
        self.tree = STRtree(self.geoms) if self.geoms else None

    def candidates_for(self, geom: BaseGeometry) -> list[Zone]:
        if self.tree is None:
            return []
        idx = {int(i) for i in self.tree.query(geom)}
        return [z for i, z in enumerate(self.zones) if i in idx]


def _compute_parcel(
    scenario: Scenario,
    parcel: Parcel,
    zone_index: ZoneIndex,
) -> ParcelResult:
    plan = scenario.plan
    tol = plan.area_tolerance_m2
    grid = plan.grid
    unassigned_id = plan.unassigned_id
    geometry = parcel.geometry
    source_area = float(geometry.area)
    notes: list[str] = []

    candidates = zone_index.candidates_for(geometry)
    candidate_ids = [z.zone_id for z in candidates]

    if source_area <= 0.0:
        return ParcelResult(
            parcel_id=parcel.parcel_id,
            energy_wh=parcel.energy_wh,
            status="zero_area",
            area_m2=source_area,
            candidates=candidate_ids,
            claims=[],
            fragments=[],
            unassigned_area_m2=0.0,
            allocated_wh=0,
            unallocated_wh=parcel.energy_wh,
            geometry_checks={
                "fragments_disjoint": True,
                "disjoint_overlap_area_m2": 0.0,
                "union_vs_parcel_area_m2": 0.0,
                "symmetric_difference_area_m2": 0.0,
                "area_closure_error_m2": abs(source_area),
                "passed": False,
                "reason": "parcel has zero area; no fragments can be built",
            },
            energy_checks={
                "sum_fragment_wh": 0,
                "energy_wh": parcel.energy_wh,
                "difference": parcel.energy_wh,
                "passed": parcel.energy_wh == 0,
                "reason": "energy left unallocated; not transferred to neighbours",
            },
            notes=[
                "parcel has zero area; reported "
                "separately and its energy is not reassigned"
            ],
        )

    claims: list[ClaimEvidence] = []
    raw_fragments: list[tuple[Zone, Polygon | MultiPolygon]] = []
    taken: BaseGeometry = MultiPolygon()
    exact_intersections = 0
    tiny_threshold = _tiny_threshold(tol, grid)

    for rank, zone in enumerate(candidates, start=1):
        raw = _polygonal(_robust("intersection", geometry, zone.geometry))
        exact_intersections += 1
        if raw.is_empty or raw.area <= 0.0:
            # Either the envelope overlapped without the geometries meeting,
            # or they meet only along lines/points.  Neither produces area, so
            # the zone cannot claim anything; the reason is recorded for
            # auditability but no fragment is created.
            reason = (
                "line_or_point_contact_only"
                if geometry.intersects(zone.geometry)
                else "envelope_candidate_no_contact"
            )
            claims.append(
                ClaimEvidence(
                    zone_id=zone.zone_id,
                    priority=zone.priority,
                    adjudication_rank=rank,
                    raw_intersection_area_m2=0.0,
                    preempted_area_m2=0.0,
                    net_fragment_area_m2=0.0,
                    accepted=False,
                    reason=reason,
                )
            )
            continue

        raw_area = float(raw.area)
        preempted = (
            _polygonal(_robust("intersection", raw, taken))
            if not taken.is_empty
            else MultiPolygon()
        )
        preempted_area = float(preempted.area)
        net = _as_fragment(_robust("difference", raw, taken)) if not taken.is_empty else raw
        atoms = _atoms(net)
        if not atoms:
            claims.append(
                ClaimEvidence(
                    zone_id=zone.zone_id,
                    priority=zone.priority,
                    adjudication_rank=rank,
                    raw_intersection_area_m2=raw_area,
                    preempted_area_m2=preempted_area,
                    net_fragment_area_m2=0.0,
                    accepted=False,
                    reason="fully_preempted_by_higher_priority",
                )
            )
        else:
            net_area = sum(a.area for a in atoms)
            reason = "claim"
            if any(_is_tiny(float(a.area), tiny_threshold) for a in atoms):
                reason = "claim_including_tiny_fragment"
                notes.append(
                    f"zone {zone.zone_id} retains a fragment of area "
                    f"{min(a.area for a in atoms):.6g} m2, at or below the "
                    f"tiny-fragment threshold {tiny_threshold:g} m2 "
                    f"(max(area_tolerance, grid^2)); it is kept, flagged "
                    "tiny_fragment, and allocated its exact area share rather "
                    "than dropped"
                )
            claims.append(
                ClaimEvidence(
                    zone_id=zone.zone_id,
                    priority=zone.priority,
                    adjudication_rank=rank,
                    raw_intersection_area_m2=raw_area,
                    preempted_area_m2=preempted_area,
                    net_fragment_area_m2=float(net_area),
                    accepted=True,
                    reason=reason,
                    fragment_count=len(atoms),
                )
            )
            for atom in atoms:
                raw_fragments.append((zone, atom))

        # Accumulate every raw zone capture, so UNASSIGNED is exactly the part
        # of the parcel that no zone (including preempted ones) touched.
        taken = raw if taken.is_empty else _robust("union", taken, raw)

    unassigned_geom = (
        _as_fragment(_robust("difference", geometry, taken))
        if not taken.is_empty
        else _as_fragment(geometry)
    )
    unassigned_atoms = _atoms(unassigned_geom)

    # Build stable fragment records.  Ordering is canonical, not input order.
    fragment_records: list[Fragment] = []
    for zone, atom in raw_fragments:
        key = canonical_key(atom, grid)
        fragment_records.append(
            Fragment(
                fragment_id=_stable_fragment_id(parcel.parcel_id, zone.zone_id, key),
                parcel_id=parcel.parcel_id,
                owner_id=zone.zone_id,
                zone_id=zone.zone_id,
                priority=zone.priority,
                kind="zone",
                geometry=atom,
                geometry_key=key,
                area_m2=float(atom.area),
                adjudication_rank=next(
                    c.adjudication_rank
                    for c in claims
                    if c.zone_id == zone.zone_id
                ),
                tiny=_is_tiny(float(atom.area), tiny_threshold),
            )
        )
    for atom in unassigned_atoms:
        key = canonical_key(atom, grid)
        fragment_records.append(
            Fragment(
                fragment_id=_stable_fragment_id(parcel.parcel_id, unassigned_id, key),
                parcel_id=parcel.parcel_id,
                owner_id=unassigned_id,
                zone_id=None,
                priority=None,
                kind="unassigned",
                geometry=atom,
                geometry_key=key,
                area_m2=float(atom.area),
                adjudication_rank=None,
                tiny=_is_tiny(float(atom.area), tiny_threshold),
            )
        )

    fragment_records.sort(
        key=lambda f: (f.owner_id, f.geometry_key, f.fragment_id)
    )

    if fragment_records:
        allocations, details = allocate_wh(
            parcel.energy_wh,
            [f.area_m2 for f in fragment_records],
            [(f.owner_id, f.geometry_key) for f in fragment_records],
        )
        for frag, wh, detail in zip(fragment_records, allocations, details):
            frag.wh = wh
            frag.share = detail
    else:
        allocations = []

    allocated_wh = sum(f.wh for f in fragment_records)
    unassigned_area = float(sum(a.area for a in unassigned_atoms))
    if unassigned_area > 0:
        notes.append(
            f"{unassigned_area:.9f} m2 ({unassigned_area / source_area:.4%}) "
            f"of the parcel is not covered by any candidate zone and is "
            f"labelled {unassigned_id}; it is retained as a fragment and "
            "receives the area-proportional share of energy"
        )

    # ---- independent geometric checks -------------------------------------
    geom_list = [f.geometry for f in fragment_records]
    sum_fragment_area = float(sum(f.area_m2 for f in fragment_records))
    overlap_area = _pairwise_overlap_area(geom_list)
    union_area = _union_area(geom_list)
    from shapely import union_all

    union_geom = union_all(geom_list) if geom_list else MultiPolygon()
    sym_diff = float(union_geom.symmetric_difference(geometry).area) if geom_list else source_area
    area_closure_error = abs(sum_fragment_area - source_area)
    disjoint_ok = overlap_area <= tol
    coverage_ok = sym_diff <= tol
    area_ok = area_closure_error <= tol
    geometry_passed = disjoint_ok and coverage_ok and area_ok
    geometry_checks = {
        "fragments_disjoint": disjoint_ok,
        "disjoint_overlap_area_m2": overlap_area,
        "union_area_m2": union_area,
        "sum_fragment_area_m2": sum_fragment_area,
        "parcel_area_m2": source_area,
        "union_vs_parcel_area_m2": abs(union_area - source_area),
        "symmetric_difference_area_m2": sym_diff,
        "area_closure_error_m2": area_closure_error,
        "tolerance_m2": tol,
        "passed": geometry_passed,
    }
    if not geometry_passed:
        notes.append(
            f"geometry closure check failed: overlap={overlap_area:.3e} m2, "
            f"symmetric difference={sym_diff:.3e} m2, area error="
            f"{area_closure_error:.3e} m2 against tolerance {tol:g} m2"
        )

    energy_passed = allocated_wh == parcel.energy_wh
    energy_checks = {
        "energy_wh": parcel.energy_wh,
        "sum_fragment_wh": allocated_wh,
        "difference": allocated_wh - parcel.energy_wh,
        "fragment_count": len(fragment_records),
        "passed": energy_passed,
    }
    if not energy_passed:  # pragma: no cover - exact arithmetic guarantees this
        notes.append(
            "energy closure check failed: fragments do not sum to parcel energy"
        )

    status = "ok"
    if not geometry_passed:
        status = "geometry_check_failed"
    elif not energy_passed:
        status = "energy_check_failed"

    return ParcelResult(
        parcel_id=parcel.parcel_id,
        energy_wh=parcel.energy_wh,
        status=status,
        area_m2=source_area,
        candidates=candidate_ids,
        claims=claims,
        fragments=fragment_records,
        unassigned_area_m2=unassigned_area,
        allocated_wh=allocated_wh,
        unallocated_wh=parcel.energy_wh - allocated_wh,
        geometry_checks=geometry_checks,
        energy_checks=energy_checks,
        notes=notes,
    )


def compute(scenario: Scenario) -> AllocationResult:
    zone_index = ZoneIndex(scenario.zones)
    parcel_results = [
        _compute_parcel(scenario, p, zone_index) for p in scenario.parcels
    ]
    # Canonical output order independent of input order.
    parcel_results.sort(key=lambda p: p.parcel_id)

    candidate_pairs = sum(len(p.candidates) for p in parcel_results)
    exact_intersections = sum(
        sum(1 for c in p.claims if c.exact_intersection_computed)
        for p in parcel_results
    )
    total_pairs = len(parcel_results) * len(scenario.zones)
    fragment_count = sum(len(p.fragments) for p in parcel_results)
    total_energy = sum(p.energy_wh for p in parcel_results)
    allocated_energy = sum(p.allocated_wh for p in parcel_results)
    rejected_energy = sum(
        r.get("energy_wh_reported") or 0 for r in scenario.rejected_parcels
    )
    zero_area_results = [p for p in parcel_results if p.status == "zero_area"]
    zero_area_rejected = [
        r for r in scenario.rejected_parcels
        if r.get("code") in ("zero_area", "empty_geometry")
    ]
    zero_area_ids = sorted(
        {p.parcel_id for p in zero_area_results}
        | {r.get("parcel_id") for r in zero_area_rejected if r.get("parcel_id")}
    )
    unallocated_zero_area_wh = sum(
        p.unallocated_wh for p in zero_area_results
    ) + sum(
        r.get("energy_wh_reported") or 0 for r in zero_area_rejected
    )

    stats = {
        "parcels_total": len(parcel_results),
        "parcels_ok": sum(1 for p in parcel_results if p.ok),
        "parcels_with_status_not_ok": [
            {"parcel_id": p.parcel_id, "status": p.status}
            for p in parcel_results
            if not p.ok
        ],
        "rejected_parcels": len(scenario.rejected_parcels),
        "zero_area_parcels": zero_area_ids,
        "zones_total": len(scenario.zones),
        "fragments_total": fragment_count,
        "zone_parcel_pairs_total": total_pairs,
        "candidate_pairs": candidate_pairs,
        "exact_intersections": exact_intersections,
        "pairs_avoided_by_spatial_filter": total_pairs - candidate_pairs,
        "energy_input_wh_accepted_parcels": total_energy,
        "energy_allocated_wh": allocated_energy,
        "energy_unallocated_wh_zero_area_parcels": unallocated_zero_area_wh,
        "energy_reported_with_rejected_parcels_wh": rejected_energy,
    }

    checks = {
        "geometry": {
            "parcels_checked": len(parcel_results),
            "parcels_passed": sum(
                1 for p in parcel_results if p.geometry_checks.get("passed")
            ),
            "failures": [
                {"parcel_id": p.parcel_id, **p.geometry_checks}
                for p in parcel_results
                if not p.geometry_checks.get("passed")
            ],
        },
        "energy": {
            "parcels_checked": len(parcel_results),
            "parcels_passed": sum(
                1 for p in parcel_results if p.energy_checks.get("passed")
            ),
            "failures": [
                {"parcel_id": p.parcel_id, **p.energy_checks}
                for p in parcel_results
                if not p.energy_checks.get("passed")
            ],
            "accepted_parcel_energy_wh": total_energy,
            "allocated_wh": allocated_energy,
            "difference_wh": allocated_energy - total_energy,
            # Rejected parcels are outside the accepted set; their energy is
            # reported with them and deliberately excluded from this closure.
            "rejected_parcel_energy_reported_wh": rejected_energy,
            "zero_area_unallocated_wh": unallocated_zero_area_wh,
        },
        "passed": (
            all(p.geometry_checks.get("passed") for p in parcel_results)
            and all(p.energy_checks.get("passed") for p in parcel_results)
        ),
    }

    return AllocationResult(
        scenario=scenario,
        parcels=parcel_results,
        rejected_parcels=list(scenario.rejected_parcels),
        stats=stats,
        checks=checks,
    )


# `explain_parcel` is implemented in output.py / cli.py on top of compute().
__all__ = [
    "compute",
    "ZoneIndex",
    "ParcelResult",
    "Fragment",
    "ClaimEvidence",
    "AllocationResult",
]
