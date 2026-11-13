"""噪声/观测权重对歧义的影响：展示权重如何改变平行路选择。

平行走廊由上下两条有向路径组成，在共享节点 K 处可以合法换路（只有共享
节点才能转向）：

    S -> K -> T   下路（near，直、短）
    S -> U -> K -> V -> T   上路（far，绕、长）

中间观测点更贴近 far；低观测权重时绕路代价主导、留在 near，高观测权重时
投影偏离主导、在 K 换到 far。近乎等价时保留 ambiguous 依据，且分数不宣称
是真实概率。
"""

from __future__ import annotations

from shapely.geometry import LineString

from mapmatching.candidates import generate_candidates
from mapmatching.config import Config
from mapmatching.io import Road
from mapmatching.matching import match
from mapmatching.network import RoadNetwork

from .conftest import make_gps


def _parallel_network():
    # 两段平行走廊，共享节点只有 S、K、T，换路只能在这三个节点发生：
    # 下路 near_sk/near_kt 沿 y=0；上路 far_sk/far_kt 水平段沿 y=6，
    # 经短竖边在 S、K、T 衔接，每段比下路长 6。
    roads = [
        Road("near_sk", "S", "K", 20.0,
             LineString([(0, 0), (50, 0)]), 50.0, 0),
        Road("near_kt", "K", "T", 20.0,
             LineString([(50, 0), (100, 0)]), 50.0, 1),
        Road("far_sk", "S", "K", 20.0,
             LineString([(0, 0), (0, 6), (50, 6), (50, 0)]), 62.0, 2),
        Road("far_kt", "K", "T", 20.0,
             LineString([(50, 0), (50, 6), (100, 6), (100, 0)]), 62.0, 3),
    ]
    return RoadNetwork.build(roads), roads


def _trajectory(accuracy=4.0):
    # g1 精确在 near_sk 上；g3 精确在 near_kt 上；g2=(75,5.5) 贴 far_kt
    # 水平段（距 far_kt 0.5m、距 near_kt 5.5m）。低权重绕路主导全程 near，
    # 高权重 g2 观测主导、经共享节点 K 翻到 far_kt 再在 T 回到 near_kt。
    return make_gps([
        ("g1", 0, 25.0, 0.0, accuracy),
        ("g2", 5000, 75.0, 5.5, accuracy),
        ("g3", 10000, 95.0, 0.0, accuracy),
    ])


def _cfg(obs_weight):
    return Config.from_dict({
        "candidate_radius_m": 12.0,
        "max_candidates": 4,
        "speed_slack_factor": 2.0,
        "obs_sigma_m": 4.0,
        "obs_weight": obs_weight,
        "transition_beta_m": 100.0,
        "transition_weight": 1.0,
        "ambiguity_cost_gap": 0.5,
    })


def _chosen_edges(obs_weight, accuracy=4.0):
    network, roads = _parallel_network()
    gps = _trajectory(accuracy)
    cfg = _cfg(obs_weight)
    candidates = [generate_candidates(p, roads, cfg) for p in gps]
    points, segments = match(gps, candidates, network, cfg)
    return [r["selection"]["edge_id"] for r in points], points, segments


def test_obs_weight_flips_parallel_choice():
    low_edges, low_points, low_segs = _chosen_edges(obs_weight=0.05)
    high_edges, high_points, high_segs = _chosen_edges(obs_weight=2.0)

    # 低权重：绕路代价主导，全程走下路，不发生平行路跳转。
    assert low_edges == ["near_sk", "near_kt", "near_kt"]
    assert low_segs[0]["directed_edge_sequence"] == ["near_sk", "near_kt"]

    # 高权重：投影距离主导，中间点翻到更近的上路 far_kt，
    # 在共享节点 K 合法换路，序列保留 near_sk -> far_kt。
    assert high_edges[0] == "near_sk"
    assert high_edges[1] == "far_kt"
    assert high_segs[0]["directed_edge_sequence"][:2] == ["near_sk", "far_kt"]

    # 低权重下 far_kt 在中间点仍是合法可达候选，差异来自代价而非可行性。
    mid_low = low_points[1]
    far_cand = next(c for c in mid_low["candidates"]
                    if c["edge_id"] == "far_kt")
    assert far_cand["dp_state"]["reachable"] is True
    # 低权重的最近候选（far_kt）没有被选，输出必须能解释原因。
    assert low_points[1]["nearest_edge_not_selected"]["edge_id"] == "far_kt"


def test_ambiguity_is_retained_not_silently_resolved():
    # w=0.5 时两条路线总分接近，验证 ambiguous 标记与分数差依据被保留。
    _edges, points, _segs = _chosen_edges(obs_weight=0.5)
    mid = points[1]
    assert mid["ambiguity_score_gap"] is not None
    # 无论是否越过阈值，都必须保留两条路线的候选与各自续链分数作为依据。
    assert len(mid["candidates"]) >= 2
    for cand in mid["candidates"]:
        assert cand["dp_state"]["reachable"] is True
    # 分数不是概率：记录里不允许出现概率字段。
    assert "probability" not in mid
    assert "posterior" not in mid


def test_accuracy_loosens_observation_penalty():
    # 同样高权重，但中间点声明很差的精度，观测代价被压制，下路应胜出。
    edges, _points, _segs = _chosen_edges(obs_weight=2.0, accuracy=30.0)
    assert edges[1] == "near_kt"
