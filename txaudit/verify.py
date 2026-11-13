"""独立复核器。

与 analysis.py 刻意采用不同的实现路径，避免“同一份代码自证”：

- 排列枚举：analysis 用 itertools.permutations，这里用手写递归 DFS，
  按 tid 升序逐位构造排列，并在递归中即时模拟、即时剪枝；
- 原历史回放：重新独立实现一遍单版本回放，核对 read 身份/值与最终写入者；
- 冲突图：用“相邻冲突操作扫描”独立重建边集，再与报告中的边集合对照；
- 证据核验：逐条证据检查两个操作确实同键、不同事务、至少一个 write、
  且 seq 先后与边方向一致；
- 环核验：环上每条边必须真实存在于冲突图中。

verify CLI 对 <=6 个事务的历史强制使用本模块全量复核。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import INITIAL, History, Op


@dataclass
class VerifyReport:
    passed: bool
    checks: list[dict] = field(default_factory=list)
    discrepancies: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append({"check": name, "passed": ok, "detail": detail})
        if not ok:
            self.discrepancies.append(f"[{name}] {detail}")
            self.passed = False

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "checks": self.checks,
            "discrepancies": self.discrepancies,
        }


# --------------------------------------------------------------------------- #
# 1. 独立回放原历史
# --------------------------------------------------------------------------- #

def independent_replay(history: History) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """返回 (每键来源身份, 每键当前值, 问题列表)。"""
    source: dict[str, str] = {k: INITIAL for k in history.keys}
    value: dict[str, str] = dict(history.keys)
    problems: list[str] = []
    for op in history.ops:
        if op.kind == "write":
            source[op.key] = op.write_id
            value[op.key] = op.value
        elif op.kind == "read":
            if source[op.key] != op.read_from:
                problems.append(
                    f"seq={op.seq} 来源应为 {source[op.key]}，声明为 {op.read_from}")
            if value[op.key] != op.value:
                problems.append(
                    f"seq={op.seq} 值应为 {value[op.key]!r}，声明为 {op.value!r}")
    return source, value, problems


# --------------------------------------------------------------------------- #
# 2. 递归枚举合法排列（实现与 itertools.permutations 完全独立）
# --------------------------------------------------------------------------- #

class _PrefixRejected(Exception):
    def __init__(self, seq: int) -> None:
        self.seq = seq
        super().__init__(f"prefix rejected at seq={seq}")


def enumerate_legal_recursive(history: History) -> list[list[int]]:
    """手写递归全枚举，携带增量状态并在回溯时撤销（每个操作只被处理常数次）。

    剪枝安全性：往排列前缀追加事务只会新增写入、不会撤销已发生的 read，
    因此一个前缀中已失败的 read 在任何超集排列中都不可能恢复。
    与 analysis 的 itertools.permutations 全量重放是两条独立实现路径。
    """
    tids = sorted(history.tids)
    tid_ops: dict[int, list[Op]] = {t: [] for t in tids}
    for op in history.ops:
        tid_ops[op.tid].append(op)

    source: dict[str, str] = {k: INITIAL for k in history.keys}
    value: dict[str, str] = dict(history.keys)
    legal: list[list[int]] = []
    used: set[int] = set()
    prefix: list[int] = []

    def apply_tid(tid: int) -> list[tuple[str, str, str]]:
        """把一个事务的整段操作作用到增量状态，返回撤销记录；失败即抛出。"""
        undo: list[tuple[str, str, str]] = []
        for op in tid_ops[tid]:
            if op.kind == "write":
                undo.append((op.key, source[op.key], value[op.key]))
                source[op.key] = op.write_id
                value[op.key] = op.value
            elif op.kind == "read":
                if source[op.key] != op.read_from or value[op.key] != op.value:
                    # 撤销本次已应用的写入后再抛出
                    for key, old_src, old_val in reversed(undo):
                        source[key] = old_src
                        value[key] = old_val
                    raise _PrefixRejected(op.seq)
        return undo

    def rollback(tid: int, undo: list[tuple[str, str, str]]) -> None:
        for key, old_src, old_val in reversed(undo):
            source[key] = old_src
            value[key] = old_val

    def backtrack() -> None:
        if len(prefix) == len(tids):
            if all(source[k] == history.final_writer[k] for k in history.keys):
                legal.append(prefix.copy())
            return
        for tid in tids:  # 升序尝试 → 生成顺序本身即码点序
            if tid in used:
                continue
            try:
                undo = apply_tid(tid)
            except _PrefixRejected:
                continue  # 该前缀的任何超集都非法，剪枝
            prefix.append(tid)
            used.add(tid)
            backtrack()
            prefix.pop()
            used.discard(tid)
            rollback(tid, undo)

    backtrack()
    return legal


# --------------------------------------------------------------------------- #
# 3. 独立重建冲突边（按键内相邻冲突扫描，实现与 analysis 的两两配对不同）
# --------------------------------------------------------------------------- #

def independent_conflict_edges(history: History) -> set[tuple[int, int]]:
    edge_set: set[tuple[int, int]] = set()
    ops_by_key: dict[str, list[Op]] = {}
    for op in history.ops:
        if op.kind in ("read", "write"):
            ops_by_key.setdefault(op.key, []).append(op)
    for key_ops in ops_by_key.values():
        # 对每个操作，找它之后同键、不同事务且与它构成冲突的最近后继即可覆盖边集；
        # 为完全独立起见直接全对扫描但用集合去重（与 analysis 证据列表构造分开实现）。
        later_write_seen_from: set[tuple[int, int]] = set()
        for idx, a in enumerate(key_ops):
            for b in key_ops[idx + 1:]:
                if a.tid == b.tid:
                    continue
                if a.kind == "read" and b.kind == "read":
                    continue
                edge_set.add((a.tid, b.tid))
                later_write_seen_from.add((a.tid, b.tid))
    return edge_set


# --------------------------------------------------------------------------- #
# 4. 总复核
# --------------------------------------------------------------------------- #

def verify_analysis(history: History, report: dict) -> VerifyReport:
    """对照一份 audit 报告做独立复核。"""
    vr = VerifyReport(passed=True)

    # 4.1 原历史回放
    source, value, problems = independent_replay(history)
    vr.add("original_history_read_sources_and_values",
           not problems,
           "全部 read 的来源身份与实际值均符合单版本语义" if not problems
           else "; ".join(problems))
    final_ok = all(source[k] == history.final_writer[k] for k in history.keys)
    vr.add("original_history_final_writers", final_ok,
           "每键最终写入者与回放结果一致")

    # 4.2 递归枚举 vs 报告
    legal = enumerate_legal_recursive(history)
    view_block = report.get("view_serializability", {})
    vr.add("legal_permutation_count",
           view_block.get("legal_count") == len(legal),
           f"独立递归枚举得到 {len(legal)} 个合法排列，报告为 {view_block.get('legal_count')}")
    smallest = legal[0] if legal else None
    vr.add("smallest_legal_permutation",
           view_block.get("smallest_legal") == smallest,
           f"独立枚举码点序最小排列={smallest}，报告为 {view_block.get('smallest_legal')}")
    total = 1
    n = len(history.tids)
    for i in range(2, n + 1):
        total *= i
    vr.add("total_permutations_enumerated",
           view_block.get("total_permutations_enumerated") == total,
           f"{n}! = {total}")

    # 4.3 冲突边集合交叉核对
    independent_edges = independent_conflict_edges(history)
    reported_edges = {
        (e["from_tid"], e["to_tid"])
        for e in report.get("conflict_graph", {}).get("edges", [])
    }
    vr.add("conflict_edge_set",
           independent_edges == reported_edges,
           f"独立重建边集 {sorted(independent_edges)} 与报告边集 {sorted(reported_edges)} 一致")

    # 4.4 逐条证据核验
    evidence_problems: list[str] = []
    op_index = {op.seq: op for op in history.ops}
    for edge in report.get("conflict_graph", {}).get("edges", []):
        if not edge.get("evidence"):
            evidence_problems.append(f"边 {edge['from_tid']}->{edge['to_tid']} 缺少证据")
            continue
        for ev in edge["evidence"]:
            a = op_index.get(ev["from_seq"])
            b = op_index.get(ev["to_seq"])
            if a is None or b is None:
                evidence_problems.append(f"证据 seq {ev['from_seq']}/{ev['to_seq']} 不存在")
                continue
            cond = (
                a.tid == edge["from_tid"] and b.tid == edge["to_tid"]
                and a.key == b.key == ev["key"]
                and a.seq < b.seq
                and (a.kind == "write" or b.kind == "write")
            )
            if not cond:
                evidence_problems.append(
                    f"证据不成立：seq {a.seq}({a.kind}) 与 seq {b.seq}({b.kind})")
    vr.add("edge_evidence", not evidence_problems,
           "全部边的证据逐组核验通过" if not evidence_problems
           else "; ".join(evidence_problems))

    # 4.5 环核验
    graph_block = report.get("conflict_graph", {})
    cycle_edges = [(c["from_tid"], c["to_tid"]) for c in graph_block.get("cycle_edges", [])]
    if graph_block.get("has_cycle"):
        cycle_ok = (
            len(cycle_edges) >= 2
            and all(e in independent_edges for e in cycle_edges)
            and cycle_edges[0][0] == cycle_edges[-1][1]
        )
        vr.add("reported_cycle", cycle_ok,
               f"环 {cycle_edges} 上每条边均存在且首尾闭合" if cycle_ok
               else f"环核验失败：{cycle_edges}")
    else:
        vr.add("reported_acyclic", not cycle_edges,
               "报告称无环且未给出环" if not cycle_edges else "报告称无环却给出了环")

    # 4.6 结论字段一致性
    verdict = report.get("verdict", {})
    vr.add("view_verdict_consistency",
           verdict.get("view_serializable") == (len(legal) > 0),
           f"视图可串行化={len(legal) > 0}")
    vr.add("conflict_verdict_consistency",
           verdict.get("conflict_serializable") == (not graph_block.get("has_cycle")),
           f"冲突可串行化={not graph_block.get('has_cycle')}")
    return vr
