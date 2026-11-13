"""候选生成：把每个采样点投影到半径内的有向边。

这里只做独立的几何候选，绝不逐点挑选最近边——序列选择交给 matching 模块的
联合决策。每个候选保留：

- projection_distance_m : 点到边几何的垂直/端点投影距离（米）；
- along_m               : 投影点沿有向边的里程，[0, length]（米）；
- projection_xy         : 投影点坐标；
- bearing_deg           : 投影所在线段的有向方位角 [0, 360)；
- obs_cost              : 观测代价（无量纲，见 cost.py）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from shapely.geometry import Point

from .config import Config
from .io import GpsPoint, Road


@dataclass(frozen=True)
class Candidate:
    cand_id: str  # f"{point_id}:{rank}"，rank 从 0 开始
    edge_id: str
    rank: int
    projection_distance_m: float
    along_m: float
    projection_x: float
    projection_y: float
    bearing_deg: float
    accuracy_m: float
    obs_cost: float
    source_edge_index: int


def _segment_bearing(road: Road, along_m: float) -> float:
    """投影里程所在的折线线段的有向方位角（度）。"""
    coords = list(road.geometry.coords)
    walked = 0.0
    target = min(max(along_m, 0.0), road.length_m)
    for (x1, y1), (x2, y2) in zip(coords[:-1], coords[1:]):
        seg_len = math.hypot(x2 - x1, y2 - y1)
        if walked + seg_len >= target - 1e-12 or seg_len == road.length_m:
            return math.degrees(math.atan2(y2 - y1, x2 - x1)) % 360.0
        walked += seg_len
    (x1, y1), (x2, y2) = coords[-2], coords[-1]
    return math.degrees(math.atan2(y2 - y1, x2 - x1)) % 360.0


def observation_cost(distance_m: float, accuracy_m: float, cfg: Config) -> float:
    """高斯观测代价：0.5 * (d / max(accuracy, sigma_floor))^2，无量纲。

    以点自带精度为主尺度；缺失或过小的精度由 obs_sigma_m 兜底，避免低精度
    声明把代价压到接近零。该量不是概率，不做归一化。
    """
    sigma = max(accuracy_m, cfg.obs_sigma_m * 0.25)
    return 0.5 * (distance_m / sigma) ** 2


def generate_candidates(point: GpsPoint, roads: list, cfg: Config) -> list:
    """为单个采样点生成排序后的候选列表（可能为空）。"""
    pt = Point(point.x, point.y)
    accuracy = point.accuracy_m if point.accuracy_m is not None else cfg.obs_sigma_m
    rows = []
    for road in roads:
        distance = road.geometry.distance(pt)
        if distance > cfg.candidate_radius_m:
            continue
        # project 给出沿有向边的归一化里程，天然带方向语义。
        fraction = road.geometry.project(pt, normalized=True)
        along = float(np.clip(fraction * road.length_m, 0.0, road.length_m))
        projected = road.geometry.interpolate(fraction, normalized=True)
        bearing = _segment_bearing(road, along)
        rows.append(
            {
                "edge_id": road.edge_id,
                "distance": float(distance),
                "along": along,
                "px": projected.x,
                "py": projected.y,
                "bearing": bearing,
                "obs_cost": observation_cost(float(distance), accuracy, cfg),
                "source_index": road.source_index,
            }
        )
    # 确定性排序：距离优先，其次边 id；输入顺序不影响候选集合与名次。
    rows.sort(key=lambda r: (round(r["distance"], 9), r["edge_id"]))
    rows = rows[: cfg.max_candidates]
    return [
        Candidate(
            cand_id=f"{point.point_id}:{rank}",
            edge_id=row["edge_id"],
            rank=rank,
            projection_distance_m=round(row["distance"], 6),
            along_m=round(row["along"], 6),
            projection_x=round(row["px"], 6),
            projection_y=round(row["py"], 6),
            bearing_deg=round(row["bearing"], 6),
            accuracy_m=round(accuracy, 6),
            obs_cost=round(row["obs_cost"], 6),
            source_edge_index=row["source_index"],
        )
        for rank, row in enumerate(rows)
    ]
