"""Tests for the zonal_stats query command.

The main implementation is cross-checked against an independent, purely
per-pixel world-coordinate determination (ray casting in plain Python, no
shapely predicates), covering rotated grids, hole boundaries, out-of-bounds
regions and varying read block sizes.
"""

import json
import math
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine

from zonal_stats import run_queries
from zonal_stats.cli import main as cli_main

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# --------------------------------------------------------------------------- #
# independent per-pixel determination (no shapely)
# --------------------------------------------------------------------------- #
def ring_covers_point(ring, x, y):
    """True if (x, y) is inside the ring or exactly on its boundary."""
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if (cross == 0.0 and min(x1, x2) <= x <= max(x1, x2)
                and min(y1, y2) <= y <= max(y1, y2)):
            return True
        if (y1 > y) != (y2 > y):
            x_int = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x_int > x:
                inside = not inside
    return inside


def independent_selected(geom, transform, row, col):
    """World-coordinate centre test for one pixel, fully independent of the
    implementation under test."""
    x, y = transform @ (col + 0.5, row + 0.5)
    shell, holes = geom["coordinates"][0], geom["coordinates"][1:]
    if not ring_covers_point(shell, x, y):
        return False
    return not any(ring_covers_point(h, x, y) for h in holes)


def independent_mask(geom, transform, height, width):
    return np.array(
        [[independent_selected(geom, transform, r, c) for c in range(width)]
         for r in range(height)], dtype=bool)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def write_raster(path, data, transform, nodata=-9999.0, mask=None,
                 crs="EPSG:3857"):
    data = np.asarray(data, dtype="float32")
    h, w = data.shape
    kwargs = dict(driver="GTiff", width=w, height=h, count=1,
                  dtype="float32", crs=crs, transform=transform)
    if nodata is not None:
        kwargs["nodata"] = nodata
    with rasterio.open(path, "w", **kwargs) as ds:
        ds.write(data, 1)
    if mask is not None:
        with rasterio.open(path, "r+") as ds:
            ds.write_mask(np.asarray(mask, dtype="uint8"))


def write_geojson(path, features):
    fc = {"type": "FeatureCollection",
          "features": [{"type": "Feature",
                        "properties": {"region_id": rid},
                        "geometry": geom} for rid, geom in features]}
    path.write_text(json.dumps(fc), encoding="utf-8")


def write_config(path, max_read_pixels=128, crs="EPSG:3857"):
    path.write_text(json.dumps({"max_read_pixels": max_read_pixels,
                                "crs": crs}), encoding="utf-8")


def square(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates":
            [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def fixture_geoms():
    fc = json.loads((FIXTURES / "regions.geojson").read_text())
    return {f["properties"]["region_id"]: f["geometry"]
            for f in fc["features"]}


def by_id(report):
    return {q["region_id"]: q for q in report["queries"]}


def run_fixture(tmp_path, max_read_pixels=None):
    return run_queries(FIXTURES / "surface.tif", FIXTURES / "regions.geojson",
                       FIXTURES / "query-config.json", tmp_path,
                       max_read_pixels=max_read_pixels)


# --------------------------------------------------------------------------- #
# fixture runs
# --------------------------------------------------------------------------- #
def test_fixture_statuses_and_deliverables(tmp_path):
    report = run_fixture(tmp_path)
    entries = by_id(report)
    assert entries["with-hole"]["status"] == "ok"
    assert entries["partial"]["status"] == "partial"
    assert entries["outside"]["status"] == "out_of_bounds"

    outside = entries["outside"]
    assert outside["pixels_read"] == 0
    assert outside["read_windows"] == []
    assert outside["outputs"] is None
    assert not (tmp_path / "outside_data.npy").exists()

    for name in ["report.json", "metadata.json", "boundaries.geojson",
                 "with-hole_data.npy", "with-hole_mask.npy",
                 "partial_data.npy", "partial_mask.npy"]:
        assert (tmp_path / name).exists(), name

    # reads are limited to each query's own window, never the whole raster
    raster_pixels = report["width"] * report["height"]
    for q in report["queries"]:
        if q["window"] is None:
            continue
        win_pixels = q["window"]["height"] * q["window"]["width"]
        assert q["pixels_read"] == win_pixels < raster_pixels
        for w in q["read_windows"]:
            assert w["pixels"] <= report["max_read_pixels"]


@pytest.mark.parametrize("rid", ["with-hole", "partial"])
def test_fixture_matches_independent_pixels(tmp_path, rid):
    report = run_fixture(tmp_path)
    entry = by_id(report)[rid]
    win = entry["window"]
    mask = np.load(tmp_path / f"{rid}_mask.npy")
    data_clip = np.load(tmp_path / f"{rid}_data.npy")
    with rasterio.open(FIXTURES / "surface.tif") as ds:
        full = ds.read(1)
        file_mask = ds.read_masks(1)
        transform, nodata = ds.transform, ds.nodata
        height, width = ds.height, ds.width

    selected = independent_mask(fixture_geoms()[rid], transform, height, width)
    valid = (file_mask != 0) & ~np.isnan(full) & (full != nodata)
    expect = selected & valid

    r0, c0 = win["row_off"], win["col_off"]
    h, w = win["height"], win["width"]
    # no selected pixel may fall outside the conservative window
    placed = np.zeros_like(expect)
    placed[r0:r0 + h, c0:c0 + w] = mask
    assert np.array_equal(placed, expect)
    # clipped array placement: element (i, j) == source (r0+i, c0+j)
    assert np.array_equal(data_clip, full[r0:r0 + h, c0:c0 + w],
                          equal_nan=True)

    vals = full[expect].astype(np.float64)
    stats = entry["stats"]
    assert stats["count"] == vals.size
    assert stats["min"] == vals.min()
    assert stats["max"] == vals.max()
    assert math.isclose(stats["mean"], vals.mean(), rel_tol=1e-6)
    assert entry["coverage"]["selected_cells"] == int(selected.sum())
    assert entry["coverage"]["valid_cells"] == int(expect.sum())


def test_block_size_does_not_change_selection(tmp_path):
    runs = {}
    for mrp in (7, 128, 4096):
        out = tmp_path / f"mrp{mrp}"
        runs[mrp] = (run_fixture(out, max_read_pixels=mrp), out)
    for rid in ("with-hole", "partial"):
        ref_report, ref_out = runs[7]
        ref = by_id(ref_report)[rid]
        ref_mask = np.load(ref_out / f"{rid}_mask.npy")
        for mrp in (128, 4096):
            report, out = runs[mrp]
            entry = by_id(report)[rid]
            # the selected pixels must be identical for any block size
            assert np.array_equal(ref_mask, np.load(out / f"{rid}_mask.npy"))
            assert entry["stats"]["count"] == ref["stats"]["count"]
            assert entry["stats"]["min"] == ref["stats"]["min"]
            assert entry["stats"]["max"] == ref["stats"]["max"]
            assert math.isclose(entry["stats"]["mean"], ref["stats"]["mean"],
                                rel_tol=report["mean_relative_tolerance"])
    for mrp, (report, _) in runs.items():
        for q in report["queries"]:
            for w in q["read_windows"]:
                assert w["pixels"] <= mrp


# --------------------------------------------------------------------------- #
# synthetic cases
# --------------------------------------------------------------------------- #
def test_hole_and_outer_boundary(tmp_path):
    # centres at (c+0.5, 9.5-r); polygon edges pass exactly through centres
    transform = Affine(1, 0, 0, 0, -1, 10)
    write_raster(tmp_path / "r.tif", np.full((10, 10), 5.0), transform)
    geom = {"type": "Polygon", "coordinates": [
        [[2.5, 7.5], [7.5, 7.5], [7.5, 2.5], [2.5, 2.5], [2.5, 7.5]],
        [[3.5, 6.5], [6.5, 6.5], [6.5, 3.5], [3.5, 3.5], [3.5, 6.5]],
    ]}
    write_geojson(tmp_path / "r.geojson", [("holey", geom)])
    write_config(tmp_path / "c.json", max_read_pixels=16)
    report = run_queries(tmp_path / "r.tif", tmp_path / "r.geojson",
                         tmp_path / "c.json", tmp_path / "out")
    entry = report["queries"][0]
    assert entry["status"] == "ok"
    assert entry["window"] == {"row_off": 2, "col_off": 2,
                               "height": 6, "width": 6}
    assert entry["stats"]["count"] == 20  # 36 outer - 16 hole (incl. boundary)
    assert entry["stats"]["mean"] == 5.0

    mask = np.load(tmp_path / "out" / "holey_mask.npy")
    assert mask[0, 0] and mask[0, 5] and mask[5, 0] and mask[5, 5]  # outer corners
    assert mask[0, 2] and mask[3, 0]                                # outer edges
    assert not mask[1, 1] and not mask[1, 4] and not mask[4, 1]     # hole boundary
    assert not mask[2, 2] and not mask[3, 3]                        # hole interior
    assert int(mask.sum()) == 20

    meta = json.loads((tmp_path / "out" / "metadata.json").read_text())
    t = meta["queries"]["holey"]["transform"]
    assert t == [1.0, 0.0, 2.0, 0.0, -1.0, 8.0]  # clipped affine


def test_rotated_grid_selection_and_placement(tmp_path):
    transform = Affine(8, 2, 1000, 1, -8, 2400)  # rotated, like the fixture
    rows, cols = np.indices((6, 9))
    data = (rows * 100 + cols).astype("float32")  # encodes (row, col)
    write_raster(tmp_path / "r.tif", data, transform)
    # pixel-space rectangle: corner coords cols 2..8, rows 1..5
    ring = [list(transform @ (c, r))
            for c, r in [(2, 1), (8, 1), (8, 5), (2, 5), (2, 1)]]
    geom = {"type": "Polygon", "coordinates": [ring]}
    write_geojson(tmp_path / "r.geojson", [("rot", geom)])
    write_config(tmp_path / "c.json", max_read_pixels=5)
    report = run_queries(tmp_path / "r.tif", tmp_path / "r.geojson",
                         tmp_path / "c.json", tmp_path / "out")
    entry = report["queries"][0]
    assert entry["status"] == "ok"
    assert entry["window"] == {"row_off": 1, "col_off": 2,
                               "height": 4, "width": 6}
    expect = data[1:5, 2:8]
    stats = entry["stats"]
    assert stats["count"] == 24
    assert stats["min"] == expect.min() and stats["max"] == expect.max()
    assert math.isclose(stats["mean"], expect.mean(), rel_tol=1e-6)
    # placement: no row/column swap, values land back on their source pixels
    assert np.array_equal(np.load(tmp_path / "out" / "rot_data.npy"), expect)
    assert np.load(tmp_path / "out" / "rot_mask.npy").all()
    # independent per-pixel agreement over the whole raster
    sel = independent_mask(geom, transform, 6, 9)
    assert sel.sum() == 24
    placed = np.zeros((6, 9), dtype=bool)
    placed[1:5, 2:8] = True
    assert np.array_equal(sel, placed)


def test_zero_nodata_nan_validity(tmp_path):
    transform = Affine(1, 0, 0, 0, -1, 4)
    data = np.array([[0, -9999, np.nan, 5],
                     [1, 2, 3, 4],
                     [0, 0, 6, 7],
                     [8, 9, 10, 11]], dtype="float32")
    write_raster(tmp_path / "r.tif", data, transform)
    write_geojson(tmp_path / "r.geojson", [("all", square(0, 0, 4, 4))])
    write_config(tmp_path / "c.json", max_read_pixels=3)
    report = run_queries(tmp_path / "r.tif", tmp_path / "r.geojson",
                         tmp_path / "c.json", tmp_path / "out")
    stats = report["queries"][0]["stats"]
    assert stats["count"] == 14          # nodata and NaN excluded
    assert stats["min"] == 0.0           # real zeros are valid
    assert stats["max"] == 11.0
    assert math.isclose(stats["mean"], 66.0 / 14.0, rel_tol=1e-6)


def test_file_mask_marks_invalid(tmp_path):
    transform = Affine(1, 0, 0, 0, -1, 4)
    data = np.full((4, 4), 2.0, dtype="float32")
    data[1, 1] = -9999.0
    data[2, 2] = np.nan
    mask = np.full((4, 4), 255, dtype="uint8")
    mask[0, 0] = mask[3, 3] = 0
    write_raster(tmp_path / "r.tif", data, transform, mask=mask)
    write_geojson(tmp_path / "r.geojson", [("all", square(0, 0, 4, 4))])
    write_config(tmp_path / "c.json")
    report = run_queries(tmp_path / "r.tif", tmp_path / "r.geojson",
                         tmp_path / "c.json", tmp_path / "out")
    stats = report["queries"][0]["stats"]
    # 16 cells - 2 file-masked - 1 nodata - 1 NaN
    assert stats["count"] == 12
    assert stats["min"] == stats["max"] == stats["mean"] == 2.0


def test_empty_region_all_nodata(tmp_path):
    transform = Affine(1, 0, 0, 0, -1, 4)
    write_raster(tmp_path / "r.tif", np.full((4, 4), -9999.0), transform)
    write_geojson(tmp_path / "r.geojson", [("all", square(0, 0, 4, 4))])
    write_config(tmp_path / "c.json")
    report = run_queries(tmp_path / "r.tif", tmp_path / "r.geojson",
                         tmp_path / "c.json", tmp_path / "out")
    entry = report["queries"][0]
    assert entry["status"] == "empty"
    assert entry["stats"] is None
    assert entry["coverage"]["selected_cells"] == 16
    assert entry["coverage"]["valid_cells"] == 0
    assert entry["coverage"]["fraction"] == 0.0


def test_small_region_reads_only_its_window(tmp_path):
    transform = Affine(8, 2, 1000, 1, -8, 2400)
    write_raster(tmp_path / "big.tif", np.ones((300, 300)), transform)
    ring = [list(transform @ (c, r)) for c, r in
            [(150, 100), (155, 100), (155, 104), (150, 104), (150, 100)]]
    geom = {"type": "Polygon", "coordinates": [ring]}
    write_geojson(tmp_path / "r.geojson", [("small", geom)])
    write_config(tmp_path / "c.json", max_read_pixels=100_000)

    out1 = tmp_path / "out1"
    report = run_queries(tmp_path / "big.tif", tmp_path / "r.geojson",
                         tmp_path / "c.json", out1)
    entry = report["queries"][0]
    assert entry["window"] == {"row_off": 100, "col_off": 150,
                               "height": 4, "width": 5}
    assert len(entry["read_windows"]) == 1
    assert entry["pixels_read"] == 20          # not the 90000-cell raster
    assert entry["stats"]["count"] == 20

    # tiny blocks: tiles partition exactly the same window, same selection
    out2 = tmp_path / "out2"
    report2 = run_queries(tmp_path / "big.tif", tmp_path / "r.geojson",
                          tmp_path / "c.json", out2, max_read_pixels=6)
    entry2 = report2["queries"][0]
    cover = np.zeros((4, 5), dtype=int)
    for w in entry2["read_windows"]:
        assert w["pixels"] <= 6
        cover[w["row_off"] - 100:w["row_off"] - 100 + w["height"],
              w["col_off"] - 150:w["col_off"] - 150 + w["width"]] += 1
    assert np.array_equal(cover, np.ones((4, 5), dtype=int))
    assert np.array_equal(np.load(out1 / "small_mask.npy"),
                          np.load(out2 / "small_mask.npy"))
    assert entry2["stats"]["count"] == 20


def test_crs_mismatch_rejected(tmp_path):
    transform = Affine(1, 0, 0, 0, -1, 4)
    write_raster(tmp_path / "r.tif", np.ones((4, 4)), transform)
    write_geojson(tmp_path / "r.geojson", [("all", square(0, 0, 4, 4))])
    write_config(tmp_path / "c.json", crs="EPSG:4326")
    with pytest.raises(ValueError, match="reprojection"):
        run_queries(tmp_path / "r.tif", tmp_path / "r.geojson",
                    tmp_path / "c.json", tmp_path / "out")


def test_unsupported_geometry_status(tmp_path):
    transform = Affine(1, 0, 0, 0, -1, 4)
    write_raster(tmp_path / "r.tif", np.ones((4, 4)), transform)
    multi = {"type": "MultiPolygon",
             "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]]]}
    write_geojson(tmp_path / "r.geojson", [("multi", multi)])
    write_config(tmp_path / "c.json")
    report = run_queries(tmp_path / "r.tif", tmp_path / "r.geojson",
                         tmp_path / "c.json", tmp_path / "out")
    assert report["queries"][0]["status"] == "unsupported"


def test_cli_query(tmp_path):
    out = tmp_path / "out"
    rc = cli_main(["query", "--raster", str(FIXTURES / "surface.tif"),
                   "--regions", str(FIXTURES / "regions.geojson"),
                   "--config", str(FIXTURES / "query-config.json"),
                   "--out", str(out)])
    assert rc == 0
    report = json.loads((out / "report.json").read_text())
    assert {q["region_id"] for q in report["queries"]} == \
        {"with-hole", "partial", "outside"}
