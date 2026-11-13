"""Data model and input validation.

Validation is deliberately strict and explicit:

* Structural problems (bad JSON, missing plan keys, CRS disagreement,
  duplicate identities) are fatal: they make the whole run ambiguous.
* Per-parcel problems (unsupported geometry, invalid rings, missing or
  negative energy) reject *that parcel only*.  The rejected parcel is reported
  separately with its energy, which is never moved to a neighbouring parcel.

Nothing is repaired or reprojected.  Invalid input is rejected, not patched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry


class ValidationError(Exception):
    """Raised when input violates the documented contract."""

    def __init__(self, code: str, message: str, *, feature: str | None = None):
        self.code = code
        self.feature = feature
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "feature": self.feature, "message": str(self)}


@dataclass(frozen=True)
class Plan:
    crs: str
    coordinate_unit: str
    priority_rule: str
    precision_grid: float
    area_tolerance_m2: float
    unassigned_id: str
    wh_remainder_tie_break: str
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def grid(self) -> float:
        return self.precision_grid


@dataclass(frozen=True)
class Parcel:
    parcel_id: str
    energy_wh: int
    # Used exactly as supplied; never snapped, repaired, or reprojected.
    geometry: Polygon | MultiPolygon
    source_index: int


@dataclass(frozen=True)
class Zone:
    zone_id: str
    priority: int
    geometry: Polygon | MultiPolygon
    source_index: int

    @property
    def sort_key(self) -> tuple[int, str]:
        """Adjudication order: higher numeric priority first, then ascending id."""
        return (-self.priority, self.zone_id)


@dataclass
class Scenario:
    plan: Plan
    parcels: list[Parcel]
    zones: list[Zone]
    rejected_parcels: list[dict[str, Any]] = field(default_factory=list)
    # Diagnostic: how closely input coordinates follow the declared grid.
    precision_report: dict[str, Any] = field(default_factory=dict)

    @property
    def crs(self) -> str:
        return self.plan.crs

    def parcel_by_id(self, parcel_id: str) -> Parcel | None:
        for p in self.parcels:
            if p.parcel_id == parcel_id:
                return p
        return None

    def zone_by_id(self, zone_id: str) -> Zone | None:
        for z in self.zones:
            if z.zone_id == zone_id:
                return z


def _require_mapping(obj: Any, what: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValidationError("invalid_json", f"{what} must be a JSON object")
    return obj


def parse_plan(raw: Any) -> Plan:
    doc = _require_mapping(raw, "plan")
    required = [
        "crs",
        "coordinate_unit",
        "priority_rule",
        "precision_grid",
        "area_tolerance_m2",
        "unassigned_id",
        "wh_remainder_tie_break",
    ]
    missing = [k for k in required if k not in doc]
    if missing:
        raise ValidationError(
            "plan_missing_field", f"plan.json is missing required keys: {missing}"
        )

    crs = doc["crs"]
    if not isinstance(crs, str) or not crs.strip():
        raise ValidationError("plan_crs", "plan.crs must be a non-empty string")
    unit = doc["coordinate_unit"]
    if unit != "metre":
        raise ValidationError(
            "plan_unit",
            f"coordinate_unit must be 'metre'; got {unit!r}. "
            "No unit conversion is supported.",
        )
    if not isinstance(doc["priority_rule"], str):
        raise ValidationError("plan_priority_rule", "priority_rule must be a string")
    if not isinstance(doc["wh_remainder_tie_break"], str):
        raise ValidationError(
            "plan_remainder_rule", "wh_remainder_tie_break must be a string"
        )

    grid = doc["precision_grid"]
    if isinstance(grid, bool) or not isinstance(grid, (int, float)) or grid <= 0:
        raise ValidationError(
            "plan_precision_grid", f"precision_grid must be > 0; got {grid!r}"
        )
    tol = doc["area_tolerance_m2"]
    if isinstance(tol, bool) or not isinstance(tol, (int, float)) or tol < 0:
        raise ValidationError(
            "plan_area_tolerance",
            f"area_tolerance_m2 must be >= 0; got {tol!r}",
        )
    unassigned = doc["unassigned_id"]
    if not isinstance(unassigned, str) or not unassigned:
        raise ValidationError(
            "plan_unassigned_id", "unassigned_id must be a non-empty string"
        )

    return Plan(
        crs=crs.strip(),
        coordinate_unit=unit,
        priority_rule=doc["priority_rule"],
        precision_grid=float(grid),
        area_tolerance_m2=float(tol),
        unassigned_id=unassigned,
        wh_remainder_tie_break=doc["wh_remainder_tie_break"],
        raw=dict(doc),
    )


def _feature_parts(feature: Any, fc_label: str, index: int) -> tuple[dict, dict]:
    if not isinstance(feature, dict):
        raise ValidationError(
            "invalid_feature",
            f"{fc_label} feature #{index} is not a JSON object",
            feature=f"{fc_label}[{index}]",
        )
    props = feature.get("properties")
    if not isinstance(props, dict):
        raise ValidationError(
            "invalid_properties",
            f"{fc_label} feature #{index} has no object 'properties'",
            feature=f"{fc_label}[{index}]",
        )
    geom_doc = feature.get("geometry")
    if not isinstance(geom_doc, dict):
        raise ValidationError(
            "invalid_geometry",
            f"{fc_label} feature #{index} has no geometry object",
            feature=f"{fc_label}[{index}]",
        )
    return props, geom_doc


def _parse_geometry(geom_doc: dict, fc_label: str, index: int) -> BaseGeometry:
    gtype = geom_doc.get("type")
    if gtype not in ("Polygon", "MultiPolygon"):
        raise ValidationError(
            "unsupported_geometry",
            f"{fc_label} feature #{index} has unsupported geometry type {gtype!r}; "
            "only Polygon and MultiPolygon are accepted",
            feature=f"{fc_label}[{index}]",
        )
    if "coordinates" not in geom_doc:
        raise ValidationError(
            "invalid_geometry",
            f"{fc_label} feature #{index} geometry has no coordinates",
            feature=f"{fc_label}[{index}]",
        )
    try:
        geom = shape(geom_doc)
    except Exception as exc:  # noqa: BLE001 - malformed coordinates
        raise ValidationError(
            "invalid_geometry",
            f"{fc_label} feature #{index} could not be parsed: {exc}",
            feature=f"{fc_label}[{index}]",
        ) from exc
    if not isinstance(geom, (Polygon, MultiPolygon)):
        raise ValidationError(
            "unsupported_geometry",
            f"{fc_label} feature #{index} parsed to {geom.geom_type}",
            feature=f"{fc_label}[{index}]",
        )
    if geom.has_z:
        raise ValidationError(
            "unsupported_geometry",
            f"{fc_label} feature #{index} has Z coordinates; only 2D input is accepted",
            feature=f"{fc_label}[{index}]",
        )
    if geom.is_empty:
        raise ValidationError(
            "empty_geometry",
            f"{fc_label} feature #{index} has an empty geometry",
            feature=f"{fc_label}[{index}]",
        )
    if not geom.is_valid:
        raise ValidationError(
            "invalid_geometry",
            f"{fc_label} feature #{index} is not a valid {gtype} "
            "(self-intersection, unclosed ring, or similar); refusing to repair it",
            feature=f"{fc_label}[{index}]",
        )
    return geom


def _int_like(value: Any) -> bool:
    return isinstance(value, int) or (
        isinstance(value, float) and value.is_integer()
    )


def parse_parcels(raw: Any) -> tuple[list[Parcel], list[dict[str, Any]]]:
    doc = _require_mapping(raw, "parcels GeoJSON")
    if doc.get("type") != "FeatureCollection":
        raise ValidationError(
            "invalid_geojson", "parcels file must be a GeoJSON FeatureCollection"
        )
    features = doc.get("features")
    if not isinstance(features, list):
        raise ValidationError("invalid_geojson", "parcels.features must be a list")

    seen: dict[str, int] = {}
    parsed: list[Parcel] = []
    rejected: list[dict[str, Any]] = []
    for index, feature in enumerate(features):
        pid: str | None = None
        energy: Any = None
        try:
            props, geom_doc = _feature_parts(feature, "parcels", index)
            pid = props.get("parcel_id")
            energy = props.get("energy_wh")
            if not isinstance(pid, str) or not pid.strip():
                raise ValidationError(
                    "missing_parcel_id",
                    f"parcels feature #{index} has no non-empty string parcel_id",
                    feature=f"parcels[{index}]",
                )
            if pid in seen:
                raise ValidationError(
                    "duplicate_parcel_id",
                    f"parcel_id {pid!r} appears at features {seen[pid]} and "
                    f"{index}; parcel identity must be unique",
                    feature=pid,
                )
            geom = _parse_geometry(geom_doc, "parcels", index)

            if "energy_wh" not in props:
                raise ValidationError(
                    "missing_energy", f"parcel {pid!r} has no energy_wh", feature=pid
                )
            if isinstance(energy, bool) or not _int_like(energy):
                raise ValidationError(
                    "invalid_energy",
                    f"parcel {pid!r} energy_wh must be an integer; got {energy!r}",
                    feature=pid,
                )
            energy_int = int(energy)
            if energy_int < 0:
                raise ValidationError(
                    "negative_energy",
                    f"parcel {pid!r} energy_wh must be non-negative; got {energy_int}",
                    feature=pid,
                )
        except ValidationError as exc:
            if exc.code == "duplicate_parcel_id":
                # Identity ambiguity is fatal: we cannot know which feature
                # owns the parcel, so neither can a consumer.
                raise
            rejected.append(
                {
                    "parcel_id": pid,
                    "source_index": index,
                    "code": exc.code,
                    "message": str(exc),
                    # Energy is reported with the rejected parcel and is not
                    # redistributed to anyone else.
                    "energy_wh_reported": (
                        int(energy) if _int_like(energy) else None
                    ),
                }
            )
            continue

        seen[pid] = index
        parsed.append(
            Parcel(
                parcel_id=pid,
                energy_wh=energy_int,
                geometry=geom,
                source_index=index,
            )
        )
    return parsed, rejected


def parse_zones(raw: Any) -> list[Zone]:
    doc = _require_mapping(raw, "zones GeoJSON")
    if doc.get("type") != "FeatureCollection":
        raise ValidationError(
            "invalid_geojson", "zones file must be a GeoJSON FeatureCollection"
        )
    features = doc.get("features")
    if not isinstance(features, list):
        raise ValidationError("invalid_geojson", "zones.features must be a list")

    seen: dict[str, int] = {}
    parsed: list[Zone] = []
    for index, feature in enumerate(features):
        props, geom_doc = _feature_parts(feature, "zones", index)
        zid = props.get("zone_id")
        if not isinstance(zid, str) or not zid.strip():
            raise ValidationError(
                "missing_zone_id",
                f"zones feature #{index} has no non-empty string zone_id",
                feature=f"zones[{index}]",
            )
        if zid in seen:
            raise ValidationError(
                "duplicate_zone_id",
                f"zone_id {zid!r} appears at features {seen[zid]} and {index}; "
                "zone identity must be unique",
                feature=zid,
            )
        seen[zid] = index

        geom = _parse_geometry(geom_doc, "zones", index)
        if "priority" not in props:
            raise ValidationError(
                "missing_priority", f"zone {zid!r} has no priority", feature=zid
            )
        priority = props["priority"]
        if isinstance(priority, bool) or not _int_like(priority):
            raise ValidationError(
                "invalid_priority",
                f"zone {zid!r} priority must be an integer; got {priority!r}",
                feature=zid,
            )
        parsed.append(
            Zone(
                zone_id=zid,
                priority=int(priority),
                geometry=geom,
                source_index=index,
            )
        )
    return parsed
