"""pytest 共享辅助：极小合成网络与匹配流水线封装。"""

from __future__ import annotations

from shapely.geometry import LineString

from mapmatching.candidates import generate_candidates
from mapmatching.config import Config
from mapmatching.io import GpsPoint, Road
from mapmatching.matching import match
from mapmatching.network import RoadNetwork


def make_loop_network():
    """单向回环 S->A->B->S + 平行孤立边 X->Y。

    bs 走外侧折线，只在 B、S 两个共享节点与环相接；xy 的节点 X/Y 与回环
    完全不共享，用于验证“几何接近但无共享节点不能转向”。
    """
    roads = [
        Road("sa", "S", "A", 10.0, LineString([(0, 0), (0, 10)]), 10.0, 0),
        Road("ab", "A", "B", 10.0, LineString([(0, 10), (10, 10)]), 10.0, 1),
        Road("bs", "B", "S", 10.0,
             LineString([(10, 10), (14, 10), (14, -4), (0, -4), (0, 0)]),
             42.0, 2),
        Road("xy", "X", "Y", 10.0, LineString([(0, 16), (10, 16)]), 10.0, 3),
    ]
    return RoadNetwork.build(roads), roads


def default_cfg(**overrides):
    base = {
        "candidate_radius_m": 4.0,
        "max_candidates": 4,
        "speed_slack_factor": 1.3,
        "obs_sigma_m": 5.0,
        "obs_weight": 1.0,
        "transition_beta_m": 10.0,
        "transition_weight": 1.0,
        "ambiguity_cost_gap": 0.5,
    }
    base.update(overrides)
    return Config.from_dict(base)


def run(network, roads, gps, cfg):
    candidates = [generate_candidates(p, roads, cfg) for p in gps]
    return match(gps, candidates, network, cfg)


def make_gps(rows):
    return [
        GpsPoint(pid, ts, x, y, acc, idx)
        for idx, (pid, ts, x, y, acc) in enumerate(rows)
    ]
