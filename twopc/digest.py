"""Canonical digests shared by coordinator and participants.

Three independent hashes let every party detect divergence without trusting a
freshly invented claim:

  request_digest  = H(canonical entire submit request)
                    (coordinator records this; it is the identity of "the
                    content" bound to a txid)
  updates_digest  = H(canonical update list for one side)
                    (each participant recomputes it from the bytes it received)
  decision_digest = H(request_digest, txid, decision, order)
                    (binds the *decision* to the *content*; a participant that
                    stored request_digest can recompute it locally and reject a
                    decision that does not belong to this transaction)

The coordinator uses the same formula, so a participant checks the buy rather
than merely echoing a string it was handed.
"""
from __future__ import annotations

import hashlib

from .request import canonical_json


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_digest(request: dict) -> str:
    return _sha256("2pc-request\n" + canonical_json(request))


def updates_digest(updates: list[dict]) -> str:
    return _sha256("2pc-updates\n" + canonical_json(updates))


def decision_digest(txid: str, req_digest: str, decision: str, order: int) -> str:
    decision = decision.upper()
    if decision not in ("COMMIT", "ABORT"):
        raise ValueError(f"invalid decision {decision!r}")
    return _sha256(
        "2pc-decision\n"
        f"txid={txid}\n"
        f"request={req_digest}\n"
        f"decision={decision}\n"
        f"order={order}\n"
    )
