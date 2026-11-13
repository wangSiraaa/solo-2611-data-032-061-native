"""Calculation-relation checking.

The supported subset is one role, acyclic, weighted summation arcs with no
``priority``/``prohibited``.  Each parent-child group is interpreted on its own
("multi-level relations are explained separately"): for every parent fact we
align children by *the same context identity and the same unit*, weight them,
and compare precision intervals.

Guarantees:

* missing children make the relation ``incomplete``; zeros are never invented;
* nil children make it ``incomplete`` (nil is not zero);
* duplicate, mutually conflicting facts make it ``ambiguous``;
* different currencies / dimensions / periods are never mixed, because
  alignment is on canonical context and unit identity;
* every conclusion carries facts, weights and the allowed interval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .model import FactKey, GroupInfo, Report
from .precision import dec_str, interval_str, scale_interval, sum_intervals
from .qname import QName
from .taxonomy import CalcArc, Taxonomy

CONSISTENT = "consistent"
INCONSISTENT = "inconsistent"
INCOMPLETE = "incomplete"
AMBIGUOUS = "ambiguous"
NIL_PARENT = "nil-parent"

STATUS_LABELS = {
    CONSISTENT: "相符",
    INCONSISTENT: "不平",
    INCOMPLETE: "缺子项",
    AMBIGUOUS: "重复矛盾",
    NIL_PARENT: "父项为 nil",
}


@dataclass
class ArcTrace:
    child: QName
    weight: Decimal
    status: str  # present | missing | nil | conflict | other-unit | other-context
    group: GroupInfo | None = None
    scaled_interval: tuple[Decimal, Decimal] | None = None
    note: str | None = None
    arc: CalcArc | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "child": self.child.clark,
            "weight": dec_str(self.weight),
            "status": self.status,
            "note": self.note,
            "scaled_interval": (
                [dec_str(self.scaled_interval[0]), dec_str(self.scaled_interval[1])]
                if self.scaled_interval
                else None
            ),
            "facts": [f.as_json() for f in self.group.facts] if self.group else [],
        }

    def render(self, indent: str = "    ") -> str:
        parts = ["%s%s * %s [%s]" % (indent, dec_str(self.weight), self.child.localname, self.status)]
        if self.scaled_interval is not None:
            parts.append("scaled=%s" % interval_str(self.scaled_interval))
        if self.note:
            parts.append("(%s)" % self.note)
        for fact in self.group.facts if self.group else []:
            line = fact.source.line if fact.source.line is not None else "?"
            parts.append(
                "%s%s:%s value=%s decimals=%s"
                % (indent, fact.source.file.rsplit("/", 1)[-1], line, fact.raw_value or "nil", fact.decimals)
            )
        return " ".join(parts)


@dataclass
class Conclusion:
    parent: QName
    context_id: str
    unit_id: str
    context_canonical: str
    unit_canonical: str
    status: str
    parent_group: GroupInfo | None
    arcs: list[ArcTrace] = field(default_factory=list)
    parent_interval: tuple[Decimal, Decimal] | None = None
    child_sum_interval: tuple[Decimal, Decimal] | None = None
    difference_interval: tuple[Decimal, Decimal] | None = None
    reason: str | None = None
    depth: int = 0

    @property
    def label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    def as_json(self) -> dict[str, Any]:
        return {
            "parent": self.parent.clark,
            "context_id": self.context_id,
            "unit_id": self.unit_id,
            "context": self.context_canonical,
            "unit": self.unit_canonical,
            "status": self.status,
            "label": self.label,
            "reason": self.reason,
            "depth": self.depth,
            "parent_interval": (
                [dec_str(self.parent_interval[0]), dec_str(self.parent_interval[1])]
                if self.parent_interval
                else None
            ),
            "child_sum_interval": (
                [dec_str(self.child_sum_interval[0]), dec_str(self.child_sum_interval[1])]
                if self.child_sum_interval
                else None
            ),
            "difference_interval": (
                [dec_str(self.difference_interval[0]), dec_str(self.difference_interval[1])]
                if self.difference_interval
                else None
            ),
            "parent_facts": [f.as_json() for f in self.parent_group.facts] if self.parent_group else [],
            "arcs": [a.as_json() for a in self.arcs],
        }

    def render(self) -> str:
        lines = [
            "%s %s  [%s | %s]  %s"
            % (
                self.label,
                self.parent.localname,
                self.context_id,
                self.unit_id,
                "depth=%d" % self.depth,
            )
        ]
        if self.parent_group:
            for fact in self.parent_group.facts:
                line = fact.source.line if fact.source.line is not None else "?"
                lines.append(
                    "  = parent %s:%s value=%s decimals=%s interval=%s"
                    % (
                        fact.source.file.rsplit("/", 1)[-1],
                        line,
                        fact.raw_value or "nil",
                        fact.decimals,
                        interval_str(fact.interval),
                    )
                )
        for arc in self.arcs:
            lines.append(arc.render())
        if self.parent_interval is not None and self.child_sum_interval is not None:
            lines.append("  parent interval      = %s" % interval_str(self.parent_interval))
            lines.append("  weighted child sum   = %s" % interval_str(self.child_sum_interval))
            lines.append("  parent - child sum   = %s  (includes 0: %s)"
                         % (interval_str(self.difference_interval),
                            "yes" if _contains_zero(self.difference_interval) else "no"))
        if self.reason:
            lines.append("  reason: %s" % self.reason)
        return "\n".join(lines)


def _contains_zero(interval: tuple[Decimal, Decimal] | None) -> bool:
    return interval is not None and interval[0] <= 0 <= interval[1]


@dataclass
class CalcResult:
    role: str | None
    conclusions: list[Conclusion] = field(default_factory=list)
    diagnostics: list[Any] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.conclusions:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    @property
    def ok(self) -> bool:
        return not any(c.status in {INCONSISTENT, INCOMPLETE, AMBIGUOUS} for c in self.conclusions)

    @property
    def has_failures(self) -> bool:
        return any(c.status == INCONSISTENT for c in self.conclusions)

    def as_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "ok": self.ok,
            "counts": self.counts(),
            "conclusions": [c.as_json() for c in self.conclusions],
        }


def _find_group(
    report: Report,
    concept: QName,
    context_key,
    unit_key,
) -> GroupInfo | None:
    for key, group in report.groups.items():
        if (
            key.concept == concept
            and key.context.key() == context_key
            and key.unit.key() == unit_key
        ):
            return group
    return None


def _level_map(by_parent: dict[QName, list[CalcArc]]) -> dict[QName, int]:
    """Depth of each parent from the roots of the calculation tree (roots = 1).

    Cycles are already rejected by the taxonomy loader, so a simple memoised
    walk over parents is safe.
    """
    all_parents = set(by_parent)
    children = {arc.child for arcs in by_parent.values() for arc in arcs}
    roots = [p for p in all_parents if p not in children]
    levels: dict[QName, int] = {}

    def walk(node: QName, level: int) -> None:
        if levels.get(node, 0) >= level:
            return
        levels[node] = level
        for arc in by_parent.get(node, []):
            walk(arc.child, level + 1)

    for root in roots:
        walk(root, 1)
    for parent in all_parents:
        levels.setdefault(parent, 1)
    return levels


def check_calculations(report: Report, taxonomy: Taxonomy) -> CalcResult:
    relations = taxonomy.calc_relations
    if not relations:
        return CalcResult(role=None)
    role = relations[0].role
    by_parent: dict[QName, list[CalcArc]] = {}
    for relation in relations:
        for arc in relation.arcs:
            by_parent.setdefault(arc.parent, []).append(arc)
    for arcs in by_parent.values():
        arcs.sort(key=lambda a: (a.order, a.child.namespace, a.child.localname))

    result = CalcResult(role=role)
    levels = _level_map(by_parent)
    for parent, arcs in by_parent.items():
        depth = levels.get(parent, 1)
        # Enumerate parent groups (context + unit) from the parent concept.
        # Unit is part of the grouping so that a CNY parent and a USD parent
        # in the same context are checked separately, never mixed.
        parent_groups: dict[Any, FactKey] = {}
        for key in report.groups:
            if key.concept == parent:
                parent_groups[(key.context.key(), key.unit.key())] = key
        if not parent_groups:
            result.conclusions.append(
                Conclusion(
                    parent=parent,
                    context_id="-",
                    unit_id="-",
                    context_canonical="-",
                    unit_canonical="-",
                    status=INCOMPLETE,
                    parent_group=None,
                    arcs=[
                        ArcTrace(child=a.child, weight=a.weight, status="missing", arc=a)
                        for a in arcs
                    ],
                    reason="no fact for parent %s in report" % parent.clark,
                    depth=depth,
                )
            )
            continue
        for parent_key in parent_groups.values():
            conclusion = _check_one(report, parent_key, arcs, depth)
            result.conclusions.append(conclusion)
    return result


def _check_one(
    report: Report,
    parent_key: FactKey,
    arcs: list[CalcArc],
    depth: int,
) -> Conclusion:
    parent_group = report.groups.get(parent_key)
    base = dict(
        parent=parent_key.concept,
        context_id=parent_group.facts[0].context_id if parent_group and parent_group.facts else "-",
        unit_id=parent_group.facts[0].unit_id if parent_group and parent_group.facts else "-",
        context_canonical=parent_key.context.canonical(),
        unit_canonical=parent_key.unit.canonical(),
        parent_group=parent_group,
        depth=depth,
    )
    if parent_group is None:
        return Conclusion(status=INCOMPLETE, reason="parent fact is absent", **base)  # type: ignore[arg-type]

    if parent_group.conflict:
        traces = [
            ArcTrace(child=a.child, weight=a.weight, status="unused", arc=a)
            for a in arcs
        ]
        return Conclusion(
            status=AMBIGUOUS,
            parent_interval=parent_group.interval,
            arcs=traces,
            reason="parent has duplicate facts with disjoint precision intervals",
            **base,  # type: ignore[arg-type]
        )

    if parent_group.nil:
        traces = [
            ArcTrace(child=a.child, weight=a.weight, status="unused", arc=a)
            for a in arcs
        ]
        return Conclusion(
            status=NIL_PARENT,
            arcs=traces,
            reason="parent is nil; nothing to check",
            **base,  # type: ignore[arg-type]
        )

    traces: list[ArcTrace] = []
    scaled: list[tuple[Decimal, Decimal]] = []
    missing: list[str] = []
    ambiguous: list[str] = []
    nil_children: list[str] = []
    for arc in arcs:
        group = _find_group(report, arc.child, parent_key.context.key(), parent_key.unit.key())
        trace = ArcTrace(child=arc.child, weight=arc.weight, status="present", group=group, arc=arc)
        if group is None:
            # Distinguish "exists elsewhere" from truly absent.  A child in
            # another unit/context is still missing here; it is never borrowed.
            elsewhere = _find_elsewhere(report, arc.child, parent_key)
            if elsewhere is None:
                trace.status = "missing"
                missing.append(arc.child.localname)
            else:
                trace.status, note = elsewhere
                trace.note = note
                missing.append(arc.child.localname)
        elif group.conflict:
            trace.status = "conflict"
            trace.note = group.reason
            ambiguous.append(arc.child.localname)
        elif group.nil:
            trace.status = "nil"
            nil_children.append(arc.child.localname)
        else:
            trace.status = "present"
            trace.scaled_interval = scale_interval(arc.weight, group.interval)  # type: ignore[arg-type]
            scaled.append(trace.scaled_interval)
        traces.append(trace)

    parent_interval = parent_group.interval
    if ambiguous:
        return Conclusion(
            status=AMBIGUOUS,
            parent_interval=parent_interval,
            arcs=traces,
            reason="duplicate conflicting facts for child " + ", ".join(ambiguous),
            **base,  # type: ignore[arg-type]
        )
    if missing or nil_children:
        bits = []
        if missing:
            bits.append("missing children: " + ", ".join(missing) + " (not treated as zero)")
        if nil_children:
            bits.append("nil children: " + ", ".join(nil_children) + " (nil is not zero)")
        return Conclusion(
            status=INCOMPLETE,
            parent_interval=parent_interval,
            arcs=traces,
            reason="; ".join(bits),
            **base,  # type: ignore[arg-type]
        )
    if parent_interval is None:
        return Conclusion(
            status=AMBIGUOUS,
            arcs=traces,
            reason="parent has no precision interval",
            **base,  # type: ignore[arg-type]
        )

    child_sum = sum_intervals(scaled)
    assert child_sum is not None
    difference = (parent_interval[0] - child_sum[1], parent_interval[1] - child_sum[0])
    status = CONSISTENT if _contains_zero(difference) else INCONSISTENT
    reason = None
    if status == INCONSISTENT:
        reason = "parent interval and weighted child sum do not overlap"
    return Conclusion(
        status=status,
        parent_interval=parent_interval,
        child_sum_interval=child_sum,
        difference_interval=difference,
        arcs=traces,
        reason=reason,
        **base,  # type: ignore[arg-type]
    )


def _find_elsewhere(
    report: Report, child: QName, parent_key: FactKey
) -> tuple[str, str] | None:
    """Locate a child fact under a neighbouring unit/context.

    Returns ``(status, note)`` where status names the mismatch so the reader can
    see *why* the fact was not aligned: ``other-unit`` or ``other-context``.
    """
    for key, group in report.groups.items():
        if key.concept != child:
            continue
        if key.context.key() == parent_key.context.key():
            return ("other-unit", "present in another unit %s" % key.unit.canonical())
        if key.unit.key() == parent_key.unit.key():
            return ("other-context", "present in another context")
    return None
