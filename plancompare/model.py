"""Plan-tree model with JSON-pointer provenance.

Only a fixed set of PostgreSQL 16 node types is interpreted:
Seq Scan, Index Scan, Sort, Aggregate, Hash and the common join nodes
(Nested Loop, Hash Join, Merge Join).  Any other node type is kept as an
*unknown boundary*: its subtree is still traversed and matched, but the
node itself receives no type-specific interpretation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCAN_TYPES = frozenset({"Seq Scan", "Index Scan"})
JOIN_TYPES = frozenset({"Nested Loop", "Hash Join", "Merge Join"})
UNARY_TYPES = frozenset({"Sort", "Aggregate", "Hash"})
SUPPORTED_NODE_TYPES = SCAN_TYPES | JOIN_TYPES | UNARY_TYPES


def escape_pointer(token: str) -> str:
    """RFC 6901 escaping for one JSON-pointer token."""
    return token.replace("~", "~0").replace("/", "~1")


def fmt_set(names) -> str:
    return ",".join(sorted(names))


@dataclass
class PlanNode:
    pointer: str
    node_type: str
    raw: dict[str, Any]
    parent: "PlanNode | None" = None
    children: list["PlanNode"] = field(default_factory=list)
    _relation_set: frozenset[str] | None = field(default=None, repr=False)

    # -- identity ------------------------------------------------------
    @property
    def supported(self) -> bool:
        return self.node_type in SUPPORTED_NODE_TYPES

    @property
    def is_scan(self) -> bool:
        return self.node_type in SCAN_TYPES

    @property
    def is_join(self) -> bool:
        return self.node_type in JOIN_TYPES

    @property
    def relation(self) -> str | None:
        return self.raw.get("Relation Name")

    @property
    def alias(self) -> str | None:
        return self.raw.get("Alias")

    @property
    def source_name(self) -> str | None:
        """Identity name of a scan: alias when present, else relation name."""
        return self.alias or self.relation

    @property
    def parent_relationship(self) -> str | None:
        return self.raw.get("Parent Relationship")

    @property
    def relation_set(self) -> frozenset[str]:
        """Names (alias preferred) of the base tables produced in this subtree."""
        if self._relation_set is None:
            if self.is_scan:
                self._relation_set = (
                    frozenset({self.source_name}) if self.source_name else frozenset()
                )
            else:
                names: set[str] = set()
                for child in self.children:
                    names |= child.relation_set
                self._relation_set = frozenset(names)
        return self._relation_set

    # -- accessors -----------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def field_pointer(self, key: str) -> str:
        return f"{self.pointer}/{escape_pointer(key)}"

    def context_path(self) -> str:
        """Node-type path from the plan root, e.g. 'Aggregate>Sort'."""
        parts = []
        node: PlanNode | None = self
        while node is not None:
            parts.append(node.node_type)
            node = node.parent
        return ">".join(reversed(parts))

    def label(self) -> str:
        if self.is_scan:
            if self.alias and self.alias != self.relation:
                return f"{self.node_type} {self.relation} {self.alias}"
            return f"{self.node_type} {self.relation or '?'}"
        if self.is_join:
            return f"{self.node_type} {{{fmt_set(self.relation_set)}}}"
        if self.node_type == "Sort":
            return f"Sort key({', '.join(self.get('Sort Key') or [])})"
        if self.node_type == "Aggregate":
            keys = ", ".join(self.get("Group Key") or [])
            return f"Aggregate[{self.get('Strategy', '?')}] group({keys})"
        if self.node_type == "Hash":
            return f"Hash {{{fmt_set(self.relation_set)}}}"
        return f"{self.node_type} (unsupported)"


@dataclass
class PlanDoc:
    path: str
    raw: Any
    root: PlanNode
    nodes: list[PlanNode]  # pre-order
    execution_time: float | None
    planning_time: float | None
    top_pointer: str = "/0"

    @property
    def execution_pointer(self) -> str:
        return f"{self.top_pointer}/Execution Time"

    @property
    def planning_pointer(self) -> str:
        return f"{self.top_pointer}/Planning Time"


def load_plan(path: str | Path) -> PlanDoc:
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, list)
        or not raw
        or not isinstance(raw[0], dict)
        or not isinstance(raw[0].get("Plan"), dict)
    ):
        raise ValueError(f"{path}: 不是 PostgreSQL EXPLAIN (FORMAT JSON) 输出")
    top = raw[0]
    nodes: list[PlanNode] = []

    def build(obj: dict, pointer: str, parent: PlanNode | None) -> PlanNode:
        node = PlanNode(
            pointer=pointer,
            node_type=obj.get("Node Type", "<missing Node Type>"),
            raw=obj,
            parent=parent,
        )
        nodes.append(node)
        plans = obj.get("Plans") or []
        if not isinstance(plans, list):
            raise ValueError(f"{path}: {pointer}/Plans 不是数组")
        for i, child in enumerate(plans):
            node.children.append(build(child, f"{pointer}/Plans/{i}", node))
        return node

    root = build(top["Plan"], "/0/Plan", None)
    return PlanDoc(
        path=str(path),
        raw=raw,
        root=root,
        nodes=nodes,
        execution_time=top.get("Execution Time"),
        planning_time=top.get("Planning Time"),
    )
