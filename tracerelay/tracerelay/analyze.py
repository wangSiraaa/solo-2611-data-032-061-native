"""归因模型与两套守恒口径的核心实现。

口径 A：根响应时间如何被覆盖（coverage ledger）
------------------------------------------------
对根 span 的有界窗口 [root_start, root_end) 做扫描线切分，产出四类**互斥且穷尽**的时间片：

  self   根服务自身活动（内部计算；以及未被任何显式 await 解释的客户端调用墙钟）
  await  显式 await 记录的实际覆盖（all 取窗口并集，重叠只算一次）
  queue  有事件证据的排队等待（dispatch/queue_enter → exec_start），且不落在 await 内
  gap    现有事件无法解释的空档（含“有客户端、无服务端”的未知传输区间，给上下界）

守恒不变量（每次分析自动核验，写 conservation_ok）：
    self_ns + await_ns + queue_ns + gap_ns == root_end - root_start
并行等待只按实际时间覆盖计入，重叠子请求永不重复相加；嵌套 await 的下层阻塞只用于
解释“为什么等”，不再与上层等待相加。

口径 B：各服务消耗了多少执行资源（resource ledger）
----------------------------------------------
按 (服务, 操作) 汇总墙钟占用的**区间并集**：execution / client_span / queue_wait /
explicit_await / background_after_root。允许跨并行分支累加（资源本来就被并行烧掉），
因此总和**不要求、也不应该**等于根窗口。含开放区间（end 缺失）时给 lower/upper 界。

重要边界：本工具没有 CPU 观测，只解释记录到的墙钟活动与等待，不能证明任何服务的 CPU 使用率。
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from .model import (BOUNDED, CERTAIN, UNKNOWN, COV_AWAIT, COV_GAP, COV_QUEUE, COV_SELF,
                    CONSUMER, CLIENT, GAP_TRANSMIT_MISSING, GAP_UNKNOWN, INTERNAL,
                    PRODUCER, RES_AWAIT, RES_BACKGROUND, RES_CLIENT, RES_EXEC,
                    RES_PRODUCER, RES_QUEUE, SERVER, WorkItem, fmt_ns, merge_union,
                    sweep_coverage, union_duration, Interval)
from .rpc import match_rpcs, persist_pairs, retry_groups
from .validate import TraceGraph, load_trace

LONG_GAP_RATIO = 0.1          # 空档超过根窗口 10% 触发 D8
RESUME_LAG_RATIO = 0.25       # 解除后恢复延迟占 await 窗口 25% 以上触发 D2


def _covered_by(start: int, end: int, union: list[Interval]) -> bool:
    """[start,end) 是否整体落在给定区间并集内（用于判断 client 是否被 await 解释）。"""
    remaining = [(start, end)]
    for iv in union:
        if iv.end is None:
            iv = Interval(iv.start, end)
        nxt = []
        for s, e in remaining:
            if iv.end <= s or e <= iv.start:
                nxt.append((s, e))
            else:
                if s < iv.start:
                    nxt.append((s, iv.start))
                if iv.end < e:
                    nxt.append((iv.end, e))
        remaining = nxt
    return not remaining


class Analyzer:
    def __init__(self, conn: sqlite3.Connection, g: TraceGraph):
        self.conn = conn
        self.g = g
        self.tid = g.trace_id
        self.diags: list[dict] = []
        self.causes: list[dict] = []
        # 邻接表一次性构建，避免可达性分析退化为 O(n^2)
        self._children: dict[str, list[str]] = {}
        for sid, s in g.spans.items():
            if s.parent_id:
                self._children.setdefault(s.parent_id, []).append(sid)

    # ------------------------------------------------------------------ #
    # 0. 根、可达性与结果身份
    # ------------------------------------------------------------------ #
    def pick_root(self) -> str | None:
        if not self.g.roots:
            return None
        non_consumer = [r for r in self.g.roots if self.g.spans[r].span_type != CONSUMER]
        pool = non_consumer or self.g.roots
        return sorted(pool, key=lambda sid: (self.g.spans[sid].start_ns is None,
                                             self.g.spans[sid].start_ns or 0))[0]

    def reachable(self, root_id: str) -> set[str]:
        """沿 parent 与显式 links 传播得到的可达集合（上下文传播 != 阻塞等待，这里只判归属）。
        邻接表遍历，复杂度 O(V+E)。"""
        seen: set[str] = set()
        stack = [root_id]
        while stack:
            u = stack.pop()
            if u in seen or u not in self.g.spans:
                continue
            seen.add(u)
            s = self.g.spans[u]
            if s.parent_id:
                stack.append(s.parent_id)
            for t, _ in s.links:
                stack.append(t)
            stack.extend(self._children.get(u, ()))
        return seen

    def build_result_index(self) -> dict[str, str]:
        """结果身份 -> 产出该结果的 span_id。合成记录可用 produces_result 显式声明，
        否则约定结果身份等于完成它的 span_id。"""
        idx: dict[str, str] = {}
        for sid, s in self.g.spans.items():
            idx[sid] = sid
        for e in self.g.events:
            rid = e.get("result_id")
            if rid:
                idx.setdefault(str(rid), e["span_id"])
        return idx

    # ------------------------------------------------------------------ #
    # 1. 排队区间（只用现有事件；缺事件就没有 queue 证据，退化为 gap/unknown）
    # 权威定义：服务端 queue_enter/enqueue -> exec_start。
    # 客户端 dispatch->exec 仅在配对服务端**缺少 queue_enter** 时兜底，
    # 且标记弱证据；dispatch->queue_enter 之间是未知传输，不得冒充排队。
    # 同一次排队的两面取并集折叠，覆盖账本只解释一次。
    # ------------------------------------------------------------------ #
    def _queue_raw(self, reach: set[str]) -> list[WorkItem]:
        raw: list[WorkItem] = []
        for sid, s in self.g.spans.items():
            if sid not in reach or s.start_ns is None:
                continue
            evs = {e["name"]: e["at_ns"] for e in s.events if e["at_ns"] is not None}
            q_start = evs.get("queue_enter") or evs.get("enqueue")
            exec_t = evs.get("exec_start")
            if q_start is not None and exec_t is not None and exec_t > q_start:
                raw.append(WorkItem(Interval(q_start, exec_t), COV_QUEUE, "queue_enter->exec",
                                    "span", sid, s.service, s.operation))
                continue
            if s.span_type == CLIENT:
                d = evs.get("dispatch")
                srv_span = self._paired_server(sid)
                if d is None or srv_span is None:
                    continue
                sev = {e["name"]: e["at_ns"] for e in srv_span.events
                       if e["at_ns"] is not None}
                if "queue_enter" in sev or "enqueue" in sev:
                    continue   # 权威排队区间由服务端那条给出，这里不重复、不吞传输段
                t = sev.get("exec_start")
                if t is not None and t > d:
                    raw.append(WorkItem(Interval(d, t), COV_QUEUE,
                                        "dispatch->exec(no_queue_event:weak)",
                                        "span", sid, s.service, s.operation))
        return raw

    def queue_intervals(self, reach: set[str]) -> list[WorkItem]:
        raw = self._queue_raw(reach)
        if not raw:
            return []
        merged_ivs = merge_union([w.interval for w in raw])
        out: list[WorkItem] = []
        for iv in merged_ivs:
            rep = next((w for w in raw if w.interval.start == iv.start), raw[0])
            out.append(WorkItem(iv, COV_QUEUE, "queue_wait_union", rep.ref_kind,
                                rep.ref_id, rep.service, rep.operation))
        return out

    def queue_evidence(self, reach: set[str]) -> list[WorkItem]:
        """未折叠的排队证据（client 与 server 两面都保留），供诊断逐条引用。"""
        return self._queue_raw(reach)

    def _paired_server(self, client_id: str):
        row = self.conn.execute(
            "SELECT server_span_id FROM rpc_pairs WHERE trace_id=? AND client_span_id=?",
            (self.tid, client_id)).fetchone()
        if row and row["server_span_id"] and row["server_span_id"] in self.g.spans:
            return self.g.spans[row["server_span_id"]]
        return None

    # ------------------------------------------------------------------ #
    # 2. 覆盖账本
    # ------------------------------------------------------------------ #
    def coverage(self, root_id: str, reach: set[str]) -> list[dict]:
        root = self.g.spans[root_id]
        if root.start_ns is None or root.end_ns is None:
            return []                       # 根窗口本身未知：不伪造，整体走未知摘要
        items: list[WorkItem] = []

        # await：显式等待，最高优先；end 缺失时用根窗口右端收口展示，open 状态另存
        for a in self.g.awaits.values():
            if a.waiter_span_id not in reach:
                continue
            end = a.end_ns if a.end_ns is not None else root.end_ns
            if end <= a.start_ns:
                continue
            items.append(WorkItem(Interval(a.start_ns, end), COV_AWAIT,
                                  f"{a.mode}:{a.await_id}", "await", a.await_id))

        # queue
        items.extend(self.queue_intervals(reach))

        # self：根服务的内部计算；客户端调用仅在被显式 await 覆盖时作为自身活动铺贴，
        # 未被 await 解释的 client 区间不允许用 self 吞掉——有事件证据的部分由
        # queue_intervals 归 queue，其余保持 gap（未知传输/处理，配合 D4）。
        await_union = merge_union([
            Interval(a.start_ns, a.end_ns if a.end_ns is not None else root.end_ns)
            for a in self.g.awaits.values() if a.waiter_span_id in reach])
        for sid, s in self.g.spans.items():
            if sid not in reach or s.start_ns is None or s.end_ns is None:
                continue
            if s.service != root.service:
                continue                   # 远端服务的执行不进根覆盖，进资源账本
            if sid == root_id:
                items.append(WorkItem(Interval(s.start_ns, s.end_ns), "root_fallback",
                                      f"root:{s.operation}", "span", sid,
                                      s.service, s.operation))
            elif s.span_type == CLIENT:
                if _covered_by(s.start_ns, s.end_ns, await_union):
                    items.append(WorkItem(Interval(s.start_ns, s.end_ns), COV_SELF,
                                          f"client:{s.operation}", "span", sid,
                                          s.service, s.operation))
            elif s.span_type in (INTERNAL, PRODUCER, SERVER, CONSUMER):
                items.append(WorkItem(Interval(s.start_ns, s.end_ns), COV_SELF,
                                      f"{s.span_type}:{s.operation}", "span", sid,
                                      s.service, s.operation))

        slices = sweep_coverage(root.start_ns, root.end_ns, items)

        # 有客户端、无服务端的未知传输：落在该客户端窗口内的 gap 改标传输未知并给界
        pairs = {p["client_span_id"]: p for p in self._pairs() if p["client_span_id"]}
        for sl in slices:
            if sl["category"] != COV_GAP:
                continue
            for cid, p in pairs.items():
                if p["server_span_id"] is not None:
                    continue
                c = self.g.spans.get(cid)
                if not c or c.start_ns is None or c.end_ns is None:
                    continue
                if c.start_ns <= sl["start_ns"] and sl["end_ns"] <= c.end_ns:
                    sl["sublabel"] = GAP_TRANSMIT_MISSING
                    sl["ref_kind"], sl["ref_id"] = "span", cid
                    sl["service"], sl["operation"] = c.service, c.operation
        return slices

    def _pairs(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM rpc_pairs WHERE trace_id=?", (self.tid,))]

    # ------------------------------------------------------------------ #
    # 3. await 解除关系与真实阻塞原因（嵌套向下追，不重复计时）
    # ------------------------------------------------------------------ #
    def explain_awaits(self, reach: set[str], result_idx: dict[str, str]) -> None:
        for a in sorted(self.g.awaits.values(), key=lambda x: x.start_ns):
            if a.waiter_span_id not in reach:
                continue
            winners = a.resolved_by if a.resolved_by is not None else []
            needed = a.result_ids
            if a.mode == "all":
                # all 由最后满足的必要结果解除；resolved_by 缺失时用结果 span 末端推断
                blocker_id, phase, detail = self._last_needed(a, result_idx, winners)
            else:
                # any 只按实际胜出结果解释；未胜出分支另计 D6
                blocker_id, phase, detail = self._winner(a, result_idx, winners)
            nested = self._nested_under(a)
            if nested:
                inner = nested[-1]
                # 外层等待的真实阻塞阶段透传到最深一层 await 的解释，
                # 而不是停在中间的 internal 编排 span
                inner_winners = inner.resolved_by if inner.resolved_by is not None else []
                if inner.mode == "all":
                    _, inner_phase, inner_detail = self._last_needed(
                        inner, result_idx, inner_winners)
                else:
                    _, inner_phase, inner_detail = self._winner(
                        inner, result_idx, inner_winners)
                phase = inner_phase
                detail["deep_blocker_detail"] = inner_detail
                detail["nested_chain"] = [x.await_id for x in nested]
                detail["deep_blocker_await"] = inner.await_id
            self.causes.append({
                "trace_id": self.tid, "await_id": a.await_id,
                "waiter_span_id": a.waiter_span_id, "mode": a.mode,
                "start_ns": a.start_ns, "end_ns": a.end_ns,
                "resolved_by": json.dumps(winners, ensure_ascii=False),
                "blocker_phase": phase,
                "blocker_detail": json.dumps(detail, ensure_ascii=False, sort_keys=True),
                "evidence": json.dumps(self._await_evidence(a, blocker_id),
                                       ensure_ascii=False, sort_keys=True),
                "upper_await_id": nested[0].await_id if nested and nested[0] is not a else "",
            })
            if nested:
                self.diags.append(self._diag(
                    "D5_nested_await_real_blocker", CERTAIN,
                    f"await {a.await_id} 的真实阻塞在嵌套等待 {nested[-1].await_id}",
                    {"outer": a.await_id, "chain": [x.await_id for x in nested],
                     "deep_phase": phase}))
            self._diag_any_losers(a, result_idx, winners)

    def _span_for_result(self, rid: str, result_idx: dict[str, str]):
        sid = result_idx.get(str(rid)) or (rid if rid in self.g.spans else None)
        sp = self.g.spans.get(sid) if sid else None
        if sp is None:
            return None
        # 结果身份通常是 client span：穿透 RPC 配对找到真正干活的服务端，
        # 找不到配对服务端时退回 client（并在阶段里体现证据不足）
        if sp.span_type == CLIENT:
            srv = self._paired_server(sp.span_id)
            if srv is not None:
                return srv
        return sp

    def _phase_of(self, span) -> tuple[str, dict]:
        evs = {e["name"]: e["at_ns"] for e in span.events if e["at_ns"] is not None}
        if "exec_start" in evs:
            return "execution", {"span_id": span.span_id, "service": span.service,
                                 "operation": span.operation}
        if "queue_enter" in evs or "enqueue" in evs:
            return "queue", {"span_id": span.span_id, "service": span.service}
        return "unknown_phase", {"span_id": span.span_id,
                                 "reason": "no_queue_or_exec_event"}

    def _last_needed(self, a, result_idx, winners):
        last, last_span = None, None
        for rid in a.result_ids:
            sp = self._span_for_result(rid, result_idx)
            if sp is None or sp.end_ns is None:
                continue
            if last is None or sp.end_ns > last:
                last, last_span = sp.end_ns, sp
        if last_span is None:
            return None, UNKNOWN, {"reason": "result_spans_missing_or_open",
                                   "result_ids": a.result_ids}
        phase, detail = self._phase_of(last_span)
        detail["satisfied_at_ns"] = last
        return last_span.span_id, phase, detail

    def _winner(self, a, result_idx, winners):
        if not winners:
            return None, UNKNOWN, {"reason": "no_resolved_by_event",
                                   "result_ids": a.result_ids}
        sp = self._span_for_result(winners[0], result_idx)
        if sp is None:
            return None, UNKNOWN, {"reason": "winner_span_missing", "winner": winners[0]}
        phase, detail = self._phase_of(sp)
        detail["winner_result"] = winners[0]
        return sp.span_id, phase, detail

    def _nested_under(self, a):
        """返回完全嵌套在 a 窗口内的其它 await 链（内层在时间上被外层覆盖，不重复计时）。"""
        chain = []
        cur = a
        moved = True
        while moved:
            moved = False
            for b in self.g.awaits.values():
                if b is cur or b in chain:
                    continue
                if b.start_ns >= cur.start_ns and (
                        cur.end_ns is None or (b.end_ns is not None and b.end_ns <= cur.end_ns)):
                    chain.append(b)
                    cur = b
                    moved = True
                    break
        return chain

    def _await_evidence(self, a, blocker_id):
        ev = {"await_source": [a.source_file, a.source_line],
              "waiter": a.waiter_span_id, "window_ns": [a.start_ns, a.end_ns]}
        if blocker_id and blocker_id in self.g.spans:
            s = self.g.spans[blocker_id]
            ev["blocker_span_source"] = [s.source_file, s.source_line]
            ev["blocker_events"] = [[e["event_id"], e["at_ns"]] for e in s.events]
        return ev

    def _diag_any_losers(self, a, result_idx, winners):
        if a.mode != "any":
            return
        win_set = {str(x) for x in winners}
        losers = []
        for rid in a.result_ids:
            if str(rid) in win_set:
                continue
            sp = self._span_for_result(rid, result_idx)
            if sp and sp.end_ns is not None:
                losers.append({"result": rid, "span_id": sp.span_id,
                               "residual_ns": max(0, sp.end_ns - (a.end_ns or sp.end_ns))})
        if losers:
            residual = sum(x["residual_ns"] for x in losers)
            self.diags.append(self._diag(
                "D6_any_loser_residual_work", CERTAIN,
                f"any 等待 {a.await_id} 胜出后，{len(losers)} 个未胜出分支仍有残留工作",
                {"await_id": a.await_id, "losers": losers,
                 "residual_ns": residual, "note": "计入资源账本，不计入根响应覆盖"}))

    # ------------------------------------------------------------------ #
    # 4. 资源账本（区间并集，分服务；开放区间给界）
    # ------------------------------------------------------------------ #
    def resource_ledger(self, root_id: str, reach: set[str], root_end: int | None):
        buckets: dict[tuple, dict] = defaultdict(
            lambda: {"exec": [], "client": [], "producer": [], "queue": [], "await": [],
                     "bg": [], "open": False})
        for sid, s in self.g.spans.items():
            key = (s.service, s.operation)
            iv = Interval(s.start_ns, s.end_ns) if s.start_ns is not None else None
            in_bg = root_end is not None and s.start_ns is not None and s.start_ns >= root_end
            if s.span_type in (SERVER, INTERNAL, CONSUMER) and iv:
                kind = "bg" if in_bg and sid not in reach else "exec"
                buckets[key][kind].append(iv)
            elif s.span_type == CLIENT and iv:
                buckets[key]["client"].append(iv)
            elif s.span_type == PRODUCER and iv:
                buckets[key]["producer"].append(iv)
            if s.start_ns is None or s.end_ns is None:
                buckets[key]["open"] = True
            q_evs = {e["name"]: e["at_ns"] for e in s.events if e["at_ns"] is not None}
            q0, q1 = q_evs.get("queue_enter") or q_evs.get("enqueue"), q_evs.get("exec_start")
            if q0 is not None and q1 is not None and q1 > q0:
                buckets[key]["queue"].append(Interval(q0, q1))
        for a in self.g.awaits.values():
            w = self.g.spans.get(a.waiter_span_id)
            if not w:
                continue
            key = (w.service, w.operation)
            buckets[key]["await"].append(
                Interval(a.start_ns, a.end_ns if a.end_ns is not None else None))
            if a.end_ns is None:
                buckets[key]["open"] = True

        rows = []
        horizon = root_end
        for (service, operation), b in sorted(buckets.items()):
            def dur(k):
                return union_duration(b[k], horizon_end=horizon)
            exec_ns, client_ns = dur("exec"), dur("client")
            prod_ns, queue_ns, await_ns, bg_ns = (dur("producer"), dur("queue"),
                                                  dur("await"), dur("bg"))
            known = [x for x in (exec_ns, client_ns, prod_ns, queue_ns, await_ns, bg_ns)
                     if x is not None]
            lower = sum(known)
            upper = lower if not b["open"] else None
            rows.append({
                "trace_id": self.tid, "service": service, "operation": operation,
                "work_kind": "aggregated",
                "exec_ns": exec_ns, "queue_ns": queue_ns, "await_ns": await_ns,
                "background_ns": bg_ns, "client_ns": client_ns, "producer_ns": prod_ns,
                "open_flag": 1 if b["open"] else 0,
                "lower_bound_ns": lower, "upper_bound_ns": upper})
        return rows

    # ------------------------------------------------------------------ #
    # 5. 诊断（只在事件支持时下确定结论，否则给界/未知）
    # ------------------------------------------------------------------ #
    def diagnostics(self, root_id: str, slices: list[dict], root_window: int | None):
        g = self.g
        reach = self.reachable(root_id)
        # D1 队列等待（逐条证据；同一次排队的两面都列出，覆盖账本里已并集折叠）
        seen_q: set[tuple] = set()
        for wi in self.queue_evidence(reach):
            d = wi.interval.duration
            sig = (wi.ref_id, wi.sublabel)
            if sig in seen_q:
                continue
            seen_q.add(sig)
            self.diags.append(self._diag(
                "D1_queue_wait_before_exec", CERTAIN,
                f"{wi.service}/{wi.operation} 下游尚未开始执行，排队 {fmt_ns(d)}",
                {"span_id": wi.ref_id, "phase": wi.sublabel,
                 "queue_start_ns": wi.interval.start, "exec_start_ns": wi.interval.end,
                 "wait_ns": d,
                 "source": [g.spans[wi.ref_id].source_file, g.spans[wi.ref_id].source_line]}))
        # D2 结果已就绪但等待者迟迟恢复
        for a in g.awaits.values():
            if a.end_ns is None:
                continue
            winners = a.resolved_by or []
            ready = []
            for rid in winners:
                sp = self._span_for_result(rid, self.build_result_index())
                if sp and sp.end_ns is not None:
                    ready.append(sp.end_ns)
            if not ready:
                continue
            ready_at = max(ready) if a.mode == "all" else min(ready)
            lag = a.end_ns - ready_at
            win = a.end_ns - a.start_ns
            if lag > 0 and win > 0 and lag / win >= RESUME_LAG_RATIO:
                self.diags.append(self._diag(
                    "D2_result_ready_but_resume_lag", CERTAIN,
                    f"await {a.await_id} 结果就绪后 {fmt_ns(lag)} 才恢复执行",
                    {"await_id": a.await_id, "ready_ns": ready_at,
                     "resume_ns": a.end_ns, "lag_ns": lag}))
        # D3 重试放大
        for group_id, ids in retry_groups(g).items():
            if len(ids) < 2:
                continue
            total = 0
            attempts = []
            for sid in ids:
                s = g.spans[sid]
                d = (s.end_ns - s.start_ns) if s.start_ns is not None and s.end_ns is not None else None
                attempts.append({"span_id": sid, "duration_ns": d,
                                 "has_timeout": any(e["name"] == "timeout" for e in s.events),
                                 "has_cancel": any(e["name"] == "cancel" for e in s.events)})
                if d is not None:
                    total += d
            last = g.spans[ids[-1]]
            success = last.end_ns is not None and not any(
                e["name"] in ("timeout", "cancel") for e in last.events)
            self.diags.append(self._diag(
                "D3_retry_background_amplification", CERTAIN,
                f"重试组 {group_id} 共 {len(ids)} 次尝试，累计墙钟 {fmt_ns(total)}（并行口径不重复计）",
                {"group": group_id, "attempts": attempts, "total_attempt_wall_ns": total,
                 "success_attempt": ids[-1] if success else None,
                 "failed_attempts": ids[:-1] if success else ids}))
        # D4 缺失服务端
        for p in self._pairs():
            if p["client_span_id"] and not p["server_span_id"]:
                c = g.spans[p["client_span_id"]]
                bound = ((c.end_ns - c.start_ns)
                         if c.start_ns is not None and c.end_ns is not None else None)
                self.diags.append(self._diag(
                    "D4_missing_server_unknown_transmit", BOUNDED,
                    f"客户端 {p['client_span_id']} 无服务端记录，传输/处理区间未知",
                    {"client_span_id": p["client_span_id"],
                     "client_window_ns": bound,
                     "upper_bound_ns": bound,
                     "lower_bound_ns": 0 if bound is not None else None,
                     "note": "不能区分网络、排队还是远端执行；删除服务端事件后结论必须退化"}))
        # D7 超时/取消残留
        for s in g.spans.values():
            for e in s.events:
                if e["name"] in ("timeout", "cancel"):
                    residual = (s.end_ns - e["at_ns"]) if (
                        e["at_ns"] is not None and s.end_ns is not None) else None
                    self.diags.append(self._diag(
                        "D7_timeout_cancel_residual",
                        CERTAIN if residual is not None else UNKNOWN,
                        f"{s.service}/{s.operation} 发生 {e['name']} 后仍记录到活动",
                        {"span_id": s.span_id, "event_id": e["event_id"],
                         "at_ns": e["at_ns"], "residual_ns": residual}))
        # D8 长空档
        if root_window:
            for sl in slices:
                if sl["category"] == COV_GAP and sl["duration_ns"] >= LONG_GAP_RATIO * root_window:
                    self.diags.append(self._diag(
                        "D8_long_unexplained_gap",
                        BOUNDED if sl["sublabel"] == GAP_TRANSMIT_MISSING else UNKNOWN,
                        f"根窗口存在 {fmt_ns(sl['duration_ns'])} 的不可解释空档",
                        {"start_ns": sl["start_ns"], "end_ns": sl["end_ns"],
                         "duration_ns": sl["duration_ns"], "sublabel": sl["sublabel"],
                         "bounds": {"lower_ns": 0, "upper_ns": sl["duration_ns"]}}))
        # D9/D10 来自验证问题
        for iss in g.issues:
            if iss["code"] in ("causal_event_order_impossible", "child_starts_before_parent",
                               "exec_start_after_cancel", "negative_duration"):
                self.diags.append(self._diag(
                    "D9_impossible_causal_order", CERTAIN,
                    f"因果顺序不可能：{iss['code']}", iss["evidence"]))
            if iss["code"] in ("parent_cycle", "link_cycle", "conflicting_span_identity"):
                self.diags.append(self._diag(
                    "D10_cycle_or_conflicting_identity", CERTAIN,
                    f"身份/链接异常：{iss['code']}", iss["evidence"]))

    def _diag(self, code: str, certainty: str, title: str, evidence: dict) -> dict:
        return {"trace_id": self.tid, "diag_code": code, "certainty": certainty,
                "title": title, "detail": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                "evidence": json.dumps(evidence, ensure_ascii=False, sort_keys=True)}

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def run(self) -> dict:
        root_id = self.pick_root()
        pairs = match_rpcs(self.g)
        persist_pairs(self.conn, pairs)
        if root_id is None:
            return {"trace_id": self.tid, "status": "no_root"}
        root = self.g.spans[root_id]
        reach = self.reachable(root_id)
        result_idx = self.build_result_index()

        slices = self.coverage(root_id, reach)
        self.explain_awaits(reach, result_idx)

        root_window = (root.end_ns - root.start_ns
                       if root.start_ns is not None and root.end_ns is not None else None)
        totals = {COV_SELF: 0, COV_AWAIT: 0, COV_QUEUE: 0, COV_GAP: 0}
        for i, sl in enumerate(slices):
            totals[sl["category"]] += sl["duration_ns"]
            self.conn.execute(
                "INSERT INTO timeline_slices(trace_id, slice_no, start_ns, end_ns, duration_ns, "
                "category, sublabel, ref_kind, ref_id, service, operation) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (self.tid, i, sl["start_ns"], sl["end_ns"], sl["duration_ns"], sl["category"],
                 sl["sublabel"], sl["ref_kind"], sl["ref_id"], sl["service"], sl["operation"]))
        conservation = (root_window is not None and
                        sum(totals.values()) == root_window)

        # 根结束后的消费者/后台工作（资源消耗，不属于根响应瓶颈）
        bg_rows = []
        bg_open = 0
        if root.end_ns is not None:
            for sid, s in self.g.spans.items():
                if s.start_ns is not None and s.start_ns >= root.end_ns:
                    bg_rows.append(Interval(s.start_ns, s.end_ns))
                    if s.end_ns is None:
                        bg_open = 1
        bg_ns = union_duration(bg_rows, horizon_end=None)

        ledger = self.resource_ledger(root_id, reach, root.end_ns)
        for r in ledger:
            self.conn.execute(
                "INSERT INTO resource_ledger(trace_id, service, operation, work_kind, exec_ns, "
                "queue_ns, await_ns, background_ns, open_flag, lower_bound_ns, upper_bound_ns) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (r["trace_id"], r["service"], r["operation"], r["work_kind"], r["exec_ns"],
                 r["queue_ns"], r["await_ns"], r["background_ns"], r["open_flag"],
                 r["lower_bound_ns"], r["upper_bound_ns"]))
        for c in self.causes:
            self.conn.execute(
                "INSERT INTO await_causes(trace_id, await_id, waiter_span_id, mode, start_ns, "
                "end_ns, resolved_by, blocker_phase, blocker_detail, evidence, upper_await_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (c["trace_id"], c["await_id"], c["waiter_span_id"], c["mode"], c["start_ns"],
                 c["end_ns"], c["resolved_by"], c["blocker_phase"], c["blocker_detail"],
                 c["evidence"], c["upper_await_id"]))

        self.diagnostics(root_id, slices, root_window)
        seen_diag = set()
        for d in self.diags:
            sig = (d["diag_code"], d["evidence"])
            if sig in seen_diag:
                continue
            seen_diag.add(sig)
            self.conn.execute(
                "INSERT INTO diagnostics(trace_id, diag_code, certainty, title, detail, evidence) "
                "VALUES(?,?,?,?,?,?)",
                (d["trace_id"], d["diag_code"], d["certainty"], d["title"], d["detail"],
                 d["evidence"]))

        gap_slices = [s for s in slices if s["category"] == COV_GAP]
        gap_exact = sum(s["duration_ns"] for s in gap_slices)
        # 完整 = 时间全已知 + 无空档 + 无缺失服务端 + 关键执行证据没有整体丢失。
        # 若存在需要执行/派发证据的 span 却没有任何对应事件，视为不完整（结论退化的标志）。
        event_names = {e["name"] for e in self.g.events if e["at_ns"] is not None}
        needs_exec = any(s.span_type in (SERVER, CONSUMER) for s in self.g.spans.values())
        missing_exec_evidence = needs_exec and not (
            {"exec_start", "queue_enter", "enqueue"} & event_names)
        complete = (root_window is not None and not self.g.has_unknown_time
                    and totals[COV_GAP] == 0
                    and not missing_exec_evidence
                    and not any(d["diag_code"] == "D4_missing_server_unknown_transmit"
                                for d in self.diags))
        summary = {
            "trace_id": self.tid, "root_span_id": root_id, "root_service": root.service,
            "root_operation": root.operation,
            "request_type": f"{root.service}/{root.operation}",
            "root_start_ns": root.start_ns, "root_end_ns": root.end_ns,
            "root_window_ns": root_window,
            "complete_flag": 1 if complete else 0,
            "has_gap": 1 if totals[COV_GAP] else 0,
            "has_unknown_time": 1 if self.g.has_unknown_time else 0,
            "n_spans": len(self.g.spans), "n_events": len(self.g.events),
            "n_awaits": len(self.g.awaits),
            "self_ns": totals[COV_SELF], "await_ns": totals[COV_AWAIT],
            "queue_ns": totals[COV_QUEUE], "gap_ns": totals[COV_GAP],
            "gap_lower_ns": 0 if gap_slices else 0,
            "gap_upper_ns": gap_exact,
            "conservation_ok": 1 if conservation else 0,
            "background_ns": bg_ns, "background_open": bg_open}
        self.conn.execute(
            "INSERT INTO analysis_trace VALUES(:trace_id,:root_span_id,:root_service,"
            ":root_operation,:request_type,:root_start_ns,:root_end_ns,:root_window_ns,"
            ":complete_flag,:has_gap,:has_unknown_time,:n_spans,:n_events,:n_awaits,"
            ":self_ns,:await_ns,:queue_ns,:gap_ns,:gap_lower_ns,:gap_upper_ns,"
            ":conservation_ok,:background_ns,:background_open)", summary)
        return summary


def analyze_all(conn: sqlite3.Connection) -> list[dict]:
    """批量分析。为避免逐 trace 多次索引往返，先按 trace_id 顺序扫描一次全部小表，
    在内存分组后再逐个分析；时间线/泳道等大结果仍按需经索引读取，不整体载入前端。"""
    from .storage import reset_analysis
    from .validate import TraceGraph, Span, AwaitRec
    reset_analysis(conn)

    def group(sql):
        bucket: dict[str, list] = {}
        for r in conn.execute(sql):
            bucket.setdefault(r["trace_id"], []).append(dict(r))
        return bucket

    spans_by = group("SELECT * FROM spans ORDER BY trace_id, start_ns")
    links_by = group("SELECT * FROM span_links ORDER BY trace_id")
    events_by = group("SELECT * FROM events ORDER BY trace_id, at_ns")
    awaits_by = group("SELECT * FROM awaits ORDER BY trace_id")
    issues_by = group("SELECT * FROM validation_issues ORDER BY trace_id")

    out = []
    tids = [r["trace_id"] for r in conn.execute(
        "SELECT DISTINCT trace_id FROM spans ORDER BY trace_id")]
    for tid in tids:
        g = _build_graph(conn, tid, spans_by.get(tid, []), links_by.get(tid, []),
                         events_by.get(tid, []), awaits_by.get(tid, []),
                         issues_by.get(tid, []))
        out.append(Analyzer(conn, g).run())
    conn.commit()          # 整批一次提交，避免逐 trace fsync（10 万 span 级关键优化）
    return out


def _build_graph(conn, trace_id, span_rows, link_rows, event_rows, await_rows, issue_rows):
    from .validate import TraceGraph, Span, AwaitRec
    import json as _json
    issues = [{"severity": r["severity"], "code": r["code"],
               "evidence": _json.loads(r["evidence"])} for r in issue_rows]
    persisted = {(it["code"], _json.dumps(it["evidence"], sort_keys=True, ensure_ascii=False))
                 for it in issues}
    spans: dict[str, Span] = {}
    for r in span_rows:
        spans[r["span_id"]] = Span(
            r["span_id"], r["service"], r["operation"], r["span_type"], r["start_ns"],
            r["end_ns"], r["parent_id"], r["retry_of"], r["attempt_no"],
            r["source_file"], r["source_line"], r["dedup_count"])
    for r in link_rows:
        if r["source_id"] in spans:
            spans[r["source_id"]].links.append((r["target_id"], r["rel"]))
    by_span: dict[str, list] = {}
    for e in event_rows:
        by_span.setdefault(e["span_id"], []).append(e)
    for sid, evs in by_span.items():
        if sid in spans:
            spans[sid].events = evs
    awaits = {}
    for r in await_rows:
        awaits[r["await_id"]] = AwaitRec(
            r["await_id"], r["waiter_span_id"], r["start_ns"], r["end_ns"], r["mode"],
            _json.loads(r["result_ids"]),
            _json.loads(r["resolved_by"]) if r["resolved_by"] is not None else None,
            bool(r["open_flag"]), r["source_file"], r["source_line"])

    # 图层面校验（与 validate.load_trace 同规则，复用其检查函数的内联版本）
    _graph_checks(conn, trace_id, spans, event_rows, awaits, issues, persisted)

    roots = [sid for sid, s in spans.items()
             if not s.parent_id or s.parent_id not in spans]
    has_unknown = any(s.start_ns is None or s.end_ns is None for s in spans.values()) or \
        any(e["at_ns"] is None for e in event_rows) or \
        any(a.end_ns is None for a in awaits.values())
    return TraceGraph(trace_id, spans, awaits, event_rows, issues, roots, has_unknown)


def _graph_checks(conn, trace_id, spans, events, awaits, issues, persisted):
    from .validate import _detect_cycle, _issue
    from .model import EVT_CANCEL, EVT_DISPATCH, EVT_ENQUEUE, EVT_EXEC_START, EVT_QUEUE_ENTER
    import json as _json

    def add(severity, code, evidence):
        sig = (code, _json.dumps(evidence, sort_keys=True, ensure_ascii=False))
        if sig in persisted:
            return
        persisted.add(sig)
        issues.append({"severity": severity, "code": code, "evidence": evidence})

    for s in spans.values():
        if s.parent_id and s.parent_id not in spans:
            add("warning", "dangling_parent_ref",
                {"span_id": s.span_id, "missing_parent_id": s.parent_id})
        for target, rel in s.links:
            if target not in spans:
                add("warning", "dangling_link_ref",
                    {"span_id": s.span_id, "missing_target_id": target, "rel": rel})
    for a in awaits.values():
        if a.waiter_span_id not in spans:
            add("error", "await_waiter_missing",
                {"await_id": a.await_id, "waiter_span_id": a.waiter_span_id})
    pedges = {sid: [s.parent_id] for sid, s in spans.items() if s.parent_id}
    cyc = _detect_cycle(set(spans), pedges)
    if cyc:
        add("error", "parent_cycle", {"cycle": cyc})
    ledges = {sid: [t for t, _ in s.links] for sid, s in spans.items()}
    lcyc = _detect_cycle(set(spans), ledges)
    if lcyc:
        add("error", "link_cycle", {"cycle": lcyc})
    for s in spans.values():
        if s.parent_id and s.parent_id in spans:
            p = spans[s.parent_id]
            if s.start_ns is not None and p.start_ns is not None and s.start_ns < p.start_ns:
                add("error", "child_starts_before_parent",
                    {"child": s.span_id, "child_start_ns": s.start_ns,
                     "parent": p.span_id, "parent_start_ns": p.start_ns})
        for e in s.events:
            if e["at_ns"] is None or s.start_ns is None or s.end_ns is None:
                continue
            if not (s.start_ns <= e["at_ns"] <= s.end_ns):
                add("warning", "event_outside_span",
                    {"span_id": s.span_id, "event_id": e["event_id"], "at_ns": e["at_ns"],
                     "span_start_ns": s.start_ns, "span_end_ns": s.end_ns})
    name_order = {EVT_DISPATCH: 0, EVT_ENQUEUE: 0, EVT_QUEUE_ENTER: 1, EVT_EXEC_START: 2}
    timed = [e for e in events if e["at_ns"] is not None and e["name"] in name_order]
    last: dict[str, tuple] = {}
    for e in sorted(timed, key=lambda x: x["at_ns"]):
        key = e["span_id"] or "?"
        rank = name_order[e["name"]]
        if key in last and rank < last[key][0]:
            add("error", "causal_event_order_impossible",
                {"span_id": key, "later_event": last[key][1],
                 "earlier_event": e["name"], "at_ns": e["at_ns"]})
        last[key] = (rank, e["name"])
    by_span: dict[str, list] = {}
    for e in events:
        by_span.setdefault(e["span_id"], []).append(e)
    for sid, evs in by_span.items():
        times = {e["name"]: e["at_ns"] for e in evs if e["at_ns"] is not None}
        if EVT_EXEC_START in times and (EVT_CANCEL in times or "timeout" in times):
            stop = min(x for n, x in times.items() if n in (EVT_CANCEL, "timeout"))
            if times[EVT_EXEC_START] > stop:
                add("error", "exec_start_after_cancel",
                    {"span_id": sid, "exec_start_ns": times[EVT_EXEC_START],
                     "cancel_or_timeout_ns": stop})
    new_rows = [(trace_id, it["severity"], it["code"],
                 _json.dumps(it["evidence"], ensure_ascii=False)) for it in issues]
    if new_rows:
        conn.executemany(
            "INSERT INTO validation_issues(trace_id, severity, code, evidence) "
            "VALUES(?,?,?,?)", new_rows)
