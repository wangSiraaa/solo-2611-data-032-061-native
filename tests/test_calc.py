"""Calculation relation checks and the unsupported-subset rejections."""

from __future__ import annotations

from decimal import Decimal

from xbrlcheck.calc import AMBIGUOUS, CONSISTENT, INCOMPLETE, INCONSISTENT, NIL_PARENT

from conftest import EXAMPLES, check, load


def _by_context(result):
    return {c.context_canonical.split(";")[2]: c for c in result.conclusions}


def test_fixture_reports_consistent():
    for name in ("report_a.xbrl", "report_b.xbrl"):
        report, result = check(__import__("pathlib").Path("fixtures") / name)
        statuses = sorted(c.status for c in result.conclusions)
        assert statuses == [CONSISTENT, NIL_PARENT], (name, statuses)


def test_hand_computed_precision_boundary():
    _, result = check(EXAMPLES / "precision_boundary.xbrl")
    by_period = {c.context_canonical: c for c in result.conclusions}
    q1 = next(c for k, c in by_period.items() if "2025-01-01" in k)
    q2 = next(c for k, c in by_period.items() if "2025-04-01" in k)

    assert q1.status == CONSISTENT
    assert q1.parent_interval == (Decimal("101.0"), Decimal("102.0"))
    assert q1.child_sum_interval == (Decimal("99.0"), Decimal("101.0"))
    # difference [101-101, 102-99] = [0, 3]; 0 is exactly the boundary
    assert q1.difference_interval == (Decimal("0"), Decimal("3"))

    assert q2.status == INCONSISTENT
    assert q2.parent_interval == (Decimal("102.0"), Decimal("103.0"))
    # difference [102-101, 103-99] = [1, 4]; 0 is outside
    assert q2.difference_interval == (Decimal("1"), Decimal("4"))


def test_nil_zero_absent_are_distinct():
    _, result = check(EXAMPLES / "nil_zero_absent.xbrl")
    by_period = {c.context_canonical: c for c in result.conclusions}
    q1 = next(c for k, c in by_period.items() if "2025-01-01" in k)
    q2 = next(c for k, c in by_period.items() if "2025-04-01" in k)
    q3 = next(c for k, c in by_period.items() if "2025-07-01" in k)
    assert q1.status == INCOMPLETE
    assert any(a.status == "nil" for a in q1.arcs)
    assert q2.status == CONSISTENT
    assert any(a.status == "present" and a.group.facts[0].value == 0 for a in q2.arcs)
    assert q3.status == INCOMPLETE
    # PartB exists only in other contexts, so it is not borrowed for q3.
    assert any(a.status == "other-context" for a in q3.arcs)
    assert "not treated as zero" in q3.reason


def test_currency_and_dimension_isolation():
    _, result = check(EXAMPLES / "isolation.xbrl")
    statuses = sorted(c.status for c in result.conclusions)
    assert statuses == [CONSISTENT, INCOMPLETE, INCOMPLETE]
    for c in result.conclusions:
        if c.status == INCOMPLETE:
            # no child was taken from the other unit/context
            assert all(a.status != "present" for a in c.arcs)
            assert "not treated as zero" in (c.reason or "")


def test_duplicate_conflict_makes_calc_ambiguous():
    _, result = check(EXAMPLES / "dup_conflict.xbrl")
    assert [c.status for c in result.conclusions] == [AMBIGUOUS]


def test_duplicate_compatible_uses_intersection():
    _, result = check(EXAMPLES / "dup_compatible.xbrl")
    conclusion = result.conclusions[0]
    assert conclusion.status == CONSISTENT
    part_a = next(a for a in conclusion.arcs if a.child.localname == "PartA")
    assert part_a.scaled_interval == (Decimal("399.7"), Decimal("400.5"))


def test_unsupported_constructs_are_rejected_not_guessed():
    report, result = check(EXAMPLES / "unsupported.xbrl")
    codes = {d.code for d in report.diagnostics if d.severity == "error"}
    assert {
        "unsupported-tuple",
        "unsupported-divide-unit",
        "unsupported-precision",
        "unknown-concept",
        "period-type-mismatch",
        "nil-with-value",
    } <= codes
    # typed dimension context is rejected, so its fact has no resolvable context
    assert "unresolved-context" in codes
    # only the valid facts remain
    assert all(f.concept.localname in {"Total", "PartA", "PartB"} for f in report.facts)
    # PartB's ratio unit and precision/nil problems mean it never enters a group
    assert all(f.unit_id == "CNY" for f in report.facts)


def test_every_conclusion_traces_facts_weight_and_interval():
    report, result = check(EXAMPLES / "dup_compatible.xbrl")
    conclusion = result.conclusions[0]
    assert conclusion.parent_group is not None
    assert conclusion.parent_interval is not None
    for arc in conclusion.arcs:
        assert arc.arc is not None
        assert arc.weight in (Decimal(1), Decimal(-1))
        assert arc.group is not None
        assert arc.group.facts
        assert arc.scaled_interval is not None


def test_multilevel_relations_reported_separately():
    _, result = check(EXAMPLES / "layers.xbrl")
    by_parent = {c.parent.localname: c for c in result.conclusions}
    assert by_parent["Grand"].status == CONSISTENT
    assert by_parent["Grand"].depth == 1
    assert by_parent["SubA"].status == INCONSISTENT
    assert by_parent["SubA"].depth == 2
    # an error at the leaf level must surface even though the aggregate is fine
    assert result.has_failures
