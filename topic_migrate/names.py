"""Human-authored names, bound to stable business topic ids.

A names file is authored after a model is trained. It binds:

* a stable business ``topic_id`` (survives retraining and renumbering),
* a human ``name``,
* the content ``fingerprint`` of the model topic it describes.

The local integer id is recorded as a convenience only. Resolution is by
fingerprint, so a consistent renumbering of the model does not move names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import NamesError
from .storage import read_json, sha256_value

NAMES_SCHEMA = "topic-migrate/names/v1"
TOPIC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class NameEntry:
    topic_id: str
    name: str
    local_id: int | None
    fingerprint: str
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "name": self.name,
            "local_id": self.local_id,
            "fingerprint": self.fingerprint,
            "note": self.note,
        }


@dataclass
class NamesRegistry:
    path: Path
    period: str
    input_sha256: str | None
    model_summary_sha256: str | None
    entries: list[NameEntry]
    raw_sha256: str = ""

    def by_topic_id(self) -> dict[str, NameEntry]:
        return {entry.topic_id: entry for entry in self.entries}

    def by_fingerprint(self) -> dict[str, NameEntry]:
        return {entry.fingerprint: entry for entry in self.entries}

    def resolve(self, model: dict[str, Any]) -> dict[str, Any]:
        """Resolve names against a model, reporting local-id remaps.

        Returns ``{fingerprint: NameEntry}`` plus a rename map of
        ``old local id -> current local id``. Any fingerprint present in the
        model but absent from names (or vice versa) is an error.
        """
        model_fps = {topic["fingerprint"] for topic in model["topics"]}
        name_fps = {entry.fingerprint for entry in self.entries}
        if self.period != model["period"]:
            raise NamesError(
                f"{self.path}: names are for period {self.period!r}, not "
                f"{model['period']!r}"
            )
        missing = sorted(model_fps - name_fps)
        extra = sorted(name_fps - model_fps)
        if missing:
            raise NamesError(
                f"{self.path}: names are missing {len(missing)} model topic(s); "
                f"run `names` against the current model. Missing fingerprints: {missing}"
            )
        if extra:
            raise NamesError(
                f"{self.path}: names reference {len(extra)} topic(s) absent from "
                f"the model; the model changed. Extra fingerprints: {extra}"
            )
        if self.model_summary_sha256 and (
            self.model_summary_sha256 != model["summary_sha256"]
        ):
            raise NamesError(
                f"{self.path}: names were authored for model summary "
                f"{self.model_summary_sha256[:12]}… but the model summary is "
                f"{model['summary_sha256'][:12]}…; re-author the names"
            )
        if self.input_sha256 and self.input_sha256 != model["input"]["sha256"]:
            raise NamesError(
                f"{self.path}: names are bound to input "
                f"{self.input_sha256[:12]}… but the model used input "
                f"{model['input']['sha256'][:12]}…"
            )

        by_fp = self.by_fingerprint()
        remap: dict[int, int] = {}
        for topic in model["topics"]:
            entry = by_fp[topic["fingerprint"]]
            if entry.local_id is not None and entry.local_id != topic["local_id"]:
                remap[entry.local_id] = topic["local_id"]
        return {"by_fingerprint": by_fp, "remap": remap}


def load_names(path: Path) -> NamesRegistry:
    try:
        raw = read_json(path)
    except FileNotFoundError:
        raise NamesError(f"{path}: names file not found")
    except ValueError as exc:
        raise NamesError(str(exc)) from exc
    if not isinstance(raw, dict):
        raise NamesError(f"{path}: names must be a JSON object")
    if raw.get("schema") != NAMES_SCHEMA:
        raise NamesError(f"{path}: expected schema {NAMES_SCHEMA!r}")
    period = raw.get("period")
    if not isinstance(period, str) or not period:
        raise NamesError(f"{path}: period is required")
    topics = raw.get("topics")
    if not isinstance(topics, list) or not topics:
        raise NamesError(f"{path}: topics must be a non-empty list")
    input_sha = raw.get("input_sha256")
    summary_sha = raw.get("model_summary_sha256")
    if input_sha is not None and not isinstance(input_sha, str):
        raise NamesError(f"{path}: input_sha256 must be a string")
    if summary_sha is not None and not isinstance(summary_sha, str):
        raise NamesError(f"{path}: model_summary_sha256 must be a string")

    entries: list[NameEntry] = []
    seen_ids: set[str] = set()
    seen_fps: set[str] = set()
    for idx, item in enumerate(topics, 1):
        if not isinstance(item, dict):
            raise NamesError(f"{path}: topics[{idx}] must be an object")
        topic_id = item.get("topic_id")
        name = item.get("name")
        fingerprint = item.get("fingerprint")
        local_id = item.get("local_id")
        note = item.get("note", "")
        if not isinstance(topic_id, str) or not TOPIC_ID_RE.match(topic_id):
            raise NamesError(
                f"{path}: topics[{idx}].topic_id must match {TOPIC_ID_RE.pattern}"
            )
        if not isinstance(name, str) or not name.strip():
            raise NamesError(f"{path}: topics[{idx}].name must be non-empty")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise NamesError(f"{path}: topics[{idx}].fingerprint is required")
        if not isinstance(local_id, int) or isinstance(local_id, bool):
            raise NamesError(f"{path}: topics[{idx}].local_id must be an integer")
        if local_id < 0:
            raise NamesError(f"{path}: topics[{idx}].local_id must be >= 0")
        if not isinstance(note, str):
            raise NamesError(f"{path}: topics[{idx}].note must be a string")
        if topic_id in seen_ids:
            raise NamesError(f"{path}: duplicate topic_id {topic_id!r}")
        if fingerprint in seen_fps:
            raise NamesError(f"{path}: duplicate topic fingerprint")
        seen_ids.add(topic_id)
        seen_fps.add(fingerprint)
        entries.append(
            NameEntry(
                topic_id=topic_id,
                name=name.strip(),
                local_id=local_id,
                fingerprint=fingerprint,
                note=note,
            )
        )

    registry = NamesRegistry(
        path=path,
        period=period,
        input_sha256=input_sha,
        model_summary_sha256=summary_sha,
        entries=entries,
    )
    registry.raw_sha256 = sha256_value(
        {
            "path": path.name,
            "content": {
                "schema": raw.get("schema"),
                "period": period,
                "input_sha256": input_sha,
                "model_summary_sha256": summary_sha,
                "topics": [entry.as_dict() for entry in entries],
            },
        }
    )
    return registry


def build_names_template(model: dict[str, Any]) -> dict[str, Any]:
    """Editable scaffold for an analyst: topic_id/name are intentionally blank."""
    topics = []
    for topic in sorted(model["topics"], key=lambda t: t["local_id"]):
        topics.append(
            {
                "local_id": topic["local_id"],
                "fingerprint": topic["fingerprint"],
                "topic_id": "",
                "name": "",
                "note": "",
                "top_terms": topic["top_terms"][:10],
                "n_assigned": topic["n_assigned"],
                "sample_ticket_ids": [
                    ticket["ticket_id"]
                    for ticket in topic["representative_tickets"][:3]
                ],
            }
        )
    return {
        "schema": NAMES_SCHEMA,
        "period": model["period"],
        "input_sha256": model["input"]["sha256"],
        "model_summary_sha256": model["summary_sha256"],
        "instructions": (
            "Fill topic_id and name for every entry. topic_id is a stable "
            "business identifier; keep it unchanged in future periods. Do not "
            "change fingerprint, local_id, input_sha256, or "
            "model_summary_sha256."
        ),
        "topics": topics,
    }


def names_for_next_period(
    model: dict[str, Any], topics: list[dict[str, Any]]
) -> dict[str, Any]:
    """Names file for the just-applied period, usable as next period's old_names.

    Preserves each confirmed business ``topic_id`` and binds it to the applied
    model's fingerprints so a later compare can resolve it.
    """
    return {
        "schema": NAMES_SCHEMA,
        "period": model["period"],
        "input_sha256": model["input"]["sha256"],
        "model_summary_sha256": model["summary_sha256"],
        "topics": [
            {
                "topic_id": topic["topic_id"],
                "name": topic["name"],
                "local_id": topic["local_id"],
                "fingerprint": topic["fingerprint"],
                "note": topic["note"],
            }
            for topic in sorted(topics, key=lambda item: item["local_id"])
        ],
    }
