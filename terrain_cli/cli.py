"""Command line interface: `python -m terrain_cli build ...`."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

from .estimate import ESTIMATED_STATUSES, STATUSES, Params, estimate_grid
from .grid import load_grid
from .points import load_points

CSV_FIELDS = [
    "row",
    "col",
    "x",
    "y",
    "z",
    "status",
    "reasons",
    "candidate_count",
    "support_count",
    "support_ids",
    "sigma",
    "max_abs_residual",
    "max_gap_deg",
    "extrapolation_ratio",
    "collinear_ratio",
]


def _fmt(v: float) -> str:
    return f"{v:.6g}"


def _write_cells_csv(path: Path, results) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_FIELDS)
        for r in results:
            estimated = r.status in ESTIMATED_STATUSES
            w.writerow(
                [
                    r.row,
                    r.col,
                    _fmt(r.x),
                    _fmt(r.y),
                    _fmt(r.z) if estimated else "",
                    r.status,
                    "|".join(r.reasons),
                    r.candidate_count,
                    r.support_count if estimated else "",
                    ";".join(r.support_ids),
                    _fmt(r.sigma) if estimated else "",
                    _fmt(r.max_abs_residual) if estimated else "",
                    _fmt(r.max_gap_deg) if estimated else "",
                    _fmt(r.extrapolation_ratio) if estimated else "",
                    _fmt(r.collinear_ratio) if estimated else "",
                ]
            )


def _write_issues_csv(path: Path, issues) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["line", "point_id", "reason"])
        for i in issues:
            w.writerow([i.line, i.point_id, i.reason])


def cmd_build(args: argparse.Namespace) -> int:
    points = load_points(args.points)
    grid = load_grid(args.grid)
    radius = args.radius
    if radius is None:
        radius = grid.max_support_radius if grid.max_support_radius else Params().radius
    params = Params(
        radius=radius,
        min_support=args.min_support,
        min_inliers=args.min_inliers,
        outlier_k=args.outlier_k,
        clip_iters=args.clip_iters,
        low_z_fraction=args.low_z_fraction,
        sigma_floor=args.sigma_floor,
        extrapolation_max=args.extrapolation_max,
        collinear_ratio=args.collinear_ratio,
        mixed_levels_min_frac=args.mixed_levels_min_frac,
        mixed_levels_min_step=args.mixed_levels_min_step,
    )

    gmask = points.ground_mask
    terrain, mask, results = estimate_grid(
        grid, points.point_ids[gmask], points.xyz[gmask], params
    )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    np.save(out / "terrain.npy", terrain)
    np.save(out / "mask.npy", mask)
    _write_cells_csv(out / "cells.csv", results)
    _write_issues_csv(out / "issues.csv", points.issues)
    (out / "run_config.json").write_text(
        json.dumps(
            {
                "points": str(args.points),
                "grid": str(args.grid),
                "params": vars(params),
                "conventions": {
                    "array": "terrain[row, col]; row 0 = smallest y (south), "
                    "col 0 = smallest x (west)",
                    "cell_center": "x = xmin + (col+0.5)*resolution, "
                    "y = ymin + (row+0.5)*resolution",
                    "invalid": "terrain is NaN exactly where mask is False; "
                    "a real 0.0 m elevation keeps mask True",
                },
            },
            indent=2,
        )
    )

    # ---- summary to stdout -------------------------------------------------
    n_valid = len(points.point_ids)
    print(f"input: {points.total_rows} data rows -> {n_valid} valid points "
          f"({points.n_ground} ground candidates), {len(points.issues)} rejected rows")
    if points.issues:
        for issue in points.issues[:10]:
            print(f"  rejected: {issue}")
        if len(points.issues) > 10:
            print(f"  ... see {out / 'issues.csv'}")
    if points.excluded_by_class:
        by_cls = ", ".join(
            f"class {c}: {n}" for c, n in sorted(points.excluded_by_class.items())
        )
        print(f"excluded non-ground valid points: {by_cls}")

    counts = {s: 0 for s in STATUSES}
    for r in results:
        counts[r.status] += 1
    total = len(results)
    covered = counts["ok"] + counts["low_confidence"]
    print(f"cells: {total} ({grid.n_rows}x{grid.n_cols}, res {grid.resolution} m)")
    for s in STATUSES:
        print(f"  {s:20s} {counts[s]:6d} ({100.0 * counts[s] / total:5.1f}%)")
    print(f"coverage (ok + low_confidence): {100.0 * covered / total:.1f}%")
    print(f"wrote {out}/terrain.npy {terrain.shape} float64, mask.npy, cells.csv, "
          f"issues.csv, run_config.json")
    return 0


def build_parser() -> argparse.ArgumentParser:
    d = Params()
    p = argparse.ArgumentParser(
        prog="terrain_cli",
        description="Local terrain estimation from a classified point cloud.",
    )
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="estimate a terrain grid from points.csv + grid.json")
    b.add_argument("--points", type=Path, required=True, help="input points CSV")
    b.add_argument("--grid", type=Path, required=True, help="grid definition JSON")
    b.add_argument("--out", type=Path, required=True, help="output directory")
    b.add_argument("--radius", type=float, default=None,
                   help="max neighbourhood radius [m]; default: the grid's "
                   "max_support_radius if present, else 3.0")
    b.add_argument("--min-support", type=int, default=d.min_support,
                   help="min ground candidates within radius")
    b.add_argument("--min-inliers", type=int, default=d.min_inliers,
                   help="min inliers after outlier rejection")
    b.add_argument("--outlier-k", type=float, default=d.outlier_k,
                   help="inlier threshold multiplier on robust sigma")
    b.add_argument("--clip-iters", type=int, default=d.clip_iters,
                   help="max sigma-clipping iterations")
    b.add_argument("--low-z-fraction", type=float, default=d.low_z_fraction,
                   help="lowest fraction of points used to seed the fit")
    b.add_argument("--sigma-floor", type=float, default=d.sigma_floor,
                   help="lower bound for robust sigma [m]")
    b.add_argument("--extrapolation-max", type=float, default=d.extrapolation_max,
                   help="sqrt(n)*leverage threshold for lopsided support")
    b.add_argument("--collinear-ratio", type=float, default=d.collinear_ratio,
                   help="s_min/s_max below which support is near-collinear")
    b.add_argument("--mixed-levels-min-frac", type=float, default=d.mixed_levels_min_frac,
                   help="min fraction of high rejects flagging a two-level cell")
    b.add_argument("--mixed-levels-min-step", type=float, default=d.mixed_levels_min_step,
                   help="min height [m] of high rejects counted as a second level")
    b.set_defaults(func=cmd_build)
    return p


def _check_params(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.command != "build":
        return
    problems = []
    if args.radius is not None and args.radius <= 0:
        problems.append("--radius must be > 0")
    if args.min_support < 3:
        problems.append("--min-support must be >= 3 (a plane needs 3 points)")
    if args.min_inliers < 3:
        problems.append("--min-inliers must be >= 3")
    if args.min_inliers > args.min_support:
        problems.append("--min-inliers must be <= --min-support")
    if args.outlier_k <= 0:
        problems.append("--outlier-k must be > 0")
    if not (0 < args.low_z_fraction <= 1):
        problems.append("--low-z-fraction must be in (0, 1]")
    if args.sigma_floor <= 0:
        problems.append("--sigma-floor must be > 0")
    for msg in problems:
        parser.error(msg)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _check_params(args, parser)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
