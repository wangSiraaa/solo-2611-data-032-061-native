"""独立逐目标像元参考实现:无窗口、无 halo、纯标量循环。

用于核对引擎产物。与引擎共享的只有计划/网格对象与 rasterio 本身;
采样与裁决逻辑在此独立重写(标量版),语义见 sampling.py 模块 docstring。

双线性误差界:产物经 float32 存储(相对量化误差 ≤ 2^-24 ≈ 6e-8),
float64 累加次序差异 << 1e-12,故容差取 atol=1e-6、rtol=1e-5 足够宽松
且仍能发现任何真实错位;最近邻应严格相等。
"""
from __future__ import annotations

import math

import numpy as np
import rasterio
from rasterio.warp import transform as crs_transform

VALUE_ATOL = 1e-6
VALUE_RTOL = 1e-5


def _sample_scalar(data, valid, gcol, grow, mode):
    """标量采样;data/valid 为整幅源数组。返回 (value, ok)。"""
    h, w = data.shape
    if mode == "nearest":
        c = math.floor(gcol + 0.5)
        r = math.floor(grow + 0.5)
        if 0 <= r < h and 0 <= c < w and valid[r, c]:
            return float(data[r, c]), True
        return math.nan, False
    if mode == "bilinear":
        c0 = math.floor(gcol)
        r0 = math.floor(grow)
        dx = gcol - c0
        dy = grow - r0
        num = 0.0
        den = 0.0
        for dr in (0, 1):
            for dc in (0, 1):
                wt = (dy if dr else 1.0 - dy) * (dx if dc else 1.0 - dx)
                if wt == 0.0:
                    continue
                rr, cc = r0 + dr, c0 + dc
                if 0 <= rr < h and 0 <= cc < w and valid[rr, cc]:
                    num += wt * float(data[rr, cc])
                    den += wt
        if den > 0.0:
            return num / den, True
        return math.nan, False
    raise ValueError(mode)


def reference_mosaic(plan, grid):
    """逐输出像元独立计算 (values float64, mask bool, sources int)。

    整幅读入各源一次,不做任何窗口化;裁决顺序与引擎相同。
    """
    order = plan.priority_order
    src_index = plan.source_index
    h, w = grid.height, grid.width
    values = np.full((h, w), np.nan)
    mask = np.zeros((h, w), bool)
    sources = np.zeros((h, w), np.int64)

    loaded = {}
    for spec in plan.sources:
        with rasterio.open(spec.path) as ds:
            data = ds.read(1).astype(np.float64)
            msk = ds.read_masks(1)
        valid = msk > 0
        valid &= np.isfinite(data)
        if spec.nodata is not None and not math.isnan(spec.nodata):
            valid &= data != float(spec.nodata)
        loaded[spec.id] = (data, valid)

    for r in range(h):
        for c in range(w):
            x, y = grid.center_xy(r, c)
            for spec in order:
                t = spec.transform
                xt, yt = crs_transform(plan.target_crs, spec.crs, [x], [y])
                gcol = (xt[0] - t.c) / t.a - 0.5
                grow = (yt[0] - t.f) / t.e - 0.5
                if not (math.isfinite(gcol) and math.isfinite(grow)):
                    continue
                data, valid = loaded[spec.id]
                v, ok = _sample_scalar(data, valid, gcol, grow, plan.resampling)
                if ok:
                    values[r, c] = v
                    mask[r, c] = True
                    sources[r, c] = src_index[spec.id]
                    break
    return values, mask, sources


def compare_products(prod_val, prod_mask, prod_src,
                     ref_val, ref_mask, ref_src,
                     atol: float = VALUE_ATOL, rtol: float = VALUE_RTOL) -> dict:
    """核对产物与参考实现;返回统计与是否通过。"""
    mask_mismatch = int((prod_mask != ref_mask).sum())
    source_mismatch = int((prod_src != ref_src).sum())
    # 无效像元在产物中必须为 NaN
    invalid_not_nan = int((~prod_mask & ~np.isnan(prod_val)).sum())

    both_valid = prod_mask & ref_mask
    diffs = np.abs(prod_val[both_valid] - ref_val[both_valid])
    tol = atol + rtol * np.abs(ref_val[both_valid])
    value_mismatch = int((diffs > tol).sum()) if diffs.size else 0
    max_abs = float(diffs.max()) if diffs.size else 0.0

    passed = (mask_mismatch == 0 and source_mismatch == 0
              and value_mismatch == 0 and invalid_not_nan == 0)
    return {
        "method": "independent per-pixel scalar reference (no windowing)",
        "tolerance": {"atol": atol, "rtol": rtol,
                      "note": "float32 存储量化 + float64 累加次序;最近邻应严格相等"},
        "pixels": int(prod_mask.size),
        "value_max_abs_diff": max_abs,
        "value_mismatches": value_mismatch,
        "mask_mismatches": mask_mismatch,
        "source_mismatches": source_mismatch,
        "invalid_value_not_nan": invalid_not_nan,
        "passed": passed,
    }
