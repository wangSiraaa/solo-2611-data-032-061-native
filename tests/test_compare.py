"""Compare: content-based candidates, competition, no local-id matching."""

from __future__ import annotations

import pytest

from topic_migrate.compare import build_decision_template, compare_models
from topic_migrate.data import load_period
from topic_migrate.errors import CompareError, NamesError
from topic_migrate.model import TrainConfig, permute_model, train_model
from topic_migrate.names import load_names

from topic_migrate.storage import sha256_file

from conftest import EXAMPLES, FIXTURES, SUPPLEMENTS, read_json


def train(path, period_key, period):
    plan_path = FIXTURES / "plan.json"
    plan = read_json(plan_path)
    plan.setdefault("seed", 122)
    return train_model(
        loaded=load_period(path),
        period=period,
        plan=plan,
        period_key=period_key,
        plan_sha256=sha256_file(plan_path),
        config=TrainConfig.from_plan(plan, period_key),
    )


def train_with_plan(path, plan_path, period_key, period):
    plan = read_json(plan_path)
    return train_model(
        loaded=load_period(path),
        period=period,
        plan=plan,
        period_key=period_key,
        plan_sha256=sha256_file(plan_path),
        config=TrainConfig.from_plan(plan, period_key),
    )


@pytest.fixture(scope="module")
def main_models():
    return (
        train(FIXTURES / "period_a.jsonl", "period_a", "a"),
        train(FIXTURES / "period_b.jsonl", "period_b", "b"),
    )


def test_compare_fixture_candidates_are_content_based(main_models):
    old, new = main_models
    names = load_names(EXAMPLES / "period_a.names.json")
    report = compare_models(old, new, names)
    assert report["comparable_space"]["shared_vocabulary_terms"] > 0
    assert report["comparable_space"]["shared_tickets"] == 9

    by_local = {p["new_local_id"]: p for p in report["proposals"]}
    # Login topic continues login; logistics continues logistics.
    assert by_local[0]["suggested_topic_id"] == "acct-login"
    assert by_local[1]["suggested_topic_id"] == "logistics-delivery"
    assert by_local[2]["suggested_topic_id"] == "billing-invoice"
    # The small logistics-exception topic looks like a split, not a plain
    # fourth continuation.
    assert "possible_split_from_old_topic" in by_local[3]["flags"]
    assert by_local[3]["suggested_topic_id"] == "logistics-delivery"

    # No confirmed relationships at compare time.
    confirmed = report["confirmed_changes"]
    assert confirmed["continued"] == []
    assert confirmed["added"] == []
    assert confirmed["net_change"] is None

    # Every candidate list is ordered by composite score.
    for proposal in report["proposals"]:
        scores = [c["scores"]["composite"] for c in proposal["candidates"]]
        assert scores == sorted(scores, reverse=True)


def test_compare_ignores_local_topic_numbers(main_models):
    old, new = main_models
    names = load_names(EXAMPLES / "period_a.names.json")
    base = compare_models(old, new, names)

    permuted = permute_model(new, [2, 0, 3, 1])
    swapped = compare_models(old, permuted, names)

    mapping = {new: old for old, new in enumerate([2, 0, 3, 1])}
    base_by_fp = {p["new_fingerprint"]: p for p in base["proposals"]}
    for proposal in swapped["proposals"]:
        counterpart = base_by_fp[proposal["new_fingerprint"]]
        assert proposal["suggested_topic_id"] == counterpart["suggested_topic_id"]
        assert proposal["flags"] == counterpart["flags"]
        assert proposal["status"] == counterpart["status"]
        assert proposal["new_local_id"] == mapping[counterpart["new_local_id"]]


def test_number_swap_supplement_matches_by_content():
    base = SUPPLEMENTS / "topic_number_swap"
    old = train_with_plan(base / "period_a.jsonl", base / "plan.json", "period_a", "a")
    new = train_with_plan(base / "period_b.jsonl", base / "plan.json", "period_b", "b")
    names = load_names(base / "period_a.names.json")
    report = compare_models(old, new, names)
    by_terms = {
        p["new_top_terms"][0]: p["suggested_topic_id"] for p in report["proposals"]
    }
    assert by_terms["丢失"] == "logistics-delivery"
    assert by_terms["失败"] == "acct-login"
    assert by_terms["发票"] == "billing-invoice"


def test_repeated_terms_different_context_supplement():
    base = SUPPLEMENTS / "repeated_terms"
    old = train_with_plan(base / "period_a.jsonl", base / "plan.json", "period_a", "a")
    new = train_with_plan(base / "period_b.jsonl", base / "plan.json", "period_b", "b")
    names = load_names(base / "period_a.names.json")
    report = compare_models(old, new, names)
    proposals = {p["suggested_topic_id"]: p for p in report["proposals"]}
    # The shared word 退款 must not collapse the two contexts.
    assert proposals["billing-refund"]["new_top_terms"][0] in {"发票", "退款"}
    assert proposals["delivery-refund"]["new_top_terms"][0] in {"包裹", "配送"}
    assert proposals["billing-refund"]["new_local_id"] != proposals["delivery-refund"]["new_local_id"]
    # A genuinely new policy topic has no continuation.
    new_topics = [p for p in report["proposals"] if p["suggested_topic_id"] is None]
    assert len(new_topics) == 1
    assert new_topics[0]["status"] == "new_topic_candidate"


def test_compare_rejects_stale_names(main_models):
    old, new = main_models
    names = load_names(EXAMPLES / "period_a.names.json")
    broken = names
    broken.model_summary_sha256 = "f" * 64
    with pytest.raises(NamesError, match="summary"):
        compare_models(old, new, broken)


def test_compare_rejects_same_period(main_models):
    old, _ = main_models
    names = load_names(EXAMPLES / "period_a.names.json")
    with pytest.raises(CompareError, match="same period"):
        compare_models(old, old, names)


def test_decision_template_is_editable_and_unset(main_models):
    old, new = main_models
    names = load_names(EXAMPLES / "period_a.names.json")
    report = compare_models(old, new, names)
    template = build_decision_template(report, old, "test")
    assert template["decisions"]
    for decision in template["decisions"]:
        assert decision["action"] == ""
        assert decision["continue_topic_id"] == ""
        assert decision["new_name"] == ""
    assert template["binding"]["compare_sha256"] == report["compare_sha256"]


def test_merge_supplement_is_flagged_not_decided():
    base = SUPPLEMENTS / "topic_merge"
    old = train_with_plan(base / "period_a.jsonl", base / "plan.json", "period_a", "a")
    new = train_with_plan(base / "period_b.jsonl", base / "plan.json", "period_b", "b")
    names = load_names(base / "period_a.names.json")
    report = compare_models(old, new, names)
    assert len(report["proposals"]) == 1
    proposal = report["proposals"][0]
    assert proposal["status"] == "ambiguous_candidates"
    assert "possible_merge_of_old_topics" in proposal["flags"]
    # Two old topics compete; no automatic inheritance and no confirmed change.
    assert [c["old_topic"]["topic_id"] for c in proposal["candidates"][:2]] != []
    assert report["competition"][0]["decision"] == "pending_human"
    assert report["confirmed_changes"]["net_change"] is None


def test_insufficient_evidence_supplement_keeps_weak_unassigned():
    base = SUPPLEMENTS / "insufficient_evidence"
    old = train_with_plan(base / "period_a.jsonl", base / "plan.json", "period_a", "a")
    new = train_with_plan(base / "period_b.jsonl", base / "plan.json", "period_b", "b")
    by_id = {a["ticket_id"]: a for a in new["assignments"]}
    assert by_id["E-WEAK1"]["status"] == "unassigned"
    assert by_id["E-WEAK2"]["status"] == "unassigned"
    assert {t["ticket_id"] for t in new["unmodelable"]} == {"E-EMPTY", "E-SPACE"}
    names = load_names(base / "period_a.names.json")
    report = compare_models(old, new, names)
    # Two clean continuations, no competition, no confirmed change.
    assert {p["suggested_topic_id"] for p in report["proposals"]} == {
        "acct-login", "logistics-delivery"
    }
    assert report["competition"] == []
    assert report["confirmed_changes"]["net_change"] is None
