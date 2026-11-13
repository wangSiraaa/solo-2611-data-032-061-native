"""端到端与单元测试。可直接运行: python -m tests.test_mosaic
也兼容 pytest(每个 test_* 函数独立、无 fixture 依赖)。

覆盖:半像元错位、重叠无效洞、边界邻域、合法零值与负值、仅 mask 失效、
网格锚点取整、开始前拒绝、窗口尺寸不变性、来源枚举顺序不变性、
独立逐像元参考核对、query-source 交叉核对、新进程按地理坐标读产物。
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rasterio.warp import transform as crs_transform

from geomosaic.engine import run_mosaic
from geomosaic.errors import PlanError
from geomosaic.grid import compute_grid
from geomosaic.plan import PRIORITY_RULE, load_plan
from geomosaic.reference import reference_mosaic

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures"
PY = sys.executable


# ---------------------------------------------------------------- 工具

def write_tif(path, data, transform, crs="EPSG:3857", nodata=-9999.0,
              mask=None, dtype="float32", count=1):
    data = np.asarray(data, dtype=dtype)
    if data.ndim == 2:
        data = data[None, :, :]
    n, h, w = data.shape
    kwargs = dict(driver="GTiff", width=w, height=h, count=n, dtype=dtype,
                  transform=transform, nodata=nodata)
    if crs is not None:
        kwargs["crs"] = CRS.from_string(crs)
    with rasterio.open(path, "w", **kwargs) as ds:
        for i in range(n):
            ds.write(data[i], i + 1)
        if mask is not None:
            ds.write_mask(np.asarray(mask, dtype="uint8"))
    return str(path)


def save_plan(directory, name, plan_dict):
    p = Path(directory) / f"{name}.json"
    p.write_text(json.dumps(plan_dict), encoding="utf-8")
    return str(p)


def base_target(**over):
    t = {"crs": "EPSG:3857", "resolution": [10, 10], "bounds": [0, 0, 40, 40],
         "grid_anchor": [0, 0], "unit": "u"}
    t.update(over)
    return t


def make_plan_dict(sources, resampling="bilinear", window_shape=(3, 3),
                   target=None, supported=("EPSG:3857", "EPSG:4326")):
    return {
        "sources": sources,
        "priority_rule": PRIORITY_RULE,
        "target": target or base_target(),
        "supported_crs": list(supported),
        "resampling": resampling,
        "window_shape": list(window_shape),
    }


def src_entry(sid, path, priority, unit="u"):
    return {"id": sid, "path": path, "priority": priority, "unit": unit}


def read_products(out):
    with rasterio.open(Path(out) / "mosaic.tif") as ds:
        val = ds.read(1).astype("float64")
    with rasterio.open(Path(out) / "mask.tif") as ds:
        msk = ds.read(1)
    with rasterio.open(Path(out) / "sources.tif") as ds:
        src = ds.read(1)
    return val, msk, src


def fixture_plan_abs(reverse=False):
    """fixtures 计划,来源路径改绝对路径,可反转枚举顺序。"""
    raw = json.loads((FIX / "plan.json").read_text(encoding="utf-8"))
    for s in raw["sources"]:
        s["path"] = str(ROOT / s["path"])
    if reverse:
        raw["sources"] = raw["sources"][::-1]
    return raw


# ---------------------------------------------------------------- 网格推算

def test_grid_snapping():
    # 计划范围不贴网格线:向外取整,实际边界由程序计算
    plan = load_plan_from_dict(make_plan_dict(
        [src_entry("S", str(FIX / "source_a.tif"), 1)],
        target=base_target(resolution=[10, 10], bounds=[3, -7, 117, 88],
                           grid_anchor=[0, 0])))
    g = compute_grid(plan)
    assert (g.width, g.height) == (12, 10), (g.width, g.height)
    assert tuple(g.transform)[:6] == (10, 0, 0, 0, -10, 90)
    assert g.bounds == (0.0, -10.0, 120.0, 90.0)
    # 非零锚点
    plan2 = load_plan_from_dict(make_plan_dict(
        [src_entry("S", str(FIX / "source_a.tif"), 1)],
        target=base_target(resolution=[10, 10], bounds=[3, -7, 117, 88],
                           grid_anchor=[5, 5])))
    g2 = compute_grid(plan2)
    assert tuple(g2.transform)[:6] == (10, 0, -5, 0, -10, 95)
    assert (g2.width, g2.height) == (13, 11)
    # 恰在网格线上的范围不得多切出像元
    plan3 = load_plan(str(FIX / "plan.json"))
    g3 = compute_grid(plan3)
    assert (g3.width, g3.height) == (20, 20)
    assert g3.bounds == (0.0, 0.0, 1000.0, 1000.0)


def load_plan_from_dict(d, directory=None):
    directory = directory or tempfile.mkdtemp()
    return load_plan(save_plan(directory, "p", d))


# ---------------------------------------------------------------- 开始前拒绝

def test_rejections():
    with tempfile.TemporaryDirectory() as td:
        t = Affine(10, 0, 0, 0, -10, 40)
        good = write_tif(Path(td) / "good.tif", np.ones((4, 4)), t)
        cases = {}

        p = write_tif(Path(td) / "nocrs.tif", np.ones((4, 4)), t, crs=None)
        cases["缺失CRS"] = make_plan_dict([src_entry("S", p, 1)])

        p = write_tif(Path(td) / "utm.tif", np.ones((4, 4)), t, crs="EPSG:32632")
        cases["CRS不在支持列表"] = make_plan_dict([src_entry("S", p, 1)])

        p = write_tif(Path(td) / "rot.tif", np.ones((4, 4)),
                      Affine(10, 1, 0, 1, -10, 40))
        cases["旋转源"] = make_plan_dict([src_entry("S", p, 1)])

        p = write_tif(Path(td) / "south.tif", np.ones((4, 4)),
                      Affine(10, 0, 0, 0, 10, 0))
        cases["非北向上"] = make_plan_dict([src_entry("S", p, 1)])

        p = write_tif(Path(td) / "twoband.tif", np.ones((2, 4, 4)), t, count=2)
        cases["多波段"] = make_plan_dict([src_entry("S", p, 1)])

        p = write_tif(Path(td) / "int.tif", np.ones((4, 4)), t, dtype="int16")
        cases["非连续量"] = make_plan_dict([src_entry("S", p, 1)])

        cases["单位不兼容"] = make_plan_dict(
            [src_entry("S", good, 1, unit="celsius")])
        bad = make_plan_dict([src_entry("S", good, 1)])
        del bad["sources"][0]["unit"]
        cases["来源缺单位"] = bad
        bad = make_plan_dict([src_entry("S", good, 1)])
        del bad["target"]["unit"]
        cases["目标缺单位"] = bad
        cases["重复id"] = make_plan_dict(
            [src_entry("S", good, 1), src_entry("S", good, 2)])
        cases["未知采样"] = make_plan_dict([src_entry("S", good, 1)],
                                           resampling="cubic")
        bad = make_plan_dict([src_entry("S", good, 1)])
        bad["priority_rule"] = "whatever"
        cases["未知裁决规则"] = bad
        cases["目标CRS不支持"] = make_plan_dict(
            [src_entry("S", good, 1)], supported=("EPSG:4326",))
        cases["文件缺失"] = make_plan_dict([src_entry("S", "/nonexistent/x.tif", 1)])

        for name, d in cases.items():
            path = save_plan(td, f"bad_{abs(hash(name))}", d)
            try:
                load_plan(path)
            except PlanError:
                continue
            raise AssertionError(f"{name}: 未被拒绝")

        # CLI 层面:退出码 2
        bad_path = save_plan(td, "bad_cli", cases["缺失CRS"])
        cp = subprocess.run([PY, "-m", "geomosaic", "plan", "--plan", bad_path],
                            capture_output=True, text=True, cwd=ROOT)
        assert cp.returncode == 2 and "拒绝" in cp.stderr, cp


# ---------------------------------------------------------------- fixtures 端到端

def test_fixture_mosaic_vs_reference():
    plan = load_plan(str(FIX / "plan.json"))
    with tempfile.TemporaryDirectory() as td:
        report = run_mosaic(plan, td)
        ver = report["verification"]
        assert ver["passed"], ver
        assert ver["mask_mismatches"] == 0 and ver["source_mismatches"] == 0

        val, msk, src = read_products(td)
        # 裁决真实发生:B 主导,A 仅在 B 无效处回退,重叠洞处双无效
        assert set(np.unique(src)) == {0, 1, 2}
        a_px = np.argwhere(src == 1)
        assert a_px.shape[0] == 6            # B 洞超出 A 洞的部分
        inv = np.argwhere(msk == 0)
        assert inv.shape[0] == 3             # 两洞重叠处
        assert (src == 2).sum() == 400 - 6 - 3
        # 负值保留;双线性只混合有效值,不越出源有效值包络(nodata 不进加权)
        assert np.nanmin(val) < 0
        lo, hi = -15.0, 24.9
        assert np.nanmin(val) >= lo - 1e-4 and np.nanmax(val) <= hi + 1e-4
        # 无效像元必须是 NaN
        assert np.isnan(val[msk == 0]).all()


def test_window_size_invariance():
    plan = load_plan(str(FIX / "plan.json"))
    products = []
    for ws in [(7, 9), (20, 20), (3, 4)]:
        with tempfile.TemporaryDirectory() as td:
            run_mosaic(plan, td, window_shape=ws, verify=False)
            products.append(read_products(td))
    for i in range(1, len(products)):
        for a, b in zip(products[0], products[i]):
            np.testing.assert_array_equal(a, b)  # 含 NaN 位置一致


def test_source_order_invariance():
    with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
        p1 = load_plan(save_plan(td1, "fwd", fixture_plan_abs()))
        p2 = load_plan(save_plan(td2, "rev", fixture_plan_abs(reverse=True)))
        run_mosaic(p1, Path(td1) / "out", verify=False)
        run_mosaic(p2, Path(td2) / "out", verify=False)
        for a, b in zip(read_products(Path(td1) / "out"),
                        read_products(Path(td2) / "out")):
            np.testing.assert_array_equal(a, b)


# ---------------------------------------------------------------- 合成小例

def test_half_pixel_offset():
    """源网格相对目标错半像元:双线性=四邻域等权平均;最近邻平局 half-up。"""
    with tempfile.TemporaryDirectory() as td:
        data = np.array([[10 * r + c for c in range(7)] for r in range(6)],
                        dtype="float32")
        p = write_tif(Path(td) / "s.tif", data, Affine(10, 0, -5, 0, -10, 55))
        d = make_plan_dict([src_entry("S", p, 1)],
                           target=base_target(bounds=[0, 0, 50, 40]))
        plan = load_plan(save_plan(td, "p", d))
        report = run_mosaic(plan, Path(td) / "out")
        assert report["verification"]["passed"]
        val, msk, _ = read_products(Path(td) / "out")
        # 每个目标中心恰好位于四个源中心中点
        expect = np.array([[np.mean(data[1:3, 0:2]), np.mean(data[1:3, 1:3]),
                            np.mean(data[1:3, 2:4]), np.mean(data[1:3, 3:5]),
                            np.mean(data[1:3, 4:6])],
                           [np.mean(data[2:4, i:i + 2]) for i in range(5)],
                           [np.mean(data[3:5, i:i + 2]) for i in range(5)],
                           [np.mean(data[4:6, i:i + 2]) for i in range(5)]])
        np.testing.assert_allclose(val, expect, rtol=0, atol=1e-6)
        assert expect[0, 0] == 15.5 and expect[3, 4] == 49.5
        assert (msk == 255).all()

        # 最近邻:gc=k+0.5 平局取上半(half-up)→ (row+1, col+1)
        d2 = make_plan_dict([src_entry("S", p, 1)], resampling="nearest",
                            target=base_target(bounds=[0, 0, 50, 40]))
        plan2 = load_plan(save_plan(td, "p2", d2))
        run_mosaic(plan2, Path(td) / "out2", verify=False)
        val2, _, _ = read_products(Path(td) / "out2")
        assert val2[0, 0] == data[2, 1] == 21.0
        assert val2[3, 4] == data[5, 5] == 55.0


def test_overlapping_holes():
    """重叠无效洞:高优先级洞由低优先级补,双洞处输出无效, nodata 不进加权。"""
    with tempfile.TemporaryDirectory() as td:
        t = Affine(10, 0, 0, 0, -10, 40)
        pv = np.array([[100 + 10 * r + c for c in range(4)] for r in range(4)],
                      dtype="float32")
        for r, c in [(1, 1), (1, 2), (2, 1)]:
            pv[r, c] = -9999.0
        qv = np.array([[200 + 10 * r + c for c in range(4)] for r in range(4)],
                      dtype="float32")
        for r, c in [(1, 1), (2, 2)]:
            qv[r, c] = -9999.0
        pp = write_tif(Path(td) / "p.tif", pv, t)
        qq = write_tif(Path(td) / "q.tif", qv, t)
        d = make_plan_dict([src_entry("P", pp, 20), src_entry("Q", qq, 10)])
        plan = load_plan(save_plan(td, "p", d))
        report = run_mosaic(plan, Path(td) / "out")
        assert report["verification"]["passed"]
        val, msk, src = read_products(Path(td) / "out")
        # 同网格对齐:双线性退化为取像元本身
        assert val[0, 0] == 100.0 and src[0, 0] == 1      # P 有效
        assert msk[1, 1] == 0 and src[1, 1] == 0          # 双洞 → 无效
        assert np.isnan(val[1, 1])
        assert val[1, 2] == 212.0 and src[1, 2] == 2      # P 洞 → Q 补
        assert val[2, 1] == 221.0 and src[2, 1] == 2
        assert val[2, 2] == 122.0 and src[2, 2] == 1      # P 有效,Q 洞无关
        assert val[3, 3] == 133.0
        # 所有有效输出都落在有效源值范围内(-9999 绝不渗入)
        assert np.nanmin(val[msk == 255]) >= 100.0
        assert np.nanmax(val[msk == 255]) <= 233.0


def test_boundary_neighborhood():
    """边界邻域:源外半像元内按边缘常数外推,之外无效;越界邻域不进加权。"""
    with tempfile.TemporaryDirectory() as td:
        data = np.array([[1.0, 2.0], [3.0, 4.0]], dtype="float32")
        p = write_tif(Path(td) / "s.tif", data, Affine(10, 0, 13, 0, -10, 33))
        d = make_plan_dict([src_entry("S", p, 1)])
        plan = load_plan(save_plan(td, "p", d))
        report = run_mosaic(plan, Path(td) / "out")
        assert report["verification"]["passed"]
        val, msk, _ = read_products(Path(td) / "out")
        nan = np.nan
        expect = np.array([
            [nan, 1.0, 1.7, 2.0],
            [nan, 1.6, 2.3, 2.6],
            [nan, 3.0, 3.7, 4.0],
            [nan, nan, nan, nan],
        ])
        np.testing.assert_allclose(val, expect, rtol=0, atol=1e-6)
        # 最近邻:越界即无效,不外推
        d2 = make_plan_dict([src_entry("S", p, 1)], resampling="nearest")
        plan2 = load_plan(save_plan(td, "p2", d2))
        run_mosaic(plan2, Path(td) / "out2", verify=False)
        val2, msk2, _ = read_products(Path(td) / "out2")
        assert msk2[0, 0] == 0 and msk2[0, 1] == 0       # 行中心在源行范围外
        assert val2[1, 2] == 2.0                          # gc=0.7→col1, gr=0.3→row0
        assert val2[2, 2] == 4.0


def test_legal_zero_and_negatives():
    """合法零与负值必须保留为有效数据,不被当作缺失。"""
    with tempfile.TemporaryDirectory() as td:
        t = Affine(10, 0, 0, 0, -10, 30)
        z = np.array([[-5.0, 0.0, 3.0],
                      [0.0, -9999.0, 1.0],
                      [-2.0, 0.0, -0.25]], dtype="float32")
        p = write_tif(Path(td) / "z.tif", z, t)
        d = make_plan_dict([src_entry("Z", p, 1)], resampling="nearest",
                           target=base_target(bounds=[0, 0, 30, 30]))
        plan = load_plan(save_plan(td, "p", d))
        report = run_mosaic(plan, Path(td) / "out")
        assert report["verification"]["passed"]
        val, msk, src = read_products(Path(td) / "out")
        assert val[0, 1] == 0.0 and msk[0, 1] == 255 and src[0, 1] == 1
        assert val[1, 0] == 0.0 and msk[1, 0] == 255
        assert val[2, 1] == 0.0 and msk[2, 1] == 255
        assert val[2, 2] == -0.25 and val[0, 0] == -5.0
        assert msk[1, 1] == 0 and np.isnan(val[1, 1])     # nodata 保持无效
        # 双线性下同网格:nodata 邻域不渗入,零值仍精确
        d2 = make_plan_dict([src_entry("Z", p, 1)],
                            target=base_target(bounds=[0, 0, 30, 30]))
        plan2 = load_plan(save_plan(td, "p2", d2))
        run_mosaic(plan2, Path(td) / "out2")
        val2, msk2, _ = read_products(Path(td) / "out2")
        assert val2[0, 1] == 0.0 and msk2[1, 1] == 0


def test_mask_only_invalid():
    """无 nodata 时,内部 mask 单独决定有效性。"""
    with tempfile.TemporaryDirectory() as td:
        t = Affine(10, 0, 0, 0, -10, 20)
        data = np.array([[7.0, 8.0], [9.0, 10.0]], dtype="float32")
        mask = np.array([[255, 0], [255, 255]], dtype="uint8")
        p = write_tif(Path(td) / "m.tif", data, t, nodata=None, mask=mask)
        d = make_plan_dict([src_entry("M", p, 1)], resampling="nearest",
                           target=base_target(bounds=[0, 0, 20, 20]))
        plan = load_plan(save_plan(td, "p", d))
        report = run_mosaic(plan, Path(td) / "out")
        assert report["verification"]["passed"]
        val, msk, _ = read_products(Path(td) / "out")
        assert val[0, 0] == 7.0 and msk[0, 1] == 0 and np.isnan(val[0, 1])
        assert val[1, 0] == 9.0 and val[1, 1] == 10.0


# ---------------------------------------------------------------- CLI 与产物核对

def test_plan_cli_ok():
    cp = subprocess.run([PY, "-m", "geomosaic", "plan", "--plan",
                         str(FIX / "plan.json")],
                        capture_output=True, text=True, cwd=ROOT)
    assert cp.returncode == 0, cp.stderr
    d = json.loads(cp.stdout)
    assert d["grid"]["width"] == 20 and d["grid"]["height"] == 20
    assert len(d["windows"]) == 9
    # 计划读取窗口均带 halo 且小于整幅源
    for w in d["windows"]:
        for rd in w["planned_reads"]:
            rw = rd["read_window"]
            assert rw["width"] <= 10 and rw["height"] <= 8


def test_query_source_cli():
    plan = load_plan(str(FIX / "plan.json"))
    grid = compute_grid(plan)
    ref_val, ref_mask, ref_src = reference_mosaic(plan, grid)
    id_by_index = {1: "A", 2: "B"}
    with tempfile.TemporaryDirectory() as td:
        run_mosaic(plan, td, verify=False)
        # 三类像元:B 主导、A 回退、双无效
        picked = {}
        for r in range(grid.height):
            for c in range(grid.width):
                key = id_by_index.get(ref_src[r, c], "none")
                picked.setdefault(key, (r, c))
        assert set(picked) == {"A", "B", "none"}
        for expect_id, (r, c) in picked.items():
            x, y = grid.center_xy(r, c)
            cp = subprocess.run(
                [PY, "-m", "geomosaic", "query-source", "--plan",
                 str(FIX / "plan.json"), "--x", str(x), "--y", str(y),
                 "--out", td], capture_output=True, text=True, cwd=ROOT)
            assert cp.returncode == 0, (expect_id, cp.stderr, cp.stdout)
            res = json.loads(cp.stdout)
            assert res["agreement"] is True, res
            assert res["pixel"] == {"row": r, "col": c,
                                    "center": list(grid.center_xy(r, c))}
            st = res["stored_product"]
            assert st["valid"] == bool(ref_mask[r, c])
            if ref_mask[r, c]:
                assert st["source_id"] == expect_id
                assert abs(st["value"] - ref_val[r, c]) <= 1e-4
            else:
                assert st["source_id"] is None
        # 同一位置用 EPSG:4326 坐标查询,结果一致
        r, c = picked["B"]
        x, y = grid.center_xy(r, c)
        lon, lat = crs_transform("EPSG:3857", "EPSG:4326", [x], [y])
        cp = subprocess.run(
            [PY, "-m", "geomosaic", "query-source", "--plan",
             str(FIX / "plan.json"), "--x", str(lon[0]), "--y", str(lat[0]),
             "--crs", "EPSG:4326", "--out", td],
            capture_output=True, text=True, cwd=ROOT)
        res = json.loads(cp.stdout)
        assert cp.returncode == 0 and res["stored_product"]["source_id"] == "B"
        # 网格外查询:不崩溃,给出说明
        cp = subprocess.run(
            [PY, "-m", "geomosaic", "query-source", "--plan",
             str(FIX / "plan.json"), "--x", "5000", "--y", "5000",
             "--out", td], capture_output=True, text=True, cwd=ROOT)
        res = json.loads(cp.stdout)
        assert cp.returncode == 0 and res["pixel"] is None


def test_fresh_process_geographic_read():
    """新进程按地理坐标独立读取产物,核对边界与数值。"""
    plan = load_plan(str(FIX / "plan.json"))
    grid = compute_grid(plan)
    ref_val, ref_mask, ref_src = reference_mosaic(plan, grid)
    with tempfile.TemporaryDirectory() as td:
        run_mosaic(plan, td, verify=False)
        # 选三个有效像元中心,经 EPSG:4326 坐标传给子进程采样
        pts = [(0, 0), (6, 5), (19, 19)]
        lons, lats = [], []
        for r, c in pts:
            x, y = grid.center_xy(r, c)
            lo, la = crs_transform("EPSG:3857", "EPSG:4326", [x], [y])
            lons.append(lo[0])
            lats.append(la[0])
        script = r"""
import json, math, sys
import rasterio
from rasterio.windows import from_bounds
from rasterio.warp import transform
out, lons, lats = sys.argv[1], sys.argv[2], sys.argv[3]
lons = [float(v) for v in lons.split(",")]
lats = [float(v) for v in lats.split(",")]
res = {}
with rasterio.open(out + "/mosaic.tif") as ds:
    res["crs"] = ds.crs.to_string()
    res["transform"] = [float(v) for v in ds.transform[:6]]
    res["bounds"] = [float(v) for v in ds.bounds]
    res["nodata_is_nan"] = math.isnan(ds.nodata)
    xs, ys = transform("EPSG:4326", ds.crs, lons, lats)
    res["sampled"] = [float(v[0]) for v in ds.sample(list(zip(xs, ys)))]
    win = from_bounds(100, 100, 400, 400, ds.transform)
    arr = ds.read(1, window=win)
    res["window"] = {"row_off": int(win.row_off), "col_off": int(win.col_off),
                     "shape": list(arr.shape), "topleft": float(arr[0, 0])}
with rasterio.open(out + "/mask.tif") as ds:
    res["mask_dtype"] = ds.dtypes[0]
with rasterio.open(out + "/sources.tif") as ds:
    res["sources_nodata"] = ds.nodata
print(json.dumps(res))
"""
        cp = subprocess.run(
            [PY, "-c", script, td, ",".join(map(str, lons)),
             ",".join(map(str, lats))],
            capture_output=True, text=True, cwd=ROOT)
        assert cp.returncode == 0, cp.stderr
        res = json.loads(cp.stdout)
        assert res["crs"] == "EPSG:3857"
        assert res["transform"] == [50, 0, 0, 0, -50, 1000]
        assert res["bounds"] == [0, 0, 1000, 1000]
        assert res["nodata_is_nan"] is True
        assert res["mask_dtype"] == "uint8"
        assert res["sources_nodata"] == 0
        for (r, c), got in zip(pts, res["sampled"]):
            assert abs(got - ref_val[r, c]) <= 1e-4, ((r, c), got, ref_val[r, c])
        assert res["window"]["shape"] == [6, 6]
        assert (res["window"]["row_off"], res["window"]["col_off"]) == (12, 2)
        assert abs(res["window"]["topleft"] - ref_val[12, 2]) <= 1e-4


def test_report_contents():
    plan = load_plan(str(FIX / "plan.json"))
    with tempfile.TemporaryDirectory() as td:
        report = run_mosaic(plan, td)
        assert report["grid"]["actual_bounds"] == [0.0, 0.0, 1000.0, 1000.0]
        assert len(report["windows"]) == 9
        by_id = {s.id: s for s in plan.sources}
        n_reads = 0
        for w in report["windows"]:
            for rd in w["reads"]:
                if "read_window" not in rd:
                    continue
                n_reads += 1
                s = by_id[rd["source"]]
                rw = rd["read_window"]
                assert 0 <= rw["col_off"] and rw["col_off"] + rw["width"] <= s.width
                assert 0 <= rw["row_off"] and rw["row_off"] + rw["height"] <= s.height
                # halo 读取窗口明显小于整幅源(不逐窗重读整个源)
                assert rw["width"] * rw["height"] < s.width * s.height
        assert n_reads > 0
        res = report["results"]
        assert res["valid_pixels"] + res["invalid_pixels"] == 400
        assert sum(res["per_source_pixels"].values()) == res["valid_pixels"]
        for info in report["outputs"].values():
            digest = hashlib.sha256((Path(td) / info["path"]).read_bytes()).hexdigest()
            assert digest == info["sha256"]
        assert report["verification"]["passed"]
        disk = json.loads((Path(td) / "report.json").read_text(encoding="utf-8"))
        assert disk["results"] == res and len(disk["windows"]) == 9


# ---------------------------------------------------------------- 运行器

def main():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
