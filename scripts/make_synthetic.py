#!/usr/bin/env python3
"""Generate a deterministic synthetic scene for validation (not the main input).

The main input is the provided fixtures/points.csv + fixtures/grid.json.
This script writes a *separate* synthetic scene (synthetic/) whose terrain
truth is known analytically, so validate.py can measure absolute errors and
exercise malformed-row handling (the provided fixture is clean).

Scene (60 m x 40 m, 1 m grid):
  * base slope z = 10 + 0.06x + 0.03y
  * 2 m step/terrace at x = 20
  * gentle smooth hill near (27, 33)
  * dense sampling for x < 35, sparse for x >= 35
  * rectangular observation hole x in [44, 52], y in [8, 20]
  * vegetation (class 3/4/5) and a flat building roof (class 6)
  * a few isolated high points mislabeled as ground (class 2)
  * 6 malformed rows (duplicate id, non-finite coords, illegal classes)

Outputs: synthetic/points.csv, synthetic/grid.json, synthetic/truth.npy
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parents[1] / "synthetic"

STEP_X = 20.0
STEP_H = 2.0
HOLE = (44.0, 52.0, 8.0, 20.0)  # x0, x1, y0, y1
NOISE = 0.05  # ground measurement sigma [m]

GRID = dict(xmin=0.0, ymin=0.0, xmax=60.0, ymax=40.0, resolution=1.0,
            max_support_radius=3.0, units="m")


def true_terrain(x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    z = 10.0 + 0.06 * x + 0.03 * y
    z = z + STEP_H * (x >= STEP_X)
    z = z + 0.8 * np.exp(-(((x - 27.0) ** 2 + (y - 33.0) ** 2)) / (2 * 7.0**2))
    return z


def in_hole(x, y):
    x0, x1, y0, y1 = HOLE
    return (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)


def main() -> None:
    rng = np.random.default_rng(20261113)
    rows: list[tuple[str, float, float, float, int]] = []
    seq = 0

    def add(cls, x, y, z):
        nonlocal seq
        rows.append((f"g{seq:05d}", float(x), float(y), float(z), cls))
        seq += 1

    # --- ground points: dense west, sparse east ------------------------------
    for n, x0, x1 in ((1750, 0.0, 35.0), (170, 35.0, 60.0)):
        x = rng.uniform(x0, x1, n)
        y = rng.uniform(0.0, 40.0, n)
        keep = ~in_hole(x, y)
        x, y = x[keep], y[keep]
        z = true_terrain(x, y) + rng.normal(0.0, NOISE, x.size)
        for xi, yi, zi in zip(x, y, z):
            add(2, xi, yi, zi)

    # --- vegetation returns (excluded by class) ------------------------------
    n = 130
    x = rng.uniform(0.0, 60.0, n)
    y = rng.uniform(0.0, 40.0, n)
    z = true_terrain(x, y) + rng.uniform(0.4, 4.0, n)
    c = rng.choice([3, 4, 5], n)
    for xi, yi, zi, ci in zip(x, y, z, c):
        add(int(ci), xi, yi, zi)

    # --- flat building roof (excluded by class) -------------------------------
    n = 45
    x = rng.uniform(10.0, 16.0, n)
    y = rng.uniform(8.0, 14.0, n)
    z = np.full(n, true_terrain(13.0, 11.0) + 3.0)
    for xi, yi, zi in zip(x, y, z):
        add(6, xi, yi, zi)

    # --- isolated high points mislabeled as ground ----------------------------
    n = 14
    x = np.concatenate([rng.uniform(0.0, 35.0, 10), rng.uniform(35.0, 60.0, 4)])
    y = rng.uniform(0.0, 40.0, n)
    keep = ~in_hole(x, y)
    x, y = x[keep], y[keep]
    z = true_terrain(x, y) + rng.uniform(1.5, 3.5, x.size)
    for xi, yi, zi in zip(x, y, z):
        add(2, xi, yi, zi)

    # shuffle valid rows so ordering carries no information
    order = rng.permutation(len(rows))
    rows = [rows[i] for i in order]

    # --- malformed rows (must be located and rejected) ------------------------
    dup_id = rows[10][0]
    bad = [
        (dup_id, 1.0, 2.0, 11.0, 2),            # duplicate point_id
        ("bad_nan", float("nan"), 5.0, 11.0, 2),
        ("bad_inf", 5.0, 5.0, float("inf"), 2),
        ("bad_cls99", 5.0, 6.0, 11.0, 99),      # illegal classification
        ("bad_clsneg", 5.0, 7.0, 11.0, -1),     # illegal classification
        ("bad_clstxt", 5.0, 8.0, 11.0, "abc"),  # unparsable classification
    ]

    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "points.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["point_id", "x", "y", "z", "classification"])
        for pid, x, y, z, c in rows:
            w.writerow([pid, f"{x:.4f}", f"{y:.4f}", f"{z:.4f}", c])
        for pid, x, y, z, c in bad:
            w.writerow([pid, x, y, z, c])

    (OUT / "grid.json").write_text(json.dumps(GRID, indent=2))

    n_cols = round((GRID["xmax"] - GRID["xmin"]) / GRID["resolution"])
    n_rows = round((GRID["ymax"] - GRID["ymin"]) / GRID["resolution"])
    xs = GRID["xmin"] + (np.arange(n_cols) + 0.5) * GRID["resolution"]
    ys = GRID["ymin"] + (np.arange(n_rows) + 0.5) * GRID["resolution"]
    XS, YS = np.meshgrid(xs, ys)
    np.save(OUT / "truth.npy", true_terrain(XS, YS))

    n_ground = sum(1 for r in rows if r[4] == 2)
    print(f"wrote {OUT}/points.csv: {len(rows)} valid rows ({n_ground} ground) "
          f"+ {len(bad)} malformed rows")
    print(f"wrote {OUT}/grid.json and {OUT}/truth.npy ({n_rows}x{n_cols})")


if __name__ == "__main__":
    main()
