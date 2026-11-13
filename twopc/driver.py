"""Drives both participants through independent subprocesses.

The coordinator records a durable decision *before* calling :meth:`deliver`.
Delivery is therefore always "do what the saved decision says"; it never
re-derives a decision from the replies.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Optional

from .coordinator import (
    ABORTED,
    COMMITTED,
    COMPLETE,
    Coordinator,
)
from .util import DemoError, crash_if, info, stamp

WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = [sys.executable, "-m", "twopc.worker"]

# How many times a transport failure is retried in-process.  Crucially, a
# transport failure is NOT a reason to change a decision: the recorded
# decision is re-delivered, never replaced by the opposite outcome.
DELIVERY_ATTEMPTS = 3


def _run_worker(argv: list[str], stdin_payload: Optional[dict] = None,
                crash_at: Optional[str] = None, timeout: float = 30.0,
                env_extra: Optional[dict[str, str]] = None) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = WORKSPACE_ROOT + os.sep + env.get("PYTHONPATH", "")
    if crash_at:
        env["TWOPC_CRASH_AT"] = crash_at
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        WORKER + argv,
        input=json.dumps(stdin_payload) if stdin_payload is not None else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
        cwd=WORKSPACE_ROOT,
    )
    stdout = proc.stdout.strip()
    if not stdout:
        raise DemoError(
            f"participant worker produced no reply (rc={proc.returncode}): "
            f"stderr={proc.stderr.strip()!r}",
            code="WORKER_FAILURE",
        )
    try:
        reply = json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError as exc:
        raise DemoError(f"participant worker produced invalid JSON: {exc}",
                        code="WORKER_FAILURE") from exc
    reply["_worker_rc"] = proc.returncode
    if proc.returncode != 0 or not reply.get("ok", True):
        # Preserve the worker's own error code (NOT_PREPARED, bad digest, ...)
        # instead of flattening every failure into a transport error.
        raise DemoError(
            reply.get("error") or f"worker failed (rc={proc.returncode})",
            code=reply.get("code") or "WORKER_FAILURE",
        )
    return reply


def worker_prepare(name: str, db_path: str, txid: str,
                   updates: list[dict], req_digest: str,
                   crash_at: Optional[str] = None) -> dict:
    return _run_worker(
        ["prepare", "--db", db_path, "--name", name, "--txid", txid],
        stdin_payload={"updates": updates, "request_digest": req_digest},
        crash_at=crash_at,
    )


def worker_apply(name: str, db_path: str, txid: str, req_digest: str,
                 decision: str, order: int, dmac: str,
                 crash_at: Optional[str] = None) -> dict:
    return _run_worker(
        ["apply", "--db", db_path, "--name", name, "--txid", txid,
         "--request-digest", req_digest, "--decision", decision,
         "--order", str(order), "--decision-digest", dmac],
        crash_at=crash_at,
    )


class Driver:
    """Orchestration on top of a :class:`Coordinator` and a file layout."""

    def __init__(self, coordinator: Coordinator, paths: dict[str, str],
                 traces_dir: str):
        self.coord = coordinator
        self.paths = paths
        self.traces_dir = traces_dir
        os.makedirs(traces_dir, exist_ok=True)

    # -- logging --------------------------------------------------------

    def _runs_path(self, txid: str) -> str:
        return os.path.join(self.traces_dir, f"{txid}-runs.jsonl")

    def _record_run(self, txid: str, phase: str, name: str, reply: dict) -> None:
        with open(self._runs_path(txid), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": stamp(), "phase": phase, "participant": name,
                "reply": reply,
            }, sort_keys=True, ensure_ascii=False) + "\n")

    # -- prepare phase ---------------------------------------------------

    def prepare_all(self, txid: str, crash_at: Optional[str] = None) -> dict:
        """Fan out prepare to both endpoints; return per-side replies.

        There is no shared transaction and no ordering between the two calls:
        one side may be YES while the other is NO, which is exactly the partial
        prepare case the protocol must abort.

        crash_at maps an endpoint name to a crash point, e.g.
        {"p1": "p1-after-prepare"}.
        """
        replies = {}
        for name in ("p1", "p2"):
            point = None if crash_at is None else crash_at.get(name)
            replies[name] = self._prepare_one(txid, name, point)
        return replies

    def _prepare_one(self, txid: str, name: str,
                     crash_at: Optional[str]) -> dict:
        """Prepare one side and persist its returned decision.

        A transport failure (including a simulated process death) leaves the
        prepare outcome genuinely UNKNOWN to the coordinator.  We persist that
        as "UNKNOWN", never as YES, and never assume the participant rolled
        itself back.
        """
        payload = self.coord.payload(txid)
        updates = payload["participants"][name]
        try:
            reply = worker_prepare(
                name, self.paths[name], txid, updates,
                payload["request_digest"], crash_at=crash_at,
            )
        except DemoError as exc:
            reply = {
                "ok": False,
                "decision": "UNKNOWN",
                "reason": exc.message,
                "code": exc.code,
            }
        prepare = reply.get("decision", "UNKNOWN")
        if prepare == "YES":
            # A replay of an already-finished transaction reports its terminal
            # state, not PREPARED.
            reported = reply.get("state")
            local_state = reported if reported in ("PREPARED", "COMMITTED") \
                else "PREPARED"
        elif prepare == "NO":
            local_state = "NOT_PREPARED"
        else:
            local_state = "UNKNOWN"
        self.coord._record_reply(
            txid, name,
            "PENDING" if prepare == "UNKNOWN" else prepare,
            local_state,
            reason=reply.get("reason"),
        )
        self._record_run(txid, "prepare", name, reply)
        info(f"[prepare] {txid} {name} -> {prepare}")
        return reply

    # -- decision --------------------------------------------------------

    def choose_decision(self, replies: dict) -> str:
        """COMMIT only when both sides answered YES.

        An UNKNOWN reply (transport failure) is not a YES.  The coordinator's
        safe reaction to a fully prepared pair is COMMIT; to anything less,
        ABORT.
        """
        return "COMMIT" if both_yes(replies) else "ABORT"

    def record_decision(self, txid: str) -> dict:
        """Record the decision derived from persisted replies and return it."""
        row = self.coord.request_row(txid)
        if row["decision"] is not None:
            return {
                "txid": txid, "decision": row["decision"],
                "order": row["decision_order"],
                "decision_digest": row["decision_digest"], "replayed": True,
            }
        replies = {
            r["participant"]: {"decision": r["prepare"]}
            for r in self.coord.conn.execute(
                "SELECT participant, prepare FROM participant_reply WHERE txid=?",
                (txid,),
            )
        }
        d = self.choose_decision(replies)
        return self.coord.decide(txid, d)

    # -- delivery --------------------------------------------------------

    def deliver(self, txid: str, *, crash_at: Optional[str] = None,
                max_attempts: int = DELIVERY_ATTEMPTS) -> dict:
        """Deliver the *already recorded* decision to both participants.

        crash_at maps participant names to a crash point for the worker call
        (e.g. ``{"p1": "p1-after-commit"}``).  A crashed/lost reply simply
        leaves the participant's state unknown to the coordinator; a later
        recovery redelivers.
        """
        row = self.coord.request_row(txid)
        if row["decision"] is None:
            raise DemoError(
                f"refusing to deliver {txid!r}: no decision is recorded",
                code="DECISION_NOT_SAVED",
            )
        decision = row["decision"]
        req_digest = row["request_digest"]
        order = row["decision_order"]
        dmac = row["decision_digest"]
        target_state = COMMITTED if decision == "COMMIT" else ABORTED
        outcome = {"txid": txid, "decision": decision, "order": order,
                   "participants": {}}

        for index, name in enumerate(("p1", "p2")):
            point = None if crash_at is None else crash_at.get(name)
            outcome["participants"][name] = self._deliver_one(
                txid, name, decision, req_digest, order, dmac,
                target_state, point, max_attempts,
            )
            if index == 0:
                # Simulates the coordinator dying after notifying exactly one
                # side.  The other side remains PREPARED until recovery.
                crash_if("coordinator-after-first-delivery")

        def finished(part: dict) -> bool:
            if part.get("state") == target_state:
                return True
            return decision == "ABORT" and part.get("state") == "NOT_PREPARED"

        if all(finished(outcome["participants"][n]) for n in ("p1", "p2")):
            self.coord.mark_complete(txid)
            outcome["complete"] = True
        else:
            # An abort also leaves participants that never prepared finished.
            outcome["complete"] = False
        return outcome

    def _deliver_one(self, txid: str, name: str, decision: str,
                     req_digest: str, order: int, dmac: str,
                     target_state: str, crash_at: Optional[str],
                     max_attempts: int) -> dict:
        final = None
        for attempt in range(1, max_attempts + 1):
            # Re-read the recorded decision before *each* attempt.  Delivery is
            # always a read of the durable decision, never a local choice.
            current = self.coord.request_row(txid)
            if current["decision"] != decision:
                raise DemoError(
                    f"decision for {txid!r} changed from {decision} to "
                    f"{current['decision']}; refusing to continue",
                    code="DECISION_CHANGED",
                )
            point = crash_at
            if point == "after-apply":
                point = (f"{name}-after-commit" if decision == "COMMIT"
                         else f"{name}-after-abort")
            try:
                reply = worker_apply(
                    name, self.paths[name], txid, req_digest, decision,
                    order, dmac, crash_at=point,
                )
            except DemoError as exc:
                reply = {"ok": False, "code": exc.code, "reason": exc.message}
            self._record_run(txid, f"deliver-{decision}", name, reply)
            if reply.get("ok") and reply.get("state") == target_state:
                self.coord._record_reply(txid, name, "YES", target_state,
                                         order=order)
                return reply
            if reply.get("code") == "NOT_PREPARED" and decision == "ABORT":
                # A side that answered NO (or was never prepared) has nothing
                # to undo.  Delivering ABORT there is a no-op and the side is
                # legitimately finished.
                self.coord._record_reply(txid, name, "NO", ABORTED,
                                         order=order)
                return {
                    "ok": True, "state": "NOT_PREPARED",
                    "nothing_to_undo": True,
                }
            if reply.get("code") == "CONTRADICTORY_DECISION":
                return reply
            info(f"[deliver] {txid} {name} attempt {attempt} "
                 f"failed: {reply.get('reason') or reply.get('code')}")
        return final or {
            "ok": False, "state": "UNKNOWN",
            "reason": "all delivery attempts failed (transport)",
        }

    # -- the happy-path submit ------------------------------------------

    def submit(self, request: dict, crash_at_during: Optional[dict[str, str]] = None,
               crash_after_decision: Optional[str] = None) -> dict:
        """Full submit: register, prepare both, decide, deliver.

        ``crash_at_during`` may contain:
          "prepare.p1"/"prepare.p2": crash the worker after prepare
          "deliver.p1"/"deliver.p2": crash the worker after commit/abort
        ``crash_after_decision`` may be "coordinator-after-decision": the
        coordinator process exits after persisting COMMIT but before any
        delivery.
        """
        reg = self.coord.register(request)
        txid = reg["txid"]
        out = {"register": reg}

        kill_prepare_at = None
        if crash_at_during:
            kill_prepare_at = {
                n: crash_at_during.get(f"prepare.{n}")
                for n in ("p1", "p2")
                if crash_at_during.get(f"prepare.{n}")
            } or None
        replies = self.prepare_all(txid, crash_at=kill_prepare_at)
        out["prepare"] = replies
        # Simulates the coordinator dying after both participants prepared but
        # before any decision exists.  Recovery must record ABORT.
        crash_if("coordinator-after-prepare")
        decision = self.record_decision(txid)
        out["decision"] = decision

        if crash_after_decision == "coordinator-after-decision":
            crash_if("coordinator-after-decision")

        kill_deliver_at = None
        if crash_at_during:
            kill_deliver_at = {
                n: crash_at_during.get(f"deliver.{n}")
                for n in ("p1", "p2")
                if crash_at_during.get(f"deliver.{n}")
            } or None
        delivered = self.deliver(txid, crash_at=kill_deliver_at)
        out["delivery"] = delivered
        out["result"] = self.coord.result(txid, self.paths)
        return out

    # -- recovery --------------------------------------------------------

    def recover(self, txid: Optional[str] = None) -> dict:
        """Recover all unfinished transactions, or just ``txid``.

        Rules:
          * No recorded decision -> record ABORT, then deliver ABORT.
          * Recorded COMMIT -> redeliver COMMIT to unfinished participants.
          * Recorded ABORT  -> redeliver ABORT to unfinished participants.
        Recovery never converts a recorded COMMIT into an abort.
        """
        with self.coord.conn:
            pass
        txs = [
            dict(r) for r in self.coord.conn.execute(
                "SELECT * FROM coordinator_txn "
                + ("WHERE txid=? " if txid else "")
                + "ORDER BY created_at",
                (txid,) if txid else (),
            )
        ]
        report = {"recovered": [], "already_complete": []}
        for row in txs:
            if row["state"] == COMPLETE:
                report["already_complete"].append({
                    "txid": row["txid"],
                    "state_before": COMPLETE,
                    "decision_before": row["decision"],
                    "action": "nothing to do; already COMPLETE",
                    "complete": True,
                })
                continue
            entry = self._recover_one(row)
            report["recovered"].append(entry)
        return report

    def _recover_one(self, row: dict) -> dict:
        txid = row["txid"]
        state_before = row["state"]
        decision_before = row["decision"]

        if row["decision"] is None:
            # Protocol rule: with no final decision the recovery program
            # records ABORT.  This is safe: a participant can only have
            # committed if a COMMIT decision was already durably recorded and
            # delivered, which it was not.
            decided = self.coord.decide(txid, "ABORT")
            action = "recorded ABORT (no final decision existed)"
        else:
            decided = {
                "decision": row["decision"],
                "order": row["decision_order"],
                "decision_digest": row["decision_digest"],
                "replayed": True,
            }
            action = f"kept recorded {row['decision']}; re-drove participants"

        delivery = self.deliver(txid, max_attempts=DELIVERY_ATTEMPTS)
        after = self.coord.request_row(txid)
        return {
            "txid": txid,
            "state_before": state_before,
            "decision_before": decision_before,
            "action": action,
            "decision": decided,
            "delivery": delivery,
            "state_after": after["state"],
            "complete": after["state"] == COMPLETE,
        }


def both_yes(replies: dict) -> bool:
    return all(
        replies.get(name, {}).get("decision", replies.get(name, {}).get("prepare"))
        == "YES"
        for name in ("p1", "p2")
    )
