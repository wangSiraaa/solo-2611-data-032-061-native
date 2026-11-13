"""Tests for the gridded temperature sampler.

Run with: ``.venv/bin/python -m pytest -q`` (pytest is optional; the module
also runs standalone via ``.venv/bin/python tests/test_sampler.py``).
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gridsampler.bilinear import sample_station  # noqa: E402
from gridsampler.times import parse_hours_since  # noqa: E402

FIXTURES = ROOT / "fixtures"
A = FIXTURES / "part-a.nc"
B = FIXTURES / "part-b.nc"
STATIONS = FIXTURES / "stations.csv"
PYTHON = sys.executable


def _run(tmp_path, files, *extra, stations=STATIONS):
    out = tmp_path / "results.csv"
    plan = tmp_path / "plan.json"
    diag = tmp_path / "diag.json"
    cmd = [PYTHON, "-m", "gridsampler", "sample"]
    for f in files:
        cmd += ["--file", str(f)]
    cmd += [
        "--stations", str(stations),
        "--out", str(out), "--plan", str(plan),
        "--diagnostics", str(diag),
        *extra,
    ]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return proc, out, plan, diag


def _rows(path):
    with Path(path).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------- unit tests

def test_parse_hours_since():
    ref = parse_hours_since("hours since 2026-01-01 00:00:00", "f.nc", "temperature")
    assert ref.year == 2026 and ref.month == 1 and ref.day == 1


def _grid():
    lat = np.array([30.0, 31.0, 32.0])
    lon = np.array([110.0, 111.0, 112.0])
    value = (lat[:, None] - 30.0) * 10.0 + (lon[None, :] - 110.0)
    valid = np.ones_like(value, dtype=bool)
    return lat, lon, value, valid


def test_interior_bilinear():
    lat, lon, value, valid = _grid()
    s = sample_station(lat, lon, value, valid, 30.5, 110.5)
    assert s.status == "ok"
    assert s.value == pytest.approx((0.0 + 1.0 + 10.0 + 11.0) / 4)
    assert s.coverage == pytest.approx(1.0)
    assert sorted(c.weight for c in s.corners) == pytest.approx([0.25] * 4)


def test_descending_axis_matches_ascending():
    lat, lon, value, valid = _grid()
    asc = sample_station(lat, lon, value, valid, 31.4, 110.3)
    desc = sample_station(lat[::-1], lon, value[::-1], valid, 31.4, 110.3)
    assert asc.value == pytest.approx(desc.value)
    assert asc.coverage == pytest.approx(desc.coverage)


def test_gridline_uses_single_neighbour():
    lat, lon, value, valid = _grid()
    s = sample_station(lat, lon, value, valid, 31.0, 111.0)
    assert s.status == "ok"
    assert s.value == pytest.approx(11.0)
    # Every other corner carries (near) zero weight.
    positive = [c for c in s.corners if c.weight > 1e-12]
    assert len(positive) == 1


def test_boundary_corner():
    lat, lon, value, valid = _grid()
    s = sample_station(lat, lon, value, valid, 32.0, 112.0)
    assert s.status == "ok"
    assert s.value == pytest.approx(22.0)


def test_zero_weight_missing_does_not_invalidate():
    lat, lon, value, valid = _grid()
    valid = valid.copy()
    # On a gridline only (31,111) has positive weight; make a zero-weight
    # neighbour missing and confirm the sample still succeeds.
    valid[2, 2] = False
    s = sample_station(lat, lon, value, valid, 31.0, 111.0)
    assert s.status == "ok"
    assert s.value == pytest.approx(11.0)


def test_positive_weight_missing_renormalises():
    lat, lon, value, valid = _grid()
    valid = valid.copy()
    valid[1, 1] = False  # interior point has weight 0.25 at this corner
    s = sample_station(lat, lon, value, valid, 30.5, 110.5)
    assert s.status == "ok"
    assert s.coverage == pytest.approx(0.75)
    expected = (0.0 + 1.0 + 10.0) / 3.0
    assert s.value == pytest.approx(expected)
    assert sum(c.weight for c in s.corners if c.valid) == pytest.approx(1.0)


def test_positive_weight_missing_strict_rejects():
    lat, lon, value, valid = _grid()
    valid = valid.copy()
    valid[1, 1] = False
    s = sample_station(lat, lon, value, valid, 30.5, 110.5, policy="strict")
    assert s.status == "missing_neighbour"
    assert s.value is None
    assert s.coverage == pytest.approx(0.75)


def test_low_coverage_rejects():
    lat, lon, value, valid = _grid()
    valid = valid.copy()
    valid[0, 0] = False
    valid[0, 1] = False
    valid[1, 0] = False
    s = sample_station(lat, lon, value, valid, 30.5, 110.5, min_coverage=0.5)
    assert s.status == "low_coverage"
    assert s.value is None


def test_out_of_range_has_reason():
    lat, lon, value, valid = _grid()
    s = sample_station(lat, lon, value, valid, 33.0, 111.0)
    assert s.status == "out_of_range"
    assert "lat" in s.reason
    assert s.value is None


# ------------------------------------------------------------ integration

def test_fixtures_full_run(tmp_path):
    proc, out, plan, diag = _run(tmp_path, [A, B])
    assert proc.returncode == 0, proc.stderr
    rows = _rows(out)
    assert len(rows) == 20  # 5 stations x 4 timestamps
    by_key = {(r["station_id"], r["time_utc"]): r for r in rows}

    # interior, time 0: mean of 12.0, 12.2, 12.3, 12.5
    r = by_key[("inside", "2026-01-01T00:00:00Z")]
    assert r["status"] == "ok"
    assert float(r["temperature_degc"]) == pytest.approx(12.25)

    # out-of-range is preserved with a reason and no value
    r = by_key[("outside", "2026-01-01T00:00:00Z")]
    assert r["status"] == "out_of_range"
    assert r["temperature_degc"] == ""
    assert "outside" in r["reason"]

    # boundary corner
    r = by_key[("corner", "2026-01-01T00:00:00Z")]
    assert float(r["temperature_degc"]) == pytest.approx(13.7)

    # positive-weight missing neighbour -> renormalised, coverage 0.58
    r = by_key[("missing_neighbour", "2026-01-01T00:00:00Z")]
    assert r["status"] == "ok"
    assert float(r["coverage"]) == pytest.approx(0.58, abs=1e-9)
    assert r["file"].endswith("part-a.nc")
    assert r["time_index"] == "0"

    # every ok row carries neighbour weights summing to 1
    for row in rows:
        neighbours = json.loads(row["neighbours"])
        if row["status"] == "ok":
            assert sum(n["weight"] for n in neighbours) == pytest.approx(1.0)


def test_file_order_independent(tmp_path):
    _, out_ab, _, _ = _run(tmp_path / "ab", [A, B])
    _, out_ba, _, _ = _run(tmp_path / "ba", [B, A])
    ra = _rows(out_ab)
    rb = _rows(out_ba)
    assert [(r["station_id"], r["time_utc"], r["temperature_degc"],
             r["file"], r["time_index"]) for r in ra] == \
           [(r["station_id"], r["time_utc"], r["temperature_degc"],
             r["file"], r["time_index"]) for r in rb]


def test_time_block_does_not_change_results(tmp_path):
    _, out1, _, _ = _run(tmp_path / "b1", [A, B], "--time-block", "1")
    _, out7, _, _ = _run(tmp_path / "b7", [A, B], "--time-block", "7")
    keys = lambda rows: [(r["station_id"], r["time_utc"], r["temperature_degc"])
                         for r in rows]
    assert keys(_rows(out1)) == keys(_rows(out7))


def test_slice_count_scales_with_blocks(tmp_path):
    _, _, _, d1 = _run(tmp_path / "d1", [A, B], "--time-block", "1")
    _, _, _, d2 = _run(tmp_path / "d2", [A, B], "--time-block", "24")
    diag1 = json.loads(d1.read_text())
    diag2 = json.loads(d2.read_text())
    assert diag2["total_slices"] <= diag1["total_slices"]
    # never loads per station: at most one block read per file
    assert diag2["total_slices"] == 2


def _write_minimal(path, *, times, lat, lon, units="K", values=None):
    import netCDF4

    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    times = np.asarray(times, dtype="i4")
    ds = netCDF4.Dataset(path, "w", format="NETCDF3_64BIT")
    ds.createDimension("time", times.size)
    ds.createDimension("lat", lat.size)
    ds.createDimension("lon", lon.size)
    t = ds.createVariable("time", "i4", ("time",))
    t.units = "hours since 2026-01-01 00:00:00"
    t.calendar = "standard"
    t[:] = times
    la = ds.createVariable("lat", "f8", ("lat",))
    la.units = "degrees_north"
    la[:] = lat
    lo = ds.createVariable("lon", "f8", ("lon",))
    lo.units = "degrees_east"
    lo[:] = lon
    v = ds.createVariable("temperature", "i2", ("time", "lat", "lon"),
                          fill_value=np.int16(-32768))
    v.units = units
    v.scale_factor = 0.1
    v.add_offset = 273.15
    if values is None:
        values = np.full((times.size, lat.size, lon.size), 100, "i2")
    v[:] = values
    ds.close()


def test_duplicate_time_across_files_rejected(tmp_path):
    # part-a has hours 0,1; craft a file reusing hour 0.
    dup = tmp_path / "dup.nc"
    _write_minimal(
        dup, times=[0], lat=[30.0, 31.0, 32.0, 33.0],
        lon=[110.0, 111.0, 112.0, 113.0, 114.0],
    )

    proc, _, _, diag = _run(tmp_path, [A, dup])
    assert proc.returncode == 2
    payload = json.loads(diag.read_text())
    assert payload["kind"] == "duplicate_time"


def test_unsupported_units_rejected(tmp_path):
    bad = tmp_path / "fahrenheit.nc"
    _write_minimal(
        bad, times=[10], lat=[30.0, 31.0, 32.0, 33.0],
        lon=[110.0, 111.0, 112.0, 113.0, 114.0], units="degF",
    )

    proc, _, _, diag = _run(tmp_path, [A, bad])
    assert proc.returncode == 2
    payload = json.loads(diag.read_text())
    assert payload["kind"] == "unsupported_units"
    assert "fahrenheit.nc" in payload["message"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
