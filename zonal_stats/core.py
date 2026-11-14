"""Core zonal-statistics engine.

Selection rule: a pixel participates when its *centre* lies inside the query
polygon or on the polygon's outer boundary; centres inside a hole or on a
hole boundary are excluded.

Validity rule: the file mask must be non-zero, the value must not be NaN and
must differ from the band nodata value.  Genuine 0.0 values are valid data.

Rotated grids: the query geometry is transformed into pixel space with the
inverse affine and a conservative read window is derived from the bounds of
the *whole* transformed geometry (never from just two world corners, which
would be wrong once rotation mixes the axes).  Pixels are then selected by
an exact centre test, so the read block size cannot change which pixels are
selected.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import rasterio
import shapely
from affine import Affine
from rasterio.crs import CRS
from rasterio.windows import Window
from shapely.affinity import affine_transform
from shapely.geometry import Polygon, mapping, shape

MEAN_RELATIVE_TOLERANCE = 1e-6

SELECTION_RULE = (
    "pixel centre inside polygon or on outer boundary; "
    "hole interiors and hole boundaries excluded"
)
VALIDITY_RULE = (
    "file mask != 0 and value is not NaN and value != nodata; "
    "real 0.0 values are valid"
)

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_OUT_OF_BOUNDS = "out_of_bounds"
STATUS_EMPTY = "empty"
STATUS_UNSUPPORTED = "unsupported"


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #
def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    mrp = cfg.get("max_read_pixels")
    if not isinstance(mrp, int) or isinstance(mrp, bool) or mrp < 1:
        raise ValueError("config 'max_read_pixels' must be a positive integer")
    return cfg


def load_regions(path):
    fc = json.loads(Path(path).read_text(encoding="utf-8"))
    if fc.get("type") != "FeatureCollection":
        raise ValueError("regions file must be a GeoJSON FeatureCollection")
    regions = []
    for i, feat in enumerate(fc.get("features", [])):
        props = feat.get("properties") or {}
        rid = props.get("region_id") or props.get("id") or feat.get("id")
        regions.append((str(rid) if rid is not None else f"region-{i}",
                        feat.get("geometry")))
    return fc, regions


# --------------------------------------------------------------------------- #
# geometry / windows
# --------------------------------------------------------------------------- #
def geometry_to_pixel(geom, transform):
    """Transform a shapely geometry from world to pixel coordinates."""
    inv = ~transform
    # shapely: x' = m0*x + m1*y + m4 ; y' = m2*x + m3*y + m5
    return affine_transform(geom, [inv.a, inv.b, inv.d, inv.e, inv.c, inv.f])


def conservative_window(geom_px, width, height):
    """Window guaranteed to contain every pixel whose centre can fall inside
    ``geom_px`` (pixel-space geometry).  Safe for rotated grids because the
    full geometry, not two corner points, defines the bounds.

    A small, magnitude-scaled epsilon absorbs affine round-trip noise when
    the geometry maps back onto (near-)integer pixel coordinates; at well
    under half a pixel it can never add or drop a selectable pixel centre.

    Returns ``(window, clipped)``; ``window`` is ``None`` when the geometry
    lies entirely outside the raster.
    """
    min_col, min_row, max_col, max_row = geom_px.bounds
    scale = max(1.0, abs(min_col), abs(min_row), abs(max_col), abs(max_row))
    eps = 1e-9 * scale
    col0, row0 = math.floor(min_col + eps), math.floor(min_row + eps)
    col1, row1 = math.ceil(max_col - eps), math.ceil(max_row - eps)
    clipped = col0 < 0 or row0 < 0 or col1 > width or row1 > height
    c0, r0 = max(0, col0), max(0, row0)
    c1, r1 = min(width, col1), min(height, row1)
    if c1 <= c0 or r1 <= r0:
        return None, clipped
    return Window(col_off=c0, row_off=r0, width=c1 - c0, height=r1 - r0), clipped


def tile_windows(window, max_pixels):
    """Split ``window`` into read tiles of at most ``max_pixels`` cells.

    Tiles exactly partition the window; only the tile dimensions depend on
    ``max_pixels``, never the set of pixels covered.
    """
    tile_w = min(window.width, max_pixels)
    tile_h = min(window.height, max(1, max_pixels // tile_w))
    row = window.row_off
    row_end = window.row_off + window.height
    col_end = window.col_off + window.width
    while row < row_end:
        h = min(tile_h, row_end - row)
        col = window.col_off
        while col < col_end:
            w = min(tile_w, col_end - col)
            yield Window(col_off=col, row_off=row, width=w, height=h)
            col += w
        row += h


def _covers_xy(poly, x, y):
    """Vectorized 'covers' (interior or boundary).  Shapely ships no
    ``covers_xy``, so combine interior containment with boundary contact."""
    return (shapely.contains_xy(poly, x, y)
            | shapely.intersects_xy(poly.boundary, x, y))


def selection_mask(shell, holes, transform, window):
    """Boolean (height, width) array: True where the pixel centre is covered
    by the outer ring (boundary included) and not covered by any hole ring
    (hole interior *and* boundary excluded).

    Evaluated in world coordinates: centres are computed with the affine
    (rotation included) and tested against the original geometry, so no
    inverse-transform round-trip can shift a centre across a boundary.
    """
    cols = window.col_off + np.arange(window.width, dtype=np.float64) + 0.5
    rows = window.row_off + np.arange(window.height, dtype=np.float64) + 0.5
    x = transform.a * cols[None, :] + transform.b * rows[:, None] + transform.c
    y = transform.d * cols[None, :] + transform.e * rows[:, None] + transform.f
    sel = _covers_xy(shell, x, y)
    for hole in holes:
        sel &= ~_covers_xy(hole, x, y)
    return sel


def validity_mask(data, file_mask, nodata):
    """Boolean array: file mask, NaN and nodata jointly decide validity."""
    valid = file_mask != 0
    valid &= ~np.isnan(data)
    if nodata is not None and not math.isnan(nodata):
        valid &= data != nodata
    return valid


def window_polygon(transform, window):
    """World-coordinates polygon of a pixel window (for boundary exports)."""
    c0, r0 = window.col_off, window.row_off
    c1, r1 = c0 + window.width, r0 + window.height
    pts = [transform @ (c, r)
           for c, r in [(c0, r0), (c1, r0), (c1, r1), (c0, r1), (c0, r0)]]
    return Polygon(pts)


# --------------------------------------------------------------------------- #
# per-region query
# --------------------------------------------------------------------------- #
@dataclass
class RegionResult:
    region_id: str
    status: str = STATUS_OK
    clipped: bool = False
    window: Window | None = None
    transform: Affine | None = None
    count: int = 0
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    selected_cells: int = 0
    read_windows: list = field(default_factory=list)
    data: np.ndarray | None = None
    mask: np.ndarray | None = None


def query_region(ds, region_id, geom, max_read_pixels):
    """Run one query region against an open single-band rasterio dataset."""
    res = RegionResult(region_id=region_id)
    if not isinstance(geom, dict) or geom.get("type") != "Polygon":
        res.status = STATUS_UNSUPPORTED
        return res
    poly = shape(geom)
    if poly.is_empty:
        res.status = STATUS_UNSUPPORTED
        return res

    poly_px = geometry_to_pixel(poly, ds.transform)
    shell = Polygon(poly.exterior)
    holes = [Polygon(ring) for ring in poly.interiors]

    window, clipped = conservative_window(poly_px, ds.width, ds.height)
    res.clipped = clipped
    if window is None:
        res.status = STATUS_OUT_OF_BOUNDS
        return res
    res.window = window
    res.transform = ds.window_transform(window)  # clipped affine

    data_out = np.full((window.height, window.width), np.nan, dtype=np.float32)
    mask_out = np.zeros((window.height, window.width), dtype=bool)

    nodata = ds.nodata
    count = 0
    sum64 = 0.0
    vmin, vmax = math.inf, -math.inf
    selected = 0

    for tile in tile_windows(window, max_read_pixels):
        tile_data = ds.read(1, window=tile)
        tile_mask = ds.read_masks(1, window=tile)
        res.read_windows.append({
            "row_off": int(tile.row_off), "col_off": int(tile.col_off),
            "height": int(tile.height), "width": int(tile.width),
            "pixels": int(tile.height * tile.width),
        })
        sel = selection_mask(shell, holes, ds.transform, tile)
        take = sel & validity_mask(tile_data, tile_mask, nodata)
        selected += int(sel.sum())
        if take.any():
            vals = tile_data[take].astype(np.float64)
            count += int(vals.size)
            sum64 += float(vals.sum())
            vmin = min(vmin, float(vals.min()))
            vmax = max(vmax, float(vals.max()))
        r = tile.row_off - window.row_off
        c = tile.col_off - window.col_off
        data_out[r:r + tile.height, c:c + tile.width] = tile_data
        mask_out[r:r + tile.height, c:c + tile.width] = take

    res.data, res.mask = data_out, mask_out
    res.count, res.selected_cells = count, selected
    if count == 0:
        res.status = STATUS_EMPTY
    elif clipped:
        res.status = STATUS_PARTIAL
    if count:
        res.min, res.max, res.mean = vmin, vmax, sum64 / count
    return res


# --------------------------------------------------------------------------- #
# whole run
# --------------------------------------------------------------------------- #
def _check_crs(cfg, fc, ds):
    cfg_crs = cfg.get("crs")
    if cfg_crs and CRS.from_user_input(cfg_crs) != ds.crs:
        raise ValueError(
            f"config crs {cfg_crs!r} != raster crs {ds.crs!r}; "
            "reprojection is not supported")
    geo = fc.get("crs")  # legacy GeoJSON crs member, if present
    if isinstance(geo, dict):
        name = (geo.get("properties") or {}).get("name")
        if name and CRS.from_user_input(name) != ds.crs:
            raise ValueError(
                f"regions crs {name!r} != raster crs {ds.crs!r}; "
                "reprojection is not supported")


def run_queries(raster_path, regions_path, config_path, out_dir,
                max_read_pixels=None):
    """Execute all queries and write report.json, metadata.json, clipped
    arrays/masks and boundaries.geojson into ``out_dir``."""
    cfg = load_config(config_path)
    mrp = max_read_pixels if max_read_pixels is not None else cfg["max_read_pixels"]
    if not isinstance(mrp, int) or mrp < 1:
        raise ValueError("max_read_pixels must be a positive integer")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fc, regions = load_regions(regions_path)

    queries, metadata = [], {}
    boundaries = []
    with rasterio.open(raster_path) as ds:
        if ds.count != 1:
            raise ValueError("only single-band rasters are supported")
        _check_crs(cfg, fc, ds)

        full_window = Window(col_off=0, row_off=0, width=ds.width, height=ds.height)
        boundaries.append({
            "type": "Feature",
            "properties": {"kind": "raster_footprint"},
            "geometry": mapping(window_polygon(ds.transform, full_window)),
        })

        for rid, geom in regions:
            res = query_region(ds, rid, geom, mrp)
            outputs = None
            if res.window is not None:
                data_fn, mask_fn = f"{rid}_data.npy", f"{rid}_mask.npy"
                np.save(out / data_fn, res.data)
                np.save(out / mask_fn, res.mask)
                outputs = {"data": data_fn, "mask": mask_fn}
                win = {
                    "row_off": int(res.window.row_off),
                    "col_off": int(res.window.col_off),
                    "height": int(res.window.height),
                    "width": int(res.window.width),
                }
                metadata[rid] = {
                    "window": win,
                    "shape": [win["height"], win["width"]],
                    "transform": list(res.transform)[:6],
                    "crs": str(ds.crs),
                    "nodata": ds.nodata,
                    "arrays": outputs,
                    "mask_semantics": "True = pixel centre selected by polygon "
                                      "and value valid (participates in stats)",
                    "pixel_placement": "array[i, j] = source pixel "
                                       "(row_off + i, col_off + j)",
                }
                boundaries.append({
                    "type": "Feature",
                    "properties": {"kind": "clip_window", "region_id": rid},
                    "geometry": mapping(window_polygon(ds.transform, res.window)),
                })
            if isinstance(geom, dict):
                boundaries.append({
                    "type": "Feature",
                    "properties": {"kind": "region", "region_id": rid},
                    "geometry": geom,
                })
            queries.append({
                "region_id": rid,
                "status": res.status,
                "clipped_by_raster_edge": res.clipped,
                "stats": None if res.count == 0 else {
                    "count": res.count, "min": res.min,
                    "max": res.max, "mean": res.mean,
                },
                "coverage": {
                    "selected_cells": res.selected_cells,
                    "valid_cells": res.count,
                    "fraction": (res.count / res.selected_cells)
                                if res.selected_cells else 0.0,
                },
                "window": metadata.get(rid, {}).get("window"),
                "read_windows": res.read_windows,
                "pixels_read": sum(w["pixels"] for w in res.read_windows),
                "outputs": outputs,
            })

        report = {
            "raster": str(raster_path),
            "regions": str(regions_path),
            "crs": str(ds.crs),
            "width": ds.width,
            "height": ds.height,
            "dtype": ds.dtypes[0],
            "nodata": ds.nodata,
            "transform": list(ds.transform)[:6],
            "config": cfg,
            "max_read_pixels": mrp,
            "selection_rule": SELECTION_RULE,
            "validity_rule": VALIDITY_RULE,
            "mean_relative_tolerance": MEAN_RELATIVE_TOLERANCE,
            "queries": queries,
        }
        metadata_doc = {
            "raster": {
                "path": str(raster_path), "crs": str(ds.crs),
                "transform": list(ds.transform)[:6],
                "width": ds.width, "height": ds.height,
                "nodata": ds.nodata,
            },
            "queries": metadata,
        }

    (out / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "metadata.json").write_text(
        json.dumps(metadata_doc, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "boundaries.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": boundaries},
                   indent=2, ensure_ascii=False), encoding="utf-8")
    return report
