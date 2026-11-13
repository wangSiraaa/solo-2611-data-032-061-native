"""Generate extra synthetic fixtures used only for local verification.

These files exercise paths the shipped ``fixtures/`` do not: descending
longitude, larger grids, and enough timestamps to cross a time-block boundary.
They are inputs, not business logic.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from netCDF4 import Dataset

FILL = -32768


def _write(path: Path, *, dims, lat, lon, times, values_k, units="K",
           scale=0.1, offset=273.15, fill=FILL, calendar="standard",
           time_units="hours since 2026-01-01 00:00:00") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = Dataset(path, "w", format="NETCDF3_64BIT")
    # Write raw stored integers directly; otherwise netCDF4 would un-scale the
    # values on assignment (the inverse of scale_factor/add_offset) and we would
    # double-pack the data.
    ds.set_auto_maskandscale(False)
    ds.set_auto_mask(False)
    ds.createDimension("time", len(times))
    ds.createDimension("lat", len(lat))
    ds.createDimension("lon", len(lon))
    tv = ds.createVariable("time", "i4", ("time",))
    tv.units = time_units
    tv.calendar = calendar
    tv[:] = np.asarray(times, dtype="i4")
    lav = ds.createVariable("lat", "f8", ("lat",))
    lav.units = "degrees_north"
    lav[:] = np.asarray(lat, dtype="f8")
    lov = ds.createVariable("lon", "f8", ("lon",))
    lov.units = "degrees_east"
    lov[:] = np.asarray(lon, dtype="f8")
    var = ds.createVariable("temperature", "i2", dims, fill_value=np.int16(fill))
    var.set_auto_maskandscale(False)
    var.units = units
    var.scale_factor = scale
    var.add_offset = offset

    # ``values_k`` is a (time, lat, lon) array in Kelvin with NaN for missing.
    safe = np.nan_to_num(values_k, nan=offset)
    stored = np.rint((safe - offset) / scale).astype(np.int64)
    missing = ~np.isfinite(values_k)
    stored[missing] = fill
    stored = np.clip(stored, -32768, 32767).astype("i2")

    order = [dims.index(d) for d in ("time", "lat", "lon")]
    payload = np.transpose(stored, order)
    var[:] = payload
    ds.close()


def make_synthetic(outdir: Path) -> None:
    rng = np.random.default_rng(20260101)
    lat = np.array([30.0, 31.0, 32.0, 33.0, 34.0, 35.0])
    lon = np.array([110.0, 111.0, 112.0, 113.0, 114.0, 115.0, 116.0])
    n_time = 30
    base = 280.0 + 0.1 * np.arange(n_time)[:, None, None]
    base = base + 0.5 * (lat[None, :, None] - 30.0) + 0.3 * (lon[None, None, :] - 110.0)
    values = base + rng.normal(0.0, 0.05, size=base.shape)
    values[3, 2, 3] = np.nan
    values[7, 4, 1] = np.nan
    times = np.arange(n_time, dtype="i4")

    _write(
        outdir / "grid-desc-lon.nc",
        dims=("time", "lat", "lon"),
        lat=lat,
        lon=lon[::-1],
        times=times,
        values_k=values,
    )
    _write(
        outdir / "grid-reordered.nc",
        dims=("lon", "lat", "time"),
        lat=lat[::-1],
        lon=lon[::-1],
        times=times + n_time,
        values_k=values,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--outdir", default="out/extra")
    args = parser.parse_args()
    make_synthetic(Path(args.outdir))
    print(f"wrote synthetic fixtures to {Path(args.outdir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
