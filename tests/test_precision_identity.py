"""Identity and precision semantics."""

from __future__ import annotations

from decimal import Decimal

import pytest

from xbrlcheck.precision import (
    PrecisionError,
    intersect_intervals,
    parse_decimals,
    parse_numeric,
    precision_interval,
    scale_interval,
    sum_intervals,
)

from conftest import EXAMPLES, check, load, load_pair


def test_decimals_normalisation():
    assert parse_decimals("0") == "0"
    assert parse_decimals("+00") == "0"
    assert parse_decimals("-2") == "-2"
    assert parse_decimals("INF") == "INF"
    with pytest.raises(PrecisionError):
        parse_decimals("1.5")
    with pytest.raises(PrecisionError):
        parse_decimals("NaN")


def test_precision_interval_integer():
    assert precision_interval(Decimal("500000"), "0") == (Decimal("499999.5"), Decimal("500000.5"))


def test_precision_interval_negative_and_inf():
    assert precision_interval(Decimal("500"), "-2") == (Decimal("450"), Decimal("550"))
    assert precision_interval(Decimal("1.23"), "INF") == (Decimal("1.23"), Decimal("1.23"))


def test_intersection_and_scale():
    assert intersect_intervals([(Decimal(0), Decimal(2)), (Decimal(1), Decimal(3))]) == (
        Decimal(1),
        Decimal(2),
    )
    assert intersect_intervals([(Decimal(0), Decimal(1)), (Decimal(2), Decimal(3))]) is None
    assert scale_interval(Decimal(-1), (Decimal("499.5"), Decimal("500.5"))) == (
        Decimal("-500.5"),
        Decimal("-499.5"),
    )
    assert sum_intervals([(Decimal(1), Decimal(2)), (Decimal(3), Decimal(4))]) == (
        Decimal(4),
        Decimal(6),
    )


def test_numeric_rejects_non_decimal():
    with pytest.raises(PrecisionError):
        parse_numeric("INF")
    with pytest.raises(PrecisionError):
        parse_numeric("1e5")  # scientific notation is not xs:decimal


def test_context_identity_ignores_prefix_id_and_dimension_order():
    _, a = load(EXAMPLES / "order_a.xbrl")
    _, b = load(EXAMPLES / "order_b.xbrl")
    assert len(a.groups) == len(b.groups) == 4
    keys_a = set(a.groups)
    keys_b = set(b.groups)
    assert keys_a == keys_b
    # XML context ids differ
    assert a.facts[0].context_id != b.facts[0].context_id


def test_compare_order_variants_identical():
    from xbrlcheck.compare import compare_reports

    _, a, b = load_pair(EXAMPLES / "order_a.xbrl", EXAMPLES / "order_b.xbrl")
    rows = compare_reports(a, b)
    assert [r.status for r in rows] == ["identical"] * 4
    assert all(r.delta == 0 for r in rows)


def test_duplicate_statuses():
    from xbrlcheck.compare import compare_reports

    report, _ = check(EXAMPLES / "dup_identical.xbrl")
    assert {g.status for g in report.groups.values() if len(g.facts) > 1} == {"identical"}

    report, _ = check(EXAMPLES / "dup_compatible.xbrl")
    dup = [g for g in report.groups.values() if len(g.facts) > 1]
    assert dup[0].status == "compatible"
    assert dup[0].interval == (Decimal("399.7"), Decimal("400.5"))

    report, _ = check(EXAMPLES / "dup_conflict.xbrl")
    dup = [g for g in report.groups.values() if len(g.facts) > 1]
    assert dup[0].status == "conflict"
    assert dup[0].interval is None


def test_report_ab_comparison_is_identity_based():
    from xbrlcheck.compare import compare_reports

    _, a, b = load_pair(
        __import__("pathlib").Path("fixtures/report_a.xbrl"),
        __import__("pathlib").Path("fixtures/report_b.xbrl"),
    )
    rows = compare_reports(a, b)
    by_concept = {}
    for row in rows:
        by_concept.setdefault(row.key.concept.localname, []).append(row.status)
    assert by_concept["Cost"] == ["identical"]
    assert by_concept["Profit"].count("nil") == 1
    # Revenue in CNY is duplicate-compatible in B, identical in A
    assert "compatible" in by_concept["Revenue"]
