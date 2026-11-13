"""合成追踪生成器 —— 全部数据由本项目合成，不采集任何真实系统。

产出 7 类手工微型/结构化案例（时间单位在代码里以毫秒换算为纳秒）：
  sync        同步链：根 -> client -> server，含显式 await 与派发/队列/执行事件
  parallel    并行 all：一个 await(all) 同时等两个下游，验证重叠不重复计
  any         竞争 any：胜出分支解除等待，未胜出分支残留工作另计
  retry       超时重试：失败尝试（timeout）与成功尝试都保留身份
  async_tail  异步尾任务：消费者在根结束后继续，计入资源账本但不进根覆盖
  shared      共享下游结果：两个调用方 link 到同一结果 span
  missing     缺失服务端：只有 client，没有 server，传输区间必须未知

另提供 ``bulk`` 批量合成（默认 10 万 span 量级），用于验证索引与按需读取。
"""
from __future__ import annotations

import argparse
import json
import os
import random

MS = 1_000_000  # 1ms 的纳秒数


class Builder:
    def __init__(self, trace_id: str, t0: int = 0):
        self.trace_id = trace_id
        self.t0 = t0
        self.records: list[dict] = []
        self._seq = 0

    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self.trace_id}-{self._seq}"

    def span(self, service: str, operation: str, span_type: str, start_ms: float,
             end_ms: float | None, parent: str | None = None,
             links: list | None = None, retry_of: str | None = None,
             attempt_no: int | None = None, span_id: str | None = None,
             produces: list[str] | None = None) -> str:
        sid = span_id or self._id(span_type[:3])
        rec = {"kind": "span", "trace_id": self.trace_id, "span_id": sid,
               "service": service, "operation": operation, "span_type": span_type,
               "start_ns": None if start_ms is None else self.t0 + int(start_ms * MS),
               "end_ns": None if end_ms is None else self.t0 + int(end_ms * MS),
               "parent_id": parent, "links": links or []}
        if retry_of is not None:
            rec["retry_of"] = retry_of
        if attempt_no is not None:
            rec["attempt_no"] = attempt_no
        if produces:
            rec["produces_result"] = produces
        self.records.append(rec)
        return sid

    def event(self, name: str, at_ms: float, span_id: str | None = None,
              result_id: str | None = None) -> str:
        eid = self._id("evt")
        rec = {"kind": "event", "trace_id": self.trace_id, "event_id": eid,
               "span_id": span_id, "name": name,
               "at_ns": None if at_ms is None else self.t0 + int(at_ms * MS)}
        if result_id:
            rec["result_id"] = result_id
        self.records.append(rec)
        return eid

    def await_rec(self, waiter: str, start_ms: float, end_ms: float | None,
                  result_ids: list[str], mode: str, resolved_by: list[str] | None,
                  await_id: str | None = None) -> str:
        aid = await_id or self._id("aw")
        rec = {"kind": "await", "trace_id": self.trace_id, "await_id": aid,
               "waiter_span_id": waiter,
               "start_ns": self.t0 + int(start_ms * MS),
               "end_ns": None if end_ms is None else self.t0 + int(end_ms * MS),
               "result_ids": result_ids, "mode": mode, "resolved_by": resolved_by}
        self.records.append(rec)
        return aid


# ---------------------------------------------------------------------- #
# 案例 1：同步链
# 根窗口 30ms 被自身活动铺贴：root(self) 0-8 -> await 8-24 -> self 24-30
# client 完全落在 await 内不另计；越界 cancel 事件用于验证 event_outside_span 告警
# ---------------------------------------------------------------------- #
def case_sync(tid: str = "T-sync") -> list[dict]:
    b = Builder(tid)
    root = b.span("gateway", "handle", "server", 0, 30)
    b.span("gateway", "parse", "internal", 0, 8, root)
    rid = b.span("gateway", "callBilling", "client", 8, 24, root)
    b.await_rec(root, 8, 24, [rid], "all", [rid], await_id="aw-sync")
    b.event("dispatch", 8, rid)
    srv = b.span("billing", "charge", "server", 10, 22, root,
                 links=[{"span_id": rid, "rel": "rpc"}])
    b.event("queue_enter", 10, srv)
    b.event("exec_start", 12, srv)
    b.span("gateway", "render", "internal", 24, 30, root)
    b.event("cancel", 99, srv)  # 越界事件，验证 event_outside_span 告警（放在窗口外）
    return b.records


# ---------------------------------------------------------------------- #
# 案例 2：并行 all —— 重叠窗口只按实际覆盖计一次
# 根窗口 32ms：self 0-8 -> await 8-24（两下游并行，各自 14/13ms 只算 16ms 并集）-> self 24-32
# ---------------------------------------------------------------------- #
def case_parallel(tid: str = "T-parallel") -> list[dict]:
    b = Builder(tid)
    root = b.span("gateway", "handle", "server", 0, 32)
    b.span("gateway", "parse", "internal", 0, 8, root)
    c1 = b.span("gateway", "callA", "client", 8, 23, root)
    c2 = b.span("gateway", "callB", "client", 9, 24, root)
    b.await_rec(root, 8, 24, [c1, c2], "all", [c1, c2], await_id="aw-all")
    b.event("dispatch", 8, c1)
    b.event("dispatch", 9, c2)
    s1 = b.span("svcA", "query", "server", 10, 22, root,
                links=[{"span_id": c1, "rel": "rpc"}])
    s2 = b.span("svcB", "query", "server", 11, 23, root,
                links=[{"span_id": c2, "rel": "rpc"}])
    for s, q, x in ((s1, 10, 12), (s2, 11, 14)):
        b.event("queue_enter", q, s)
        b.event("exec_start", x, s)
    b.span("gateway", "merge", "internal", 24, 32, root)
    return b.records


# ---------------------------------------------------------------------- #
# 案例 3：竞争 any —— 只按胜出结果解释，败者残留进 D6/资源账本
# 根窗口 24ms：self 0-5 -> await 5-16（11ms）-> self 16-24
# ---------------------------------------------------------------------- #
def case_any(tid: str = "T-any") -> list[dict]:
    b = Builder(tid)
    root = b.span("gateway", "handle", "server", 0, 24)
    b.span("gateway", "parse", "internal", 0, 5, root)
    c1 = b.span("gateway", "raceA", "client", 5, 15, root)
    c2 = b.span("gateway", "raceB", "client", 5, 19, root)
    c3 = b.span("gateway", "raceC", "client", 5, 22, root)
    b.await_rec(root, 5, 16, [c1, c2, c3], "any", [c1], await_id="aw-any")
    b.event("dispatch", 5, c1)
    b.event("dispatch", 5, c2)
    b.event("dispatch", 5, c3)
    s1 = b.span("svcA", "fast", "server", 6, 15, root,
                links=[{"span_id": c1, "rel": "rpc"}])
    s2 = b.span("svcB", "slow", "server", 6, 19, root,
                links=[{"span_id": c2, "rel": "rpc"}])
    s3 = b.span("svcC", "slow", "server", 6, 22, root,
                links=[{"span_id": c3, "rel": "rpc"}])
    for s in (s1, s2, s3):
        b.event("exec_start", 6, s)
    b.span("gateway", "use_winner", "internal", 16, 24, root)
    return b.records


# ---------------------------------------------------------------------- #
# 案例 4：超时重试 —— 失败/成功尝试都保留身份
# 根窗口 40ms：self 0-5 -> await 5-37（32ms）-> self 37-40
# ---------------------------------------------------------------------- #
def case_retry(tid: str = "T-retry") -> list[dict]:
    b = Builder(tid)
    root = b.span("gateway", "handle", "server", 0, 40)
    b.span("gateway", "parse", "internal", 0, 5, root)
    c1 = b.span("gateway", "callPay", "client", 6, 20, root,
                retry_of="pay-group", attempt_no=1)
    c2 = b.span("gateway", "callPay", "client", 21, 36, root,
                retry_of="pay-group", attempt_no=2)
    b.await_rec(root, 5, 37, [c1, c2], "all", [c2], await_id="aw-retry")
    b.event("dispatch", 6, c1)
    b.event("timeout", 19, c1)
    b.event("cancel", 19, c1)
    s1 = b.span("pay", "pay", "server", 7, 20, root,
                links=[{"span_id": c1, "rel": "rpc"}])
    b.event("exec_start", 8, s1)
    b.event("timeout", 19, s1)
    b.event("dispatch", 21, c2)
    s2 = b.span("pay", "pay", "server", 22, 35, root,
                links=[{"span_id": c2, "rel": "rpc"}])
    b.event("queue_enter", 22, s2)
    b.event("exec_start", 23, s2)
    b.span("gateway", "ack", "internal", 37, 40, root)
    return b.records


# ---------------------------------------------------------------------- #
# 案例 5：异步尾任务 —— 消费者在根结束后运行（后台资源，非根瓶颈）
# 根窗口 18ms 全部 self 可解释；consumer 30~60ms 属 background（下界 30ms）
# ---------------------------------------------------------------------- #
def case_async_tail(tid: str = "T-async") -> list[dict]:
    b = Builder(tid)
    root = b.span("gateway", "handle", "server", 0, 18)
    b.span("gateway", "parse", "internal", 0, 6, root)
    prod = b.span("gateway", "emitAudit", "producer", 6, 10, root)
    b.event("enqueue", 9, prod)
    b.span("gateway", "answer", "internal", 10, 18, root)
    cons = b.span("auditWorker", "consumeAudit", "consumer", 30, 60, None,
                  links=[{"span_id": prod, "rel": "follows_from"}])
    b.event("queue_enter", 30, cons)
    b.event("exec_start", 35, cons)
    return b.records


# ---------------------------------------------------------------------- #
# 案例 6：共享下游结果 —— 两个调用方 link 到同一 server span
# 根窗口 30ms：self 0-8 -> await 8-22（14ms）-> self 22-30
# ---------------------------------------------------------------------- #
def case_shared(tid: str = "T-shared") -> list[dict]:
    b = Builder(tid)
    root = b.span("gateway", "handle", "server", 0, 30)
    b.span("gateway", "parse", "internal", 0, 8, root)
    shared = b.span("profile", "getProfile", "server", 10, 20, span_id="shared-srv")
    b.event("exec_start", 10, shared)
    c1 = b.span("gateway", "lookup1", "client", 8, 21, root,
                links=[{"span_id": shared, "rel": "rpc"}])
    c2 = b.span("gateway", "lookup2", "client", 9, 21, root,
                links=[{"span_id": shared, "rel": "rpc"}])
    b.await_rec(root, 8, 22, [c1, c2], "all", [c1, c2], await_id="aw-shared")
    b.span("gateway", "render", "internal", 22, 30, root)
    return b.records


# ---------------------------------------------------------------------- #
# 案例 7：缺失服务端 —— 只有 client，无 server；传输区间未知（D4，给界）
# 根窗口 24ms：self 0-8 -> await 8-18（10ms）-> self 18-24；
# 因无服务端证据，该 trace 标记不完整，await 内部无法进一步解释
# ---------------------------------------------------------------------- #
def case_missing(tid: str = "T-missing") -> list[dict]:
    b = Builder(tid)
    root = b.span("gateway", "handle", "server", 0, 24)
    b.span("gateway", "parse", "internal", 0, 8, root)
    c1 = b.span("gateway", "callGhost", "client", 8, 18, root)
    b.await_rec(root, 8, 18, [c1], "all", [c1], await_id="aw-missing")
    b.event("dispatch", 8, c1)          # 有派发，但没有任何服务端/队列/执行事件
    b.span("gateway", "render", "internal", 18, 24, root)
    return b.records


# ---------------------------------------------------------------------- #
# 案例 8：排队等待 —— 无显式 await 的调用，dispatch->queue_enter->exec_start
# 之间的空档只有事件证据，归 queue（D1）；exec 后到 client 结束仍有一段无证据空档归 gap（D8）
# 根窗口 26ms：self 0-6 -> queue 6-12（6ms）-> self(server 执行, 远端不计入根覆盖,
# 因此 12-20 在根视角为 gap) -> self 20-26
# ---------------------------------------------------------------------- #
def case_queue(tid: str = "T-queue") -> list[dict]:
    b = Builder(tid)
    root = b.span("gateway", "handle", "server", 0, 26)
    b.span("gateway", "parse", "internal", 0, 6, root)
    c1 = b.span("gateway", "callSlow", "client", 6, 20, root)   # 无 await：fire-and-forget 式
    b.event("dispatch", 6, c1)
    srv = b.span("slowdown", "process", "server", 12, 19, root,
                 links=[{"span_id": c1, "rel": "rpc"}])
    b.event("queue_enter", 8, srv)
    b.event("exec_start", 12, srv)
    b.span("gateway", "finalize", "internal", 20, 26, root)
    return b.records


# ---------------------------------------------------------------------- #
# 案例 9：纯未知空档 —— 根窗口中段没有任何 span/事件证据，必须归 gap 并给上下界
# ---------------------------------------------------------------------- #
def case_gap(tid: str = "T-gap") -> list[dict]:
    b = Builder(tid)
    root = b.span("gateway", "handle", "server", 0, 20)
    b.span("gateway", "before", "internal", 0, 6, root)
    b.span("gateway", "after", "internal", 15, 20, root)       # 6-15 完全无证据
    return b.records


# ---------------------------------------------------------------------- #
# 案例 10：嵌套等待 —— 外层 await 套内层 await，内层才是真实阻塞；
# 覆盖只按外层窗口计一次，内层只用于解释，不与外层相加进总响应时间
# 根窗口 28ms：self 0-4 -> 外层 await 4-24（20ms）-> self 24-28
# ---------------------------------------------------------------------- #
def case_nested(tid: str = "T-nested") -> list[dict]:
    b = Builder(tid)
    root = b.span("gateway", "handle", "server", 0, 28)
    b.span("gateway", "parse", "internal", 0, 4, root)
    mid = b.span("gateway", "orchestrate", "internal", 4, 24, root)
    leaf = b.span("gateway", "callDeep", "client", 10, 22, mid)
    # 外层 await 4-24，内层 await 10-22 完全嵌套其中
    b.await_rec(root, 4, 24, [mid], "all", [mid], await_id="aw-outer")
    b.await_rec(mid, 10, 22, [leaf], "all", [leaf], await_id="aw-inner")
    b.event("dispatch", 10, leaf)
    srv = b.span("deep", "work", "server", 11, 21, mid,
                 links=[{"span_id": leaf, "rel": "rpc"}])
    b.event("queue_enter", 11, srv)
    b.event("exec_start", 13, srv)
    b.span("gateway", "render", "internal", 24, 28, root)
    return b.records


# ---------------------------------------------------------------------- #
# 案例 11：结果就绪却迟迟恢复 —— 服务端 12ms 已完成，等待者 20ms 才恢复，
# 恢复滞后占 await 窗口比例超阈值，触发 D2（客户端已获得结果却迟迟恢复执行）
# 根窗口 24ms：self 0-4 -> await 4-20（16ms，其中结果 12ms 就绪）-> self 20-24
# ---------------------------------------------------------------------- #
def case_resume_lag(tid: str = "T-lag") -> list[dict]:
    b = Builder(tid)
    root = b.span("gateway", "handle", "server", 0, 24)
    b.span("gateway", "parse", "internal", 0, 4, root)
    c1 = b.span("gateway", "callFast", "client", 4, 12, root)
    b.await_rec(root, 4, 20, [c1], "all", [c1], await_id="aw-lag")
    b.event("dispatch", 4, c1)
    srv = b.span("fastsvc", "reply", "server", 5, 12, root,
                 links=[{"span_id": c1, "rel": "rpc"}])
    b.event("exec_start", 5, srv)       # 12ms 结果就绪，等待者 20ms 才恢复，滞后 8ms
    b.span("gateway", "render", "internal", 20, 24, root)
    return b.records


CASES = {
    "sync": case_sync,
    "parallel": case_parallel,
    "any": case_any,
    "retry": case_retry,
    "async_tail": case_async_tail,
    "shared": case_shared,
    "missing": case_missing,
    "queue": case_queue,
    "gap": case_gap,
    "nested": case_nested,
    "resume_lag": case_resume_lag,
}


def write_jsonl(records: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def build_all(out_dir: str) -> dict[str, str]:
    paths = {}
    for name, fn in CASES.items():
        p = os.path.join(out_dir, f"case_{name}.jsonl")
        write_jsonl(fn(), p)
        paths[name] = p
    return paths


# ---------------------------------------------------------------------- #
# 批量合成：n 条 trace，每条若干 span；时间戳拉开避免巨型重叠
# ---------------------------------------------------------------------- #
def build_bulk(path: str, n_traces: int = 9500, seed: int = 20261113) -> dict:
    rng = random.Random(seed)
    n_span = n_evt = n_aw = 0
    t_cursor = 0
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(n_traces):
            tid = f"BULK-{i:06d}"
            b = Builder(tid, t0=t_cursor)
            depth = rng.randint(1, 4)
            width = rng.randint(1, 3)
            root = b.span("gateway", "handle", "server", 0, rng.randint(20, 80))
            results = []
            for d in range(depth):
                for w in range(width):
                    st = rng.randint(2, 10)
                    et = st + rng.randint(5, 30)
                    cid = b.span("gateway", f"call{d}", "client", st, et, root)
                    srv = b.span(f"svc{d}", rng.choice(["query", "write", "enrich"]),
                                 "server", st + 1, et - 1, root,
                                 links=[{"span_id": cid, "rel": "rpc"}])
                    b.event("queue_enter", st + 1, srv)
                    b.event("exec_start", st + 2, srv)
                    results.append(cid)
                    n_span += 2
                    n_evt += 2
            if results:
                b.await_rec(root, 2, 18, results[:2], rng.choice(["all", "any"]),
                            results[:1])
                n_aw += 1
            n_span += 1
            for r in b.records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            t_cursor += 100 * MS
    return {"path": path, "spans": n_span, "events": n_evt, "awaits": n_aw,
            "traces": n_traces}


def main(argv=None):
    ap = argparse.ArgumentParser(description="合成追踪数据生成器（本地合成，不采集真实系统）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_cases = sub.add_parser("cases", help="生成 7 类手工案例")
    p_cases.add_argument("--out-dir", default="var/data")
    p_bulk = sub.add_parser("bulk", help="批量合成（默认十万 span 量级）")
    p_bulk.add_argument("--out", default="var/data/bulk.jsonl")
    p_bulk.add_argument("--traces", type=int, default=2000)
    args = ap.parse_args(argv)
    if args.cmd == "cases":
        paths = build_all(args.out_dir)
        for k, v in paths.items():
            print(f"case {k:10s} -> {v}")
    else:
        info = build_bulk(args.out, args.traces)
        print(json.dumps(info, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
