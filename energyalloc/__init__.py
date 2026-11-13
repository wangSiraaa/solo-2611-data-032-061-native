"""Energy allocation across service zones (fixture-agnostic).

Public API::

    scenario = load_inputs(plan_path, parcels_path, zones_path)
    result = allocate(scenario, output_dir)       # writes GeoJSON/CSV/evidence
    doc = explain_parcel(scenario, parcel_id)     # per-parcel evidence dict

Low-level helpers::

    result = compute(scenario)                    # no file output
    allocate_wh(total_wh, weights, tie_keys)      # exact integer apportionment

The package performs no reprojection and no geometry repair of inputs.
"""

from .model import Parcel, Plan, Scenario, ValidationError, Zone
from .payload import load_inputs
from .engine import AllocationResult, Fragment, ParcelResult, compute
from .grid import allocate_wh, split_energy

__all__ = [
    "Scenario",
    "Parcel",
    "Zone",
    "Plan",
    "ValidationError",
    "load_inputs",
    "compute",
    "AllocationResult",
    "ParcelResult",
    "Fragment",
    "allocate_wh",
    "split_energy",
    "allocate",
    "explain_parcel",
]


def allocate(scenario: Scenario, output_dir) -> AllocationResult:
    """Run allocation and write all artifacts under ``output_dir``.

    Returns the in-memory result as well, so callers (and tests) can inspect
    fragments, per-parcel evidence, and closure checks without re-reading files.
    """
    from .output import write_all

    result = compute(scenario)
    write_all(result, output_dir)
    return result


def explain_parcel(scenario: Scenario, parcel_id: str) -> dict:
    """Build the evidence document for one parcel.

    Raises ``ValidationError`` if the id is neither an accepted parcel nor a
    rejected input.  A rejected parcel returns its rejection record instead of
    a normal explanation, because it has no adjudication to explain.
    """
    from .output import parcel_evidence

    rejected = [
        r for r in scenario.rejected_parcels if r.get("parcel_id") == parcel_id
    ]
    if rejected:
        return {"parcel_id": parcel_id, "status": "rejected", **rejected[0]}
    if scenario.parcel_by_id(parcel_id) is None:
        raise ValidationError(
            "unknown_parcel", f"no parcel with parcel_id {parcel_id!r} in input"
        )
    result = compute(scenario)
    parcel_result = next(p for p in result.parcels if p.parcel_id == parcel_id)
    return parcel_evidence(result, parcel_result)
