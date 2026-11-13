"""图加载与完整性/一致性验证。

验证只报告**有证据**的问题；证据缺失走 analyze 的未知/上下界处理，不在这里臆测。
- 循环：parent 链或显式 links 成环 → D10，附环上节点序列。
- 因果不可能：子/被链接记录早于父项开始、事件落在所属 span 外、队列事件顺序倒置等。
- 身份冲突与重复折叠在导入阶段处理（storage.py），这里补图层面的检查。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from .model import (EVT_CANCEL, EVT_DISPATCH, EVT_ENQUEUE, EVT_EXEC_START,
                    EVT_QUEUE_ENTER, EVT_TIMEOUT)


@dataclass
class Span:
    span_id: str
    service: str
    operation: str
    span_type: str
    start_ns: int | None
    end_ns: int | None
    parent_id: str | None
    retry_of: str | None
    attempt_no: int | None
    source_file: str
    source_line: int
    dedup_count: int
    links: list[tuple[str, str]] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)


@dataclass
class AwaitRec:
    await_id: str
    waiter_span_id: str
    start_ns: int
    end_ns: int | None
    mode: str
    result_ids: list[str]
    resolved_by: list[str] | None
    open_flag: bool
    source_file: str
    source_line: int


@dataclass
class TraceGraph:
    trace_id: str
    spans: dict[str, Span]
    awaits: dict[str, AwaitRec]
    events: list[dict]
    issues: list[dict]
    roots: list[str]
    has_unknown_time: bool

    def children_of(self, span_id: str) -> list[Span]:
        return [s for s in self.spans.values() if s.parent_id == span_id]


def _issue(issues: list[dict], severity: str, code: str, evidence: dict) -> None:
    issues.append({"severity": severity, "code": code, "evidence": evidence})


def _detect_cycle(nodes: set[str], edges: dict[str, list[str]]) -> list[str]:
    """DFS 三色法；返回第一条环的节点序列，无环返回 []。"""
    color = {n: 0 for n in nodes}          # 0 白 1 灰 2 黑
    stack: list[str] = []

    def dfs(u: str) -> list[str]:
        color[u] = 1
        stack.append(u)
        for v in edges.get(u, []):
            if v not in color:
                continue                   # 悬挂引用另报
            if color[v] == 1:
                i = stack.index(v)
                return stack[i:] + [v]
            if color[v] == 0:
                found = dfs(v)
                if found:
                    return found
        stack.pop()
        color[u] = 2
        return []

    for n in sorted(nodes):
        if color[n] == 0:
            found = dfs(n)
            if found:
                return found
    return []


def load_trace(conn: sqlite3.Connection, trace_id: str) -> TraceGraph:
    issues: list[dict] = [dict(r) for r in conn.execute(
        "SELECT severity, code, evidence FROM validation_issues WHERE trace_id=?",
        (trace_id,))]
    for it in issues:
        it["evidence"] = json.loads(it["evidence"])
    persisted = {(it["code"], json.dumps(it["evidence"], sort_keys=True, ensure_ascii=False))
                 for it in issues}

    spans: dict[str, Span] = {}
    for r in conn.execute("SELECT * FROM spans WHERE trace_id=? ORDER BY start_ns", (trace_id,)):
        spans[r["span_id"]] = Span(
            r["span_id"], r["service"], r["operation"], r["span_type"],
            r["start_ns"], r["end_ns"], r["parent_id"], r["retry_of"], r["attempt_no"],
            r["source_file"], r["source_line"], r["dedup_count"])
    for r in conn.execute("SELECT * FROM span_links WHERE trace_id=?", (trace_id,)):
        if r["source_id"] in spans:
            spans[r["source_id"]].links.append((r["target_id"], r["rel"]))

    events = [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE trace_id=? ORDER BY at_ns", (trace_id,))]
    by_span: dict[str, list[dict]] = {}
    for e in events:
        by_span.setdefault(e["span_id"], []).append(e)
    for sid, evs in by_span.items():
        if sid in spans:
            spans[sid].events = evs

    awaits: dict[str, AwaitRec] = {}
    for r in conn.execute("SELECT * FROM awaits WHERE trace_id=?", (trace_id,)):
        awaits[r["await_id"]] = AwaitRec(
            r["await_id"], r["waiter_span_id"], r["start_ns"], r["end_ns"], r["mode"],
            json.loads(r["result_ids"]),
            json.loads(r["resolved_by"]) if r["resolved_by"] is not None else None,
            bool(r["open_flag"]), r["source_file"], r["source_line"])

    # ---- 悬挂 parent / link 引用 ----
    for s in spans.values():
        if s.parent_id and s.parent_id not in spans:
            _issue(issues, "warning", "dangling_parent_ref",
                   {"span_id": s.span_id, "missing_parent_id": s.parent_id})
        for target, rel in s.links:
            if target not in spans:
                _issue(issues, "warning", "dangling_link_ref",
                       {"span_id": s.span_id, "missing_target_id": target, "rel": rel})
    for a in awaits.values():
        if a.waiter_span_id not in spans:
            _issue(issues, "error", "await_waiter_missing",
                   {"await_id": a.await_id, "waiter_span_id": a.waiter_span_id})

    # ---- parent 成环 ----
    pedges = {sid: [s.parent_id] for sid, s in spans.items() if s.parent_id}
    cyc = _detect_cycle(set(spans), pedges)
    if cyc:
        _issue(issues, "error", "parent_cycle", {"cycle": cyc})
    # ---- links 成环 ----
    ledges = {sid: [t for t, _ in s.links] for sid, s in spans.items()}
    lcyc = _detect_cycle(set(spans), ledges)
    if lcyc:
        _issue(issues, "error", "link_cycle", {"cycle": lcyc})

    # ---- 因果顺序不可能性 ----
    for s in spans.values():
        if s.parent_id and s.parent_id in spans:
            p = spans[s.parent_id]
            if s.start_ns is not None and p.start_ns is not None and s.start_ns < p.start_ns:
                _issue(issues, "error", "child_starts_before_parent",
                       {"child": s.span_id, "child_start_ns": s.start_ns,
                        "parent": p.span_id, "parent_start_ns": p.start_ns})
        for e in s.events:
            if e["at_ns"] is None or s.start_ns is None or s.end_ns is None:
                continue
            if not (s.start_ns <= e["at_ns"] <= s.end_ns):
                _issue(issues, "warning", "event_outside_span",
                       {"span_id": s.span_id, "event_id": e["event_id"],
                        "at_ns": e["at_ns"], "span_start_ns": s.start_ns,
                        "span_end_ns": s.end_ns})
        for target, _rel in s.links:
            if target in spans:
                t = spans[target]
                if (s.start_ns is not None and t.end_ns is not None
                        and s.start_ns < t.end_ns and t.span_type in ("server", "consumer")):
                    # 链接到一个在本 span 开始时就已结束的下游：可能是共享结果（合法），
                    # 仅 info 提示，由 analyze 结合结果身份判定，不在这里定罪。
                    _issue(issues, "info", "link_to_earlier_span",
                           {"source": s.span_id, "target": target})

    # 队列事件顺序：dispatch/enqueue/queue_enter 应早于 exec_start
    name_order = {EVT_DISPATCH: 0, EVT_ENQUEUE: 0, EVT_QUEUE_ENTER: 1, EVT_EXEC_START: 2}
    timed = [e for e in events if e["at_ns"] is not None and e["name"] in name_order]
    last: dict[str, tuple[int, str]] = {}
    for e in sorted(timed, key=lambda x: x["at_ns"]):
        key = e["span_id"] or "?"
        rank = name_order[e["name"]]
        if key in last and rank < last[key][0]:
            _issue(issues, "error", "causal_event_order_impossible",
                   {"span_id": key, "later_event": last[key][1],
                    "earlier_event": e["name"], "at_ns": e["at_ns"]})
        last[key] = (rank, e["name"])
    # exec_start 晚于 cancel/timeout 仍执行
    for sid, evs in by_span.items():
        times = {e["name"]: e["at_ns"] for e in evs if e["at_ns"] is not None}
        if EVT_EXEC_START in times and (EVT_CANCEL in times or EVT_TIMEOUT in times):
            stop = min(x for n, x in times.items() if n in (EVT_CANCEL, EVT_TIMEOUT))
            if times[EVT_EXEC_START] > stop:
                _issue(issues, "error", "exec_start_after_cancel",
                       {"span_id": sid, "exec_start_ns": times[EVT_EXEC_START],
                        "cancel_or_timeout_ns": stop})

    roots = [sid for sid, s in spans.items()
             if not s.parent_id or s.parent_id not in spans]
    has_unknown = any(s.start_ns is None or s.end_ns is None for s in spans.values()) or \
        any(e["at_ns"] is None for e in events) or any(a.end_ns is None for a in awaits.values())

    # 回写本次新发现的问题（导入阶段已持久化的不重复插入）。
    # 不在这里 commit：由 analyze_all 统一批量提交，避免每个 trace 多次 fsync。
    new_rows = []
    for it in issues:
        sig = (it["code"], json.dumps(it["evidence"], sort_keys=True, ensure_ascii=False))
        if sig not in persisted:
            persisted.add(sig)
            new_rows.append((trace_id, it["severity"], it["code"],
                             json.dumps(it["evidence"], ensure_ascii=False)))
    if new_rows:
        conn.executemany(
            "INSERT INTO validation_issues(trace_id, severity, code, evidence) "
            "VALUES(?,?,?,?)", new_rows)

    return TraceGraph(trace_id, spans, awaits, events, issues, roots, has_unknown)
