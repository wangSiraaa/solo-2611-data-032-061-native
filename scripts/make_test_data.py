"""Generate supplementary test data under .runtime/ (git-ignored).

The shipped fixtures/ stay untouched. This material covers branches the small
fixture cannot: an all-missing numeric column, shuffled training rows and a
deliberately distribution-shifted batch used to prove the artifact is frozen.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "data"
RUNTIME.mkdir(parents=True, exist_ok=True)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


# Clean version of the shipped batch/online fixture (r4's boolean is repaired)
# so the happy-path matrix can run end to end.
clean = [
    {"record_id": "r1", "amount": 3, "constant": 5, "channel": "web"},
    {"record_id": "r2", "amount": None, "constant": None, "channel": None},
    {"record_id": "r3", "amount": 12, "constant": 5, "channel": "partner"},
    {"record_id": "r4", "amount": 100, "constant": 5, "channel": "store"},
    {"record_id": "r5", "amount": 7, "constant": 5, "channel": "__UNKNOWN__"},
    {"record_id": "r6", "amount": 4, "constant": 5, "channel": ""},
]
write_jsonl(RUNTIME / "clean.jsonl", clean)

# Same training rows as fixtures/train.jsonl in a deliberately different
# order; fitting on it must yield the byte-identical artifact digest.
shuffled = [
    {"record_id": "t6", "amount": 9, "constant": 5, "channel": ""},
    {"record_id": "t3", "amount": 5, "constant": 5, "channel": None},
    {"record_id": "t1", "amount": 1, "constant": 5, "channel": "web"},
    {"record_id": "t5", "amount": 7, "constant": None, "channel": "web"},
    {"record_id": "t4", "amount": None, "constant": 5, "channel": "__UNKNOWN__"},
    {"record_id": "t2", "amount": 3, "constant": 5, "channel": "store"},
]
write_jsonl(RUNTIME / "train_shuffled.jsonl", shuffled)

# All-missing numeric column ("gap") plus a normal one ("amount").
(RUNTIME / "allmissing_spec.json").write_text(
    json.dumps(
        {
            "record_id": "record_id",
            "numeric": ["amount", "gap"],
            "categorical": "channel",
        }
    ),
    encoding="utf-8",
)
all_missing_rows = [
    {"record_id": "a1", "amount": 2, "gap": None, "channel": "web"},
    {"record_id": "a2", "amount": 4, "gap": None, "channel": "store"},
    {"record_id": "a3", "amount": 6, "gap": None, "channel": None},
]
write_jsonl(RUNTIME / "allmissing_train.jsonl", all_missing_rows)
write_jsonl(
    RUNTIME / "allmissing_batch.jsonl",
    [
        {"record_id": "b1", "amount": 10, "gap": None, "channel": "web"},
        {"record_id": "b2", "amount": None, "gap": None, "channel": "new"},
    ],
)

# Distribution-shifted data: very different means from training. Transform
# output for a shared record must stay identical because stats never update.
shifted = [
    {"record_id": "s1", "amount": 999, "constant": 5, "channel": "web"},
    {"record_id": "s2", "amount": -500, "constant": 5, "channel": "store"},
    {"record_id": "r1", "amount": 3, "constant": 5, "channel": "web"},
]
write_jsonl(RUNTIME / "shifted.jsonl", shifted)

print(json.dumps({"generated_at": str(RUNTIME), "files": sorted(p.name for p in RUNTIME.iterdir())}))
