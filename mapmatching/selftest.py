"""内置冒烟自检：不依赖 pytest，覆盖断段、零时间、几何立交、孤立点。

更完整的独立枚举验证见 tests/ 目录（pytest）。
"""

from __future__ import annotations

import os

from shapely.geometry import LineString

from .candidates import generate_candidates
from .config import Config
from .io import GpsPoint, Road
from .matching import match
from .network import RoadNetwork


def _fixture_pipeline(fixtures_dir: str):
    from .pipeline import run_pipeline

    return run_pipeline(
        os.path.join(fixtures_dir, "roads.geojson"),
        os.path.join(fixtures_dir, "nodes.csv"),
        os.path.join(fixtures_dir, "gps.csv"),
        os.path.join(fixtures_dir, "config.json"),
    )


def _check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"selftest 失败: {name} {detail}")
    print(f"  PASS {name}")


def run_selftest(fixtures_dir: str) -> int:
    print("[1] 合成 fixture 端到端检查")
    points, segments, manifest = _fixture_pipeline(fixtures_dir)
    by_id = {r["point_id"]: r for r in points}

    _check("p1-p6、p8 匹配，p7 孤立点未知",
           [by_id[f"p{i}"]["status"] for i in range(1, 9)] ==
           ["MATCHED"] * 6 + ["UNKNOWN", "MATCHED"])
    _check("p7 无候选",
           by_id["p7"]["unknown_reason"] == "NO_CANDIDATE"
           and by_id["p7"]["candidates"] == [])
    _check("断段数量为 2（p7 切开）", len(segments) == 2)
    _check("第一段覆盖 p1-p6",
           segments[0]["start_point_id"] == "p1"
           and segments[0]["end_point_id"] == "p6")
    _check("第二段仅 p8",
           segments[1]["start_point_id"] == "p8"
           and segments[1]["end_point_id"] == "p8")

    # 几何立交：gh 与主干道在 (100,0)/(100,12) 几何交叉但不共享节点，
    # 联合决策必须拒绝投影到 gh 的候选，即使它比某些可行候选更近。
    p3 = by_id["p3"]
    gh_cand = next(c for c in p3["candidates"] if c["edge_id"] == "gh")
    _check("p3 的 gh 立交候选被保留用于解释", gh_cand is not None)
    _check("gh 候选的转移失败原因包含无有向路径",
           gh_cand["dp_state"]["reachable"] is False
           and "no_directed_path" in gh_cand["dp_state"]["reasons"])
    _check("p3 未选几何交叉的 gh，选择有向可达的 bc",
           p3["selection"]["edge_id"] == "bc")

    # 零时间重复点 p5->p6：允许原地停留，禁止用欧氏距离放行跨边移动。
    p6_trans = by_id["p6"]["selection"]["incoming_transition"]
    _check("p5->p6 零时间且零路程停留可行",
           p6_trans["dt_ms"] == 0 and p6_trans["path_length_m"] == 0.0
           and p6_trans["feasible"] is True)

    # 平行辅路：主链应稳定在 ab/bc 上而不是反复跳转到 de/ef。
    chain_edges = segments[0]["directed_edge_sequence"]
    _check("主链沿下方有向路 ab->bc 且无平行路跳转",
           chain_edges == ["ab", "bc"])

    print("[2] 极小合成网络：方向与连接的独立检查")
    _synthetic_checks()
    print(f"selftest 全部通过（manifest counts={manifest['counts']}）")
    return 0


def _synthetic_checks() -> None:
    # 单向回环 S->A->B->S：sa 与 ab 正交，回环边 bs 走外侧折线，仅在 B、S
    # 两个共享节点与环相接；另加一条与 ab 几何平行但节点孤立的边 X->Y。
    roads = [
        Road("sa", "S", "A", 10.0, LineString([(0, 0), (0, 10)]), 10.0, 0),
        Road("ab", "A", "B", 10.0, LineString([(0, 10), (10, 10)]), 10.0, 1),
        Road("bs", "B", "S", 10.0,
             LineString([(10, 10), (14, 10), (14, -4), (0, -4), (0, 0)]),
             42.0, 2),
        Road("xy", "X", "Y", 10.0, LineString([(0, 16), (10, 16)]), 10.0, 3),
    ]
    network = RoadNetwork.build(roads)
    cfg = Config.from_dict({"candidate_radius_m": 4.0, "max_candidates": 4})

    # 轨迹沿 S->A->B->S 回环一圈：sa 中段、ab 中段、bs 外段、回到 sa 下段。
    # 时间间隔保证沿有向路径的平均速度不超过 松弛系数 * 速度上限。
    gps = [
        GpsPoint("t1", 0, 0.5, 5.0, None, 0),
        GpsPoint("t2", 2000, 5.0, 10.5, None, 1),
        GpsPoint("t3", 5000, 14.0, 3.0, None, 2),
        GpsPoint("t4", 8000, 0.0, 5.0, None, 3),
    ]
    candidates = [generate_candidates(p, roads, cfg) for p in gps]
    point_records, seg_records = match(gps, candidates, network, cfg)
    selected = [r["selection"]["edge_id"] for r in point_records]
    _check("单向回环选边 sa->ab->bs->sa", selected == ["sa", "ab", "bs", "sa"])
    _check("回环有向边序列保留完整重复 sa",
           seg_records[0]["directed_edge_sequence"] == ["sa", "ab", "bs", "sa"])

    # 逆向尝试：在同一条有向边上沿里程倒退的两个点，转移必须被禁止。
    rev = [
        GpsPoint("r1", 0, 2.0, 10.0, None, 0),
        GpsPoint("r2", 1000, 8.0, 10.0, None, 1),
        GpsPoint("r3", 2000, 3.0, 10.0, None, 2),
    ]
    rev_cands = [generate_candidates(p, roads, cfg) for p in rev]
    rev_records, _ = match(rev, rev_cands, network, cfg)
    _check("同边逆向移动被断段（r3 另起链）",
           rev_records[2]["status"] == "UNKNOWN"
           and "reverse_direction"
           in rev_records[2]["transition_failure_reasons"])

    # 平行孤立边 xy 上的点与回环之间不存在共享节点路径。
    jump = [
        GpsPoint("j1", 0, 5.0, 10.0, None, 0),   # 精确在 ab 上
        GpsPoint("j2", 1000, 5.0, 16.0, None, 1),  # 精确在孤立边 xy 上
    ]
    jump_cands = [generate_candidates(p, roads, cfg) for p in jump]
    jump_records, jump_segs = match(jump, jump_cands, network, cfg)
    _check("无共享节点的平行边转移被禁止",
           jump_records[1]["status"] == "UNKNOWN"
           and "no_directed_path"
           in jump_records[1]["transition_failure_reasons"]
           and len(jump_segs) == 1
           and jump_segs[0]["end_point_id"] == "j1")
