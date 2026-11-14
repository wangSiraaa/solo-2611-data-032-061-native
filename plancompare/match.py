"""Node correspondence between two plan trees.

Matching never uses array positions and never uses Node Type alone:
identity comes from relations, aliases, the set of participating tables
of each subtree and node-specific keys (sort keys, group keys), with
parent context only as a last-resort disambiguator.  Nodes that cannot
be matched uniquely are reported as ambiguous instead of being forced,
and different subtrees are never cross-compared.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import PlanDoc, PlanNode, fmt_set


@dataclass
class Correspondence:
    cid: str
    status: str  # matched | added | removed | ambiguous
    before: PlanNode | None = None
    after: PlanNode | None = None
    evidence: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    candidates_before: list[PlanNode] = field(default_factory=list)
    candidates_after: list[PlanNode] = field(default_factory=list)


def _scan_key(n: PlanNode):
    return ("scan", n.relation, n.alias)


def _join_key(n: PlanNode):
    return ("join", n.relation_set)


def _sort_key(n: PlanNode):
    return ("sort", tuple(n.get("Sort Key") or ()), n.relation_set)


def _aggregate_key(n: PlanNode):
    return ("aggregate", tuple(n.get("Group Key") or ()), n.relation_set)


def _hash_key(n: PlanNode):
    return ("hash", n.relation_set)


def _scan_evidence(key) -> str:
    return f"扫描节点按 关系={key[1]} 别名={key[2]} 对应，两侧均唯一"


def _join_evidence(key) -> str:
    return f"连接节点按参与表集合 {{{fmt_set(key[1])}}} 对应，两侧均唯一"


def _sort_evidence(key) -> str:
    return f"Sort 按排序键 {list(key[1])} + 输入表集合 {{{fmt_set(key[2])}}} 对应，两侧均唯一"


def _aggregate_evidence(key) -> str:
    return f"Aggregate 按分组键 {list(key[1])} + 输入表集合 {{{fmt_set(key[2])}}} 对应，两侧均唯一"


def _hash_evidence(key) -> str:
    return f"Hash 按输入表集合 {{{fmt_set(key[1])}}} 对应，两侧均唯一"


# Passes run from the most stable identity (base-table scans) to derived
# unary operators.  A node consumed by one pass is invisible to the next.
_PASSES = [
    ("scan", lambda n: n.is_scan, _scan_key, _scan_evidence),
    ("join", lambda n: n.is_join, _join_key, _join_evidence),
    ("sort", lambda n: n.node_type == "Sort", _sort_key, _sort_evidence),
    ("aggregate", lambda n: n.node_type == "Aggregate", _aggregate_key, _aggregate_evidence),
    ("hash", lambda n: n.node_type == "Hash", _hash_key, _hash_evidence),
]


def match_plans(before: PlanDoc, after: PlanDoc) -> list[Correspondence]:
    pairs: list[Correspondence] = []
    unmatched_b = {id(n) for n in before.nodes}
    unmatched_a = {id(n) for n in after.nodes}
    partner: dict[int, PlanNode] = {}

    def link(b: PlanNode, a: PlanNode, evidence: list[str]) -> None:
        pairs.append(Correspondence(cid="", status="matched", before=b, after=a,
                                    evidence=evidence))
        unmatched_b.discard(id(b))
        unmatched_a.discard(id(a))
        partner[id(b)] = a
        partner[id(a)] = b

    # -- signature passes ----------------------------------------------
    for pass_name, pred, keyfn, evfn in _PASSES:
        groups_b: dict = {}
        groups_a: dict = {}
        for n in before.nodes:
            if id(n) in unmatched_b and pred(n):
                groups_b.setdefault(keyfn(n), []).append(n)
        for n in after.nodes:
            if id(n) in unmatched_a and pred(n):
                groups_a.setdefault(keyfn(n), []).append(n)
        for key in sorted(groups_b.keys() & groups_a.keys(), key=repr):
            bs, as_ = groups_b[key], groups_a[key]
            if len(bs) == 1 and len(as_) == 1:
                link(bs[0], as_[0], [evfn(key)])
            else:
                pairs.append(Correspondence(
                    cid="",
                    status="ambiguous",
                    evidence=[
                        f"{pass_name} 签名在两侧不唯一（before {len(bs)} 个 / "
                        f"after {len(as_)} 个候选），不强行配对"
                    ],
                    candidates_before=bs,
                    candidates_after=as_,
                ))
                for n in bs:
                    unmatched_b.discard(id(n))
                for n in as_:
                    unmatched_a.discard(id(n))

    # -- contextual fallback -------------------------------------------
    # Same node type, parents already matched, and the role
    # (Parent Relationship) is unique on both sides.  This is the only
    # way unknown-boundary node types can ever be paired.
    for b in before.nodes:
        if id(b) not in unmatched_b or b.parent is None:
            continue
        parent_partner = partner.get(id(b.parent))
        if parent_partner is None:
            continue
        cands = [
            a for a in parent_partner.children
            if id(a) in unmatched_a
            and a.node_type == b.node_type
            and a.parent_relationship == b.parent_relationship
        ]
        siblings = [
            x for x in b.parent.children
            if id(x) in unmatched_b
            and x.node_type == b.node_type
            and x.parent_relationship == b.parent_relationship
        ]
        if len(cands) == 1 and len(siblings) == 1:
            link(b, cands[0], [
                f"父节点已对应（{b.parent.label()}），且两侧各仅一个 类型={b.node_type}、"
                f"Parent Relationship={b.parent_relationship} 的未配对子节点"
            ])

    # -- leftovers -------------------------------------------------------
    for n in before.nodes:
        if id(n) in unmatched_b:
            pairs.append(Correspondence(
                cid="", status="removed", before=n,
                evidence=[f"after 中不存在相同签名的节点（{_describe(n)}）"]))
    for n in after.nodes:
        if id(n) in unmatched_a:
            pairs.append(Correspondence(
                cid="", status="added", after=n,
                evidence=[f"before 中不存在相同签名的节点（{_describe(n)}）"]))

    # -- structural changes for matched pairs -----------------------------
    for c in pairs:
        if c.status == "matched":
            c.changes = _structural_changes(c.before, c.after)

    # -- stable ordering + ids --------------------------------------------
    order_b = {id(n): i for i, n in enumerate(before.nodes)}
    order_a = {id(n): i for i, n in enumerate(after.nodes)}
    rank = {"matched": 0, "removed": 1, "added": 2, "ambiguous": 3}

    def sort_key(c: Correspondence):
        ob = order_b.get(id(c.before), 10 ** 6) if c.before else 10 ** 6
        oa = order_a.get(id(c.after), 10 ** 6) if c.after else 10 ** 6
        return (rank[c.status], ob, oa)

    pairs.sort(key=sort_key)
    for i, c in enumerate(pairs, 1):
        c.cid = f"c{i:02d}"
    return pairs


def _describe(n: PlanNode) -> str:
    if n.is_scan:
        return f"scan 关系={n.relation} 别名={n.alias}"
    if n.is_join:
        return f"join 参与表={{{fmt_set(n.relation_set)}}}"
    if n.node_type == "Sort":
        return f"Sort 键={n.get('Sort Key')} 输入表={{{fmt_set(n.relation_set)}}}"
    if n.node_type == "Aggregate":
        return f"Aggregate 分组键={n.get('Group Key')} 输入表={{{fmt_set(n.relation_set)}}}"
    if n.node_type == "Hash":
        return f"Hash 输入表={{{fmt_set(n.relation_set)}}}"
    return f"节点类型 {n.node_type}（不在支持集内，仅按父上下文匹配）"


def _structural_changes(b: PlanNode, a: PlanNode) -> list[str]:
    changes = []
    if b.node_type != a.node_type:
        changes.append(f"节点类型变化：{b.node_type} → {a.node_type}")
    for field_name, label in (
        ("Join Type", "连接类型"),
        ("Strategy", "Aggregate 策略"),
        ("Inner Unique", "Inner Unique"),
    ):
        vb, va = b.get(field_name), a.get(field_name)
        if vb != va:
            changes.append(f"{label}变化：{vb} → {va}")
    bp = b.parent.node_type if b.parent else "<root>"
    ap = a.parent.node_type if a.parent else "<root>"
    if bp != ap or b.parent_relationship != a.parent_relationship:
        changes.append(
            f"树中位置变化：before 位于 {b.context_path()}，after 位于 {a.context_path()}"
        )
    return changes
