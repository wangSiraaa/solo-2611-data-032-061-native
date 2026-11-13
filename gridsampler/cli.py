"""Command-line entry point: read two NetCDF files, sample stations hourly."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import __version__
from .bilinear import POLICIES, sample_station
from .errors import SamplerError
from .merge import (
    check_spatial_compatibility,
    duplicate_time_map,
    merge_points,
)
from .ncfile import discover, open_metadata, read_time_block, select_time
from .stations import load_stations
from .times import hours_to_datetimes, iso

DEFAULT_BLOCK = 24

REJECTED_STATUSES = {
    "missing_neighbour",
    "low_coverage",
    "incomplete_neighbourhood",
    "out_of_range",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gridsampler",
        description=(
            "Sample station temperatures from two packed classic NetCDF grids "
            "and merge them by timestamp."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"gridsampler {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sample", help="sample stations and write results")
    p.add_argument(
        "--file", action="append", required=True, dest="files", metavar="PATH",
        help="input NetCDF file (exactly two, order-independent)",
    )
    p.add_argument("--stations", required=True, metavar="CSV", help="station CSV")
    p.add_argument("--out", required=True, metavar="CSV", help="results CSV path")
    p.add_argument("--plan", required=True, metavar="JSON", help="plan JSON path")
    p.add_argument(
        "--diagnostics", metavar="JSON", help="diagnostics JSON path (optional)"
    )
    p.add_argument(
        "--variable", default="temperature", metavar="NAME",
        help="temperature variable name (default: temperature)",
    )
    p.add_argument(
        "--missing-policy", choices=POLICIES, default="renormalize",
        help="how to treat missing positive-weight neighbours (default: renormalize)",
    )
    p.add_argument(
        "--min-coverage", type=float, default=0.5,
        help="minimum valid positive-weight coverage for renormalize (default: 0.5)",
    )
    p.add_argument(
        "--time-block", type=int, default=DEFAULT_BLOCK,
        help=f"time indices per read block (default: {DEFAULT_BLOCK})",
    )
    return parser


def _validate_args(args) -> None:
    paths = [Path(f).resolve() for f in args.files]
    if len(paths) != 2:
        raise SamplerError(
            f"exactly two --file arguments are required, got {len(paths)}",
            kind="usage_error",
        )
    if paths[0] == paths[1]:
        raise SamplerError(
            f"the same file was given twice: {paths[0]}",
            path=str(paths[0]),
            kind="usage_error",
        )
    if not (0.0 <= args.min_coverage <= 1.0):
        raise SamplerError(
            f"--min-coverage must be within [0, 1], got {args.min_coverage}",
            kind="usage_error",
        )
    if args.time_block < 1:
        raise SamplerError(
            f"--time-block must be >= 1, got {args.time_block}",
            kind="usage_error",
        )


def _file_metadata(meta) -> dict:
    return {
        "path": str(meta.path),
        "variable": meta.variable,
        "dimensions": list(meta.dims),
        "lat": [float(x) for x in meta.lat.tolist()],
        "lon": [float(x) for x in meta.lon.tolist()],
        "lat_descending": bool(meta.lat[0] > meta.lat[-1]),
        "lon_descending": bool(meta.lon[0] > meta.lon[-1]),
        "n_time": meta.n_time,
        "time_units": meta.time_units,
        "calendar": meta.time_calendar,
        "source_units": meta.source_units,
        "output_units": "degC",
        "scale_factor": meta.scale_factor,
        "add_offset": meta.add_offset,
        "fill_value": None if meta.fill_value is None else int(meta.fill_value),
        "dtype": meta.dtype,
        "chunking": list(meta.chunksizes) if meta.chunksizes else "contiguous",
        "contiguous": meta.contiguous,
    }


def _make_row(meta, station, time_index, time_utc, time_hours, sample, args):
    corners = [
        {
            "lat_index": c.i,
            "lon_index": c.j,
            "lat": c.lat,
            "lon": c.lon,
            "weight": c.weight,
            "original_weight": c.original_weight,
            "valid": c.valid,
            "value_degc": c.value_c,
        }
        for c in sample.corners
    ]
    return {
        "station_id": station.station_id,
        "time_utc": time_utc,
        "station_lat": station.lat,
        "station_lon": station.lon,
        "temperature_degc": sample.value,
        "status": sample.status,
        "reason": sample.reason or "",
        "file": str(meta.path),
        "time_index": time_index,
        "time_hours": time_hours,
        "lat_index": sample.lat_index,
        "lon_index": sample.lon_index,
        "coverage": sample.coverage,
        "missing_policy": args.missing_policy,
        "min_coverage": args.min_coverage,
        "neighbours": json.dumps(corners, separators=(",", ":")),
    }


def run_sample(args) -> int:
    handles = []
    try:
        _validate_args(args)
        stations, station_warnings = load_stations(args.stations)
        var_name = discover(args.variable)
        files = [str(Path(f).resolve()) for f in args.files]

        metas = []
        for path in files:
            ds, meta = open_metadata(path, var_name)
            handles.append(ds)
            metas.append(meta)
        check_spatial_compatibility(metas[0], metas[1])

        # Absolute timestamps per file, computed once for the whole file.
        times_per_file = [
            hours_to_datetimes(m.times_hours, m.time_units, m.path, m.variable)
            for m in metas
        ]
        duplicate_entries = [
            (str(m.path), dt, m.time_units, idx)
            for m, times in zip(metas, times_per_file)
            for idx, dt in enumerate(times)
        ]
        duplicates = duplicate_time_map(duplicate_entries)
        if duplicates:
            shown = []
            for dt in sorted(duplicates):
                locations = ", ".join(
                    f"{Path(p).name}[{i}]" for p, i in duplicates[dt]
                )
                shown.append(f"{iso(dt)} -> {locations}")
            raise SamplerError(
                "duplicate timestamps across input files: " + "; ".join(shown),
                path=metas[1].path,
                variable=var_name,
                kind="duplicate_time",
            )

        plans = [_file_metadata(m) for m in metas]
        diagnostics_warnings = list(station_warnings)
        for meta in metas:
            missing_attr = meta.extra.get("missing_value")
            if missing_attr is not None and (
                meta.fill_value is None or missing_attr != meta.fill_value
            ):
                diagnostics_warnings.append(
                    f"{meta.path}: variable also declares missing_value "
                    f"{missing_attr!r}"
                )

        rows: list[dict] = []
        slice_counts: dict[str, int] = {}
        rejected_records = 0
        missing_positive = 0
        renormalized_records = 0
        block = max(1, int(args.time_block))
        for ds, meta, times in zip(handles, metas, times_per_file):
            slice_counts[str(meta.path)] = 0
            for start in range(0, meta.n_time, block):
                stop = min(meta.n_time, start + block)
                tb, n_slices = read_time_block(ds, meta, start, stop)
                slice_counts[str(meta.path)] += n_slices
                for local in range(stop - start):
                    values_c, valid = select_time(tb, local)
                    global_index = start + local
                    for station in stations:
                        rec = sample_station(
                            meta.lat,
                            meta.lon,
                            values_c,
                            valid,
                            station.lat,
                            station.lon,
                            policy=args.missing_policy,
                            min_coverage=args.min_coverage,
                        )
                        rows.append(
                            _make_row(
                                meta=meta,
                                station=station,
                                time_index=global_index,
                                time_utc=iso(times[global_index]),
                                time_hours=float(tb.times_hours[local]),
                                sample=rec,
                                args=args,
                            )
                        )
                        if rec.status in REJECTED_STATUSES:
                            rejected_records += 1
                        if rec.status == "missing_neighbour":
                            missing_positive += 1
                        if rec.status == "ok" and rec.coverage < 1.0 - 1e-12:
                            renormalized_records += 1

        merged = merge_points(rows)

        plan = {
            "tool": "gridsampler",
            "version": __version__,
            "command": "sample",
            "variable": var_name,
            "policy": {
                "missing_positive_weight": args.missing_policy,
                "min_coverage": args.min_coverage,
                "zero_weight_neighbours": (
                    "ignored; never invalidate a sample with a valid "
                    "positive-weight neighbour"
                ),
                "units_input": "Kelvin or Celsius",
                "output_units": "degC",
                "order_of_operations": (
                    "fill mask on stored integer values -> scale_factor and "
                    "add_offset -> Kelvin-to-Celsius conversion (exactly once)"
                ),
                "duplicate_timestamps": "error (never take the last one)",
                "incompatible_spatial_metadata": "error",
                "out_of_range": "report reason, never zero-fill",
                "merge_order": (
                    "sorted by (time_utc, station_id); independent of --file order"
                ),
            },
            "time_block_size": block,
            "stations_file": str(Path(args.stations).resolve()),
            "inputs": plans,
        }

        diagnostics = {
            "tool": "gridsampler",
            "version": __version__,
            "ok": True,
            "command": "sample",
            "slice_counts": slice_counts,
            "total_slices": sum(slice_counts.values()),
            "time_block_size": block,
            "files": plans,
            "stations": {
                "count": len(stations),
                "ids": [s.station_id for s in stations],
            },
            "records": {
                "raw": len(rows),
                "merged": len(merged),
                "rejected": rejected_records,
                "missing_positive_weight": missing_positive,
                "renormalized": renormalized_records,
            },
            "warnings": diagnostics_warnings,
            "errors": [],
        }

        _write_results(args.out, merged)
        _write_json(args.plan, plan)
        if args.diagnostics:
            _write_json(args.diagnostics, diagnostics)
        return 0
    except SamplerError as exc:
        _emit_error(exc, args)
        return 2
    finally:
        for ds in handles:
            ds.close()


def _write_results(path: str, rows: list[dict]) -> None:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "station_id",
        "time_utc",
        "station_lat",
        "station_lon",
        "temperature_degc",
        "status",
        "reason",
        "file",
        "time_index",
        "lat_index",
        "lon_index",
        "coverage",
        "missing_policy",
        "min_coverage",
        "neighbours",
    ]
    with path_obj.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            out = {k: row[k] for k in fieldnames}
            if out["temperature_degc"] is None:
                out["temperature_degc"] = ""
            if out["lat_index"] is None:
                out["lat_index"] = ""
            if out["lon_index"] is None:
                out["lon_index"] = ""
            writer.writerow(out)


def _write_json(path: str, payload: dict) -> None:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    with path_obj.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
        fh.write("\n")


def _emit_error(exc: SamplerError, args) -> None:
    payload = {
        "tool": "gridsampler",
        "version": __version__,
        "ok": False,
        "kind": exc.kind,
        "path": exc.path,
        "variable": exc.variable,
        "message": exc.format(),
        "warnings": [],
        "errors": [
            {
                "kind": exc.kind,
                "path": exc.path,
                "variable": exc.variable,
                "message": exc.format(),
            }
        ],
    }
    if args.diagnostics:
        _write_json(args.diagnostics, payload)
    print(f"error: {exc.format()}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "sample":
        return run_sample(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
