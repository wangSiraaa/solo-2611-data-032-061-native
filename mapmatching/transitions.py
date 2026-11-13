"""候选间有向转移的可行性与代价。

硬可行性（任一不满足则该转移被禁止，欧氏距离再近也不能放行）：

1. 逆向移动：同一条有向边上沿里程倒退，或图中不存在 from->to 的有向路径；
2. 不可达：两候选边之间只能通过共享节点转向，几何交叉不构成路口；
3. 零时间移动：dt <= 0 时只允许路程为 0 的原地停留；
4. 超速：沿有向路径的平均速度超过 松弛系数 * 沿途速度上限。

软代价只对可行转移计算：绕路量 = max(0, 有向路径里程 - 投影点欧氏距离)，
经 transition_beta_m 指数尺度转为无量纲量。这里不把观测不到的中间路段耗时
分摊到任何采样点，速度只按“两端边内位置 + 中间有向路径”的总里程与真实
时间差做平均检查。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .candidates import Candidate
from .config import Config
from .network import RoadNetwork

_EPS_M = 1e-9


@dataclass(frozen=True)
class TransitionEval:
    feasible: bool
    reasons: tuple  # 不可行原因代码，空元组表示可行
    dt_ms: int
    path_edge_ids: tuple = field(default_factory=tuple)
    path_length_m: float = 0.0
    euclidean_m: float = 0.0
    detour_m: float = 0.0
    implied_speed_mps: Optional[float] = None
    speed_limit_mps: Optional[float] = None
    trans_cost: float = 0.0


def _merge_adjacent(edge_ids: list) -> tuple:
    """合并相邻重复边 id，保留有向序列中的真实重复（如回环多次经过）。"""
    merged: list = []
    for edge_id in edge_ids:
        if not merged or merged[-1] != edge_id:
            merged.append(edge_id)
    return tuple(merged)


def evaluate_transition(network: RoadNetwork, prev: Candidate, nxt: Candidate,
                        dt_ms: int, cfg: Config) -> TransitionEval:
    euclidean = math.hypot(
        nxt.projection_x - prev.projection_x,
        nxt.projection_y - prev.projection_y,
    )
    road_prev = network.roads[prev.edge_id]
    road_next = network.roads[nxt.edge_id]
    reasons: list = []
    edge_ids: list = []
    path_length = 0.0

    if prev.edge_id == nxt.edge_id:
        delta = nxt.along_m - prev.along_m
        if delta < -_EPS_M:
            # 有向边不允许倒退；即便欧氏距离很小也是硬禁止。
            return TransitionEval(
                feasible=False,
                reasons=("reverse_direction",),
                dt_ms=dt_ms,
                euclidean_m=round(euclidean, 6),
            )
        edge_ids = [prev.edge_id]
        path_length = max(delta, 0.0)
    else:
        # 先走完当前边剩余部分，经共享节点的有向路径，再进入下一条边。
        route = network.shortest_route(road_prev.to_node, road_next.from_node)
        if route is None:
            return TransitionEval(
                feasible=False,
                reasons=("no_directed_path",),
                dt_ms=dt_ms,
                euclidean_m=round(euclidean, 6),
            )
        middle_edges, middle_length = route
        edge_ids = [prev.edge_id] + middle_edges + [nxt.edge_id]
        path_length = (
            (road_prev.length_m - prev.along_m)
            + middle_length
            + nxt.along_m
        )

    traversed = _merge_adjacent(edge_ids)
    speed_limit = max(network.roads[e].max_speed_mps for e in traversed)
    limit = cfg.speed_slack_factor * speed_limit

    dt_s = dt_ms / 1000.0
    implied_speed: Optional[float]
    if dt_s <= 0:
        if path_length > _EPS_M:
            reasons.append("zero_time_move")
        implied_speed = None
    else:
        implied_speed = path_length / dt_s
        if implied_speed > limit + _EPS_M:
            reasons.append("exceeds_speed_limit")

    detour = max(0.0, path_length - euclidean)
    trans_cost = cfg.transition_weight * (detour / cfg.transition_beta_m)

    return TransitionEval(
        feasible=not reasons,
        reasons=tuple(reasons),
        dt_ms=dt_ms,
        path_edge_ids=traversed,
        path_length_m=round(path_length, 6),
        euclidean_m=round(euclidean, 6),
        detour_m=round(detour, 6),
        implied_speed_mps=None if implied_speed is None else round(implied_speed, 6),
        speed_limit_mps=round(limit, 6),
        trans_cost=0.0 if reasons else round(trans_cost, 6),
    )
