"""Input validation: stable identity, conflicts, empty text retention."""

from __future__ import annotations

import pytest

from topic_migrate.data import load_period
from topic_migrate.errors import InputConflictError, InputError

from conftest import write_jsonl


def test_loads_fixture_and_retains_empty_body(tmp_path):
    loaded = load_period(__import__("conftest").FIXTURES / "period_a.jsonl")
    assert loaded.n_input_records == 43
    assert len(loaded.tickets) == 43
    assert [t.ticket_id for t in loaded.unmodelable] == ["EMPTY-A"]
    # Original text is preserved verbatim for review.
    first = loaded.tickets[0]
    assert first.title == "发票问题"
    assert first.body.startswith("发票 金额 不符")
    assert first.body == first.review_payload()["body"]


def test_conflicting_duplicate_identity_is_rejected(tmp_path):
    path = write_jsonl(
        tmp_path / "bad.jsonl",
        [
            {"ticket_id": "X1", "channel": "web", "title": "a", "body": "发票"},
            {"ticket_id": "X1", "channel": "web", "title": "a", "body": "退款"},
        ],
    )
    with pytest.raises(InputConflictError, match="conflicting content"):
        load_period(path)


def test_identical_duplicate_identity_is_deduplicated_and_reported(tmp_path):
    row = {"ticket_id": "X1", "channel": "web", "title": "a", "body": "发票"}
    path = write_jsonl(tmp_path / "dup.jsonl", [row, dict(row)])
    loaded = load_period(path)
    assert len(loaded.tickets) == 1
    assert loaded.n_input_records == 2
    assert loaded.duplicate_identical == [
        {"ticket_id": "X1", "first_record": 1, "repeated_record": 2}
    ]


def test_whitespace_only_body_is_retained_but_unmodelable(tmp_path):
    path = write_jsonl(
        tmp_path / "ws.jsonl",
        [
            {"ticket_id": "W1", "channel": "web", "title": "空", "body": "   "},
        ],
    )
    loaded = load_period(path)
    assert len(loaded.tickets) == 1
    assert loaded.tickets[0].is_modelable is False
    assert loaded.tickets[0].body == "   "


def test_missing_field_is_rejected(tmp_path):
    path = write_jsonl(
        tmp_path / "missing.jsonl",
        [{"ticket_id": "X1", "channel": "web", "title": "a"}],
    )
    with pytest.raises(InputError, match="missing required field"):
        load_period(path)


def test_non_string_field_is_rejected(tmp_path):
    path = write_jsonl(
        tmp_path / "badtype.jsonl",
        [{"ticket_id": "X1", "channel": "web", "title": 3, "body": "x"}],
    )
    with pytest.raises(InputError, match="must be a string"):
        load_period(path)
