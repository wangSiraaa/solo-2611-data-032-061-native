"""目标网格计算:由计划给定的 CRS、分辨率、范围与锚点推出实际输出边界。

锚点网格线 x = ax + k*rx、y = ay + k*ry;实际输出范围为能覆盖计划范围的
最小整像元包围盒(向外取整),因此实际边界可与计划范围不同。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from affine import Affine

from .errors import PlanError

_EPS = 1e-9  # 抑制浮点噪声,避免恰在网格线上的范围被多切出一像元


@dataclass
class TargetGrid:
    crs: str
    transform: Affine
    width: int
    height: int
    resolution: tuple[float, float]

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        x0 = self.transform.c
        y1 = self.transform.f
        rx, ry = self.resolution
        return (x0, y1 - self.height * ry, x0 + self.width * rx, y1)

    def center_xy(self, row: int, col: int) -> tuple[float, float]:
        """输出像元 (row, col) 中心的目标 CRS 坐标。"""
        x, y = self.transform * (col + 0.5, row + 0.5)
        return x, y


def compute_grid(plan) -> TargetGrid:
    rx, ry = plan.resolution
    ax, ay = plan.grid_anchor
    xmin, ymin, xmax, ymax = plan.bounds
    c0 = math.floor((xmin - ax) / rx + _EPS)
    c1 = math.ceil((xmax - ax) / rx - _EPS)
    r0 = math.floor((ymin - ay) / ry + _EPS)
    r1 = math.ceil((ymax - ay) / ry - _EPS)
    if c1 <= c0 or r1 <= r0:
        raise PlanError("目标范围在网格锚点下为空,请检查 bounds/resolution/grid_anchor")
    transform = Affine(rx, 0.0, ax + c0 * rx, 0.0, -ry, ay + r1 * ry)
    return TargetGrid(plan.target_crs, transform, c1 - c0, r1 - r0, (rx, ry))


def window_iter(grid: TargetGrid, window_shape: tuple[int, int]):
    """按 (高, 宽) 像元的输出窗口迭代,边缘窗口收缩,不重叠不漏。"""
    wh, ww = int(window_shape[0]), int(window_shape[1])
    if wh < 1 or ww < 1:
        raise PlanError(f"window_shape 必须为正整数: {window_shape}")
    for r0 in range(0, grid.height, wh):
        for c0 in range(0, grid.width, ww):
            yield r0, c0, min(wh, grid.height - r0), min(ww, grid.width - c0)
