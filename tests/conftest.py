"""Shared fixtures for the topic-migrate test suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "fixtures"
EXAMPLES = REPO_ROOT / "examples"
SUPPLEMENTS = EXAMPLES / "supplement"


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def ticket_factory():
    def make(index: int, body: str, *, prefix: str = "T", **extra):
        row = {
            "ticket_id": f"{prefix}{index:03d}",
            "channel": extra.pop("channel", "web"),
            "title": extra.pop("title", "标题"),
            "body": body,
        }
        row.update(extra)
        return row

    return make
