"""Loading and validation of the input point cloud.

Input contract (fixtures/points.csv):
    header columns: point_id,x,y,z,classification
    x, y, z        : metres, finite, already in a common local CRS
    classification : integer ASPRS class in [0, 18]; class 2 = ground candidate
    point_id       : non-empty, unique

Rows that violate the contract are rejected individually and *located*
(file line + point_id + reason); they never affect the valid points.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

GROUND_CLASS = 2
MAX_CLASS = 18  # ASPRS standard classes are 0..18

REQUIRED_COLUMNS = ("point_id", "x", "y", "z", "classification")


@dataclass(frozen=True)
class PointIssue:
    """One rejected input row, located for manual follow-up."""

    line: int  # 1-based file line (header is line 1)
    point_id: str
    reason: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"line {self.line} (point_id={self.point_id!r}): {self.reason}"


@dataclass
class PointCloud:
    """Validated points plus the audit trail of rejected rows."""

    point_ids: np.ndarray  # (n,) str, valid points only
    xyz: np.ndarray  # (n, 3) float64
    classification: np.ndarray  # (n,) int64
    issues: list[PointIssue]
    excluded_by_class: dict[int, int]  # valid non-ground points, per class
    total_rows: int  # data rows seen in the file (valid + rejected)

    @property
    def ground_mask(self) -> np.ndarray:
        return self.classification == GROUND_CLASS

    @property
    def n_ground(self) -> int:
        return int(self.ground_mask.sum())


def load_points(path: str | Path) -> PointCloud:
    path = Path(path)
    issues: list[PointIssue] = []
    ids: list[str] = []
    xyz: list[tuple[float, float, float]] = []
    cls: list[int] = []
    seen_ids: set[str] = set()
    excluded: dict[int, int] = {}
    total = 0

    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing required columns: {missing}")
        for row in reader:
            total += 1
            line = reader.line_num  # header consumed -> first data row is line 2
            pid = (row.get("point_id") or "").strip()

            if not pid:
                issues.append(PointIssue(line, pid, "empty point_id"))
                continue
            if pid in seen_ids:
                issues.append(
                    PointIssue(line, pid, "duplicate point_id (first occurrence kept)")
                )
                continue

            try:
                x = float(row["x"])
                y = float(row["y"])
                z = float(row["z"])
            except (TypeError, ValueError):
                issues.append(PointIssue(line, pid, "unparsable coordinate"))
                continue
            if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                issues.append(PointIssue(line, pid, "non-finite coordinate"))
                continue

            raw_cls = (row.get("classification") or "").strip()
            try:
                c = int(raw_cls)
            except ValueError:
                issues.append(
                    PointIssue(line, pid, f"unparsable classification {raw_cls!r}")
                )
                continue
            if not (0 <= c <= MAX_CLASS):
                issues.append(
                    PointIssue(line, pid, f"illegal classification {c} (allowed 0..{MAX_CLASS})")
                )
                continue

            seen_ids.add(pid)
            ids.append(pid)
            xyz.append((x, y, z))
            cls.append(c)
            if c != GROUND_CLASS:
                excluded[c] = excluded.get(c, 0) + 1

    return PointCloud(
        point_ids=np.asarray(ids, dtype=object),
        xyz=np.asarray(xyz, dtype=np.float64).reshape(-1, 3),
        classification=np.asarray(cls, dtype=np.int64),
        issues=issues,
        excluded_by_class=excluded,
        total_rows=total,
    )
