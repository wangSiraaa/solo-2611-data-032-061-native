"""断段与重新起链：UNKNOWN 之后能另起新链，且不做直线补路。"""

from __future__ import annotations

from shapely.geometry import LineString

from mapmatching.candidates import generate_candidates
from mapmatching.io import Road
from mapmatching.matching import match

from .conftest import default_cfg, make_gps


def _two_component_network():
    # 两个互不共享节点的有向组件，中间任何转移都不可达。
    roads = [
        Road("e1", "A", "B", 10.0, LineString([(0, 0), (10, 0)]), 10.0, 0),
        Road("e2", "B", "C", 10.0, LineString([(10, 0), (20, 0)]), 10.0, 1),
        Road("q1", "X", "Y", 10.0, LineString([(0, 50), (10, 50)]), 10.0, 2),
    ]
    from mapmatching.network import RoadNetwork
    return RoadNetwork.build(roads), roads


def test_unknown_then_new_chain_produces_two_segments():
    network, roads = _two_component_network()
    gps = make_gps([
        ("a1", 0, 2.0, 0.0, None),
        ("a2", 1000, 12.0, 0.0, None),
        ("gap", 2000, 100.0, 100.0, None),   # 无候选 -> NO_CANDIDATE
        ("b1", 3000, 5.0, 50.0, None),       # 另一组件，重新起链
    ])
    cfg = default_cfg(candidate_radius_m=5.0)
    candidates = [generate_candidates(p, roads, cfg) for p in gps]
    points, segments = match(gps, candidates, network, cfg)

    statuses = [r["status"] for r in points]
    assert statuses == ["MATCHED", "MATCHED", "UNKNOWN", "MATCHED"]
    assert points[2]["unknown_reason"] == "NO_CANDIDATE"

    assert len(segments) == 2
    assert segments[0]["directed_edge_sequence"] == ["e1", "e2"]
    assert segments[1]["directed_edge_sequence"] == ["q1"]
    # 两个 segment 之间没有任何直线/虚拟边。
    assert segments[1]["break_before"] == {
        "point_id": "b1", "reason": "PREVIOUS_POINT_UNKNOWN"}
    assert segments[0]["break_after"] == {
        "point_id": "a2", "reason": "NEXT_POINT_UNKNOWN"}


def test_all_infeasible_point_restarts_chain_without_straight_line():
    network, roads = _two_component_network()
    # 三个点都有候选，但中间点跳到另一组件 -> 转移全部不可行，
    # 它被标记 UNKNOWN，第三个点在该组件上重新起链。
    gps = make_gps([
        ("a1", 0, 2.0, 0.0, None),
        ("jump", 1000, 5.0, 50.0, None),
        ("b1", 2000, 8.0, 50.0, None),
    ])
    cfg = default_cfg(candidate_radius_m=5.0)
    candidates = [generate_candidates(p, roads, cfg) for p in gps]
    points, segments = match(gps, candidates, network, cfg)

    assert points[0]["status"] == "MATCHED"
    assert points[1]["status"] == "UNKNOWN"
    assert points[1]["unknown_reason"] == "NO_FEASIBLE_TRANSITION"
    assert "no_directed_path" in points[1]["transition_failure_reasons"]
    assert points[2]["status"] == "MATCHED"
    assert points[2]["selection"]["edge_id"] == "q1"
    assert points[2]["selection"]["incoming_transition"] is None  # 新链首

    assert len(segments) == 2
    assert segments[0]["directed_edge_sequence"] == ["e1"]
    assert segments[1]["directed_edge_sequence"] == ["q1"]
