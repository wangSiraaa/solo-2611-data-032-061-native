"""Input loading and CRS declaration checks.

Geometry is used exactly as supplied: no reprojection, no snapping, no
repair.  ``precision_grid`` is consumed as a reporting and tie-break
precision, not as a licence to alter source coordinates.  That keeps the
"fragments reconstruct the original parcel" check honest.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .model import (
    Plan,
    Scenario,
    ValidationError,
    parse_parcels,
    parse_plan,
    parse_zones,
)


def _read_json(path: str | Path) -> Any:
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise ValidationError("missing_file", f"file not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError("invalid_json", f"{p} is not valid JSON: {exc}") from exc


_EPSG_RE = re.compile(r"EPSG[:_ ]?(\d+)", re.IGNORECASE)
_URN_EPSG_RE = re.compile(r"urn:ogc:def:crs:EPSG:[^:]*:(\d+)", re.IGNORECASE)


def _crs_tokens(value: str) -> set[str]:
    """Normalise CRS spellings to comparable tokens.

    Accepts ``EPSG:3857``, ``epsg:3857``, ``urn:ogc:def:crs:EPSG::3857``,
    ``http://www.opengis.net/def/crs/EPSG/0/3857`` and bare names.  Only
    textual comparison happens here; there is no reprojection anywhere.
    """
    text = value.strip()
    tokens = {text.upper().replace(" ", "").replace("_", ":")}
    for regex in (_EPSG_RE, _URN_EPSG_RE):
        for match in regex.finditer(text):
            tokens.add(f"EPSG:{int(match.group(1))}")
    return tokens


def _declared_crs(doc: dict[str, Any], label: str) -> str | None:
    """Return a GeoJSON ``crs`` declaration if the document carries one.

    Modern GeoJSON has no CRS member; the plan supplies it.  If a document
    nonetheless declares one, it must agree with the plan.  We never
    reproject: disagreeing declarations are rejected, not transformed.
    """
    if "crs" not in doc:
        return None
    crs = doc["crs"]
    if isinstance(crs, str):
        return crs
    if isinstance(crs, dict):
        name = crs.get("name") or crs.get("id") or crs.get("properties")
        if isinstance(name, dict):
            name = name.get("name") or name.get("code")
        if isinstance(name, (str, int)):
            return str(name)
    raise ValidationError(
        "invalid_crs_declaration",
        f"{label} contains a crs member that cannot be interpreted; only a "
        "textual name or EPSG code is understood",
        feature=label,
    )


def _check_crs(doc: dict[str, Any], plan: Plan, label: str) -> None:
    declared = _declared_crs(doc, label)
    if declared is None:
        # Absent declaration: coordinates are interpreted in the plan CRS.
        return
    if _crs_tokens(plan.crs).isdisjoint(_crs_tokens(declared)):
        raise ValidationError(
            "crs_mismatch",
            f"{label} declares CRS {declared!r} but plan.json declares "
            f"{plan.crs!r}; reprojection is not supported, so this input is "
            "rejected rather than silently misread",
            feature=label,
        )


def _grid_residual(geom, grid: float) -> float:
    """Largest distance of any vertex from the declared precision grid.

    This is diagnostic only: coordinates are never moved.  A non-zero
    residual simply tells the operator that input coordinates are finer than
    (or not aligned to) the declared grid.
    """
    if grid <= 0:
        return 0.0
    worst = 0.0
    for x, y in _iter_coords(geom):
        for value in (x, y):
            remainder = value % grid
            distance = min(remainder, grid - remainder)
            worst = max(worst, distance)
    return worst


def _iter_coords(geom):
    coords = geom.__geo_interface__["coordinates"]

    def walk(node):
        if isinstance(node, (list, tuple)):
            if node and isinstance(node[0], (int, float)):
                yield float(node[0]), float(node[1])
            else:
                for item in node:
                    yield from walk(item)

    yield from walk(coords)


def load_inputs(
    plan_path: str | Path,
    parcels_path: str | Path,
    zones_path: str | Path,
) -> Scenario:
    plan_doc = _read_json(plan_path)
    plan = parse_plan(plan_doc)

    parcels_doc = _read_json(parcels_path)
    zones_doc = _read_json(zones_path)
    if not isinstance(parcels_doc, dict):
        raise ValidationError("invalid_geojson", "parcels file is not an object")
    if not isinstance(zones_doc, dict):
        raise ValidationError("invalid_geojson", "zones file is not an object")

    _check_crs(parcels_doc, plan, "parcels.geojson")
    _check_crs(zones_doc, plan, "zones.geojson")

    parcels, rejected = parse_parcels(parcels_doc)
    zones = parse_zones(zones_doc)

    usage_parcels = []
    for p in parcels:
        if p.geometry.area <= 0:
            # Valid polygon with zero area: report separately, never
            # redistribute its energy to another parcel.
            rejected.append(
                {
                    "parcel_id": p.parcel_id,
                    "source_index": p.source_index,
                    "code": "zero_area",
                    "message": (
                        f"parcel {p.parcel_id!r} has zero area; it is reported "
                        "separately and its energy is not reassigned"
                    ),
                    "energy_wh_reported": p.energy_wh,
                }
            )
            continue
        usage_parcels.append(p)

    for z in zones:
        if z.geometry.area <= 0:
            raise ValidationError(
                "zero_area_zone",
                f"zone {z.zone_id!r} has zero area and cannot receive parcels",
                feature=z.zone_id,
            )

    grid = plan.precision_grid
    worst_parcel = max(
        (_grid_residual(p.geometry, grid) for p in usage_parcels), default=0.0
    )
    worst_zone = max((_grid_residual(z.geometry, grid) for z in zones), default=0.0)
    precision_report = {
        "precision_grid": grid,
        "max_vertex_residual_parcels": worst_parcel,
        "max_vertex_residual_zones": worst_zone,
        "grid_applied_to_source_coordinates": False,
        "statement": (
            "Source coordinates are used unmodified. precision_grid is used "
            "for canonical geometry keys, the tiny-fragment threshold, and "
            "this diagnostic; it does not move vertices, so fragment geometry "
            "is directly comparable with the source parcel."
        ),
    }

    return Scenario(
        plan=plan,
        parcels=usage_parcels,
        zones=zones,
        rejected_parcels=rejected,
        precision_report=precision_report,
    )
