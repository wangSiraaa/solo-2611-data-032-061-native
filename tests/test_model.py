"""Model training: local vocabulary, weak evidence, determinism, invariant."""

from __future__ import annotations

import json

import pytest

from topic_migrate.data import load_period
from topic_migrate.errors import ModelError
from topic_migrate.model import (
    TrainConfig,
    permute_model,
    train_model,
    validate_model,
)

from topic_migrate.storage import sha256_file

from conftest import FIXTURES, read_json, write_jsonl


def train(path, period_key="period_a", period="a", plan=None, seed=122):
    plan_path = FIXTURES / "plan.json"
    plan = plan or read_json(plan_path)
    loaded = load_period(path)
    return train_model(
        loaded=loaded,
        period=period,
        plan=plan,
        period_key=period_key,
        plan_sha256=sha256_file(plan_path),
        config=TrainConfig.from_plan(plan, period_key),
    )


def test_train_period_a_shape_and_preserved_text():
    model = train(FIXTURES / "period_a.jsonl")
    validate_model(model)
    assert len(model["topics"]) == 3
    assert model["input"]["n_unmodelable"] == 1
    assert model["unmodelable"][0]["ticket_id"] == "EMPTY-A"
    assert model["unmodelable"][0]["reason"] == "empty_text"

    ticket_ids = {t["ticket_id"] for t in model["tickets"]}
    assert "EMPTY-A" in ticket_ids
    assignment_ids = {a["ticket_id"] for a in model["assignments"]}
    assert "EMPTY-A" not in assignment_ids  # never modeled

    for topic in model["topics"]:
        assert topic["keywords"]
        assert topic["n_assigned"] > 0
        for rep in topic["representative_tickets"]:
            assert rep["period"] == "a"
            assert rep["body"]  # original text preserved
            assert rep["ticket_id"] in topic["assigned_ticket_ids"]


def test_vocabulary_is_period_local(tmp_path):
    model_a = train(FIXTURES / "period_a.jsonl")
    # A token that only exists in period B must not appear in A's vocabulary.
    assert "团队" not in model_a["vocabulary"]
    assert "权限" not in model_a["vocabulary"]
    # A's own terms are present.
    assert "发票" in model_a["vocabulary"]


def test_same_seed_is_deterministic():
    first = train(FIXTURES / "period_b.jsonl", "period_b", "b")
    second = train(FIXTURES / "period_b.jsonl", "period_b", "b")
    assert first["summary_sha256"] == second["summary_sha256"]
    assert first["topics"] == second["topics"]


def test_weak_evidence_stays_unassigned(tmp_path):
    rows = [
        {"ticket_id": f"L{i}", "channel": "web", "title": "账户", "body": "账户 登录 密码 重置 失败"}
        for i in range(6)
    ] + [
        {"ticket_id": f"D{i}", "channel": "web", "title": "配送", "body": "配送 延误 包裹 地址 查询"}
        for i in range(6)
    ] + [
        {"ticket_id": "W1", "channel": "mail", "title": "咨询", "body": "发票"},
        {"ticket_id": "W2", "channel": "mail", "title": "咨询", "body": "请 协助"},
        {"ticket_id": "EMPTY", "channel": "web", "title": "空", "body": ""},
    ]
    path = write_jsonl(tmp_path / "weak.jsonl", rows)
    model = train(
        path,
        period_key="period_a",
        period="a",
        plan={"period_a": {"n_topics": 2}, "seed": 122},
    )
    by_id = {a["ticket_id"]: a for a in model["assignments"]}
    assert by_id["W1"]["status"] == "unassigned"
    assert by_id["W1"]["reason"] == "insufficient_evidence"
    assert by_id["W2"]["status"] == "unassigned"
    assert "EMPTY" not in by_id
    assert model["input"]["n_unmodelable"] == 1


def test_permutation_preserves_summary_and_fingerprints():
    model = train(FIXTURES / "period_b.jsonl", "period_b", "b")
    permuted = permute_model(model, [2, 0, 3, 1])
    assert permuted["summary_sha256"] == model["summary_sha256"]
    assert [t["fingerprint"] for t in permuted["topics"]] != [
        t["fingerprint"] for t in model["topics"]
    ]
    assert sorted(t["fingerprint"] for t in permuted["topics"]) == sorted(
        t["fingerprint"] for t in model["topics"]
    )
    # local ids are consistently relabeled
    mapping = {old: new for new, old in enumerate([2, 0, 3, 1])}
    for old, new in mapping.items():
        assert permuted["topics"][new]["fingerprint"] == model["topics"][old]["fingerprint"]


def test_inconsistent_renumbering_is_rejected():
    model = train(FIXTURES / "period_b.jsonl", "period_b", "b")
    broken = json.loads(json.dumps(model))
    broken["topics"][0]["local_id"], broken["topics"][1]["local_id"] = 1, 0
    with pytest.raises(ModelError):
        validate_model(broken)


def test_n_topics_larger_than_corpus_is_rejected(tmp_path):
    path = write_jsonl(
        tmp_path / "tiny.jsonl",
        [{"ticket_id": "T1", "channel": "web", "title": "a", "body": "发票"}],
    )
    with pytest.raises(ModelError, match="exceeds modelable tickets"):
        train(path, plan={"period_a": {"n_topics": 3}, "seed": 1})


def test_no_modelable_tickets_is_rejected(tmp_path):
    path = write_jsonl(
        tmp_path / "empty.jsonl",
        [{"ticket_id": "T1", "channel": "web", "title": "a", "body": ""}],
    )
    with pytest.raises(ModelError, match="no modelable tickets"):
        train(path, plan={"period_a": {"n_topics": 1}, "seed": 1})


def test_tampered_fingerprint_is_rejected():
    model = train(FIXTURES / "period_b.jsonl", "period_b", "b")
    broken = json.loads(json.dumps(model))
    broken["topics"][0]["top_terms"] = ["伪造"]
    # top_terms do not feed the fingerprint, but term_weights do.
    broken["topics"][0]["term_weights"] = {"伪造": 1.0}
    with pytest.raises(ModelError, match="fingerprint"):
        validate_model(broken)
