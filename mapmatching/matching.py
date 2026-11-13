"""联合序列匹配：观测偏离 + 有向路径可行性的 Viterbi 决策。

代价量纲说明（全部为无量纲代价，不是概率，不做归一化或 softmax）：

- 观测代价 c_obs = w_obs * 0.5 * (d / sigma)^2，d 为投影距离，sigma 取点
  精度（由 obs_sigma_m 设下限），w_obs = obs_weight；
- 转移代价 c_trans = w_trans * max(0, 有向路径里程 - 欧氏距离) / beta，
  w_trans = transition_weight，beta = transition_beta_m；
- 总分数 = 路径上各点 c_obs 与各可行转移 c_trans 之和，越小越好。

硬断段规则（不使用直线补路）：
- 某点半径内无候选 -> NO_CANDIDATE，前后切分为不同 segment；
- 某点候选与上一个已匹配点之间所有转移都不可行 -> NO_FEASIBLE_TRANSITION，
  该点另起新链（链首只承担观测代价）；
- 零时间跨边移动、逆向、不可达、超速均为转移级硬禁止。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .candidates import Candidate
from .config import Config
from .io import GpsPoint
from .network import RoadNetwork
from .transitions import TransitionEval, evaluate_transition


@dataclass
class _DPState:
    score: float
    prev_cand_id: Optional[str]
    trans: Optional[TransitionEval]


def _weighted_obs(cand: Candidate, cfg: Config) -> float:
    return cfg.obs_weight * cand.obs_cost


def run_viterbi(points: list, candidates_per_point: list,
                network: RoadNetwork, cfg: Config):
    """返回 (dp_chain, states_by_point)。

    dp_chain 为按时间排列的点级结构，未知点为 None；states_by_point 只给
    有候选的点，内容为 cand_id -> _DPState。
    """
    states_by_point: list = []
    chain: list = []
    prev_live: dict = {}
    prev_candidates: list = []
    prev_point: Optional[GpsPoint] = None

    for point, candidates in zip(points, candidates_per_point):
        if not candidates:
            states_by_point.append({})
            chain.append(None)
            prev_live = {}
            prev_candidates = []
            prev_point = point
            continue

        dt_ms = point.ts_ms - prev_point.ts_ms if prev_point is not None else None
        states: dict = {}
        prev_by_id = {c.cand_id: c for c in prev_candidates}

        for cand in candidates:
            obs = _weighted_obs(cand, cfg)
            if not prev_live:
                # 链首：没有前驱，只承担观测代价。
                states[cand.cand_id] = _DPState(obs, None, None)
                continue

            best: Optional[tuple] = None
            failure_reasons: set = set()
            for prev_id, prev_state in sorted(prev_live.items()):
                trans = evaluate_transition(
                    network, prev_by_id[prev_id], cand, dt_ms, cfg
                )
                if not trans.feasible:
                    failure_reasons.update(trans.reasons)
                    continue
                score = prev_state.score + trans.trans_cost + obs
                # 确定性平局规则：分数相同取前驱 cand_id 字典序最小者。
                key = (round(score, 12), prev_id)
                if best is None or key < best[0]:
                    best = (key, _DPState(score, prev_id, trans))
            if best is not None:
                states[cand.cand_id] = best[1]
            else:
                # 全部前驱转移不可行：该候选在此点无法续链，记录失败原因。
                states[cand.cand_id] = _DPState(
                    float("inf"),
                    None,
                    TransitionEval(
                        feasible=False,
                        reasons=tuple(sorted(failure_reasons)),
                        dt_ms=dt_ms if dt_ms is not None else 0,
                    ),
                )

        live = {cid: st for cid, st in states.items() if st.score != float("inf")}
        states_by_point.append(states)
        if not live:
            chain.append(None)
            prev_live = {}
            prev_candidates = []
        else:
            chain.append({"states": states, "live": live,
                          "candidates": candidates})
            prev_live = live
            prev_candidates = candidates
        prev_point = point

    return chain, states_by_point


def _best_and_gap(live: dict):
    """返回 (best_cand_id, best_score, 与次优状态的分数差)。"""
    ordered = sorted(live.items(), key=lambda kv: (round(kv[1].score, 12), kv[0]))
    best_id, best_state = ordered[0]
    gap = None
    if len(ordered) > 1:
        gap = round(ordered[1][1].score - best_state.score, 9)
    return best_id, best_state.score, gap


def build_records(points: list, candidates_per_point: list, chain: list,
                  states_by_point: list, cfg: Config):
    """回溯 Viterbi 链，生成逐点记录与连续路段（segment）记录。"""
    n = len(points)
    chosen_cand_id: list = [None] * n
    chosen_score: list = [None] * n
    ambiguity_gap: list = [None] * n

    # 链末端 = 有活状态、且其后一个点无活状态（未知点或轨迹结尾）。
    end_indices = [
        i for i in range(n)
        if chain[i] is not None
        and (i == n - 1 or chain[i + 1] is None)
    ]
    chains: list = []
    for end in end_indices:
        idxs: list = []
        i = end
        while i >= 0 and chain[i] is not None:
            live = chain[i]["live"]
            best_id, best_score, gap = _best_and_gap(live)
            chosen_cand_id[i] = best_id
            chosen_score[i] = round(best_score, 9)
            ambiguity_gap[i] = gap
            idxs.append(i)
            state = live[best_id]
            if state.prev_cand_id is None:
                # 链首（轨迹起点，或前一点全部转移不可行后另起的新链）。
                break
            prev_state = chain[i - 1]["live"].get(state.prev_cand_id)
            if prev_state is None:  # 防御性边界：回溯指针必须落在相邻活链上
                break
            i -= 1  # 回溯指针只可能指向相邻前一个点
        chains.append(sorted(idxs))
    chains.sort()

    point_records = []
    cand_lookup = [
        {c.cand_id: c for c in cands} for cands in candidates_per_point
    ]

    for i, point in enumerate(points):
        candidates = candidates_per_point[i]
        states = states_by_point[i]
        record = {
            "point_id": point.point_id,
            "ts_ms": point.ts_ms,
            "x": point.x,
            "y": point.y,
            "accuracy_m": point.accuracy_m,
            "source": {"file": "gps.csv", "row_index": point.source_index},
            "candidates": [_candidate_payload(c, cfg) for c in candidates],
        }

        nearest = candidates[0] if candidates else None
        if not candidates:
            record["status"] = "UNKNOWN"
            record["unknown_reason"] = "NO_CANDIDATE"
            record["selection"] = None
        elif chain[i] is None:
            record["status"] = "UNKNOWN"
            reasons = _collect_failure_reasons(states)
            record["unknown_reason"] = "NO_FEASIBLE_TRANSITION"
            record["transition_failure_reasons"] = reasons
            record["selection"] = None
        else:
            sel_id = chosen_cand_id[i]
            sel = cand_lookup[i][sel_id]
            state = chain[i]["live"][sel_id]
            record["status"] = "MATCHED"
            record["unknown_reason"] = None
            record["selection"] = _selection_payload(sel, state, cfg)
            record["total_score"] = chosen_score[i]
            gap = ambiguity_gap[i]
            record["ambiguous"] = (
                gap is not None and gap <= cfg.ambiguity_cost_gap
            )
            record["ambiguity_score_gap"] = gap
            if nearest is not None and sel.edge_id != nearest.edge_id:
                record["nearest_edge_not_selected"] = {
                    "edge_id": nearest.edge_id,
                    "cand_id": nearest.cand_id,
                    "projection_distance_m": nearest.projection_distance_m,
                    "failure_reasons": _state_failure_reasons(states, nearest.cand_id),
                }
            else:
                record["nearest_edge_not_selected"] = None

        # 每个候选的 DP 续链状态：失败候选附原因，可行候选附续链总分，
        # 用于解释“为什么没有选某条几何上很近的道路”。
        for cand_record in record["candidates"]:
            st = states.get(cand_record["cand_id"])
            cand_record["dp_state"] = _dp_state_payload(st)
        point_records.append(record)

    segment_records = [
        _build_segment(chain_idxs, points, candidates_per_point,
                       chosen_cand_id, states_by_point, cand_lookup)
        for chain_idxs in chains
    ]
    return point_records, segment_records


def _state_failure_reasons(states: dict, cand_id: str):
    state = states.get(cand_id)
    if state is None or state.trans is None or state.trans.feasible:
        return []
    return list(state.trans.reasons)


def _dp_state_payload(state):
    if state is None:
        return None
    if state.score == float("inf"):
        return {
            "reachable": False,
            "reasons": list(state.trans.reasons) if state.trans else [],
        }
    return {
        "reachable": True,
        "chain_score": round(state.score, 9),
        "prev_cand_id": state.prev_cand_id,
    }


def _collect_failure_reasons(states: dict) -> list:
    reasons: set = set()
    for state in states.values():
        if state.trans is not None and not state.trans.feasible:
            reasons.update(state.trans.reasons)
    return sorted(reasons)


def _candidate_payload(cand: Candidate, cfg: Config) -> dict:
    return {
        "cand_id": cand.cand_id,
        "edge_id": cand.edge_id,
        "rank": cand.rank,
        "projection_distance_m": cand.projection_distance_m,
        "along_m": cand.along_m,
        "projection_xy": [cand.projection_x, cand.projection_y],
        "bearing_deg": cand.bearing_deg,
        "accuracy_m": cand.accuracy_m,
        "obs_cost": cand.obs_cost,
        "weighted_obs_cost": round(cfg.obs_weight * cand.obs_cost, 9),
        "source": {"file": "roads.geojson", "feature_index": cand.source_edge_index},
    }


def _selection_payload(cand: Candidate, state: _DPState, cfg: Config) -> dict:
    payload = {
        "cand_id": cand.cand_id,
        "edge_id": cand.edge_id,
        "projection_distance_m": cand.projection_distance_m,
        "along_m": cand.along_m,
        "projection_xy": [cand.projection_x, cand.projection_y],
        "bearing_deg": cand.bearing_deg,
        "obs_cost": cand.obs_cost,
        "weighted_obs_cost": round(cfg.obs_weight * cand.obs_cost, 9),
        "incoming_transition": None,
    }
    if state.trans is not None:
        t = state.trans
        payload["incoming_transition"] = {
            "feasible": t.feasible,
            "dt_ms": t.dt_ms,
            "path_edge_ids": list(t.path_edge_ids),
            "path_length_m": t.path_length_m,
            "euclidean_m": t.euclidean_m,
            "detour_m": t.detour_m,
            "implied_speed_mps": t.implied_speed_mps,
            "speed_limit_mps": t.speed_limit_mps,
            "trans_cost": t.trans_cost,
        }
    return payload


def _build_segment(chain_idxs: list, points: list, candidates_per_point: list,
                   chosen_cand_id: list, states_by_point: list,
                   cand_lookup: list) -> dict:
    ordered = sorted(chain_idxs)
    edge_sequence: list = []
    point_refs = []
    total_transition_cost = 0.0
    total_obs_cost = 0.0

    for pos, i in enumerate(ordered):
        cand = cand_lookup[i][chosen_cand_id[i]]
        total_obs_cost += cand.obs_cost
        point_refs.append({
            "point_id": points[i].point_id,
            "edge_id": cand.edge_id,
            "along_m": cand.along_m,
        })
        state = states_by_point[i][chosen_cand_id[i]]
        if pos == 0:
            edge_sequence.append(cand.edge_id)
        else:
            t = state.trans
            total_transition_cost += t.trans_cost
            for edge_id in t.path_edge_ids:
                if not edge_sequence or edge_sequence[-1] != edge_id:
                    edge_sequence.append(edge_id)

    first_i, last_i = ordered[0], ordered[-1]
    first_cand = cand_lookup[first_i][chosen_cand_id[first_i]]
    last_cand = cand_lookup[last_i][chosen_cand_id[last_i]]
    return {
        "segment_id": f"seg-{len(ordered):02d}-{points[first_i].point_id}-{points[last_i].point_id}",
        "start_point_id": points[first_i].point_id,
        "end_point_id": points[last_i].point_id,
        "start_ts_ms": points[first_i].ts_ms,
        "end_ts_ms": points[last_i].ts_ms,
        "directed_edge_sequence": edge_sequence,
        "points": point_refs,
        "break_before": {
            "point_id": points[first_i].point_id,
            "reason": "SEGMENT_START" if first_i == 0 else "PREVIOUS_POINT_UNKNOWN",
        },
        "break_after": {
            "point_id": points[last_i].point_id,
            "reason": "SEGMENT_END" if last_i == len(points) - 1
                      else "NEXT_POINT_UNKNOWN",
        },
        "boundary_positions": {
            "start": {"edge_id": first_cand.edge_id, "along_m": first_cand.along_m},
            "end": {"edge_id": last_cand.edge_id, "along_m": last_cand.along_m},
        },
        "sum_obs_cost": round(total_obs_cost, 9),
        "sum_transition_cost": round(total_transition_cost, 9),
    }


def match(points: list, candidates_per_point: list, network: RoadNetwork,
          cfg: Config):
    """对外主入口：返回 (point_records, segment_records)。"""
    chain, states_by_point = run_viterbi(
        points, candidates_per_point, network, cfg
    )
    return build_records(
        points, candidates_per_point, chain, states_by_point, cfg
    )
