"""Run every mandated fault scenario in a fresh demo directory.

Each scenario uses the public CLI, prints a short verdict and exits non-zero on
the first failure.  This is the one-command reproduction of the README's
scenario table.

  python3 scripts/demo_faults.py            # all scenarios, fresh dirs
  python3 scripts/demo_faults.py --scenario 3
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from twopc.coordinator import read_coordinator_txns  # noqa: E402


def cli(base: str, *argv: str, rc=0) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "twopc", "--base", base, *argv],
        cwd=ROOT, text=True, capture_output=True,
    )
    if proc.returncode != rc:
        raise SystemExit(
            f"FAIL {argv}: rc={proc.returncode} expected {rc}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    text = proc.stdout.strip()
    return json.loads(text.splitlines()[-1]) if text else {}


def business(base: str, name: str) -> dict:
    conn = sqlite3.connect(os.path.join(base, f"participant-{name}.sqlite3"))
    try:
        return dict(conn.execute("SELECT key, value FROM business").fetchall())
    finally:
        conn.close()


class Ctx:
    def __init__(self, name: str):
        self.tmp = tempfile.mkdtemp(prefix=f"twopc-{name}-")
        self.base = os.path.join(self.tmp, "demo")
        cli(self.base, "init", "--force")

    def request(self, kind: str, seed: int) -> tuple[str, dict]:
        out = cli(self.base, "generate", "--kind", kind, "--seed", str(seed))
        path = out["requests"][0]["path"]
        with open(path) as fh:
            return path, json.load(fh)

    def verify(self, txid: str, path: str) -> dict:
        out = cli(self.base, "verify", txid, "--request-file", path)
        assert out["verify"]["ok"], out["verify"]["failures"]
        return out

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def scenario_1(_args):
    """One side YES, the other NO."""
    c = Ctx("yes-no")
    path = os.path.join(c.tmp, "req.json")
    with open(path, "w") as fh:
        json.dump({"txid": "tx-yes-no", "participants": {
            "p1": [{"key": "a", "expected": 1, "new": 10},
                   {"key": "new-key", "expected": "ABSENT", "new": 5}],
            "p2": [{"key": "x", "expected": 999, "new": 20}],
        }}, fh)
    before = {n: business(c.base, n) for n in ("p1", "p2")}
    out = cli(c.base, "submit", "--request-file", path)
    assert out["prepare"]["p1"]["decision"] == "YES"
    assert out["prepare"]["p2"]["decision"] == "NO"
    assert out["decision"]["decision"] == "ABORT"
    after = {n: business(c.base, n) for n in ("p1", "p2")}
    assert after == before, (before, after)
    st = cli(c.base, "participant-status", "tx-yes-no")
    assert [p["state"] for p in st["participants"]] == ["ABORTED", None]
    c.verify("tx-yes-no", path)
    print("  p1 YES / p2 NO -> ABORT; visible values unchanged; p1 claim released")
    c.cleanup()


def scenario_2(_args):
    """Both prepared, coordinator exits before any decision."""
    c = Ctx("coord-crash")
    path, req = c.request("realistic", 11)
    before = {n: business(c.base, n) for n in ("p1", "p2")}
    cli(c.base, "submit", "--request-file", path, "--fault", "before-decision", rc=70)
    row = read_coordinator_txns(os.path.join(c.base, "coordinator.sqlite3"))[0]
    assert row["state"] == "PREPARED_BOTH" and row["decision"] is None
    st = cli(c.base, "participant-status", req["txid"])
    assert {p["state"] for p in st["participants"]} == {"PREPARED"}
    assert all(p["reservations"] for p in st["participants"])
    locks = cli(c.base, "locks")
    assert locks["unresolved"] > 0
    # An ordinary write is refused while the claim stands.
    cli(c.base, "ordinary-set", "--participant", "p1", "--key",
        req["participants"]["p1"][0]["key"], "--value", "12345", rc=2)
    rec = cli(c.base, "recover", "--txid", req["txid"])
    assert "recorded ABORT" in rec["recovered"][0]["action"]
    afters = {n: business(c.base, n) for n in ("p1", "p2")}
    assert afters == before, (before, afters)
    assert cli(c.base, "locks")["unresolved"] == 0
    c.verify(req["txid"], path)
    print("  both PREPARED + coordinator crash -> recovery records ABORT; values unchanged")
    c.cleanup()


def scenario_3(_args):
    """COMMIT durably saved, only one side notified."""
    c = Ctx("one-notified")
    path, req = c.request("basic", 4)
    cli(c.base, "submit", "--request-file", path, "--fault",
        "after-first-delivery", rc=70)
    row = read_coordinator_txns(os.path.join(c.base, "coordinator.sqlite3"))[0]
    assert row["decision"] == "COMMIT"
    st = cli(c.base, "participant-status", req["txid"])["participants"]
    assert st[0]["state"] == "COMMITTED" and st[1]["state"] == "PREPARED"
    res = cli(c.base, "result", req["txid"])
    assert res["complete"] is False
    rec = cli(c.base, "recover", "--txid", req["txid"])
    assert "kept recorded COMMIT" in rec["recovered"][0]["action"]
    st2 = cli(c.base, "participant-status", req["txid"])["participants"]
    assert {p["state"] for p in st2} == {"COMMITTED"}
    v = c.verify(req["txid"], path)
    assert v["final_values"]["sides"] == {"p1": "NEW", "p2": "NEW"}
    print("  COMMIT saved, one side notified -> recovery continued COMMIT on the other")
    c.cleanup()


def scenario_4(_args):
    """COMMIT saved but no side notified."""
    c = Ctx("none-notified")
    path, req = c.request("basic", 5)
    cli(c.base, "submit", "--request-file", path, "--fault", "after-decision", rc=70)
    row = read_coordinator_txns(os.path.join(c.base, "coordinator.sqlite3"))[0]
    assert row["decision"] == "COMMIT"
    assert {p["state"] for p in
            cli(c.base, "participant-status", req["txid"])["participants"]} == {"PREPARED"}
    cli(c.base, "recover", "--txid", req["txid"])
    v = c.verify(req["txid"], path)
    assert v["final_values"]["sides"] == {"p1": "NEW", "p2": "NEW"}
    print("  COMMIT saved, nobody notified -> recovery re-drove both to COMMITTED")
    c.cleanup()


def scenario_5(_args):
    """Participant commits locally, receipt lost."""
    c = Ctx("lost-receipt")
    path, req = c.request("realistic", 7)
    cli(c.base, "submit", "--request-file", path, "--fault", "after-apply:p1")
    st = cli(c.base, "participant-status", req["txid"])["participants"]
    assert st[0]["state"] == "COMMITTED"
    first = cli(c.base, "recover", "--txid", req["txid"])
    before = {n: business(c.base, n) for n in ("p1", "p2")}
    second = cli(c.base, "recover", "--txid", req["txid"])
    after = {n: business(c.base, n) for n in ("p1", "p2")}
    assert before == after
    assert second["already_complete"][0]["action"] == "nothing to do; already COMPLETE"
    v = c.verify(req["txid"], path)
    assert v["final_values"]["sides"] == {"p1": "NEW", "p2": "NEW"}
    print("  1 side committed with receipt lost -> recovery re-acknowledged; repeat is no-op")
    c.cleanup()


def scenario_6(_args):
    """Repeated recovery does not change business state."""
    c = Ctx("repeat-recover")
    path, req = c.request("realistic", 13)
    cli(c.base, "submit", "--request-file", path, "--fault", "before-decision", rc=70)
    cli(c.base, "recover", "--txid", req["txid"])
    snap = {n: business(c.base, n) for n in ("p1", "p2")}
    audit_before = _audit_counts(c.base)
    for _ in range(5):
        out = cli(c.base, "recover", "--txid", req["txid"])
        assert out["recovered"] == []
    assert {n: business(c.base, n) for n in ("p1", "p2")} == snap
    assert _audit_counts(c.base) == audit_before
    v = c.verify(req["txid"], path)
    print("  5 extra recoveries -> business and audit rows unchanged")
    c.cleanup()


def _audit_counts(base: str) -> dict:
    out = {}
    for name in ("p1", "p2"):
        conn = sqlite3.connect(os.path.join(base, f"participant-{name}.sqlite3"))
        try:
            out[name] = conn.execute("SELECT COUNT(*) FROM change_audit").fetchone()[0]
        finally:
            conn.close()
    return out


SCENARIOS = {
    1: ("one side YES, one side NO", scenario_1),
    2: ("both prepared then coordinator exits", scenario_2),
    3: ("COMMIT saved, only one side notified", scenario_3),
    4: ("COMMIT saved, no side notified", scenario_4),
    5: ("participant committed, receipt lost", scenario_5),
    6: ("repeated recovery", scenario_6),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", type=int, choices=sorted(SCENARIOS))
    args = ap.parse_args()
    todo = [args.scenario] if args.scenario else sorted(SCENARIOS)
    failures = 0
    for number in todo:
        title, fn = SCENARIOS[number]
        print(f"scenario {number}: {title}")
        try:
            fn(args)
        except Exception as exc:  # noqa: BLE001 - report and continue
            failures += 1
            print(f"  FAILED: {exc}")
    print(f"\n{len(todo) - failures}/{len(todo)} scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
