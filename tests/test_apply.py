"""apply-names: explicit decisions, bindings, permutation stability, exports."""

from __future__ import annotations

import copy
import json

import pytest

from topic_migrate.apply import apply_names, write_applied_outputs
from topic_migrate.compare import build_decision_template, compare_models
from topic_migrate.data import load_period
from topic_migrate.errors import (
    BindingError,
    DecisionError,
    SourceChangedError,
)
from topic_migrate.model import TrainConfig, permute_model, train_model
from topic_migrate.names import load_names

from topic_migrate.storage import sha256_file

from conftest import EXAMPLES, FIXTURES, read_json, write_jsonl


@pytest.fixture(scope="module")
def setup():
    plan = read_json(FIXTURES / "plan.json")
    old = train_model(
        loaded=load_period(FIXTURES / "period_a.jsonl"),
        period="a",
        plan=plan,
        period_key="period_a",
        plan_sha256=sha256_file(FIXTURES / "plan.json"),
        config=TrainConfig.from_plan(plan, "period_a"),
    )
    new = train_model(
        loaded=load_period(FIXTURES / "period_b.jsonl"),
        period="b",
        plan=plan,
        period_key="period_b",
        plan_sha256=sha256_file(FIXTURES / "plan.json"),
        config=TrainConfig.from_plan(plan, "period_b"),
    )
    names = load_names(EXAMPLES / "period_a.names.json")
    report = compare_models(old, new, names)
    template = build_decision_template(report, old, "test")
    return old, new, names, report, template


def resolve_all(template, *, split_new="logistics-exceptions"):
    decision = copy.deepcopy(template)
    continued = {0: "acct-login", 1: "logistics-delivery", 2: "billing-invoice"}
    for item in decision["decisions"]:
        local = item["new_local_id"]
        if local in continued:
            item["action"] = "continue"
            item["continue_topic_id"] = continued[local]
        else:
            item["action"] = "new"
            item["new_topic_id"] = split_new
            item["new_name"] = "物流异常（拆分）"
            item["note"] = "人工确认拆分"
    return decision


def test_apply_full_flow_exports_evidence(setup, tmp_path):
    old, new, names, report, template = setup
    decision = resolve_all(template)
    applied = apply_names(old, new, names, decision, FIXTURES / "period_b.jsonl")
    assert applied["counts"]["topics"] == 4
    assert applied["confirmed_changes"]["counts"] == {
        "continued": 3,
        "added": 1,
        "retired": 0,
        "unrepresented_old": 0,
        "net_confirmed_change": 1,
    }
    for topic in applied["topics"]:
        assert topic["members"]
        for member in topic["members"]:
            assert member["period"] == "b"  # evidence from the correct period
            assert member["body"]  # original text retained
            assert member["assignment_score"] is not None

    write_applied_outputs(applied, tmp_path)
    evidence_lines = [
        json.loads(line)
        for line in (tmp_path / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assigned = [line for line in evidence_lines if line["kind"] == "assigned"]
    unassigned = [line for line in evidence_lines if line["kind"] == "unassigned"]
    assert assigned and unassigned
    assert all(line["period"] == "b" for line in assigned)
    assert (tmp_path / "named.review.html").exists()


def test_empty_action_is_rejected(setup):
    old, new, names, _, template = setup
    decision = copy.deepcopy(template)
    with pytest.raises(DecisionError, match="unresolved"):
        apply_names(old, new, names, decision, FIXTURES / "period_b.jsonl")


def test_duplicate_continue_target_is_rejected(setup):
    old, new, names, _, template = setup
    decision = resolve_all(template)
    # Point two new topics at the same old business topic.
    decision["decisions"][3]["action"] = "continue"
    decision["decisions"][3]["continue_topic_id"] = "acct-login"
    with pytest.raises(DecisionError, match="two new topics"):
        apply_names(old, new, names, decision, FIXTURES / "period_b.jsonl")


def test_competing_flag_requires_note(setup):
    old, new, names, _, template = setup
    decision = resolve_all(template)
    split = decision["decisions"][3]
    assert split["flags"]  # this topic has a split flag
    split["note"] = ""
    with pytest.raises(DecisionError, match="note"):
        apply_names(old, new, names, decision, FIXTURES / "period_b.jsonl")


def test_permuted_new_model_keeps_confirmed_names(setup, tmp_path):
    old, new, names, _, template = setup
    decision = resolve_all(template)

    base = apply_names(old, new, names, decision, FIXTURES / "period_b.jsonl")
    permuted = permute_model(new, [2, 0, 3, 1])
    shifted = apply_names(old, permuted, names, decision, FIXTURES / "period_b.jsonl")

    def projection(applied):
        return {
            topic["topic_id"]: (
                topic["name"],
                topic["fingerprint"],
                tuple(m["ticket_id"] for m in topic["members"]),
            )
            for topic in applied["topics"]
        }

    assert projection(base) == projection(shifted)
    assert base["confirmed_changes"]["counts"] == shifted["confirmed_changes"]["counts"]
    # local ids really did move, demonstrating matching is not by number
    assert {t["topic_id"]: t["local_id"] for t in base["topics"]} != {
        t["topic_id"]: t["local_id"] for t in shifted["topics"]
    }


def test_changed_new_input_is_rejected(setup, tmp_path):
    old, new, names, _, template = setup
    decision = resolve_all(template)
    rows = [
        json.loads(line)
        for line in (FIXTURES / "period_b.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rows[0]["body"] = rows[0]["body"] + " 变更"
    changed = write_jsonl(tmp_path / "period_b.changed.jsonl", rows)
    with pytest.raises(SourceChangedError):
        apply_names(old, new, names, decision, changed)


def test_stale_decision_binding_is_rejected(setup, tmp_path):
    old, new, names, _, template = setup
    decision = resolve_all(template)
    decision["binding"]["old_input_sha256"] = "0" * 64
    with pytest.raises(BindingError, match="stale"):
        apply_names(old, new, names, decision, FIXTURES / "period_b.jsonl")


def test_changed_old_names_is_rejected(setup):
    old, new, names, _, template = setup
    decision = resolve_all(template)
    tampered = copy.deepcopy(names)
    tampered.entries[0] = type(tampered.entries[0])(
        topic_id=tampered.entries[0].topic_id,
        name="改名了",
        local_id=tampered.entries[0].local_id,
        fingerprint=tampered.entries[0].fingerprint,
        note="",
    )
    tampered.raw_sha256 = "e" * 64
    with pytest.raises(BindingError, match="old_names_sha256"):
        apply_names(old, new, tampered, decision, FIXTURES / "period_b.jsonl")


def test_old_model_change_invalidates_decision(setup):
    old, new, names, _, template = setup
    decision = resolve_all(template)
    # Retrain the old period after its input changed; even with the same names
    # object the model summary differs, so the old decision must be rejected.
    retrained = copy.deepcopy(old)
    retrained["model"]["tol"] = 1e-6
    retrained["summary_sha256"] = __import__(
        "topic_migrate.model", fromlist=["model_summary_hash"]
    ).model_summary_hash(retrained)
    with pytest.raises(BindingError, match="old_model_summary_sha256"):
        apply_names(retrained, new, names, decision, FIXTURES / "period_b.jsonl")


def test_explicit_retirement_is_counted(setup):
    old, new, names, _, template = setup
    decision = resolve_all(template)
    # Continue only two old topics; explicitly retire the third and make the
    # split topic a new business topic.
    decision["decisions"][2]["action"] = "new"
    decision["decisions"][2]["new_topic_id"] = "billing-v2"
    decision["decisions"][2]["new_name"] = "账单（新版）"
    decision["retirements"] = [
        {"topic_id": "billing-invoice", "action": "retire", "note": "人工确认替换"}
    ]
    applied = apply_names(old, new, names, decision, FIXTURES / "period_b.jsonl")
    counts = applied["confirmed_changes"]["counts"]
    assert counts["continued"] == 2
    assert counts["added"] == 2
    assert counts["retired"] == 1
    assert counts["net_confirmed_change"] == 1
    assert counts["unrepresented_old"] == 0


def test_continue_and_retire_same_topic_is_rejected(setup):
    old, new, names, _, template = setup
    decision = resolve_all(template)
    decision["retirements"] = [
        {"topic_id": "acct-login", "action": "retire", "note": "冲突"}
    ]
    with pytest.raises(DecisionError, match="both continued and retired"):
        apply_names(old, new, names, decision, FIXTURES / "period_b.jsonl")


def test_new_topic_id_cannot_collide_with_continued_id(setup):
    old, new, names, _, template = setup
    decision = resolve_all(template)
    decision["decisions"][3]["new_topic_id"] = "acct-login"
    decision["decisions"][3]["new_name"] = "撞名"
    with pytest.raises(DecisionError):
        apply_names(old, new, names, decision, FIXTURES / "period_b.jsonl")


def test_decision_with_unknown_fingerprint_is_rejected(setup):
    old, new, names, _, template = setup
    decision = resolve_all(template)
    decision["decisions"][0]["new_fingerprint"] = "0" * 64
    with pytest.raises(DecisionError, match="absent from the new model"):
        apply_names(old, new, names, decision, FIXTURES / "period_b.jsonl")


def test_stale_compare_report_is_rejected(setup):
    old, new, names, report, template = setup
    decision = resolve_all(template)
    tampered = copy.deepcopy(report)
    tampered["config"]["candidate_floor"] = 0.5
    with pytest.raises(BindingError, match="inconsistent"):
        apply_names(old, new, names, decision, FIXTURES / "period_b.jsonl", tampered)


def test_decision_bound_to_different_compare_report_is_rejected(setup):
    old, new, names, report, template = setup
    decision = resolve_all(template)
    other = copy.deepcopy(report)
    other["compare_sha256"] = "a" * 64
    with pytest.raises(BindingError, match="compare_sha256"):
        apply_names(old, new, names, decision, FIXTURES / "period_b.jsonl", other)


def test_names_for_wrong_period_are_rejected(setup):
    old, new, names, _, template = setup
    decision = resolve_all(template)
    wrong = copy.deepcopy(names)
    wrong.period = "z"
    with pytest.raises(Exception, match="period"):
        apply_names(old, new, wrong, decision, FIXTURES / "period_b.jsonl")


def test_applied_names_chain_to_next_period(setup, tmp_path):
    old, new, names, _, template = setup
    decision = resolve_all(template)
    applied = apply_names(old, new, names, decision, FIXTURES / "period_b.jsonl")
    write_applied_outputs(applied, tmp_path)
    chained = load_names(tmp_path / "names.next.json")
    assert chained.period == "b"
    resolved = chained.resolve(new)
    assert set(resolved["by_fingerprint"]) == {
        topic["fingerprint"] for topic in new["topics"]
    }
    # business ids survived the transition
    assert {entry.topic_id for entry in chained.entries} == {
        "acct-login", "logistics-delivery", "billing-invoice", "logistics-exceptions"
    }
