"""可串行化分析。

两部分相互独立，结论分别报告：

1. 视图串行等价（view serializability）
   把每个事务自身的操作按原顺序、连续地执行，枚举事务的全部排列，
   逐条核对：
     a) 每个 read 的来源 write 身份（write_id / INITIAL）与原历史完全相同，
        而不是只比较读到的字符串；
     b) 每个键的最终写入者（最后一次 write 的 write_id，未写入则 INITIAL）
        与原历史完全相同。
   输出合法排列总数，以及按事务编号码点序（元组字典序）最小的合法排列。
   一次排列失败绝不提前终止，必须枚举完全部 n! 个排列。

2. 冲突先后图（conflict precedence graph）
   不同事务对同一键的两个操作，只要至少一个是 write，就按它们在原历史中的
   先后顺序加有向边。每条边保留至少一组可核验的来源操作证据。
   图无环 ⇔ 冲突可串行化；检测到环时给出一条逐边可核验的环。

注意：冲突图有环只否定“冲突可串行化”，不直接否定“视图可串行化”
（盲写场景可能视图等价但冲突不可串行化）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations

from .model import INITIAL, History, Op


# --------------------------------------------------------------------------- #
# 视图串行等价：全枚举
# --------------------------------------------------------------------------- #

@dataclass
class ViewResult:
    legal_count: int
    smallest: list[int] | None  # 码点序最小的合法排列；不存在为 None
    total_permutations: int
    legal_permutations: list[list[int]]  # 排序后的全部合法排列（报告中仅小 n 全量保留）
    enumeration_order: str = "itertools.permutations over ascending tid list"

    def to_dict(self, include_all: bool) -> dict:
        d: dict = {
            "legal_count": self.legal_count,
            "smallest_legal": self.smallest,
            "total_permutations_enumerated": self.total_permutations,
        }
        if include_all:
            d["all_legal_permutations"] = self.legal_permutations
        return d


def _split_ops(history: History) -> tuple[
    dict[int, list[Op]], list[tuple[int, int, str, str, str]], list[tuple[int, str, str]]
]:
    """拆出每事务有序操作、全部 read 期望、全部 write。

    read 期望: (tid, seq, key, read_from 身份, 实际值)
    write:     (tid, seq, key, write_id, 值)
    """
    per_tid: dict[int, list[Op]] = {t: [] for t in history.tids}
    reads: list[tuple[int, int, str, str, str]] = []
    writes: list[tuple[int, str, str, str]] = []
    for op in history.ops:
        per_tid[op.tid].append(op)
        if op.kind == "read":
            reads.append((op.tid, op.seq, op.key, op.read_from, op.value))
        elif op.kind == "write":
            writes.append((op.tid, op.key, op.write_id, op.value))
    return per_tid, reads, writes  # type: ignore[return-value]


def check_permutation(history: History, order: tuple[int, ...]) -> tuple[bool, list[dict]]:
    """模拟单个事务排列。返回 (是否合法, 失败原因列表)。

    模拟采用单版本状态：每个键记录当前来源 write_id（或 INITIAL）与当前值。
    事务按 order 依次整段执行，事务内部保持原操作顺序。
    """
    per_tid, _, _ = _split_ops(history)
    current_source: dict[str, str] = {k: INITIAL for k in history.keys}
    current_value: dict[str, str] = dict(history.keys)
    failures: list[dict] = []

    for tid in order:
        for op in per_tid[tid]:
            if op.kind == "write":
                current_source[op.key] = op.write_id
                current_value[op.key] = op.value
            elif op.kind == "read":
                if current_source[op.key] != op.read_from:
                    failures.append({
                        "reason": "READ_SOURCE_MISMATCH",
                        "seq": op.seq,
                        "transaction": tid,
                        "key": op.key,
                        "expected_source": op.read_from,
                        "actual_source_in_permutation": current_source[op.key],
                    })
                if current_value[op.key] != op.value:
                    failures.append({
                        "reason": "READ_VALUE_MISMATCH",
                        "seq": op.seq,
                        "transaction": tid,
                        "key": op.key,
                        "expected_value": op.value,
                        "actual_value_in_permutation": current_value[op.key],
                    })
            # commit 在排列模拟中无额外效果：语义校验已保证每事务恰好一次且在末尾

    for key in sorted(history.keys):
        if current_source[key] != history.final_writer[key]:
            failures.append({
                "reason": "FINAL_WRITER_MISMATCH",
                "key": key,
                "expected_final_writer": history.final_writer[key],
                "actual_final_writer_in_permutation": current_source[key],
            })
    return (not failures), failures


def enumerate_view_equivalent(history: History) -> ViewResult:
    """枚举全部 n! 个排列，不允许因一次失败提前结束。"""
    tids = history.tids  # 已升序
    per_tid, _reads, _writes = _split_ops(history)
    initial_source = {k: INITIAL for k in history.keys}
    initial_value = dict(history.keys)
    final_writer = history.final_writer
    legal: list[list[int]] = []
    total = 0
    for order in permutations(tids):
        total += 1
        # 热路径内联模拟，避免每个排列重复拆分/构造对象
        source = dict(initial_source)
        value = dict(initial_value)
        valid = True
        for tid in order:
            for op in per_tid[tid]:
                if op.kind == "write":
                    source[op.key] = op.write_id
                    value[op.key] = op.value
                elif op.kind == "read":
                    if source[op.key] != op.read_from or value[op.key] != op.value:
                        valid = False
            if not valid:
                break
        if valid:
            for key in history.keys:
                if source[key] != final_writer[key]:
                    valid = False
                    break
        if valid:
            legal.append(list(order))
    legal.sort()  # 码点序（元组字典序），同时确定最小值
    smallest = legal[0] if legal else None
    return ViewResult(
        legal_count=len(legal),
        smallest=smallest,
        total_permutations=total,
        legal_permutations=legal,
    )


# --------------------------------------------------------------------------- #
# 冲突先后图
# --------------------------------------------------------------------------- #

@dataclass
class Edge:
    src: int
    dst: int
    evidence: list[dict] = field(default_factory=list)


@dataclass
class ConflictGraph:
    nodes: list[int]
    edges: list[Edge]
    has_cycle: bool
    cycle_vertices: list[int]
    cycle_edges: list[tuple[int, int]]

    def to_dict(self) -> dict:
        return {
            "nodes": self.nodes,
            "edges": [
                {
                    "from_tid": e.src,
                    "to_tid": e.dst,
                    "evidence": e.evidence,
                }
                for e in self.edges
            ],
            "has_cycle": self.has_cycle,
            "cycle_vertices": self.cycle_vertices,
            "cycle_edges": [{"from_tid": a, "to_tid": b} for a, b in self.cycle_edges],
        }


def build_conflict_graph(history: History) -> ConflictGraph:
    """按键收集操作，两两配对生成冲突边，证据保留到具体 seq。"""
    by_key: dict[str, list[Op]] = {k: [] for k in history.keys}
    for op in history.ops:
        if op.kind in ("read", "write"):
            by_key[op.key].append(op)

    # (src, dst) -> evidence list；同一对事务可能因多个键/多对操作重复冲突
    raw: dict[tuple[int, int], list[dict]] = {}
    for key in sorted(by_key):
        key_ops = by_key[key]  # 已按 seq 升序（history.ops 有序）
        for i in range(len(key_ops)):
            a = key_ops[i]
            for j in range(i + 1, len(key_ops)):
                b = key_ops[j]
                if a.tid == b.tid:
                    continue
                if a.kind != "write" and b.kind != "write":
                    continue  # read-read 不构成冲突
                pair = (a.tid, b.tid)
                raw.setdefault(pair, []).append({
                    "key": key,
                    "from_seq": a.seq,
                    "to_seq": b.seq,
                    "from_op": a.kind,
                    "to_op": b.kind,
                    "from_write_id": a.write_id,
                    "to_write_id": b.write_id,
                })

    edges: list[Edge] = []
    adjacency: dict[int, set[int]] = {t: set() for t in history.tids}
    for (src, dst) in sorted(raw):
        evidence = sorted(raw[(src, dst)],
                          key=lambda e: (e["from_seq"], e["to_seq"], e["key"]))
        edges.append(Edge(src=src, dst=dst, evidence=evidence))
        adjacency[src].add(dst)

    cycle_vertices, cycle_edges = _find_cycle(history.tids, adjacency)
    return ConflictGraph(
        nodes=sorted(history.tids),
        edges=edges,
        has_cycle=bool(cycle_edges),
        cycle_vertices=cycle_vertices,
        cycle_edges=cycle_edges,
    )


def _find_cycle(nodes: list[int], adjacency: dict[int, set[int]]) -> tuple[list[int], list[tuple[int, int]]]:
    """三色 DFS 找环。节点与邻接均按事务编号升序访问，保证输出可复现。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[int, int] = {t: WHITE for t in nodes}
    stack_path: list[int] = []
    on_path: set[int] = set()

    def dfs(u: int) -> list[int] | None:
        color[u] = GRAY
        stack_path.append(u)
        on_path.add(u)
        for v in sorted(adjacency[u]):
            if color[v] == GRAY:
                # 环：栈中从 v 第一次出现处到当前 u，再回到 v
                idx = stack_path.index(v)
                return stack_path[idx:] + [v]
            if color[v] == WHITE:
                found = dfs(v)
                if found is not None:
                    return found
        stack_path.pop()
        on_path.discard(u)
        color[u] = BLACK
        return None

    cycle: list[int] | None = None
    for t in sorted(nodes):
        if color[t] == WHITE:
            cycle = dfs(t)
            if cycle is not None:
                break
    if cycle is None:
        return [], []
    vertices = cycle[:-1]
    edge_list = list(zip(cycle[:-1], cycle[1:]))
    return vertices, edge_list


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #

@dataclass
class Analysis:
    view: ViewResult
    graph: ConflictGraph

    @property
    def view_serializable(self) -> bool:
        return self.view.legal_count > 0

    @property
    def conflict_serializable(self) -> bool:
        return not self.graph.has_cycle


def analyze(history: History) -> Analysis:
    return Analysis(
        view=enumerate_view_equivalent(history),
        graph=build_conflict_graph(history),
    )
