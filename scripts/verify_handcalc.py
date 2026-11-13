"""Independent, deliberately naive verification of one sampled timestamp.

This script does not import the sampler. It reads four raw stored values from
each NetCDF file, unpacks them by hand (mask -> scale -> offset -> Kelvin to
Celsius), computes a bilinear estimate with explicit arithmetic, and checks it
against the results CSV. It is a cross-check, not part of the shipped tool.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np
from netCDF4 import Dataset


def _read_axes(ds):
    lat = np.asarray(ds.variables["lat"][:], dtype=float)
    lon = np.asarray(ds.variables["lon"][:], dtype=float)
    return lat, lon


def _unpack_raw(raw, var):
    fill = getattr(var, "_FillValue", None)
    scale = float(getattr(var, "scale_factor", 1.0))
    offset = float(getattr(var, "add_offset", 0.0))
    units = str(getattr(var, "units", "")).strip().lower()
    if fill is not None and raw == fill:
        return None
    value = raw * scale + offset
    if units in {"k", "kelvin", "degk"}:
        value -= 273.15
    return value


def _bracket(axis, target):
    if axis[0] <= axis[-1]:
        asc, base, step = axis, 0, 1
    else:
        asc, base, step = axis[::-1], axis.size - 1, -1
    if target < asc[0] or target > asc[-1]:
        return None
    exact = np.nonzero(asc == target)[0]
    if exact.size:
        pos = int(exact[0])
        i = base + step * pos
        if pos == asc.size - 1:
            return i, i, 0.0
        return i, base + step * (pos + 1), 0.0
    pos = int(np.searchsorted(asc, target) - 1)
    frac = (target - asc[pos]) / (asc[pos + 1] - asc[pos])
    return base + step * pos, base + step * (pos + 1), float(frac)


def verify(file_path: str, variable: str, time_index: int, lat: float, lon: float):
    ds = Dataset(file_path, "r")
    ds.set_auto_maskandscale(False)
    ds.set_auto_mask(False)
    var = ds.variables[variable]
    lat_axis, lon_axis = _read_axes(ds)
    lb = _bracket(lat_axis, lat)
    xb = _bracket(lon_axis, lon)
    if lb is None or xb is None:
        ds.close()
        return {"out_of_range": True}

    i0, i1, fy = lb
    j0, j1, fx = xb
    dims = var.dimensions
    index = [0, 0, 0]
    index[dims.index("time")] = time_index

    corners = []
    for i, wy in ((i0, 1.0 - fy), (i1, fy)):
        for j, wx in ((j0, 1.0 - fx), (j1, fx)):
            index[dims.index("lat")] = i
            index[dims.index("lon")] = j
            raw = int(var[tuple(index)])
            value_c = _unpack_raw(raw, var)
            corners.append(
                {
                    "lat_index": int(i),
                    "lon_index": int(j),
                    "lat": float(lat_axis[i]),
                    "lon": float(lon_axis[j]),
                    "raw": raw,
                    "value_c": value_c,
                    "weight": wy * wx,
                }
            )
    ds.close()

    positive = [c for c in corners if c["weight"] > 1e-12]
    valid_positive = [c for c in positive if c["value_c"] is not None]
    total = sum(c["weight"] for c in positive)
    covered = sum(c["weight"] for c in valid_positive)
    result = {
        "out_of_range": False,
        "corners": corners,
        "coverage": covered / total if total else 0.0,
        "value_c": None,
    }
    if valid_positive:
        result["value_c"] = (
            sum(c["weight"] * c["value_c"] for c in valid_positive) / covered
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--variable", default="temperature")
    parser.add_argument("--time-index", type=int, required=True)
    parser.add_argument("--station", required=True)
    parser.add_argument("--tolerance", type=float, default=1e-9)
    args = parser.parse_args()

    row = None
    with Path(args.results).open(newline="", encoding="utf-8") as fh:
        for candidate in csv.DictReader(fh):
            if (
                candidate["station_id"] == args.station
                and int(candidate["time_index"]) == args.time_index
                and Path(candidate["file"]).resolve()
                == Path(args.file).resolve()
            ):
                row = candidate
                break
    if row is None:
        print(f"no result row for station {args.station!r} at index {args.time_index}")
        return 1

    manual = verify(
        args.file, args.variable, args.time_index,
        float(row["station_lat"]), float(row["station_lon"]),
    )
    print("manual calculation:")
    print(json.dumps(manual, indent=2))
    print("result row:")
    print(json.dumps({k: row[k] for k in (
        "temperature_degc", "status", "coverage", "lat_index", "lon_index",
        "neighbours",
    )}, indent=2))

    if manual["out_of_range"]:
        ok = row["status"] == "out_of_range"
    else:
        ok = (
            row["temperature_degc"] != ""
            and abs(float(row["temperature_degc"]) - manual["value_c"]) <= args.tolerance
        )
    print("MATCH" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
