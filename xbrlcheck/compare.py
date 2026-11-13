"""Cross-report fact comparison on canonical identities.

Facts are matched by ``(concept, context identity, unit identity)``.  Because
context identity excludes the XML ``id`` and dimension order, reports with
different context numbering compare fact-by-fact.  Per matched pair we decide:

* ``identical``  -- same value and same decimals;
* ``compatible`` -- different values/precision but declared precision intervals
  overlap (rounding-compatible, not proof of equality);
* ``conflict``   -- declared precision intervals are disjoint;
* ``nil``        -- both sides nil;
* ``nil-vs-value`` ``value-vs-nil`` -- one side nil and the other numeric.

Duplicate facts (same key, seen more than once within one report) are never
collapsed: if the duplicates disagree the comparison is ``ambiguous`` and no
single value is invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .model import FactKey, GroupInfo, Report
from .precision import dec_str, intersect_intervals, interval_str

ONLY_A = "only_a"
ONLY_B = "only_b"

STATUS_LABELS = {
    "identical": "相同",
    "compatible": "精度相容",
    "conflict": "矛盾",
    "nil": "nil",
    "nil-vs-value": "nil/数值",
    "value-vs-nil": "数值/nil",
    "ambiguous": "重复矛盾",
    "only_a": "仅 A",
    "only_b": "仅 B",
}


@dataclass
class ComparisonRow:
    key: FactKey
    status: str
    left: GroupInfo | None
    right: GroupInfo | None
    interval: tuple[Decimal, Decimal] | None = None
    delta: Decimal | None = None
    detail: str | None = None

    @property
    def label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    def as_json(self) -> dict[str, Any]:
        return {
            "concept": self.key.concept.clark,
            "context": self.key.context.as_json(),
            "unit": self.key.unit.as_json(),
            "status": self.status,
            "label": self.label,
            "common_interval": (
                [dec_str(self.interval[0]), dec_str(self.interval[1])] if self.interval else None
            ),
            "delta": dec_str(self.delta),
            "detail": self.detail,
            "a": _group_json(self.left),
            "b": _group_json(self.right),
        }

    def summary(self) -> str:
        return "%s %s" % (self.label, self.key.concept.localname)


def _group_json(group: GroupInfo | None) -> dict[str, Any] | None:
    if group is None:
        return None
    return {
        "status": group.status,
        "interval": (
            [dec_str(group.interval[0]), dec_str(group.interval[1])] if group.interval else None
        ),
        "reason": group.reason,
        "facts": [f.as_json() for f in group.facts],
    }


def _delta(left: GroupInfo, right: GroupInfo) -> Decimal | None:
    if left.nil or right.nil or not left.facts or not right.facts:
        return None
    a, b = left.facts[0].value, right.facts[0].value
    if a is None or b is None:
        return None
    return a - b


def compare_reports(left: Report, right: Report) -> list[ComparisonRow]:
    rows: list[ComparisonRow] = []
    keys = sorted(
        set(left.groups) | set(right.groups),
        key=lambda k: (k.concept.namespace, k.concept.localname, k.context.canonical(), k.unit.canonical()),
    )
    for key in keys:
        a = left.groups.get(key)
        b = right.groups.get(key)
        rows.append(_compare_one(key, a, b))
    return rows


def _compare_one(key: FactKey, a: GroupInfo | None, b: GroupInfo | None) -> ComparisonRow:
    if a is None:
        if b is not None and b.status == "conflict":
            return ComparisonRow(
                key=key, status="ambiguous", left=None, right=b, detail="B has conflicting duplicates"
            )
        return ComparisonRow(key=key, status=ONLY_B, left=None, right=b, detail="only in B")
    if b is None:
        if a.status == "conflict":
            return ComparisonRow(
                key=key, status="ambiguous", left=a, right=None, detail="A has conflicting duplicates"
            )
        return ComparisonRow(key=key, status=ONLY_A, left=a, right=None, detail="only in A")

    if a.status == "conflict" or b.status == "conflict":
        reasons = []
        if a.status == "conflict":
            reasons.append("A: %s" % (a.reason or "duplicate conflict"))
        if b.status == "conflict":
            reasons.append("B: %s" % (b.reason or "duplicate conflict"))
        return ComparisonRow(
            key=key,
            status="ambiguous",
            left=a,
            right=b,
            detail="; ".join(reasons),
        )

    if a.nil and b.nil:
        return ComparisonRow(key=key, status="nil", left=a, right=b)
    if a.nil and not b.nil:
        return ComparisonRow(
            key=key, status="nil-vs-value", left=a, right=b, detail="A is nil, B has a value"
        )
    if b.nil and not a.nil:
        return ComparisonRow(
            key=key, status="value-vs-nil", left=a, right=b, detail="A has a value, B is nil"
        )

    if a.interval is None or b.interval is None:
        return ComparisonRow(
            key=key,
            status="ambiguous",
            left=a,
            right=b,
            detail="cannot compare without a precision interval",
        )

    common = intersect_intervals([a.interval, b.interval])
    delta = _delta(a, b)
    if common is None:
        return ComparisonRow(
            key=key,
            status="conflict",
            left=a,
            right=b,
            delta=delta,
            detail="precision intervals %s and %s are disjoint"
            % (interval_str(a.interval), interval_str(b.interval)),
        )
    same_value = (
        len(a.facts) == 1
        and len(b.facts) == 1
        and a.facts[0].value == b.facts[0].value
        and a.facts[0].decimals == b.facts[0].decimals
    )
    status = "identical" if same_value else "compatible"
    detail = None
    if status == "compatible":
        detail = "values differ but precision intervals overlap in %s" % interval_str(common)
    return ComparisonRow(
        key=key, status=status, left=a, right=b, interval=common, delta=delta, detail=detail
    )
