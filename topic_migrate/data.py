"""Ticket loading and validation.

Rules encoded here:
- ``ticket_id`` is a stable identity; it must be unique within one period.
- Repeats of the same identity with conflicting content are rejected.
- Exact repeats (after NFC/whitespace normalization) are de-duplicated and
  reported; they are not modeled twice.
- Whitespace-only bodies are valid records but are *not* modelable. They are
  retained for review and never assigned to a topic.

The title is review metadata. Modeling uses the body so that short synthetic
titles such as "发票问题" are not used as labels; both fields are preserved.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import InputConflictError, InputError
from .storage import read_jsonl

KNOWN_FIELDS = {"ticket_id", "channel", "title", "body"}


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    channel: str
    title: str
    body: str
    line_no: int
    extra: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def model_text(self) -> str:
        """Body text is the only modeling input; title stays review metadata."""
        return self.body

    @property
    def is_modelable(self) -> bool:
        return bool(self.body.strip())

    def review_payload(self) -> dict[str, Any]:
        payload = {
            "ticket_id": self.ticket_id,
            "channel": self.channel,
            "title": self.title,
            "body": self.body,
        }
        if self.extra:
            payload["extra"] = dict(self.extra)
        return payload


@dataclass
class LoadedPeriod:
    path: Path
    tickets: list[Ticket]
    n_input_records: int
    duplicate_identical: list[dict[str, Any]] = field(default_factory=list)

    @property
    def modelable(self) -> list[Ticket]:
        return [t for t in self.tickets if t.is_modelable]

    @property
    def unmodelable(self) -> list[Ticket]:
        return [t for t in self.tickets if not t.is_modelable]


def _require_text(row: dict[str, Any], key: str, path: Path, line_no: int) -> str:
    if key not in row:
        raise InputError(f"{path}:{line_no}: missing required field {key!r}")
    value = row[key]
    if not isinstance(value, str):
        raise InputError(f"{path}:{line_no}: field {key!r} must be a string")
    return value


def _identity_key(ticket: Ticket) -> tuple[str, str, str]:
    def norm(value: str) -> str:
        return unicodedata.normalize("NFC", value).strip()

    return (norm(ticket.channel), norm(ticket.title), norm(ticket.body))


def load_period(path: Path) -> LoadedPeriod:
    """Load and validate one JSONL period.

    Content comparison uses NFC-normalized fields so that a visually identical
    repeat is de-duplicated while a real edit is a conflict.
    """
    try:
        raw_rows = read_jsonl(path)
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    if not raw_rows:
        raise InputError(f"{path}: input contains no tickets")

    tickets: list[Ticket] = []
    by_id: dict[str, Ticket] = {}
    origin: dict[str, int] = {}
    duplicates: list[dict[str, Any]] = []

    for idx, row in enumerate(raw_rows, 1):
        if not isinstance(row, dict):
            raise InputError(f"{path}: record {idx} is not a JSON object")
        ticket_id = _require_text(row, "ticket_id", path, idx).strip()
        channel = _require_text(row, "channel", path, idx)
        title = _require_text(row, "title", path, idx)
        body = _require_text(row, "body", path, idx)
        if not ticket_id:
            raise InputError(f"{path}: record {idx} has an empty ticket_id")

        extra = {k: v for k, v in row.items() if k not in KNOWN_FIELDS}
        ticket = Ticket(
            ticket_id=ticket_id,
            channel=channel.strip(),
            title=title,
            body=body,
            line_no=idx,
            extra=extra,
        )
        prior = by_id.get(ticket_id)
        if prior is not None:
            if _identity_key(prior) != _identity_key(ticket):
                raise InputConflictError(
                    f"{path}: ticket_id {ticket_id!r} repeats at records "
                    f"{origin[ticket_id]} and {idx} with conflicting content"
                )
            duplicates.append(
                {
                    "ticket_id": ticket_id,
                    "first_record": origin[ticket_id],
                    "repeated_record": idx,
                }
            )
            continue
        by_id[ticket_id] = ticket
        origin[ticket_id] = idx
        tickets.append(ticket)

    return LoadedPeriod(
        path=path,
        tickets=tickets,
        n_input_records=len(raw_rows),
        duplicate_identical=duplicates,
    )
