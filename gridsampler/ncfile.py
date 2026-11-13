"""NetCDF access that is explicit about packing and axis layout.

The important rule in this module is that missing values are detected on the
*stored* integer values (against the variable's ``_FillValue``) *before* any
``scale_factor``/``add_offset`` math or Kelvin-to-Celsius conversion happens.
``netCDF4`` would otherwise auto-scale on read and may convert the fill value
to ``nan``; doing the masking ourselves keeps the order of operations fixed and
auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from netCDF4 import Dataset

from .errors import SamplerError

SUPPORTED_TEMPERATURE_UNITS = {
    "k": ("K", 0.0),
    "kelvin": ("K", 0.0),
    "degk": ("K", 0.0),
    "degree_kelvin": ("K", 0.0),
    "degrees_kelvin": ("K", 0.0),
    "deg_c": ("degC", 0.0),
    "degc": ("degC", 0.0),
    "c": ("degC", 0.0),
    "celsius": ("degC", 0.0),
    "degree_celsius": ("degC", 0.0),
    "degrees_celsius": ("degC", 0.0),
    "degreec": ("degC", 0.0),
    "degreesc": ("degC", 0.0),
    "deg_celsius": ("degC", 0.0),
    "degcelsius": ("degC", 0.0),
}

# Units that are explicitly unsupported even though they look temperature-ish.
UNSUPPORTED_TEMPERATURE_UNITS = {
    "f",
    "degf",
    "deg_f",
    "fahrenheit",
    "degree_fahrenheit",
    "degrees_fahrenheit",
}

KELVIN_OFFSET = 273.15


@dataclass
class VariableMeta:
    path: str
    variable: str
    dims: tuple[str, ...]
    time_dim: str
    lat_dim: str
    lon_dim: str
    lat_axis: int
    lon_axis: int
    time_axis: int
    n_time: int
    n_lat: int
    n_lon: int
    lat: np.ndarray
    lon: np.ndarray
    times_hours: np.ndarray
    time_units: str
    time_calendar: str
    source_units: str
    display_units: str
    scale_factor: float
    add_offset: float
    fill_value: object
    dtype: str
    chunksizes: tuple[int, int, int] | None
    contiguous: bool
    extra: dict = field(default_factory=dict)


def _norm_unit(units: str) -> str:
    return units.strip().lower().replace(" ", "_")


def _convert_units(raw: np.ndarray, units: str, path: str, variable: str) -> np.ndarray:
    key = _norm_unit(units)
    if key in UNSUPPORTED_TEMPERATURE_UNITS:
        raise SamplerError(
            f"unsupported temperature units {units!r} (only Kelvin and Celsius are supported)",
            path=path,
            variable=variable,
            kind="unsupported_units",
        )
    if key not in SUPPORTED_TEMPERATURE_UNITS:
        raise SamplerError(
            f"unsupported units {units!r} (only Kelvin and Celsius are supported)",
            path=path,
            variable=variable,
            kind="unsupported_units",
        )
    display, offset = SUPPORTED_TEMPERATURE_UNITS[key]
    if display == "K":
        # Kelvin -> Celsius. Applied exactly once.
        return raw - KELVIN_OFFSET
    return raw + offset


def _read_axis(var, path: str, variable: str, name: str, units_attr: str) -> np.ndarray:
    values = np.asarray(var[:], dtype=np.float64)
    if values.ndim != 1:
        raise SamplerError(
            f"coordinate variable {name!r} is not one-dimensional",
            path=path,
            variable=variable,
            kind="invalid_axis",
        )
    if values.size < 2:
        raise SamplerError(
            f"coordinate variable {name!r} needs at least 2 points",
            path=path,
            variable=variable,
            kind="invalid_axis",
        )
    if not np.all(np.isfinite(values)):
        raise SamplerError(
            f"coordinate variable {name!r} contains non-finite values",
            path=path,
            variable=variable,
            kind="invalid_axis",
        )
    units = getattr(var, "units", None)
    if units is not None and units_attr not in units:
        raise SamplerError(
            f"coordinate {name!r} has units {units!r}, expected "
            f"something containing {units_attr!r}",
            path=path,
            variable=variable,
            kind="invalid_axis",
        )
    diffs = np.diff(values)
    if np.any(diffs == 0):
        dup = np.unique(values[:-1][diffs == 0])
        raise SamplerError(
            f"coordinate {name!r} has duplicate values {dup.tolist()}",
            path=path,
            variable=variable,
            kind="duplicate_coordinate",
        )
    all_pos = bool(np.all(diffs > 0))
    all_neg = bool(np.all(diffs < 0))
    if not (all_pos or all_neg):
        raise SamplerError(
            f"coordinate {name!r} is not a regular monotonic axis",
            path=path,
            variable=variable,
            kind="irregular_axis",
        )
    step = diffs[0]
    if not np.allclose(diffs, step, rtol=0.0, atol=1e-9):
        raise SamplerError(
            f"coordinate {name!r} is not evenly spaced",
            path=path,
            variable=variable,
            kind="irregular_axis",
        )
    return values


def _read_time(var, path: str, variable: str) -> tuple[np.ndarray, str, str]:
    units = getattr(var, "units", None)
    calendar = getattr(var, "calendar", "standard")
    if not units:
        raise SamplerError(
            "time variable has no units attribute", path=path, variable=variable,
            kind="invalid_time",
        )
    # We only handle the standard Gregorian calendar.
    if str(calendar).lower() not in {
        "standard", "gregorian", "proleptic_gregorian",
    }:
        raise SamplerError(
            f"unsupported calendar {calendar!r} (only standard Gregorian is supported)",
            path=path,
            variable=variable,
            kind="unsupported_calendar",
        )
    collapsed = " ".join(str(units).split()).lower()
    if " since " not in f" {collapsed} ":
        raise SamplerError(
            f"time units {units!r} are not of the form '<unit> since <reference>'",
            path=path,
            variable=variable,
            kind="invalid_time",
        )
    unit_word = collapsed.split(" ", 1)[0]
    if unit_word not in {"hour", "hours"}:
        raise SamplerError(
            f"time units {units!r} are not in hours "
            "(only 'hours since ...' supported)",
            path=path,
            variable=variable,
            kind="unsupported_time_units",
        )
    values = np.asarray(var[:])
    if values.ndim != 1:
        raise SamplerError(
            "time variable is not one-dimensional", path=path, variable=variable,
            kind="invalid_time",
        )
    values = values.astype(np.float64)
    if not np.all(np.isfinite(values)):
        raise SamplerError(
            "time axis contains missing or non-finite values",
            path=path,
            variable=variable,
            kind="invalid_time",
        )
    return values, str(units), str(calendar)


def discover(variable: str = "temperature") -> str:
    """Return the default variable name (extensible hook)."""
    return variable


def open_metadata(path: str, variable: str) -> tuple[Dataset, VariableMeta]:
    """Open ``path`` and validate layout, dimensions, units and axes."""
    try:
        ds = Dataset(path, "r")
    except OSError as exc:
        raise SamplerError(
            f"cannot open NetCDF file: {exc}", path=path, kind="file_error"
        ) from exc

    try:
        # Read *stored* values only. With auto mask/scale enabled netCDF4 would
        # apply scale_factor/add_offset (and rewrite _FillValue to NaN), so we
        # would scale a second time below. Disable it and own the order of
        # operations explicitly.
        ds.set_auto_maskandscale(False)
        ds.set_auto_mask(False)
        if variable not in ds.variables:
            available = ", ".join(sorted(ds.variables))
            raise SamplerError(
                f"no variable named {variable!r} (available: {available})",
                path=path,
                variable=variable,
                kind="missing_variable",
            )
        var = ds.variables[variable]
        dims = tuple(var.dimensions)
        if len(dims) != 3:
            raise SamplerError(
                f"expected a 3-D variable, got dimensions {dims}",
                path=path,
                variable=variable,
                kind="invalid_dims",
            )
        for needed in ("time", "lat", "lon"):
            if needed not in dims:
                raise SamplerError(
                    f"variable dimensions {dims} do not include {needed!r}",
                    path=path,
                    variable=variable,
                    kind="invalid_dims",
                )
        for needed in ("time", "lat", "lon"):
            if needed not in ds.variables:
                raise SamplerError(
                    f"missing coordinate variable {needed!r}",
                    path=path,
                    variable=variable,
                    kind="missing_coordinate",
                )

        time_axis = dims.index("time")
        lat_axis = dims.index("lat")
        lon_axis = dims.index("lon")
        shape = var.shape
        n_time = int(shape[time_axis])
        n_lat = int(shape[lat_axis])
        n_lon = int(shape[lon_axis])

        lat = _read_axis(ds.variables["lat"], path, variable, "lat", "degrees_north")
        lon = _read_axis(ds.variables["lon"], path, variable, "lon", "degrees_east")
        if lat.size != n_lat:
            raise SamplerError(
                f"lat coordinate length {lat.size} != variable axis size {n_lat}",
                path=path,
                variable=variable,
                kind="invalid_axis",
            )
        if lon.size != n_lon:
            raise SamplerError(
                f"lon coordinate length {lon.size} != variable axis size {n_lon}",
                path=path,
                variable=variable,
                kind="invalid_axis",
            )

        times_hours, time_units, time_calendar = _read_time(
            ds.variables["time"], path, variable
        )
        if times_hours.size != n_time:
            raise SamplerError(
                f"time coordinate length {times_hours.size} != variable axis size {n_time}",
                path=path,
                variable=variable,
                kind="invalid_time",
            )
        if np.unique(times_hours).size != times_hours.size:
            raise SamplerError(
                "time axis contains duplicate timestamps",
                path=path,
                variable=variable,
                kind="duplicate_time",
            )

        units = getattr(var, "units", None)
        if units is None:
            raise SamplerError(
                "temperature variable has no units attribute",
                path=path,
                variable=variable,
                kind="unsupported_units",
            )
        key = _norm_unit(str(units))
        if key not in SUPPORTED_TEMPERATURE_UNITS:
            raise SamplerError(
                f"unsupported units {units!r} (only Kelvin and Celsius are supported)",
                path=path,
                variable=variable,
                kind="unsupported_units",
            )
        display_units = SUPPORTED_TEMPERATURE_UNITS[key][0]

        scale_factor = float(getattr(var, "scale_factor", 1.0))
        add_offset = float(getattr(var, "add_offset", 0.0))
        fill_value = getattr(var, "_FillValue", None)
        missing_value = getattr(var, "missing_value", None)
        if fill_value is None and missing_value is not None:
            fill_value = missing_value

        chunks = None
        try:
            chunks = var.chunking()
        except (AttributeError, TypeError):
            chunks = getattr(var, "chunking", None)
            if callable(chunks):
                chunks = None
        chunksizes: tuple[int, int, int] | None = None
        contiguous = True
        if chunks not in (None, "contiguous"):
            contiguous = False
            # ``chunking()`` gives chunks in the variable's own dimension order.
            chunksizes = tuple(int(c) for c in chunks)

        meta = VariableMeta(
            path=path,
            variable=variable,
            dims=dims,
            time_dim="time",
            lat_dim="lat",
            lon_dim="lon",
            lat_axis=lat_axis,
            lon_axis=lon_axis,
            time_axis=time_axis,
            n_time=n_time,
            n_lat=n_lat,
            n_lon=n_lon,
            lat=lat,
            lon=lon,
            times_hours=times_hours,
            time_units=time_units,
            time_calendar=time_calendar,
            source_units=str(units),
            display_units=display_units,
            scale_factor=scale_factor,
            add_offset=add_offset,
            fill_value=fill_value,
            dtype=str(var.dtype),
            chunksizes=chunksizes,
            contiguous=contiguous,
            extra={"missing_value": missing_value},
        )
        return ds, meta
    except Exception:
        ds.close()
        raise


@dataclass
class TimeBlock:
    """One timestamp's sparse reading of a time block, in original units."""

    block_start: int
    block_stop: int
    times_hours: np.ndarray
    values_k: np.ndarray  # (n_block, n_lat, n_lon) after unpacking, in Kelvin
    valid: np.ndarray  # (n_block, n_lat, n_lon) bool, fill masked out


def _read_block_raw(var, meta: VariableMeta, start: int, stop: int):
    """Read ``[start:stop)`` along time and full lat/lon, in storage order.

    The variable is sliced one axis at a time so NetCDF only touches the
    requested time indices, regardless of the file's axis order.
    """
    # Build an index tuple with full slices everywhere and a time slice.
    index: list = [slice(None)] * 3
    index[meta.time_axis] = slice(start, stop)
    raw = np.asarray(var[tuple(index)])
    # ``raw`` is in the variable's declared dimension order; move time to the
    # front and lat/lon/stray axes into (time, lat, lon).
    raw = np.moveaxis(
        raw,
        (meta.time_axis, meta.lat_axis, meta.lon_axis),
        (0, 1, 2),
    )
    return raw


def _unpack(raw: np.ndarray, meta: VariableMeta) -> tuple[np.ndarray, np.ndarray]:
    """Mask fill values on stored data, then scale, then convert units."""
    if meta.fill_value is not None:
        valid = raw != meta.fill_value
    else:
        valid = np.ones(raw.shape, dtype=bool)
    if meta.extra.get("missing_value") is not None:
        valid &= raw != meta.extra["missing_value"]

    # Cast to float only after the fill mask is fixed, so a fill code can never
    # survive scaling as a plausible temperature.
    scaled = raw.astype(np.float64) * meta.scale_factor + meta.add_offset
    # Kelvin -> Celsius (or verified no-op for Celsius) exactly once.
    converted = _convert_units(scaled, meta.source_units, meta.path, meta.variable)
    return converted, valid


def read_time_block(
    ds: Dataset,
    meta: VariableMeta,
    start: int,
    stop: int,
) -> tuple[TimeBlock, int]:
    """Read and unpack a time block; return it plus the slice count used."""
    if start < 0 or stop > meta.n_time or start >= stop:
        raise ValueError(f"bad time block [{start}, {stop}) for {meta.n_time} times")
    var = ds.variables[meta.variable]
    raw = _read_block_raw(var, meta, start, stop)
    converted, valid = _unpack(raw, meta)
    block = TimeBlock(
        block_start=start,
        block_stop=stop,
        times_hours=meta.times_hours[start:stop],
        values_k=converted,
        valid=valid,
    )
    return block, 1


def select_time(block: TimeBlock, local_index: int) -> tuple[np.ndarray, np.ndarray]:
    """Return the (lat, lon) value and validity arrays for one block slot."""
    return block.values_k[local_index], block.valid[local_index]
