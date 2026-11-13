"""独立枚举验证：用全枚举候选路径对照 Viterbi 的可行性与最优选择。

不调用匹配模块内部的 DP，而是独立枚举每个采样点候选边的笛卡尔积，逐条
候选序列用 transitions.evaluate_transition 判定硬可行性、累计与主程序
完全相同的代价，取枚举最优序列与匹配结果逐项比对。
"""

from __future__ import annotations

import itertools

from mapmatching.candidates import generate_candidates
from mapmatching.matching import match
from mapmatching.transitions import evaluate_transition

from .conftest import default_cfg, make_gps, make_loop_network


def _enumerate_best(network, roads, gps, cfg):
    """返回 (最优边序列, 最优分数, 可行序列集合的边序列列表)。"""
    candidates = [generate_candidates(p, roads, cfg) for p in gps]
    feasible_paths: list = []
    best = None  # (score, edge_tuple)

    for combo in itertools.product(*candidates):
        score = cfg.obs_weight * combo[0].obs_cost
        ok = True
        edges = [combo[0].edge_id]
        for prev, nxt, point_prev, point_next in zip(
            combo[:-1], combo[1:], gps[:-1], gps[1:]
        ):
            dt = point_next.ts_ms - point_prev.ts_ms
            trans = evaluate_transition(network, prev, nxt, dt, cfg)
            if not trans.feasible:
                ok = False
                break
            score += trans.trans_cost + cfg.obs_weight * nxt.obs_cost
            for edge_id in trans.path_edge_ids:
                if edges[-1] != edge_id:
                    edges.append(edge_id)
        if not ok:
            continue
        path = tuple(c.edge_id for c in combo)
        feasible_paths.append(path)
        key = (round(score, 12), path)
        if best is None or key < best[0]:
            best = (key, path, tuple(edges))
    return best, feasible_paths, candidates


def test_viterbi_matches_independent_enumeration():
    network, roads = make_loop_network()
    # 点略微偏离边线，使每个点在候选半径内产生多条候选边，枚举才有区分度。
    # t4 取 sa 高处，远离回环边 bs 的外侧折线，保证回环一圈后明确回到 sa。
    gps = make_gps([
        ("t1", 0, 0.8, 5.0, None),
        ("t2", 2000, 5.0, 10.8, None),
        ("t3", 5000, 13.4, 3.0, None),
        ("t4", 8000, 0.8, 8.0, None),
    ])
    cfg = default_cfg(candidate_radius_m=6.0)
    (best_key, best_path, best_edges), feasible, candidates = _enumerate_best(
        network, roads, gps, cfg
    )

    assert best_path == ("sa", "ab", "bs", "sa")
    assert best_edges == ("sa", "ab", "bs", "sa")
    # 多个点产生多候选，枚举空间大于单点序列。
    assert max(len(c) for c in candidates) >= 3
    assert len(feasible) >= 1

    point_records, seg_records = match(gps, candidates, network, cfg)
    chosen = tuple(r["selection"]["edge_id"] for r in point_records)
    assert chosen == best_path
    assert seg_records[0]["directed_edge_sequence"] == list(best_edges)
    # 主程序总分与枚举总分一致。
    assert point_records[-1]["total_score"] == round(best_key[0], 9)


def test_enumeration_marks_all_infeasible_combos_unknown():
    network, roads = make_loop_network()
    # 第二个点在孤立边 xy 上：任何包含它的序列在 t1->t2 都不可行。
    gps = make_gps([
        ("j1", 0, 5.0, 10.0, None),
        ("j2", 1000, 5.0, 16.0, None),
    ])
    cfg = default_cfg()
    best, feasible, _candidates = _enumerate_best(network, roads, gps, cfg)
    assert best is None and feasible == []

    candidates = [generate_candidates(p, roads, cfg) for p in gps]
    point_records, seg_records = match(gps, candidates, network, cfg)
    assert point_records[0]["status"] == "MATCHED"
    assert point_records[1]["status"] == "UNKNOWN"
    assert point_records[1]["unknown_reason"] == "NO_FEASIBLE_TRANSITION"
    assert "no_directed_path" in point_records[1]["transition_failure_reasons"]
    # 不允许用直线补路：只有一个 segment，且不包含 xy。
    assert len(seg_records) == 1
    assert "xy" not in seg_records[0]["directed_edge_sequence"]


def test_reverse_direction_is_hard_blocked_in_enumeration():
    network, roads = make_loop_network()
    gps = make_gps([
        ("r1", 0, 2.0, 10.0, None),
        ("r2", 1000, 8.0, 10.0, None),
        ("r3", 2000, 3.0, 10.0, None),  # 沿 ab 倒退
    ])
    cfg = default_cfg()
    best, feasible, _candidates = _enumerate_best(network, roads, gps, cfg)
    # 任何含 r3 的序列都因 reverse_direction 失败 -> 无全程可行序列。
    assert feasible == []

    candidates = [generate_candidates(p, roads, cfg) for p in gps]
    point_records, _ = match(gps, candidates, network, cfg)
    assert point_records[2]["status"] == "UNKNOWN"
    assert "reverse_direction" in point_records[2]["transition_failure_reasons"]


def test_zero_time_cross_edge_cannot_be_allowed_by_euclidean():
    network, roads = make_loop_network()
    # 同一时刻两个点分别落在不同有向边上：欧氏距离很小但零时间跨边必须禁止。
    gps = make_gps([
        ("z1", 1000, 5.0, 10.0, None),   # ab 上
        ("z2", 1000, 0.0, 5.0, None),    # sa 上，dt=0
    ])
    cfg = default_cfg()
    best, feasible, _ = _enumerate_best(network, roads, gps, cfg)
    assert feasible == []

    candidates = [generate_candidates(p, roads, cfg) for p in gps]
    point_records, _ = match(gps, candidates, network, cfg)
    assert point_records[1]["status"] == "UNKNOWN"
    assert "zero_time_move" in point_records[1]["transition_failure_reasons"]

    # 同边同位的零时间重复采样必须允许（原地停留）。
    stay = make_gps([
        ("s1", 1000, 5.0, 10.0, None),
        ("s2", 1000, 5.0, 10.0, None),
    ])
    stay_cands = [generate_candidates(p, roads, cfg) for p in stay]
    stay_records, stay_segs = match(stay, stay_cands, network, cfg)
    assert all(r["status"] == "MATCHED" for r in stay_records)
    trans = stay_records[1]["selection"]["incoming_transition"]
    assert trans["dt_ms"] == 0 and trans["path_length_m"] == 0.0
    assert stay_segs[0]["directed_edge_sequence"] == ["ab"]


def test_speed_limit_violation_blocks_transition():
    network, roads = make_loop_network()
    # 正常轨迹但把时间差压到不可能完成有向路程。
    gps = make_gps([
        ("f1", 0, 0.5, 5.0, None),
        ("f2", 10, 5.0, 10.5, None),   # 10ms 内走完 sa 尾段+ab 段
    ])
    cfg = default_cfg()
    best, feasible, _ = _enumerate_best(network, roads, gps, cfg)
    assert feasible == []

    candidates = [generate_candidates(p, roads, cfg) for p in gps]
    point_records, _ = match(gps, candidates, network, cfg)
    assert point_records[1]["status"] == "UNKNOWN"
    assert "exceeds_speed_limit" in point_records[1]["transition_failure_reasons"]
