"""窗口化镶嵌引擎。

执行方式:
- 输出按 window_shape 切窗;每个输出像元的取值只依赖其自身坐标与源数据,
  与窗口划分、来源枚举顺序无关,因此改窗口大小不会产生额外接缝。
- 每个窗口内,按裁决顺序(优先级降序、id 升序)依次采样各源;
  只对尚未填满的像元计算坐标,并按采样核所需的最小索引范围(halo)
  从源读取一个窗口,绝不逐窗重读整幅源。每次实际读取都记入报告。
- 同一采样与裁决过程同时产出数值与来源栅格:每个有效输出像元记录
  真正贡献的来源,而不是按覆盖框填一个来源号。
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.warp import transform as crs_transform
from rasterio.windows import Window

from . import __version__
from .grid import TargetGrid, compute_grid, window_iter
from .plan import PRIORITY_RULE, Plan, SourceSpec
from .sampling import needed_read_range, sample_block

MASK_VALID = 255
MASK_INVALID = 0


class SourceSession:
    """持有打开的源数据集,对一组目标 CRS 坐标做带 halo 的最小窗口读取。"""

    def __init__(self, plan: Plan):
        self.plan = plan
        self._handles = {s.id: rasterio.open(s.path) for s in plan.sources}

    def close(self):
        for ds in self._handles.values():
            ds.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def sample_points(self, spec: SourceSpec, xs, ys, mode: str):
        """对目标 CRS 坐标点采样单个源。

        返回 (values, ok, read_log);read_log 记录实际读取窗口(含 halo)
        或跳过原因,用于报告说明执行方式。
        """
        xs = np.asarray(xs, dtype=np.float64)
        ys = np.asarray(ys, dtype=np.float64)
        n = xs.size
        values = np.full(n, np.nan)
        ok = np.zeros(n, bool)
        ds = self._handles[spec.id]
        t = spec.transform
        log: dict = {"source": spec.id, "crs": spec.crs, "points": n}

        xt, yt = crs_transform(self.plan.target_crs, spec.crs, xs, ys)
        gx = np.asarray(xt, dtype=np.float64)
        gy = np.asarray(yt, dtype=np.float64)
        # 中心相对小数像元坐标(北向上无旋转已在校验阶段保证)
        gc = (gx - t.c) / t.a - 0.5
        gr = (gy - t.f) / t.e - 0.5
        finite = np.isfinite(gc) & np.isfinite(gr)
        if not finite.any():
            log["skipped"] = "coordinates_not_transformable"
            return values, ok, log
        idx = np.flatnonzero(finite)

        c_lo, c_hi = needed_read_range(float(gc[idx].min()), float(gc[idx].max()), mode)
        r_lo, r_hi = needed_read_range(float(gr[idx].min()), float(gr[idx].max()), mode)
        log["footprint_window"] = {
            "col_off": math.floor(float(gc[idx].min())),
            "row_off": math.floor(float(gr[idx].min())),
            "width": math.floor(float(gc[idx].max())) - math.floor(float(gc[idx].min())) + 1,
            "height": math.floor(float(gr[idx].max())) - math.floor(float(gr[idx].min())) + 1,
        }
        # halo 读取窗口裁剪到源栅格范围;源外邻域按无效处理,不影响边界
        cc_lo, cc_hi = max(0, c_lo), min(spec.width, c_hi)
        rr_lo, rr_hi = max(0, r_lo), min(spec.height, r_hi)
        if cc_lo >= cc_hi or rr_lo >= rr_hi:
            log["skipped"] = "no_overlap_with_source"
            return values, ok, log
        win = Window(cc_lo, rr_lo, cc_hi - cc_lo, rr_hi - rr_lo)
        data = ds.read(1, window=win).astype(np.float64)
        msk = ds.read_masks(1, window=win)
        valid = msk > 0
        valid &= np.isfinite(data)
        if spec.nodata is not None and not math.isnan(spec.nodata):
            valid &= data != float(spec.nodata)
        fp = log["footprint_window"]
        log["read_window"] = {"col_off": int(win.col_off), "row_off": int(win.row_off),
                              "width": int(win.width), "height": int(win.height)}
        log["halo_px"] = {
            "left": fp["col_off"] - int(win.col_off),
            "right": int(win.col_off + win.width) - (fp["col_off"] + fp["width"]),
            "top": fp["row_off"] - int(win.row_off),
            "bottom": int(win.row_off + win.height) - (fp["row_off"] + fp["height"]),
        }

        lc = gc[idx] - win.col_off
        lr = gr[idx] - win.row_off
        v, ok_sub = sample_block(data, valid, lc, lr, mode)
        values[idx] = v
        ok[idx] = ok_sub
        log["valid_samples"] = int(ok_sub.sum())
        return values, ok, log


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _source_extent_in_target(plan: Plan, spec: SourceSpec) -> list[float]:
    t = spec.transform
    xs = [t.c, t.c + spec.width * t.a]
    ys = [t.f, t.f + spec.height * t.e]
    xt, yt = crs_transform(spec.crs, plan.target_crs, xs, ys)
    return [float(min(xt)), float(min(yt)), float(max(xt)), float(max(yt))]


def source_dict(plan: Plan, spec: SourceSpec) -> dict:
    t = spec.transform
    return {
        "id": spec.id, "path": spec.path, "priority": spec.priority, "unit": spec.unit,
        "crs": spec.crs, "width": spec.width, "height": spec.height,
        "transform": [t.a, t.b, t.c, t.d, t.e, t.f],
        "resolution": [t.a, -t.e], "nodata": spec.nodata, "dtype": spec.dtype,
        "mask_flags": spec.mask_flags,
        "extent_in_target_crs": _source_extent_in_target(plan, spec),
    }


def grid_dict(plan: Plan, grid: TargetGrid) -> dict:
    t = grid.transform
    return {
        "crs": grid.crs, "resolution": list(grid.resolution),
        "grid_anchor": list(plan.grid_anchor),
        "requested_bounds": list(plan.bounds),
        "transform": [t.a, t.b, t.c, t.d, t.e, t.f],
        "width": grid.width, "height": grid.height,
        "actual_bounds": [float(v) for v in grid.bounds],
    }


def plan_diagnostics(plan: Plan) -> dict:
    """plan 命令:校验后的网格推算与逐窗口读取计划(不读像元数据)。"""
    grid = compute_grid(plan)
    windows = []
    for r0, c0, h, w in window_iter(grid, plan.window_shape):
        xs, ys = _window_corner_centers(grid, r0, c0, h, w)
        reads = []
        for spec in plan.sources:
            reads.append(_planned_read(plan, grid, spec, xs, ys))
        windows.append({
            "output_window": {"row_off": r0, "col_off": c0, "height": h, "width": w},
            "planned_reads": reads,
        })
    return {
        "plan": plan.path, "valid": True,
        "resampling": plan.resampling, "priority_rule": PRIORITY_RULE,
        "window_shape": list(plan.window_shape),
        "grid": grid_dict(plan, grid),
        "sources": [source_dict(plan, s) for s in plan.sources],
        "source_index": {str(v): k for k, v in
                         sorted(plan.source_index.items(), key=lambda kv: kv[1])},
        "windows": windows,
    }


def _window_corner_centers(grid: TargetGrid, r0, c0, h, w):
    corners = [(r0, c0), (r0, c0 + w - 1), (r0 + h - 1, c0), (r0 + h - 1, c0 + w - 1)]
    pts = [grid.center_xy(r, c) for r, c in corners]
    return [p[0] for p in pts], [p[1] for p in pts]


def _planned_read(plan: Plan, grid: TargetGrid, spec: SourceSpec, xs, ys) -> dict:
    t = spec.transform
    xt, yt = crs_transform(plan.target_crs, spec.crs, list(xs), list(ys))
    gc = [(x - t.c) / t.a - 0.5 for x in xt]
    gr = [(y - t.f) / t.e - 0.5 for y in yt]
    c_lo, c_hi = needed_read_range(min(gc), max(gc), plan.resampling)
    r_lo, r_hi = needed_read_range(min(gr), max(gr), plan.resampling)
    cc_lo, cc_hi = max(0, c_lo), min(spec.width, c_hi)
    rr_lo, rr_hi = max(0, r_lo), min(spec.height, r_hi)
    if cc_lo >= cc_hi or rr_lo >= rr_hi:
        return {"source": spec.id, "skipped": "no_overlap_with_source"}
    return {"source": spec.id,
            "read_window": {"col_off": cc_lo, "row_off": rr_lo,
                            "width": cc_hi - cc_lo, "height": rr_hi - rr_lo}}


def run_mosaic(plan: Plan, out_dir, window_shape=None, verify: bool = True) -> dict:
    """执行镶嵌,写出产品与 report.json,返回报告字典。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    grid = compute_grid(plan)
    wshape = tuple(window_shape) if window_shape else plan.window_shape
    order = plan.priority_order
    src_index = plan.source_index
    src_dtype = "uint8" if len(plan.sources) <= 255 else "uint16"
    crs = CRS.from_string(grid.crs)

    profiles = {
        "mosaic": dict(driver="GTiff", width=grid.width, height=grid.height, count=1,
                       dtype="float32", crs=crs, transform=grid.transform,
                       nodata=float("nan")),
        "mask": dict(driver="GTiff", width=grid.width, height=grid.height, count=1,
                     dtype="uint8", crs=crs, transform=grid.transform),
        "sources": dict(driver="GTiff", width=grid.width, height=grid.height, count=1,
                        dtype=src_dtype, crs=crs, transform=grid.transform, nodata=0),
    }
    windows_log: list[dict] = []
    valid_pixels = 0
    per_source = {s.id: 0 for s in plan.sources}
    vsum = 0.0
    vmin = math.inf
    vmax = -math.inf

    with SourceSession(plan) as session, \
            rasterio.open(out / "mosaic.tif", "w", **profiles["mosaic"]) as d_mos, \
            rasterio.open(out / "mask.tif", "w", **profiles["mask"]) as d_msk, \
            rasterio.open(out / "sources.tif", "w", **profiles["sources"]) as d_src:
        d_mos.update_tags(unit=plan.unit, resampling=plan.resampling,
                          priority_rule=PRIORITY_RULE)
        d_msk.update_tags(semantics="valid=255 invalid=0")
        d_src.update_tags(source_ids=",".join(sorted(src_index, key=src_index.get)),
                          semantics="0=no source; 1..N per source_ids order")

        for r0, c0, h, w in window_iter(grid, wshape):
            xs = grid.transform.c + (c0 + 0.5 + np.arange(w)) * grid.resolution[0]
            ys = grid.transform.f - (r0 + 0.5 + np.arange(h)) * grid.resolution[1]
            xx, yy = np.meshgrid(xs, ys)
            filled = np.zeros((h, w), bool)
            val = np.full((h, w), np.nan)
            sid = np.zeros((h, w), src_dtype)
            reads: list[dict] = []

            for spec in order:
                rem = ~filled
                n_rem = int(rem.sum())
                if n_rem == 0:
                    reads.append({"source": spec.id, "skipped": "window_already_filled"})
                    continue
                v, ok, log = session.sample_points(
                    spec, xx[rem], yy[rem], plan.resampling)
                log["remaining_pixels"] = n_rem
                reads.append(log)
                if ok.any():
                    sel = np.flatnonzero(rem.ravel())[ok]
                    val.ravel()[sel] = v[ok]
                    sid.ravel()[sel] = src_index[spec.id]
                    filled.ravel()[sel] = True

            msk = np.where(filled, MASK_VALID, MASK_INVALID).astype("uint8")
            win = Window(c0, r0, w, h)
            val32 = val.astype("float32")
            d_mos.write(val32, 1, window=win)
            d_msk.write(msk, 1, window=win)
            d_src.write(sid, 1, window=win)

            n_valid = int(filled.sum())
            valid_pixels += n_valid
            if n_valid:
                vv = val32[filled]
                vsum += float(vv.sum())
                vmin = min(vmin, float(vv.min()))
                vmax = max(vmax, float(vv.max()))
            ids, counts = np.unique(sid, return_counts=True)
            for i_, c_ in zip(ids.tolist(), counts.tolist()):
                if i_ != 0:
                    per_source[order_id_by_index(src_index, i_)] += int(c_)
            windows_log.append({
                "output_window": {"row_off": r0, "col_off": c0, "height": h, "width": w},
                "reads": reads,
            })

    index_doc = {"0": None}
    for sid_, idx_ in sorted(src_index.items(), key=lambda kv: kv[1]):
        index_doc[str(idx_)] = sid_
    (out / "sources_index.json").write_text(
        json.dumps(index_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    outputs = {}
    for name, fname, dtype, nodata in [
        ("mosaic", "mosaic.tif", "float32", "NaN"),
        ("mask", "mask.tif", "uint8", None),
        ("sources", "sources.tif", src_dtype, 0),
        ("sources_index", "sources_index.json", None, None),
    ]:
        p = out / fname
        outputs[name] = {"path": fname, "sha256": _sha256(p)}
        if dtype:
            outputs[name]["dtype"] = dtype
        if nodata is not None:
            outputs[name]["nodata"] = nodata

    report = {
        "tool": {"name": "geomosaic", "version": __version__,
                 "rasterio": rasterio.__version__, "numpy": np.__version__},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan": plan.path,
        "resampling": plan.resampling,
        "priority_rule": PRIORITY_RULE,
        "window_shape": list(wshape),
        "grid": grid_dict(plan, grid),
        "sources": [source_dict(plan, s) for s in plan.sources],
        "source_index": index_doc,
        "windows": windows_log,
        "results": {
            "valid_pixels": valid_pixels,
            "invalid_pixels": grid.width * grid.height - valid_pixels,
            "per_source_pixels": per_source,
            "value_stats": {
                "min": vmin if valid_pixels else None,
                "max": vmax if valid_pixels else None,
                "mean": (vsum / valid_pixels) if valid_pixels else None,
            },
        },
        "outputs": outputs,
    }

    if verify:
        from .reference import compare_products, reference_mosaic
        ref_val, ref_mask, ref_src = reference_mosaic(plan, grid)
        with rasterio.open(out / "mosaic.tif") as ds:
            pv = ds.read(1).astype(np.float64)
        with rasterio.open(out / "mask.tif") as ds:
            pm = ds.read(1) > 0
        with rasterio.open(out / "sources.tif") as ds:
            ps = ds.read(1).astype(np.int64)
        report["verification"] = compare_products(pv, pm, ps, ref_val, ref_mask, ref_src)
    else:
        report["verification"] = {"skipped": True}

    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def order_id_by_index(src_index: dict[str, int], idx: int) -> str:
    for sid_, i_ in src_index.items():
        if i_ == idx:
            return sid_
    raise KeyError(idx)
