"""Output grid definition and array orientation conventions.

grid.json schema (as provided by the survey):

    {"xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10,
     "resolution": 0.5, "max_support_radius": 1.5, "units": "m"}

* ``resolution`` is the cell size [m]; the grid spans
  [xmin, xmax] x [ymin, ymax], so n_cols = (xmax-xmin)/resolution and
  n_rows = (ymax-ymin)/resolution (must be whole numbers).
* ``max_support_radius`` (optional) is the largest neighbourhood radius the
  grid is meant to be used with; the CLI adopts it when --radius is not given.
* ``units`` must be "m" (inputs are already metric; nothing is reprojected).

Array conventions (also written into run_config.json by the CLI):

* terrain[row, col] / mask[row, col] with row = 0 at the *southern* edge
  (smallest y) and col = 0 at the *western* edge (smallest x).
* Cell (row, col) is estimated at its centre
      x = xmin + (col + 0.5) * resolution
      y = ymin + (row + 0.5) * resolution
* terrain.npy holds NaN exactly where mask.npy is False, so a genuine
  0.0 m elevation is never confused with "no estimate".
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class GridSpec:
    x_min: float
    y_min: float
    resolution: float
    n_rows: int
    n_cols: int
    max_support_radius: float | None = None

    @property
    def shape(self) -> tuple[int, int]:
        return (self.n_rows, self.n_cols)

    @property
    def extent(self) -> tuple[float, float, float, float]:
        """(x_min, x_max, y_min, y_max) of the outer cell edges."""
        return (
            self.x_min,
            self.x_min + self.n_cols * self.resolution,
            self.y_min,
            self.y_min + self.n_rows * self.resolution,
        )

    def axis_centers(self) -> tuple[np.ndarray, np.ndarray]:
        """1-D arrays of cell-centre coordinates: xs (n_cols,), ys (n_rows,)."""
        xs = self.x_min + (np.arange(self.n_cols) + 0.5) * self.resolution
        ys = self.y_min + (np.arange(self.n_rows) + 0.5) * self.resolution
        return xs, ys

    def center_grid(self) -> tuple[np.ndarray, np.ndarray]:
        """(XS, YS), each shaped (n_rows, n_cols), of cell centres."""
        xs, ys = self.axis_centers()
        return np.meshgrid(xs, ys)


def load_grid(path: str | Path) -> GridSpec:
    path = Path(path)
    spec = json.loads(path.read_text())
    required = ("xmin", "ymin", "xmax", "ymax", "resolution")
    missing = [k for k in required if k not in spec]
    if missing:
        raise ValueError(f"{path}: missing keys: {missing}")

    units = spec.get("units", "m")
    if units != "m":
        raise ValueError(f"{path}: units must be 'm' (inputs are metric), got {units!r}")

    x_min = float(spec["xmin"])
    y_min = float(spec["ymin"])
    x_max = float(spec["xmax"])
    y_max = float(spec["ymax"])
    res = float(spec["resolution"])
    for v in (x_min, y_min, x_max, y_max, res):
        if not math.isfinite(v):
            raise ValueError(f"{path}: grid bounds and resolution must be finite")
    if res <= 0:
        raise ValueError(f"{path}: resolution must be > 0, got {res}")
    if x_max <= x_min or y_max <= y_min:
        raise ValueError(f"{path}: xmax/xmax must exceed xmin/ymin")

    n_cols_f = (x_max - x_min) / res
    n_rows_f = (y_max - y_min) / res
    n_cols = int(round(n_cols_f))
    n_rows = int(round(n_rows_f))
    if abs(n_cols_f - n_cols) > 1e-9 or abs(n_rows_f - n_rows) > 1e-9:
        raise ValueError(
            f"{path}: extent/resolution is not a whole number of cells "
            f"({n_cols_f} x {n_rows_f})"
        )

    msr = spec.get("max_support_radius")
    if msr is not None:
        msr = float(msr)
        if not (math.isfinite(msr) and msr > 0):
            raise ValueError(f"{path}: max_support_radius must be positive")

    return GridSpec(
        x_min=x_min,
        y_min=y_min,
        resolution=res,
        n_rows=n_rows,
        n_cols=n_cols,
        max_support_radius=msr,
    )
