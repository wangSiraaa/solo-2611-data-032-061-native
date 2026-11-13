"""query-source 命令:按地理坐标查询来源与数值。

对查询点做两件事并相互核对:
1. 用与引擎相同的采样/裁决过程,从源数据现场重算(单个点、带 halo 的最小读取);
2. 若已有产物,按地理坐标独立读取 mosaic/mask/sources 三个栅格。
两者一致才视为可信;同时给出查询点处的连续估计。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.warp import transform as crs_transform

from .engine import SourceSession
from .grid import compute_grid
from .reference import VALUE_ATOL, VALUE_RTOL


def _recompute_at(plan, x_t, y_t):
    """在目标 CRS 坐标处按优先级现场重算 (value, source_id)。"""
    with SourceSession(plan) as session:
        for spec in plan.priority_order:
            v, ok, _ = session.sample_points(
                spec, np.array([x_t]), np.array([y_t]), plan.resampling)
            if ok[0]:
                return {"valid": True, "value": float(v[0]), "source_id": spec.id}
    return {"valid": False, "value": None, "source_id": None}


def _read_stored(out_dir, row, col):
    out = Path(out_dir)
    paths = [out / "mosaic.tif", out / "mask.tif", out / "sources.tif",
             out / "sources_index.json"]
    if not all(p.exists() for p in paths):
        return None
    with rasterio.open(out / "mosaic.tif") as ds:
        val = float(ds.read(1)[row, col])
    with rasterio.open(out / "mask.tif") as ds:
        msk = int(ds.read(1)[row, col])
    with rasterio.open(out / "sources.tif") as ds:
        sidx = int(ds.read(1)[row, col])
    index_doc = json.loads((out / "sources_index.json").read_text(encoding="utf-8"))
    valid = msk > 0
    return {
        "valid": valid,
        "value": val if valid else None,
        "raw_value": val,
        "source_index": sidx,
        "source_id": index_doc.get(str(sidx)),
    }


def _agree(stored, recomputed) -> bool | None:
    if stored is None or recomputed is None:
        return None
    if stored["valid"] != recomputed["valid"]:
        return False
    if not stored["valid"]:
        return stored["source_id"] in (None, recomputed["source_id"])
    if stored["source_id"] != recomputed["source_id"]:
        return False
    diff = abs(stored["value"] - recomputed["value"])
    return bool(diff <= VALUE_ATOL + VALUE_RTOL * abs(recomputed["value"]))


def query_point(plan, x, y, crs=None, out_dir=None) -> dict:
    grid = compute_grid(plan)
    q_crs = crs or plan.target_crs
    if CRS.from_string(q_crs) != CRS.from_string(plan.target_crs):
        xt, yt = crs_transform(q_crs, plan.target_crs, [x], [y])
        x_t, y_t = float(xt[0]), float(yt[0])
    else:
        x_t, y_t = float(x), float(y)

    result = {
        "query": {"x": x, "y": y, "crs": q_crs},
        "target_crs": plan.target_crs,
        "target_point": [x_t, y_t],
    }

    col = math.floor((x_t - grid.transform.c) / grid.resolution[0])
    row = math.floor((grid.transform.f - y_t) / grid.resolution[1])
    inside = 0 <= row < grid.height and 0 <= col < grid.width

    result["continuous_at_point"] = _recompute_at(plan, x_t, y_t)

    stored = None
    recomputed_center = None
    if inside:
        xc, yc = grid.center_xy(row, col)
        result["pixel"] = {"row": row, "col": col, "center": [xc, yc]}
        recomputed_center = _recompute_at(plan, xc, yc)
        result["recomputed_at_pixel_center"] = recomputed_center
        if out_dir is not None:
            stored = _read_stored(out_dir, row, col)
            result["stored_product"] = stored
    else:
        result["pixel"] = None
        result["note"] = "查询点在输出网格外,无像元产品;仅给出连续估计"

    result["agreement"] = _agree(stored, recomputed_center)
    return result
