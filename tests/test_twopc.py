"""End-to-end tests for the two-phase-commit demo.

Run with:  python3 -m unittest discover -s tests -v

Fault-injection scenarios must run in subprocesses because the injected crash
is a real process death (os._exit), so each scenario drives the public CLI.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from twopc.coordinator import Coordinator, read_coordinator_txns  # noqa: E402
from twopc.driver import Driver  # noqa: E402
from twopc.layout import layout, participant_paths  # noqa: E402
from twopc.participant import Participant, read_participant_status  # noqa: E402
from twopc.request import validate_request  # noqa: E402
from twopc.util import DemoError  # noqa: E402
from twopc.digest import decision_digest, request_digest, updates_digest  # noqa: E402


def run_cli(base: str, *argv: str, expect_rc: int | None = 0,
            input_text: str | None = None) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "twopc", "--base", base, *argv],
        cwd=ROOT, text=True, capture_output=True, input=input_text,
        timeout=120,
    )
    if expect_rc is not None and proc.returncode != expect_rc:
        raise AssertionError(
            f"command {argv} rc={proc.returncode} expected {expect_rc}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    out = proc.stdout.strip()
    if not out:
        return {"_rc": proc.returncode, "_stderr": proc.stderr}
    return json.loads(out.splitlines()[-1])


def business(path: str) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute("SELECT key, value FROM business").fetchall())
    finally:
        conn.close()


def read_request(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class DemoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="twopc-test-")
        self.base = os.path.join(self.tmp, "demo")
        run_cli(self.base, "init", "--force")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def generate(self, kind="basic", seed=7) -> list[dict]:
        out = run_cli(self.base, "generate", "--kind", kind, "--seed", str(seed),
                      "--count", "1")
        return out["requests"]

    def submit(self, request_path: str, *faults: str, expect_rc=0) -> dict:
        argv = ["submit", "--request-file", request_path]
        for f in faults:
            argv += ["--fault", f]
        return run_cli(self.base, *argv, expect_rc=expect_rc)

    def verify_ok(self, txid: str, request_path: str | None = None) -> dict:
        argv = ["verify", txid]
        if request_path:
            argv += ["--request-file", request_path]
        else:
            argv += ["--use-recorded-request"]
        return run_cli(self.base, *argv, expect_rc=0)

    def recover(self, txid: str | None = None) -> dict:
        argv = ["recover"]
        if txid:
            argv += ["--txid", txid]
        return run_cli(self.base, *argv, expect_rc=0)

    def status(self, txid: str) -> dict:
        return run_cli(self.base, "participant-status", txid)


class TestRequestValidation(DemoCase):
    def test_duplicate_key_on_side_rejected(self):
        req = {"txid": "t1", "participants": {
            "p1": [{"key": "a", "expected": 1, "new": 2},
                   {"key": "a", "expected": 1, "new": 3}],
            "p2": [],
        }}
        with self.assertRaises(DemoError) as ctx:
            validate_request(req)
        self.assertIn("duplicate key", str(ctx.exception))

    def test_more_than_ten_keys_rejected(self):
        req = {"txid": "t1", "participants": {
            "p1": [{"key": f"k{i}", "expected": "ABSENT", "new": i}
                   for i in range(11)],
            "p2": [],
        }}
        with self.assertRaises(DemoError):
            validate_request(req)

    def test_ten_keys_accepted(self):
        req = {"txid": "t1", "participants": {
            "p1": [{"key": f"k{i}", "expected": "ABSENT", "new": i}
                   for i in range(10)],
            "p2": [],
        }}
        validate_request(req)  # must not raise

    def test_absent_is_valid_sentinel(self):
        validate_request({"txid": "t1", "participants": {
            "p1": [{"key": "a", "expected": "ABSENT", "new": 5}],
            "p2": [],
        }})

    def test_non_integer_values_rejected(self):
        for bad in ("five", 1.5, True, None):
            with self.assertRaises(DemoError):
                validate_request({"txid": "t1", "participants": {
                    "p1": [{"key": "a", "expected": "ABSENT", "new": bad}],
                    "p2": [],
                }})

    def test_unknown_endpoint_rejected(self):
        with self.assertRaises(DemoError):
            validate_request({"txid": "t1", "participants": {
                "p1": [], "p2": [], "p3": [],
            }})

    def test_illegal_request_reaches_protocol_and_aborts(self):
        # A request that violates the shape rules is refused by prepare with
        # NO, so the protocol still ends in a clean ABORT (no visible change).
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w") as fh:
            json.dump({"txid": "bad-shape", "participants": {
                "p1": [{"key": "d", "expected": "ABSENT", "new": 1},
                       {"key": "d", "expected": "ABSENT", "new": 2}],
                "p2": [],
            }}, fh)
        out = self.submit(path)
        self.assertEqual(out["prepare"]["p1"]["decision"], "NO")
        self.assertTrue(out["prepare"]["p1"].get("invalid"))
        self.assertEqual(out["decision"]["decision"], "ABORT")
        self.assertEqual(business(os.path.join(self.base, "participant-p1.sqlite3")).get("d"),
                         None)


class TestHappyPath(DemoCase):
    def test_commit_both_sides(self):
        req = self.generate()[0]
        path = req["path"]
        request = read_request(path)
        before = {n: business(os.path.join(self.base, f"participant-{n}.sqlite3"))
                  for n in ("p1", "p2")}
        out = self.submit(path)
        self.assertEqual(out["decision"]["decision"], "COMMIT")
        self.assertTrue(out["delivery_complete"])
        self.assertTrue(out["result"]["complete"])
        v = self.verify_ok(request["txid"], path)
        self.assertTrue(v["verify"]["ok"], v["verify"]["failures"])
        self.assertEqual(v["final_values"]["sides"], {"p1": "NEW", "p2": "NEW"})
        after = {n: business(os.path.join(self.base, f"participant-{n}.sqlite3"))
                 for n in ("p1", "p2")}
        for side in ("p1", "p2"):
            for item in request["participants"][side]:
                self.assertEqual(after[side][item["key"]], item["new"])
                self.assertEqual(before[side].get(item["key"]),
                                 None if item["expected"] == "ABSENT"
                                 else item["expected"])

    def test_three_independent_files_and_wal(self):
        paths = layout(self.base)
        for key in ("coordinator", "p1", "p2"):
            self.assertTrue(os.path.exists(paths[key]))
        with sqlite3.connect(paths["p1"]) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        # No ATTACH statement anywhere in the implementation (prose mentions
        # of the word in docstrings are allowed).
        for name in os.listdir(os.path.join(ROOT, "twopc")):
            if name.endswith(".py"):
                with open(os.path.join(ROOT, "twopc", name)) as fh:
                    text = fh.read()
                self.assertIsNone(
                    re.search(r"ATTACH\s+(DATABASE|[\"'])", text, re.I),
                    f"{name} contains an ATTACH statement",
                )

    def test_generate_is_deterministic(self):
        base2 = os.path.join(self.tmp, "demo2")
        run_cli(base2, "init", "--force")
        a = run_cli(self.base, "generate", "--kind", "coverage", "--seed", "3")
        b = run_cli(base2, "generate", "--kind", "coverage", "--seed", "3")
        ra = read_request(a["requests"][0]["path"])
        rb = read_request(b["requests"][0]["path"])
        self.assertEqual(ra, rb)
        self.assertEqual(len(ra["participants"]["p1"]), 10)
        self.assertTrue(any(i["expected"] == "ABSENT" for i in ra["participants"]["p1"]))

    def test_coverage_workload_commits(self):
        req = self.generate("coverage", 9)[0]
        out = self.submit(req["path"])
        self.assertTrue(out["delivery_complete"])
        v = self.verify_ok(req["txid"], req["path"])
        self.assertTrue(v["verify"]["ok"], v["verify"]["failures"])


class TestPartialPrepare(DemoCase):
    def test_yes_no_aborts_cleanly(self):
        path = os.path.join(self.tmp, "req.json")
        with open(path, "w") as fh:
            json.dump({"txid": "yn", "participants": {
                "p1": [{"key": "a", "expected": 1, "new": 10},
                       {"key": "new-key", "expected": "ABSENT", "new": 5}],
                "p2": [{"key": "x", "expected": 999, "new": 20}],
            }}, fh)
        out = self.submit(path)
        self.assertEqual(out["prepare"]["p1"]["decision"], "YES")
        self.assertEqual(out["prepare"]["p2"]["decision"], "NO")
        self.assertEqual(out["decision"]["decision"], "ABORT")
        # p1 prepared then aborted: business unchanged, no claims.
        self.assertEqual(business(os.path.join(self.base, "participant-p1.sqlite3")),
                         {"a": 1, "b": 2, "c": 3, "checking": 1000,
                          "savings": 500, "reserve": 250})
        st = self.status("yn")
        p1 = st["participants"][0]
        self.assertEqual(p1["state"], "ABORTED")
        self.assertEqual(p1["reservations"], [])
        self.assertEqual(st["participants"][1]["state"], None)
        v = self.verify_ok("yn", path)
        self.assertTrue(v["verify"]["ok"], v["verify"]["failures"])
        self.assertEqual(v["final_values"]["sides"],
                         {"p1": "OLD", "p2": "UNCHANGED"})

    def test_no_prepare_modifies_nothing(self):
        path = os.path.join(self.tmp, "req.json")
        with open(path, "w") as fh:
            json.dump({"txid": "no", "participants": {
                "p1": [{"key": "a", "expected": 5, "new": 9}],
                "p2": [{"key": "x", "expected": 5, "new": 9}],
            }}, fh)
        out = self.submit(path)
        self.assertEqual(out["decision"]["decision"], "ABORT")
        self.assertEqual(business(os.path.join(self.base, "participant-p1.sqlite3"))["a"], 1)
        self.assertEqual(business(os.path.join(self.base, "participant-p2.sqlite3"))["x"], 1)


class TestCoordinatorCrash(DemoCase):
    def test_crash_after_both_prepared_records_abort(self):
        req = self.generate("realistic", 11)[0]
        # Coordinator exits before writing any decision.
        self.submit(req["path"], "before-decision", expect_rc=70)
        txns = read_coordinator_txns(layout(self.base)["coordinator"])
        self.assertEqual(txns[0]["state"], "PREPARED_BOTH")
        self.assertIsNone(txns[0]["decision"])
        st = self.status(req["txid"])
        for p in st["participants"]:
            self.assertEqual(p["state"], "PREPARED")
            self.assertTrue(p["reservations"])

        rec = self.recover(req["txid"])
        entry = rec["recovered"][0]
        self.assertIn("recorded ABORT", entry["action"])
        self.assertEqual(entry["state_after"], "COMPLETE")
        v = self.verify_ok(req["txid"], req["path"])
        self.assertTrue(v["verify"]["ok"], v["verify"]["failures"])
        self.assertEqual(v["final_values"]["sides"],
                         {"p1": "OLD", "p2": "OLD"})
        # No lingering claims.
        self.assertEqual(run_cli(self.base, "locks")["unresolved"], 0)

    def test_crash_after_one_prepare_then_recover(self):
        req = self.generate()[0]
        self.submit(req["path"], "after-prepare:p1", expect_rc=0)
        # The coordinator saw UNKNOWN for p1 and must ABORT.  Because it then
        # delivered ABORT, p1's durable prepared claim was resolved safely.
        out = run_cli(self.base, "result", req["txid"])
        self.assertEqual(out["decision"], "ABORT")
        st = self.status(req["txid"])["participants"][0]
        self.assertEqual(st["state"], "ABORTED")
        self.assertEqual(st["reservations"], [])
        self.assertEqual(run_cli(self.base, "locks")["unresolved"], 0)
        v = self.verify_ok(req["txid"], req["path"])
        self.assertTrue(v["verify"]["ok"], v["verify"]["failures"])
        self.assertEqual(business(os.path.join(self.base, "participant-p1.sqlite3"))["a"], 1)

    def test_commit_saved_only_one_notified(self):
        req = self.generate()[0]
        self.submit(req["path"], "after-first-delivery", expect_rc=70)
        row = read_coordinator_txns(layout(self.base)["coordinator"])[0]
        self.assertEqual(row["decision"], "COMMIT")
        st = self.status(req["txid"])["participants"]
        self.assertEqual(st[0]["state"], "COMMITTED")
        self.assertEqual(st[1]["state"], "PREPARED")
        self.assertTrue(st[1]["reservations"])
        # Not complete yet: the coordinator must not claim success.
        res = run_cli(self.base, "result", req["txid"])
        self.assertFalse(res["complete"])
        self.assertIn("NOT complete", res["message"])

        rec = self.recover(req["txid"])
        entry = rec["recovered"][0]
        self.assertIn("kept recorded COMMIT", entry["action"])
        self.assertEqual(entry["state_after"], "COMPLETE")
        st_after = self.status(req["txid"])["participants"]
        self.assertEqual({p["state"] for p in st_after}, {"COMMITTED"})
        v = self.verify_ok(req["txid"], req["path"])
        self.assertTrue(v["verify"]["ok"], v["verify"]["failures"])
        self.assertEqual(v["final_values"]["sides"], {"p1": "NEW", "p2": "NEW"})

    def test_commit_saved_nothing_notified(self):
        req = self.generate()[0]
        self.submit(req["path"], "after-decision", expect_rc=70)
        row = read_coordinator_txns(layout(self.base)["coordinator"])[0]
        self.assertEqual(row["decision"], "COMMIT")
        st = self.status(req["txid"])["participants"]
        self.assertEqual({p["state"] for p in st}, {"PREPARED"})
        self.recover(req["txid"])
        v = self.verify_ok(req["txid"], req["path"])
        self.assertTrue(v["verify"]["ok"], v["verify"]["failures"])
        self.assertEqual(v["final_values"]["sides"], {"p1": "NEW", "p2": "NEW"})

    def test_abort_saved_nothing_notified(self):
        path = os.path.join(self.tmp, "req.json")
        with open(path, "w") as fh:
            json.dump({"txid": "ab", "participants": {
                "p1": [{"key": "a", "expected": 1, "new": 10}],
                "p2": [{"key": "x", "expected": 7, "new": 20}],
            }}, fh)
        self.submit(path, "after-decision", expect_rc=70)
        row = read_coordinator_txns(layout(self.base)["coordinator"])[0]
        self.assertEqual(row["decision"], "ABORT")
        self.recover("ab")
        v = self.verify_ok("ab", path)
        self.assertTrue(v["verify"]["ok"], v["verify"]["failures"])
        self.assertEqual(business(os.path.join(self.base, "participant-p1.sqlite3"))["a"], 1)


class TestParticipantFaults(DemoCase):
    def test_commit_applied_but_receipt_lost(self):
        req = self.generate("realistic", 5)[0]
        # p1 commits locally, then dies before replying.
        self.submit(req["path"], "after-apply:p1", expect_rc=0)
        st = self.status(req["txid"])["participants"]
        self.assertEqual(st[0]["state"], "COMMITTED")
        # Verify the business update is durable despite the lost receipt.
        self.assertEqual(business(os.path.join(self.base, "participant-p1.sqlite3"))["checking"],
                         read_request(req["path"])["participants"]["p1"][0]["new"])
        # The first recovery completes the request; a second one finds nothing
        # to do and reports it as already_complete (idempotent).
        self.recover(req["txid"])
        rec = self.recover(req["txid"])
        self.assertEqual(rec["recovered"], [])
        self.assertEqual(rec["already_complete"][0]["action"],
                         "nothing to do; already COMPLETE")
        v = self.verify_ok(req["txid"], req["path"])
        self.assertTrue(v["verify"]["ok"], v["verify"]["failures"])
        self.assertEqual(v["final_values"]["sides"], {"p1": "NEW", "p2": "NEW"})

    def test_prepare_crash_before_reply(self):
        req = self.generate()[0]
        # p1 prepares durably, then crashes; its YES never reaches coordinator.
        # The coordinator sees UNKNOWN (not YES), so it safely aborts; the
        # delivered ABORT then resolves p1's prepared claim.
        self.submit(req["path"], "after-prepare:p1", expect_rc=0)
        txns = read_coordinator_txns(layout(self.base)["coordinator"])
        self.assertEqual(txns[0]["decision"], "ABORT")
        st = self.status(req["txid"])["participants"][0]
        self.assertEqual(st["state"], "ABORTED")
        self.assertEqual(st["reservations"], [])
        self.assertEqual(business(os.path.join(self.base, "participant-p1.sqlite3"))["a"], 1)

    def test_prepare_crash_then_coordinator_crash_leaves_prepared(self):
        req = self.generate()[0]
        # p1 prepared then died; the coordinator also crashed before deciding,
        # so p1 remains PREPARED with a durable claim until recovery.
        self.submit(req["path"], "after-prepare:p1", "before-decision", expect_rc=70)
        txns = read_coordinator_txns(layout(self.base)["coordinator"])
        self.assertIsNone(txns[0]["decision"])
        st = self.status(req["txid"])["participants"]
        self.assertEqual(st[0]["state"], "PREPARED")
        self.assertTrue(st[0]["reservations"])
        self.recover(req["txid"])
        st = self.status(req["txid"])["participants"]
        self.assertEqual({p["state"] for p in st}, {"ABORTED"})
        self.assertEqual(run_cli(self.base, "locks")["unresolved"], 0)
        v = self.verify_ok(req["txid"], req["path"])
        self.assertTrue(v["verify"]["ok"], v["verify"]["failures"])

    def test_recovery_twice_is_idempotent(self):
        req = self.generate()[0]
        self.submit(req["path"], "before-decision", expect_rc=70)
        self.recover(req["txid"])
        before = {n: business(os.path.join(self.base, f"participant-{n}.sqlite3"))
                  for n in ("p1", "p2")}
        first = self.recover(req["txid"])
        after_first = {n: business(os.path.join(self.base, f"participant-{n}.sqlite3"))
                       for n in ("p1", "p2")}
        second = self.recover(req["txid"])
        after_second = {n: business(os.path.join(self.base, f"participant-{n}.sqlite3"))
                         for n in ("p1", "p2")}
        self.assertEqual(before, after_first)
        self.assertEqual(after_first, after_second)
        self.assertEqual([e["txid"] for e in first["already_complete"]], [req["txid"]])
        self.assertEqual([e["txid"] for e in second["already_complete"]], [req["txid"]])
        # No additional log application events on replay.
        logs = run_cli(self.base, "journal", "--txid", req["txid"])
        applied = [r for r in logs["coordinator_log"]
                   if r["event"] in ("DECIDED_ABORT",)]
        self.assertEqual(len(applied), 1)


class TestIdempotencyAndReuse(DemoCase):
    def test_repeat_prepare_commit_abort(self):
        req = self.generate()[0]
        self.submit(req["path"])
        lay = layout(self.base)
        coord = Coordinator(lay["coordinator"])
        try:
            row = coord.request_row(req["txid"])
            drv = Driver(coord, participant_paths(lay), lay["traces"])
            payload = coord.payload(req["txid"])
            r1 = drv.deliver(req["txid"])
            r2 = drv.deliver(req["txid"])
            self.assertTrue(all(v.get("replayed") for v in r2["participants"].values()))
            for name in ("p1", "p2"):
                p = Participant(name, lay[name])
                try:
                    replay = p.prepare(req["txid"], payload["participants"][name],
                                       payload["request_digest"])
                    self.assertEqual(replay["decision"], "YES")
                    self.assertEqual(replay["state"], "COMMITTED")
                    self.assertTrue(replay["replayed"])
                finally:
                    p.close()
        finally:
            coord.close()

    def test_txid_reuse_with_different_content_rejected(self):
        req = self.generate()[0]
        path = req["path"]
        self.submit(path)
        req2 = read_request(path)
        req2["participants"]["p1"][0]["new"] += 1
        path2 = os.path.join(self.tmp, "changed.json")
        with open(path2, "w") as fh:
            json.dump(req2, fh)
        out = run_cli(self.base, "submit", "--request-file", path2, expect_rc=2)
        self.assertEqual(out["code"], "TXID_CONTENT_MISMATCH")

    def test_repeat_identical_submit_is_idempotent(self):
        req = self.generate()[0]
        self.submit(req["path"])
        before = business(os.path.join(self.base, "participant-p1.sqlite3"))
        out = self.submit(req["path"])
        self.assertTrue(out["register"]["replayed"])
        self.assertTrue(out["delivery_complete"])
        self.assertEqual(business(os.path.join(self.base, "participant-p1.sqlite3")),
                         before)


class TestClaims(DemoCase):
    def test_ordinary_write_blocked_by_prepared_claim(self):
        req = self.generate("realistic", 11)[0]
        self.submit(req["path"], "before-decision", expect_rc=70)
        out = run_cli(self.base, "ordinary-set", "--participant", "p1",
                      "--key", "checking", "--value", "777", expect_rc=2)
        self.assertIn("reserved", out["error"])
        self.assertEqual(business(os.path.join(self.base, "participant-p1.sqlite3"))["checking"],
                         1000)
        run_cli(self.base, "recover", "--txid", req["txid"])
        # After resolution the key is writable again.
        run_cli(self.base, "ordinary-set", "--participant", "p1",
                "--key", "checking", "--value", "1200")
        self.assertEqual(business(os.path.join(self.base, "participant-p1.sqlite3"))["checking"],
                         1200)

    def test_second_transaction_cannot_prepare_reserved_key(self):
        req = self.generate("realistic", 11)[0]
        self.submit(req["path"], "before-decision", expect_rc=70)
        conflict = {"txid": "overlap", "participants": {
            "p1": [{"key": "checking", "expected": 1000, "new": 9999}],
            "p2": [{"key": "partner-checking", "expected": 800, "new": 8888}],
        }}
        p = os.path.join(self.tmp, "overlap.json")
        with open(p, "w") as fh:
            json.dump(conflict, fh)
        out = self.submit(p)
        self.assertEqual(out["prepare"]["p1"]["decision"], "NO")
        self.assertIn("reserved", out["prepare"]["p1"]["reason"])
        self.assertEqual(out["decision"]["decision"], "ABORT")
        self.assertEqual(business(os.path.join(self.base, "participant-p1.sqlite3"))["checking"],
                         1000)
        run_cli(self.base, "recover", "--txid", "overlap")

    def test_locks_explains_claims(self):
        req = self.generate("realistic", 11)[0]
        self.submit(req["path"], "before-decision", expect_rc=70)
        locks = run_cli(self.base, "locks")
        self.assertTrue(locks["reservations"])
        self.assertGreaterEqual(locks["unresolved"], 3)
        entry = locks["reservations"][0]
        for field in ("key", "txid", "participant_state",
                      "coordinator_state", "explanation"):
            self.assertIn(field, entry)
        self.assertIn("recover", entry["explanation"]["what_to_do"])
        run_cli(self.base, "recover", "--txid", req["txid"])
        self.assertEqual(run_cli(self.base, "locks")["unresolved"], 0)


class TestDecisionIntegrity(DemoCase):
    def test_decision_cannot_be_flipped(self):
        req = self.generate()[0]
        self.submit(req["path"])
        lay = layout(self.base)
        coord = Coordinator(lay["coordinator"])
        try:
            with self.assertRaises(DemoError) as ctx:
                coord.decide(req["txid"], "ABORT")
            self.assertEqual(ctx.exception.code, "CONTRADICTORY_DECISION")
            row = coord.request_row(req["txid"])
            self.assertEqual(row["decision"], "COMMIT")
            self.assertEqual(row["decision_order"], 1)
        finally:
            coord.close()

    def test_participant_rejects_forged_decision(self):
        req = self.generate()[0]
        self.submit(req["path"], "after-decision", expect_rc=70)
        lay = layout(self.base)
        coord = Coordinator(lay["coordinator"])
        try:
            row = coord.request_row(req["txid"])
            with Participant("p1", lay["p1"]) as p:
                with self.assertRaises(DemoError) as ctx:
                    p.apply_decision(req["txid"], row["request_digest"],
                                     row["decision"], row["decision_order"],
                                     "0" * 64)
                self.assertEqual(ctx.exception.code, "BAD_DECISION_DIGEST")
                # Still prepared, nothing changed, claim intact.
                self.assertEqual(p.txn_row(req["txid"])["state"], "PREPARED")
                self.assertEqual(p.get_value("a"), 1)
                self.assertTrue(p.reservations_for(req["txid"]))
        finally:
            coord.close()

    def test_digest_formula_is_shared(self):
        # A participant can recompute the coordinator's digest locally.
        reqd = request_digest({"txid": "t", "participants": {"p1": [], "p2": []}})
        self.assertEqual(len(reqd), 64)
        self.assertEqual(updates_digest([]), updates_digest([]))
        self.assertEqual(
            decision_digest("t", reqd, "COMMIT", 1),
            decision_digest("t", reqd, "COMMIT", 1),
        )
        self.assertNotEqual(
            decision_digest("t", reqd, "COMMIT", 1),
            decision_digest("t", reqd, "COMMIT", 2),
        )


class TestRestartPersistence(DemoCase):
    def test_state_survives_new_processes(self):
        req = self.generate()[0]
        self.submit(req["path"], "before-decision", expect_rc=70)
        # Each of these reads from a fresh process / fresh connection.
        for _ in range(3):
            st = self.status(req["txid"])
            self.assertEqual({p["state"] for p in st["participants"]}, {"PREPARED"})
        rec = self.recover(req["txid"])
        self.assertEqual(rec["recovered"][0]["state_after"], "COMPLETE")
        for _ in range(3):
            st = self.status(req["txid"])
            self.assertEqual({p["state"] for p in st["participants"]}, {"ABORTED"})

    def test_prepared_is_not_auto_rolled_back(self):
        req = self.generate()[0]
        self.submit(req["path"], "before-decision", expect_rc=70)
        # Reopening repeatedly must leave it PREPARED, not silently abort it.
        for _ in range(5):
            st = self.status(req["txid"])
            self.assertEqual({p["state"] for p in st["participants"]}, {"PREPARED"})
            self.assertTrue(all(p["reservations"] for p in st["participants"]))
        # Only an explicit recover (with the coordinator's recorded decision)
        # resolves it.
        self.recover(req["txid"])


if __name__ == "__main__":
    unittest.main()
