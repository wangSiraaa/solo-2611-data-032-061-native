"""RPC 客户端/服务端配对与重试身份识别。

关键守恒规则：客户端 span 与服务端 span 是**同一次 RPC 的两面**，配对成功后
覆盖账本只通过客户端区间（被 await 解释时则由 await 解释）计入一次，绝不当两份耗时相加。
资源账本中它们分属不同服务，各自记录各自的墙钟占用（那本来就发生在两台/两处执行体上）。

匹配依据按强度排序：
  1. 双方显式携带相同 rpc_id（合成器可写）—— certain
  2. 显式 link（rel=rpc/follows_from 指向对方）—— certain
  3. 服务端 parent_id == 客户端 span_id（上下文沿调用传播）—— certain
  4. 时间窗包含 + 服务/操作启发式 —— probable（证据弱，标注不确定性）
无法配对的客户端给出 D4（缺失服务端，未知传输区间），不臆造服务端。
"""
from __future__ import annotations

import json
import sqlite3

from .model import CLIENT, SERVER
from .validate import TraceGraph

RPC_LINK_RELS = {"rpc", "rpc_call", "follows_from"}


def match_rpcs(g: TraceGraph) -> list[dict]:
    """配对 RPC 双方。一个 server 可被多个 client 显式 link（共享下游结果），
    因此 link 证据不受 used_servers 独占限制；仅时间启发式要求 server 未被占用。"""
    clients = [s for s in g.spans.values() if s.span_type == CLIENT]
    servers = [s for s in g.spans.values() if s.span_type == SERVER]
    heuristic_used: set[str] = set()
    pairs: list[dict] = []

    # 1) 反向索引：server 通过 link 指向 client（合成器常用方向）；允许一对多
    server_link_to_client: dict[str, list[str]] = {}
    for srv in servers:
        for target, rel in srv.links:
            if rel in RPC_LINK_RELS or rel == "link":
                server_link_to_client.setdefault(target, []).append(srv.span_id)

    for c in clients:
        matched = None
        basis = ""
        # 2a) 服务端反向 link 到本 client
        if c.span_id in server_link_to_client:
            matched, basis = g.spans[server_link_to_client[c.span_id][0]], "link:reverse"
        # 2b) client 正向 link 到 server
        if matched is None:
            for target, rel in c.links:
                if target in g.spans and g.spans[target].span_type == SERVER:
                    matched, basis = g.spans[target], f"link:{rel}"
                    break
        # 3) 服务端 parent 指向客户端
        if matched is None:
            for srv in servers:
                if srv.span_id in heuristic_used:
                    continue
                if srv.parent_id == c.span_id:
                    matched, basis = srv, "parent_context"
                    break
        # 4) 时间包含启发式（弱证据，要求 server 未被启发式占用）
        if matched is None and c.start_ns is not None and c.end_ns is not None:
            candidates = [srv for srv in servers
                          if srv.span_id not in heuristic_used
                          and srv.start_ns is not None and srv.end_ns is not None
                          and c.start_ns <= srv.start_ns and srv.end_ns <= c.end_ns]
            if len(candidates) == 1:
                matched, basis = candidates[0], "temporal_heuristic"
        if matched is not None:
            if basis == "temporal_heuristic" or basis == "parent_context":
                heuristic_used.add(matched.span_id)
            pairs.append({
                "trace_id": g.trace_id, "client_span_id": c.span_id,
                "server_span_id": matched.span_id, "match_basis": basis,
                "confidence": "certain" if basis != "temporal_heuristic" else "probable",
                "detail": {"client_op": c.operation, "server_op": matched.operation,
                           "client_service": c.service, "server_service": matched.service}})
        else:
            pairs.append({"trace_id": g.trace_id, "client_span_id": c.span_id,
                          "server_span_id": None, "match_basis": "unmatched",
                          "confidence": "unknown",
                          "detail": {"reason": "no_server_evidence", "client_op": c.operation,
                                     "client_service": c.service}})
    linked_servers = {p["server_span_id"] for p in pairs if p["server_span_id"]}
    for srv in servers:
        if srv.span_id not in linked_servers and srv.span_id not in heuristic_used:
            pairs.append({"trace_id": g.trace_id, "client_span_id": None,
                          "server_span_id": srv.span_id, "match_basis": "unmatched",
                          "confidence": "unknown",
                          "detail": {"reason": "no_client_evidence", "server_op": srv.operation}})
    return pairs


def retry_groups(g: TraceGraph) -> dict[str, list[str]]:
    """把重试尝试按 retry_of 归组；无显式 retry_of 时同服务+操作+父项的 client 尝试
    按 attempt_no/开始时间聚合为隐式组（标记为推断，证据仍逐条保留）。"""
    groups: dict[str, list[str]] = {}
    for s in g.spans.values():
        if s.retry_of:
            groups.setdefault(s.retry_of, []).append(s.span_id)
            groups.setdefault(s.retry_of, [])  # 键存在
    # 隐式组：同 parent + 同 service/operation 的多个 client
    buckets: dict[tuple, list[str]] = {}
    for s in g.spans.values():
        if s.span_type == CLIENT and not s.retry_of:
            key = (s.parent_id, s.service, s.operation)
            buckets.setdefault(key, []).append(s.span_id)
    for key, ids in buckets.items():
        if len(ids) > 1:
            label = f"inferred:{key[1]}.{key[2]}@{key[0] or 'root'}"
            groups[label] = sorted(ids, key=lambda sid: (g.spans[sid].start_ns or 0))
    # 组内按开始时间排序
    for k, ids in groups.items():
        groups[k] = sorted(set(ids), key=lambda sid: (g.spans[sid].start_ns or 0, sid))
    return groups


def persist_pairs(conn: sqlite3.Connection, pairs: list[dict]) -> None:
    for p in pairs:
        conn.execute(
            "INSERT INTO rpc_pairs(trace_id, client_span_id, server_span_id, match_basis, "
            "confidence, detail) VALUES(?,?,?,?,?,?)",
            (p["trace_id"], p["client_span_id"], p["server_span_id"], p["match_basis"],
             p["confidence"], json.dumps(p["detail"], ensure_ascii=False, sort_keys=True)))
