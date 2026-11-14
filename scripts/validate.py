#!/usr/bin/env python3
"""Validation suite for the terrain estimator. Run from the repo root:

    .venv/bin/python scripts/validate.py          # expects synthetic/ to exist
    .venv/bin/python scripts/make_synthetic.py    # regenerate it if missing

Checks:
  A. analytic: exact plane -> exact centre elevation; removing one side of
     the support demotes the cell (one_sided) and worsens diagnostics;
     near-collinear support flagged; empty/insufficient reported.
  B. provided fixtures (fixtures/): input audit, two neighbourhood parameter
     sets compared on coverage and leave-one-out error, hole / step /
     mislabeled-outlier behaviour, mask<->NaN consistency.
  C. synthetic scene (synthetic/, known truth): malformed rows located,
     absolute RMSE for both parameter sets, hole interior never fabricated.
  D. CLI smoke test on the provided fixtures.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scipy.spatial import cKDTree  # noqa: E402

from terrain_cli.estimate import (  # noqa: E402
    ESTIMATED_STATUSES,
    STATUS_EMPTY,
    STATUS_INSUFFICIENT,
    STATUS_LOW,
    STATUS_OK,
    Params,
    estimate_cell,
    estimate_grid,
)
from terrain_cli.grid import load_grid  # noqa: E402
from terrain_cli.points import load_points  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def status_array(results, shape):
    arr = np.empty(shape, dtype=object)
    for r in results:
        arr[r.row, r.col] = r.status
    return arr


# ---------------------------------------------------------------------------
# A. analytic checks
# ---------------------------------------------------------------------------
def analytic_checks() -> None:
    print("\n== A. analytic checks ==")
    rng = np.random.default_rng(42)
    cx, cy = 5.0, 5.0
    plane = lambda x, y: 1.5 + 0.3 * (x - cx) - 0.2 * (y - cy)  # noqa: E731
    params = Params(radius=3.0, min_support=6, min_inliers=5)

    xy = rng.uniform([cx - 2, cy - 2], [cx + 2, cy + 2], size=(60, 2))
    z = plane(xy[:, 0], xy[:, 1])
    ids = np.array([f"p{i}" for i in range(len(xy))], dtype=object)

    r = estimate_cell((cx, cy), xy, z, ids, params)
    check("plane: status ok", r.status == STATUS_OK, f"status={r.status} {r.reasons}")
    check("plane: centre elevation exact", abs(r.z - 1.5) < 1e-6,
          f"est={r.z:.9f} true=1.5 err={abs(r.z - 1.5):.2e}")

    # delete all support west of the target -> must be reported, not smoothed
    west = xy[:, 0] < cx
    z_noisy = z + rng.normal(0.0, 0.02, z.size)
    r_bal = estimate_cell((cx, cy), xy, z_noisy, ids, params)
    r_one = estimate_cell((cx, cy), xy[~west], z_noisy[~west], ids[~west], params)
    check("one-sided: status demoted",
          r_one.status == STATUS_LOW and "one_sided" in r_one.reasons,
          f"status={r_one.status} reasons={r_one.reasons}")
    check("one-sided: diagnostics worsen",
          r_one.extrapolation_ratio > r_bal.extrapolation_ratio
          and r_one.max_gap_deg > 180.0,
          f"extrapolation {r_bal.extrapolation_ratio:.2f} -> "
          f"{r_one.extrapolation_ratio:.2f}, max_gap={r_one.max_gap_deg:.0f} deg")
    print(f"       |err| balanced={abs(r_bal.z - 1.5):.4f} m, "
          f"one-sided={abs(r_one.z - 1.5):.4f} m")

    # near-collinear support
    t = rng.uniform(-2.0, 2.0, 30)
    xy_col = np.column_stack([cx + t, cy + 0.05 * t + rng.normal(0, 1e-3, t.size)])
    r_col = estimate_cell((cx, cy), xy_col, plane(xy_col[:, 0], xy_col[:, 1]),
                          ids[:30], params)
    check("near-collinear: flagged",
          r_col.status == STATUS_LOW and "near_collinear" in r_col.reasons,
          f"status={r_col.status} reasons={r_col.reasons}")

    r_empty = estimate_cell((cx, cy), np.empty((0, 2)), np.empty(0), [], params)
    check("empty: reported", r_empty.status == STATUS_EMPTY)
    r_few = estimate_cell((cx, cy), xy[:4], z[:4], ids[:4], params)
    check("insufficient: reported, no estimate",
          r_few.status == STATUS_INSUFFICIENT and np.isnan(r_few.z))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def loo_cv_errors(xyz, ids, params, exclude_ids=()):
    """Leave-one-out error at each ground point (truth-free error probe)."""
    tree = cKDTree(xyz[:, :2])
    errs, xs = [], []
    for i in range(len(xyz)):
        if ids[i] in exclude_ids:
            continue
        idx = [j for j in tree.query_ball_point(xyz[i, :2], params.radius) if j != i]
        if len(idx) < params.min_support:
            continue
        sel = np.asarray(idx)
        r = estimate_cell(tuple(xyz[i, :2]), xyz[sel, :2], xyz[sel, 2],
                          ids[sel], params)
        if r.status in ESTIMATED_STATUSES:
            errs.append(r.z - xyz[i, 2])
            xs.append(xyz[i, 0])
    return np.array(errs), np.array(xs)


# ---------------------------------------------------------------------------
# B. provided fixture checks
# ---------------------------------------------------------------------------
# tight: ~2 ground spacings in the dense area; wide: the grid's declared
# max_support_radius
TIGHT = Params(radius=1.0, min_support=6, min_inliers=5)
WIDE = Params(radius=1.5, min_support=4, min_inliers=4)


def provided_fixture_checks() -> dict:
    print("\n== B. provided fixtures (fixtures/) ==")
    points = load_points(ROOT / "fixtures" / "points.csv")
    grid = load_grid(ROOT / "fixtures" / "grid.json")

    check("input: 336 valid rows, 334 ground",
          points.total_rows == 336 and points.n_ground == 334,
          f"rows={points.total_rows} ground={points.n_ground}")
    check("input: clean file, no issues", len(points.issues) == 0)
    check("input: 2 non-ground points excluded by class",
          points.excluded_by_class == {5: 2}, f"{points.excluded_by_class}")
    check("grid: 20x20 cells of 0.5 m, max_support_radius 1.5",
          grid.shape == (20, 20) and grid.resolution == 0.5
          and grid.max_support_radius == 1.5)

    g = points.ground_mask
    gids, gxyz = points.point_ids[g], points.xyz[g]
    XS, YS = grid.center_grid()

    runs = {}
    print("\n-- parameter comparison: coverage and leave-one-out error --")
    print(f"{'params':<7}{'radius':>7}{'min_sup':>8}{'coverage':>10}"
          f"{'CV med|e|':>11}{'CV p90':>9}{'CV max|e|':>11}")
    for name, params in (("tight", TIGHT), ("wide", WIDE)):
        terrain, mask, results = estimate_grid(grid, gids, gxyz, params)
        status = status_array(results, grid.shape)
        runs[name] = (terrain, mask, results, status)

        check(f"{name}: mask <-> finite terrain consistent",
              bool(np.all(np.isfinite(terrain) == mask)))

        # mislabeled high points p333 (z=8) / p334 (z=13) must support nothing
        supported = set()
        for r in results:
            supported.update(r.support_ids)
        check(f"{name}: outliers p333/p334 never used as support",
              "p333" not in supported and "p334" not in supported)

        # hole: cells far from any ground point
        tree = cKDTree(gxyz[:, :2])
        dist, _ = tree.query(np.column_stack([XS.ravel(), YS.ravel()]))
        hole = dist.reshape(grid.shape) > 1.0
        hole_statuses = Counter(status[hole].tolist())
        if name == "tight":
            check(f"{name}: hole cells carry no estimate",
                  not bool(np.any(mask[hole])), f"{dict(hole_statuses)}")
        else:
            check(f"{name}: hole cells never 'ok' (one-sided at best)",
                  hole_statuses.get(STATUS_OK, 0) == 0, f"{dict(hole_statuses)}")

        # step at x=7: adjacent cells must be flagged, not silently averaged
        mixed = [r for r in results
                 if "mixed_levels" in r.reasons and abs(r.x - 7.0) <= params.radius]
        check(f"{name}: step cells flagged mixed_levels", len(mixed) >= 4,
              f"{len(mixed)} cells near x=7, x={sorted({r.x for r in mixed})}")

        cv, cvx = loo_cv_errors(gxyz, gids, params, exclude_ids=("p333", "p334"))
        cov = 100.0 * mask.mean()
        med = float(np.median(np.abs(cv)))
        p90 = float(np.percentile(np.abs(cv), 90))
        mx = float(np.abs(cv).max())
        print(f"{name:<7}{params.radius:>7.2f}{params.min_support:>8}"
              f"{cov:>9.1f}%{med:>11.4f}{p90:>9.4f}{mx:>11.4f}")
        runs[name] += (cov, med, p90)

    t, w = runs["tight"], runs["wide"]
    check("coverage: wide > tight", w[4] > t[4], f"tight={t[4]:.1f}% wide={w[4]:.1f}%")
    check("LOO error: tight median <= wide", t[5] <= w[5] + 1e-12,
          f"tight={t[5]:.4f} wide={w[5]:.4f}")
    check("LOO error: tight p90 <= wide", t[6] <= w[6] + 1e-12,
          f"tight={t[6]:.4f} wide={w[6]:.4f}")

    # sparse east (x >= 7.5): tight radius must not fabricate coverage
    east = XS >= 7.5
    cov_east_t = float(t[1][east].mean())
    cov_east_w = float(w[1][east].mean())
    check("sparse east: tight coverage much lower than wide",
          cov_east_t < 0.35 < cov_east_w,
          f"tight={100 * cov_east_t:.0f}% wide={100 * cov_east_w:.0f}%")
    return runs


# ---------------------------------------------------------------------------
# C. synthetic scene with known truth
# ---------------------------------------------------------------------------
SYN_TIGHT = Params(radius=2.0, min_support=8, min_inliers=6)
SYN_WIDE = Params(radius=5.0, min_support=4, min_inliers=4)


def synthetic_checks() -> None:
    print("\n== C. synthetic scene (synthetic/, known truth) ==")
    syn = ROOT / "synthetic"
    if not (syn / "points.csv").exists():
        print("  synthetic/ missing - run scripts/make_synthetic.py first")
        FAILURES.append("synthetic scene missing")
        return
    points = load_points(syn / "points.csv")
    grid = load_grid(syn / "grid.json")
    truth = np.load(syn / "truth.npy")

    check("input: 6 malformed rows located", len(points.issues) == 6,
          "; ".join(f"line {i.line} {i.point_id!r}" for i in points.issues))
    check("input: valid points unaffected",
          points.total_rows - len(points.issues) == len(points.point_ids))
    check("input: non-ground classes counted",
          set(points.excluded_by_class) == {3, 4, 5, 6},
          f"{points.excluded_by_class}")

    XS, YS = grid.center_grid()
    step_region = np.abs(XS - 20.0) <= 3.0
    hole_interior = (XS >= 47.0) & (XS <= 49.0) & (YS >= 11.0) & (YS <= 17.0)

    g = points.ground_mask
    gids, gxyz = points.point_ids[g], points.xyz[g]

    print(f"\n{'params':<7}{'radius':>7}{'min_sup':>8}{'coverage':>10}"
          f"{'RMSE':>8}{'MAE':>8}{'RMSE_step':>10}")
    runs = {}
    for name, params in (("tight", SYN_TIGHT), ("wide", SYN_WIDE)):
        terrain, mask, results = estimate_grid(grid, gids, gxyz, params)
        status = status_array(results, grid.shape)
        err = terrain[mask] - truth[mask]
        rmse = float(np.sqrt(np.mean(err**2)))
        mae = float(np.mean(np.abs(err)))
        es = terrain[mask & step_region] - truth[mask & step_region]
        rmse_step = float(np.sqrt(np.mean(es**2)))
        cov = 100.0 * mask.mean()
        print(f"{name:<7}{params.radius:>7.1f}{params.min_support:>8}"
              f"{cov:>9.1f}%{rmse:>8.4f}{mae:>8.4f}{rmse_step:>10.4f}")
        runs[name] = (terrain, mask, status, rmse, rmse_step, cov)

        check(f"{name}: no reliable estimate inside the hole",
              not bool(np.any(status[hole_interior] == STATUS_OK)),
              f"interior: {dict(Counter(status[hole_interior].tolist()))}")
        mixed = [r for r in results
                 if "mixed_levels" in r.reasons and abs(r.x - 20.0) <= 3.0]
        check(f"{name}: step cells flagged mixed_levels", len(mixed) >= 4,
              f"{len(mixed)} cells")

    t, w = runs["tight"], runs["wide"]
    check("coverage: wide > tight", w[5] > t[5], f"{t[5]:.1f}% vs {w[5]:.1f}%")
    check("accuracy: tight RMSE <= wide RMSE", t[3] <= w[3] + 1e-12,
          f"{t[3]:.4f} vs {w[3]:.4f}")
    check("accuracy: tight step RMSE <= wide step RMSE", t[4] <= w[4] + 1e-12,
          f"{t[4]:.4f} vs {w[4]:.4f}")
    check("accuracy: tight RMSE < 0.15 m", t[3] < 0.15, f"{t[3]:.4f}")
    check("accuracy: wide RMSE < 0.35 m", w[3] < 0.35, f"{w[3]:.4f}")

    # mislabeled high ground points must not pull the surface up
    terrain, mask, status = t[0], t[1], t[2]
    ok_outside_step = (status == STATUS_OK) & ~step_region
    err = np.abs(terrain[ok_outside_step] - truth[ok_outside_step])
    check("outliers: p99 |err| on ok cells < 0.25 m",
          float(np.percentile(err, 99)) < 0.25,
          f"p99={np.percentile(err, 99):.4f} m")


# ---------------------------------------------------------------------------
# D. CLI smoke test on the provided fixtures
# ---------------------------------------------------------------------------
def cli_check() -> None:
    print("\n== D. CLI smoke test (provided fixtures) ==")
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [sys.executable, "-m", "terrain_cli", "build",
               "--points", str(ROOT / "fixtures" / "points.csv"),
               "--grid", str(ROOT / "fixtures" / "grid.json"),
               "--out", tmp]
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        check("cli: exit 0", proc.returncode == 0,
              proc.stderr.strip().splitlines()[-1] if proc.stderr else "")
        if proc.returncode != 0:
            print(proc.stderr)
            return
        import json

        terrain = np.load(Path(tmp) / "terrain.npy")
        mask = np.load(Path(tmp) / "mask.npy")
        check("cli: terrain shape (20, 20)", terrain.shape == (20, 20),
              f"{terrain.shape}")
        check("cli: NaN exactly where mask False",
              bool(np.all(np.isnan(terrain) == ~mask)))
        cfg = json.loads((Path(tmp) / "run_config.json").read_text())
        check("cli: default radius taken from grid max_support_radius",
              cfg["params"]["radius"] == 1.5, f"radius={cfg['params']['radius']}")
        lines = (Path(tmp) / "cells.csv").read_text().strip().splitlines()
        check("cli: cells.csv has 400 rows", len(lines) == 401,
              f"{len(lines) - 1} data rows")
        issues = (Path(tmp) / "issues.csv").read_text().strip().splitlines()
        check("cli: issues.csv header only (clean input)", len(issues) == 1)


def main() -> int:
    analytic_checks()
    provided_fixture_checks()
    synthetic_checks()
    cli_check()
    print("\n" + ("ALL CHECKS PASSED" if not FAILURES else
                  f"{len(FAILURES)} CHECK(S) FAILED: {FAILURES}"))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
