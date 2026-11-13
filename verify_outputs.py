"""Independent verification of produced artifacts.

This module deliberately does **not** import the allocation engine.  It reads
the emitted GeoJSON, CSV, and summary with ordinary readers, rebuilds Shapely
geometries from the raw GeoJSON coordinates, and checks the invariants
independently:

1. Geometric closure: fragments within a parcel are pairwise disjoint, their
   areas sum to the union area, and the union reconstructs the *source* parcel
   within the declared tolerance.
2. Energy closure: fragment Wh sum to the parcel's ``parcel_energy_wh``.
3. Accounting closure: every source parcel with energy is either present in
   the fragments or reported in summary.json as rejected/zero-area; no energy
   silently disappears or is transferred.

Usage::

    python verify_outputs.py --out out/ --parcels fixtures/parcels.geojson
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from shapely import union_all
from shapely.geometry import shape
from shapely.strtree import STRtree


def _load_geojson(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("type") != "FeatureCollection":
        raise ValueError(f"{path} is not a FeatureCollection")
    return doc["features"]


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _load_summary(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _source_parcels(path: Path) -> dict[str, tuple[float | None, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        doc = json.load(fh)
    result: dict[str, tuple[float | None, Any]] = {}
    for feature in doc.get("features", []):
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        pid = props.get("parcel_id")
        if not isinstance(pid, str):
            continue
        energy = props.get("energy_wh")
        geom_doc = feature.get("geometry")
        geom = shape(geom_doc) if isinstance(geom_doc, dict) else None
        result[pid] = (energy if isinstance(energy, int) else None, geom)
    return result


def verify(
    outdir: str | Path,
    parcels_path: str | Path,
    tolerance: float = 1e-5,
) -> dict[str, Any]:
    out = Path(outdir)
    features = _load_geojson(out / "fragments.geojson")
    rows = _load_csv(out / "allocation.csv")
    summary = _load_summary(out / "summary.json")
    source = _source_parcels(Path(parcels_path))

    # ---- cross-check GeoJSON against CSV row-for-row ----------------------
    csv_by_id = {row["fragment_id"]: row for row in rows}
    feature_by_id = {f["properties"]["fragment_id"]: f for f in features}
    mismatches: list[str] = []
    if set(csv_by_id) != set(feature_by_id):
        mismatches.append(
            "fragment id sets differ between fragments.geojson and "
            f"allocation.csv: only-geojson={sorted(set(feature_by_id) - set(csv_by_id))}, "
            f"only-csv={sorted(set(csv_by_id) - set(feature_by_id))}"
        )
    for fid, feat in feature_by_id.items():
        row = csv_by_id.get(fid)
        if row is None:
            continue
        if int(row["wh"]) != int(feat["properties"]["wh"]):
            mismatches.append(f"{fid}: wh differs between GeoJSON and CSV")
        if abs(float(row["area_m2"]) - float(feat["properties"]["area_m2"])) > 1e-9:
            mismatches.append(f"{fid}: area differs between GeoJSON and CSV")

    # ---- group fragments by parcel ----------------------------------------
    by_parcel_geo: dict[str, list[dict]] = defaultdict(list)
    by_parcel_csv: dict[str, list[dict]] = defaultdict(list)
    for feat in features:
        by_parcel_geo[feat["properties"]["parcel_id"]].append(feat)
    for row in rows:
        by_parcel_csv[row["parcel_id"]].append(row)

    parcel_reports = []
    all_geoms = []
    for pid in sorted(by_parcel_csv):
        geoms = [shape(f["geometry"]) for f in by_parcel_geo.get(pid, [])]
        csv_rows = by_parcel_csv[pid]
        all_geoms.extend(geoms)

        overlap_area = 0.0
        if len(geoms) > 1:
            tree = STRtree(geoms)
            for i, g in enumerate(geoms):
                for j in tree.query(g):
                    j = int(j)
                    if j <= i:
                        continue
                    overlap_area += g.intersection(geoms[j]).area

        union = union_all(geoms) if geoms else None
        union_area = float(union.area) if union is not None else 0.0
        area_sum = float(sum(g.area for g in geoms))

        source_energy, source_geom = source.get(pid, (None, None))
        source_area = float(source_geom.area) if source_geom is not None else None
        sym_diff = (
            float(union.symmetric_difference(source_geom).area)
            if union is not None and source_geom is not None
            else None
        )

        wh_sum = sum(int(r["wh"]) for r in csv_rows)
        parcel_energy = int(csv_rows[0]["parcel_energy_wh"])
        if source_energy is not None and parcel_energy != int(source_energy):
            mismatches.append(
                f"{pid}: parcel_energy_wh {parcel_energy} in CSV does not "
                f"match source energy {source_energy}"
            )
        unassigned_wh = sum(
            int(r["wh"]) for r in csv_rows if r["kind"] == "unassigned"
        )

        checks = {
            "disjoint": overlap_area <= tolerance,
            "area_sum_matches_union": abs(area_sum - union_area) <= tolerance,
            "energy_conserved": wh_sum == parcel_energy,
        }
        if source_geom is not None:
            checks["union_reconstructs_source"] = (
                sym_diff is not None and sym_diff <= tolerance
            )
        parcel_reports.append(
            {
                "parcel_id": pid,
                "fragment_count": len(csv_rows),
                "fragment_area_sum_m2": area_sum,
                "union_area_m2": union_area,
                "overlap_area_m2": overlap_area,
                "symmetric_difference_vs_source_m2": sym_diff,
                "source_area_m2": source_area,
                "wh_sum": wh_sum,
                "parcel_energy_wh": parcel_energy,
                "zone_wh": wh_sum - unassigned_wh,
                "unassigned_wh": unassigned_wh,
                "checks": checks,
                "passed": all(checks.values()),
            }
        )

    # ---- global accounting ------------------------------------------------
    global_overlap = 0.0
    if len(all_geoms) > 1:
        tree = STRtree(all_geoms)
        for i, g in enumerate(all_geoms):
            for j in tree.query(g):
                j = int(j)
                if j <= i:
                    continue
                global_overlap += g.intersection(all_geoms[j]).area

    reported_ids = set(by_parcel_csv)
    rejected_records: dict[str, int] = {}
    if summary is not None:
        for record in summary.get("rejected_parcels", []):
            rid = record.get("parcel_id")
            if isinstance(rid, str):
                rejected_records[rid] = int(record.get("energy_wh_reported") or 0)
    missing: list[dict[str, Any]] = []
    for pid, (energy, _geom) in source.items():
        if pid in reported_ids or pid in rejected_records:
            continue
        missing.append({"parcel_id": pid, "energy_wh": energy})
    unaccounted_missing = [m for m in missing if m["energy_wh"]]

    total_wh_out = sum(int(r["wh"]) for r in rows)
    total_energy_accounted = (
        total_wh_out + sum(rejected_records.values())
    )
    total_source_energy = sum(
        energy for energy, _geom in source.values() if isinstance(energy, int)
    )
    accounting_ok = (
        not unaccounted_missing
        and total_energy_accounted == total_source_energy
    )

    passed = (
        all(r["passed"] for r in parcel_reports)
        and not mismatches
        and global_overlap <= tolerance
        and accounting_ok
    )
    return {
        "outdir": str(out),
        "fragments": len(features),
        "parcels_in_output": len(parcel_reports),
        "total_wh_in_output": total_wh_out,
        "total_source_energy_wh": total_source_energy,
        "rejected_energy_wh": sum(rejected_records.values()),
        "global_overlap_area_m2": global_overlap,
        "geojson_csv_mismatches": mismatches,
        "unaccounted_parcels": missing,
        "accounting_ok": accounting_ok,
        "parcel_reports": parcel_reports,
        "passed": passed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Independently verify fragments.geojson and allocation.csv"
    )
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--parcels", required=True, help="original parcels.geojson")
    parser.add_argument("--tolerance", type=float, default=1e-5)
    parser.add_argument("--json", action="store_true", help="print full JSON report")
    args = parser.parse_args(argv)

    report = verify(args.out, args.parcels, args.tolerance)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"fragments={report['fragments']} parcels={report['parcels_in_output']} "
            f"total_wh={report['total_wh_in_output']} "
            f"rejected_wh={report['rejected_energy_wh']} "
            f"global_overlap={report['global_overlap_area_m2']:.3e} m2"
        )
        for p in report["parcel_reports"]:
            status = "ok" if p["passed"] else "FAIL"
            print(
                f"  {p['parcel_id']}: {status} fragments={p['fragment_count']} "
                f"area={p['fragment_area_sum_m2']:.6f} m2 "
                f"wh={p['wh_sum']}/{p['parcel_energy_wh']} "
                f"overlap={p['overlap_area_m2']:.3e} "
                f"symdiff={p['symmetric_difference_vs_source_m2']}"
            )
        for mismatch in report["geojson_csv_mismatches"]:
            print(f"  MISMATCH: {mismatch}")
        for item in report["unaccounted_parcels"]:
            print(f"  UNACCOUNTED: {item}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
