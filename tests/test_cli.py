"""CLI end-to-end: train -> compare -> apply-names and exit codes."""

from __future__ import annotations

import re

from topic_migrate.cli import main

from conftest import EXAMPLES, FIXTURES, read_json, write_json


def test_cli_end_to_end(tmp_path):
    a_out = tmp_path / "a"
    b_out = tmp_path / "b"
    assert main([
        "train", "--input", str(FIXTURES / "period_a.jsonl"),
        "--plan", str(FIXTURES / "plan.json"), "--period", "a",
        "--out", str(a_out),
    ]) == 0
    assert main([
        "train", "--input", str(FIXTURES / "period_b.jsonl"),
        "--plan", str(FIXTURES / "plan.json"), "--period", "b",
        "--out", str(b_out),
    ]) == 0
    assert (a_out / "model.json").exists()
    assert (a_out / "names.template.json").exists()

    compare_out = tmp_path / "compare"
    assert main([
        "compare",
        "--old-model", str(a_out / "model.json"),
        "--new-model", str(b_out / "model.json"),
        "--old-names", str(EXAMPLES / "period_a.names.json"),
        "--out", str(compare_out),
    ]) == 0
    assert (compare_out / "compare.review.html").exists()
    template = read_json(compare_out / "decision.template.json")
    html = (compare_out / "compare.review.html").read_text(encoding="utf-8")
    assert "只读" in html
    # The report must not contain executable script/event handlers.
    assert "<script" not in html.lower()
    assert not re.search(r"\son\w+\s*=", html, re.IGNORECASE)

    continued = {0: "acct-login", 1: "logistics-delivery", 2: "billing-invoice"}
    for item in template["decisions"]:
        local = item["new_local_id"]
        if local in continued:
            item["action"] = "continue"
            item["continue_topic_id"] = continued[local]
        else:
            item["action"] = "new"
            item["new_topic_id"] = "logistics-exceptions"
            item["new_name"] = "物流异常"
            item["note"] = "人工确认拆分"
    decision_path = write_json(tmp_path / "decision.json", template)

    apply_out = tmp_path / "applied"
    assert main([
        "apply-names",
        "--old-model", str(a_out / "model.json"),
        "--new-model", str(b_out / "model.json"),
        "--old-names", str(EXAMPLES / "period_a.names.json"),
        "--decision", str(decision_path),
        "--new-input", str(FIXTURES / "period_b.jsonl"),
        "--out", str(apply_out),
    ]) == 0
    assert (apply_out / "named_topics.json").exists()
    assert (apply_out / "evidence.jsonl").exists()
    assert (apply_out / "named.review.html").exists()


def test_cli_rejects_stale_decision_with_exit_code_3(tmp_path):
    a_out = tmp_path / "a"
    b_out = tmp_path / "b"
    main(["train", "--input", str(FIXTURES / "period_a.jsonl"),
          "--plan", str(FIXTURES / "plan.json"), "--period", "a",
          "--out", str(a_out)])
    main(["train", "--input", str(FIXTURES / "period_b.jsonl"),
          "--plan", str(FIXTURES / "plan.json"), "--period", "b",
          "--out", str(b_out)])
    compare_out = tmp_path / "compare"
    main(["compare", "--old-model", str(a_out / "model.json"),
          "--new-model", str(b_out / "model.json"),
          "--old-names", str(EXAMPLES / "period_a.names.json"),
          "--out", str(compare_out)])
    template = read_json(compare_out / "decision.template.json")
    template["binding"]["old_input_sha256"] = "0" * 64
    decision_path = write_json(tmp_path / "decision.json", template)
    assert main([
        "apply-names",
        "--old-model", str(a_out / "model.json"),
        "--new-model", str(b_out / "model.json"),
        "--old-names", str(EXAMPLES / "period_a.names.json"),
        "--decision", str(decision_path),
        "--new-input", str(FIXTURES / "period_b.jsonl"),
        "--out", str(tmp_path / "applied"),
    ]) == 3


def test_cli_rejects_unresolved_decisions_with_exit_code_4(tmp_path):
    a_out = tmp_path / "a"
    b_out = tmp_path / "b"
    main(["train", "--input", str(FIXTURES / "period_a.jsonl"),
          "--plan", str(FIXTURES / "plan.json"), "--period", "a",
          "--out", str(a_out)])
    main(["train", "--input", str(FIXTURES / "period_b.jsonl"),
          "--plan", str(FIXTURES / "plan.json"), "--period", "b",
          "--out", str(b_out)])
    compare_out = tmp_path / "compare"
    main(["compare", "--old-model", str(a_out / "model.json"),
          "--new-model", str(b_out / "model.json"),
          "--old-names", str(EXAMPLES / "period_a.names.json"),
          "--out", str(compare_out)])
    decision_path = write_json(
        tmp_path / "decision.json", read_json(compare_out / "decision.template.json")
    )
    assert main([
        "apply-names",
        "--old-model", str(a_out / "model.json"),
        "--new-model", str(b_out / "model.json"),
        "--old-names", str(EXAMPLES / "period_a.names.json"),
        "--decision", str(decision_path),
        "--new-input", str(FIXTURES / "period_b.jsonl"),
        "--out", str(tmp_path / "applied"),
    ]) == 4
    assert not (tmp_path / "applied" / "named_topics.json").exists()
