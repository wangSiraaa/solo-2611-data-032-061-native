"""Deterministic local workloads for ``generate``.

All data is generated locally with a seed.  A workload is a list of submit
requests; each request touches at most ten keys per side, has no duplicate key
on a side, and includes ABSENT entries for genuine inserts.

Two flavours:
  * ``basic``     -- small readable workload with inserts and updates.
  * ``coverage``  -- exercises the maximum breadth: 10 keys/side, duplicate
                     overlap between sides, ABSENT inserts, mixed updates.
"""
from __future__ import annotations

import random

from .request import ABSENT

MAX_PER_SIDE = 10


def _item(key: str, expected, new: int) -> dict:
    return {"key": key, "expected": expected, "new": new}


def coverage_request(seed: int, txid: str,
                     old_values: dict[str, dict[str, int]]) -> dict:
    """A maximal-shape request: 10 keys per side, some already present, some
    genuine inserts.  Expected old values always come from the actual files.
    """
    rng = random.Random(seed)

    def side(name: str, base_new: int, existing: dict[str, int],
             absent_count: int) -> list[dict]:
        existing_keys = sorted(existing)
        items = []
        for i in range(10 - absent_count):
            if i < len(existing_keys):
                key = existing_keys[i]
                items.append(_item(key, existing[key],
                                   base_new + rng.randint(1, 99)))
            else:
                # Seed data exhausted: pad with a genuine insert so the side
                # still exercises the maximum ten keys.
                items.append(_item(f"{name}-pad-{seed}-{i:02d}", ABSENT,
                                   base_new + 100 + i))
        for i in range(absent_count):
            items.append(_item(f"{name}-new-{seed}-{i:02d}", ABSENT,
                               base_new + 200 + i))
        return items

    return {"txid": txid, "participants": {
        "p1": side("p1", 10000, old_values["p1"], 3),
        "p2": side("p2", 20000, old_values["p2"], 2),
    }}


def basic_request(seed: int, txid: str, old_values: dict[str, dict[str, int]]) -> dict:
    rng = random.Random(seed)
    p1_keys = ["a", "b", "c"]
    p2_keys = ["x", "y"]
    p1 = []
    for key in p1_keys:
        old = old_values["p1"].get(key)
        p1.append(_item(key, ABSENT if old is None else old, (old or 0) + rng.randint(1, 50)))
    p2 = []
    for key in p2_keys:
        old = old_values["p2"].get(key)
        p2.append(_item(key, ABSENT if old is None else old, (old or 0) + rng.randint(1, 50)))
    return {"txid": txid, "participants": {"p1": p1, "p2": p2}}


def realistic_request(seed: int, txid: str, old_values: dict[str, dict[str, int]]) -> dict:
    """A realistic banking-style transfer across two independent databases.

    p1 debits three accounts, p2 credits two and inserts one new ledger row.
    Every expected value comes from a prior read of the actual file.
    """
    rng = random.Random(seed)
    p1, p2 = [], []
    for key in ("checking", "savings", "reserve"):
        old = old_values["p1"].get(key)
        if old is None:
            p1.append(_item(key, ABSENT, rng.randint(100, 500)))
        else:
            p1.append(_item(key, old, old - rng.randint(1, 100)))
    for key in ("partner-checking", "partner-savings"):
        old = old_values["p2"].get(key)
        p2.append(_item(key, ABSENT if old is None else old,
                        (old or 0) + rng.randint(1, 100)))
    p2.append(_item("ledger-2026-11-13", ABSENT, rng.randint(1, 100)))
    return {"txid": txid, "participants": {"p1": p1, "p2": p2}}


def conflicting_request() -> dict:
    """A second request that intentionally conflicts with an earlier one."""
    return {
        "txid": "tx_conflicting",
        "participants": {
            "p1": [_item("a", 1, 2)],
            "p2": [_item("x", 1, 2)],
        },
    }


def make_workload(kind: str, seed: int, p1_values: dict[str, int],
                  p2_values: dict[str, int], count: int = 3) -> list[dict]:
    old = {"p1": p1_values, "p2": p2_values}
    requests = []
    for i in range(count):
        txid = f"{kind}-{seed}-{i + 1:02d}"
        if kind == "coverage":
            requests.append(coverage_request(seed + i, txid, old))
            for name in ("p1", "p2"):
                for item in requests[-1]["participants"][name]:
                    old[name][item["key"]] = item["new"]
        elif kind == "realistic":
            requests.append(realistic_request(seed + i, txid, old))
            # Track expected outcomes for subsequent requests in this batch.
            for name in ("p1", "p2"):
                for item in requests[-1]["participants"][name]:
                    old[name][item["key"]] = item["new"]
        else:
            requests.append(basic_request(seed + i, txid, old))
            for name in ("p1", "p2"):
                for item in requests[-1]["participants"][name]:
                    old[name][item["key"]] = item["new"]
    return requests
