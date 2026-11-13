"""Independent post-recovery verifier.

The verifier never trusts the coordinator's in-memory outcome.  It reads the
three SQLite files separately and checks the safety conditions the task asks
for:

  * both participants adopted the new values, or both kept the old values
    (no half-update);
  * no dangling logical claim (reservation) survives a finished transaction;
  * no participant holds a PREPARED state after recovery;
  * the coordinator has no contradictory decision, and its recorded decision
    agrees with what each participant actually did;
  * every business value is exactly either the pre-state or the intended
    new-state for each key (nothing partial within a side);
  * the expected final side (COMMIT => new values, ABORT => old values) was
    reached where the decision says so.
"""
from __future__ import annotations

import json

from .participant import ABORTED, COMMITTED
from .request import ABSENT
from .util import open_database


def _business(path: str) -> dict[str, int]:
    conn = open_database(path, read_only=True)
    try:
        return {
            r["key"]: r["value"]
            for r in conn.execute("SELECT key, value FROM business ORDER BY key")
        }
    finally:
        conn.close()


def _coordinator_row(path: str, txid: str) -> dict | None:
    conn = open_database(path, read_only=True)
    try:
        row = conn.execute(
            "SELECT * FROM coordinator_txn WHERE txid=?", (txid,)
        ).fetchone()
        return None if row is None else dict(row)
    finally:
        conn.close()


def _participant_rows(path: str) -> dict[str, dict]:
    conn = open_database(path, read_only=True)
    try:
        txn = {
            r["txid"]: dict(r)
            for r in conn.execute("SELECT * FROM participant_txn ORDER BY txid")
        }
        reserved = [
            dict(r) for r in conn.execute("SELECT key, txid FROM reservation")
        ]
        audit = []
        if _has_table(conn, "change_audit"):
            audit = [
                dict(r) for r in conn.execute(
                    "SELECT txid, seq, key, old_value, new_value, outcome "
                    "FROM change_audit"
                )
            ]
        return {"txn": txn, "reservations": reserved, "audit": audit}
    finally:
        conn.close()


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def verify(layout_paths: dict[str, str], txid: str) -> dict:
    """Return a machine-readable verification report; raise nothing."""
    coord_path = layout_paths["coordinator"]
    p_paths = {"p1": layout_paths["p1"], "p2": layout_paths["p2"]}

    failures: list[str] = []
    notes: list[str] = []

    coord = _coordinator_row(coord_path, txid)
    if coord is None:
        return {
            "txid": txid, "ok": False,
            "failures": [f"coordinator has no record for {txid!r}"] ,
            "notes": notes,
        }
    request = json.loads(coord["request_json"])
    update_lists = request["participants"]

    # ---- per-participant checks ----
    states = {}
    final_values = {}
    for name, path in p_paths.items():
        info = _participant_rows(path)
        business = _business(path)
        final_values[name] = business
        row = info["txn"].get(txid)
        states[name] = None if row is None else row["state"]

        # Ground truth for the pre-state comes from the participant's own
        # prepare audit, which recorded what it actually read.  This is
        # essential when a side answered NO: the client's "expected" value was
        # itself wrong, so it cannot be used as the old value.
        audit_for_tx = [a for a in info.get("audit", []) if a["txid"] == txid]
        recorded_old: dict[str, object] = {
            a["key"]: a["old_value"] for a in audit_for_tx
        }
        if row is not None:
            # Field-level check: each key is exactly its recorded old value or
            # the requested new value, never a partial / unrelated value.
            for item in update_lists[name]:
                if not isinstance(item, dict):
                    continue
                key = item.get("key")
                if not isinstance(key, str):
                    continue
                got = business.get(key)
                expected = item.get("expected")
                old = recorded_old.get(key, expected
                                       if expected != ABSENT else None)
                new = item.get("new")
                if got not in (old, new):
                    failures.append(
                        f"{name}: key {key!r} has value {got!r}, which is neither "
                        f"the recorded old value {old!r} nor the requested new "
                        f"value {new!r}"
                    )

        # Dangling claims for any *finished* transaction are a bug; claims for
        # an in-flight prepared transaction are legitimate.
        for res in info["reservations"]:
            owner = res["txid"]
            owner_row = info["txn"].get(owner)
            if owner_row is not None and owner_row["state"] in ("COMMITTED", "ABORTED"):
                failures.append(
                    f"{name}: key {res['key']!r} still claimed by finished "
                    f"transaction {owner!r}"
                )

    # ---- no prepared survivors after a complete recovery ----
    if coord["state"] == "COMPLETE":
        for name in ("p1", "p2"):
            if states[name] == "PREPARED":
                failures.append(f"{name} is still PREPARED after coordinator COMPLETE")

    # ---- decision agreement ----
    decision = coord["decision"]
    if decision == "COMMIT":
        for name in ("p1", "p2"):
            if states[name] not in ("COMMITTED", None):
                failures.append(
                    f"{name} state is {states[name]!r} but coordinator recorded COMMIT"
                )
            if states[name] == "COMMITTED":
                for item in update_lists[name]:
                    key = item["key"]
                    if final_values[name].get(key) != item["new"]:
                        failures.append(
                            f"{name}: committed key {key!r} is "
                            f"{final_values[name].get(key)!r}, expected new value "
                            f"{item['new']!r}"
                        )
    elif decision == "ABORT":
        for name in ("p1", "p2"):
            info = _participant_rows(p_paths[name])
            audit_for_tx = {
                a["key"]: a for a in info.get("audit", []) if a["txid"] == txid
            }
            if states[name] == "COMMITTED":
                failures.append(
                    f"{name} is COMMITTED but coordinator recorded ABORT "
                    "(contradictory decision)"
                )
            if states[name] == "ABORTED":
                for item in update_lists[name]:
                    key = item["key"]
                    recorded = audit_for_tx.get(key)
                    if recorded is None:
                        failures.append(
                            f"{name}: no change audit for aborted key {key!r}"
                        )
                        continue
                    if final_values[name].get(key) != recorded["old_value"]:
                        failures.append(
                            f"{name}: aborted key {key!r} is "
                            f"{final_values[name].get(key)!r}, expected old value "
                            f"{recorded['old_value']!r}"
                        )
    else:
        # No decision: nothing may be committed on either side.
        notes.append("coordinator has no final decision in this snapshot")

    # ---- cross-side atomicity: both new or both old (for keys that exist
    #      on both sides only in aggregate; here we assert side-level atomicity)
    if decision in ("COMMIT", "ABORT") and coord["state"] == "COMPLETE":
        target = "COMMITTED" if decision == "COMMIT" else "ABORTED"
        if decision == "COMMIT":
            ok_states = all(states[n] == COMMITTED for n in ("p1", "p2"))
        else:
            # A side that answered NO has nothing to undo and is legitimately
            # absent from the participant ledger at the end.
            ok_states = all(
                states[n] in (ABORTED, None) for n in ("p1", "p2")
            )
        if not ok_states:
            failures.append(
                f"participants did not reach {target}: {states}"
            )

    # ---- reservations must not linger anywhere for a completed request ----
    if coord["state"] == "COMPLETE":
        for name, path in p_paths.items():
            info = _participant_rows(path)
            for res in info["reservations"]:
                if res["txid"] == txid:
                    failures.append(f"{name}: reservation for {txid!r} not released")

    return {
        "txid": txid,
        "ok": not failures,
        "decision": decision,
        "coordinator_state": coord["state"],
        "participant_states": states,
        "failures": failures,
        "notes": notes,
        "business_after": final_values,
    }


def verify_final_values(layout_paths: dict[str, str], txid: str,
                        request: dict) -> dict:
    """Strict statement of the user-facing safety property.

    Computes, for each side, whether the side holds exactly its pre-transaction
    values or exactly the requested new values for every touched key.  The old
    values are taken from each participant's prepare audit (its own record of
    what it read), not from the client's claimed expectations.
    """
    coord = _coordinator_row(layout_paths["coordinator"], txid)
    decision = None if coord is None else coord["decision"]
    sides = {}
    any_new = False
    any_old = False
    failures = []
    for name in ("p1", "p2"):
        business = _business(layout_paths[name])
        info = _participant_rows(layout_paths[name])
        recorded_old = {
            a["key"]: a["old_value"]
            for a in info.get("audit", []) if a["txid"] == txid
        }
        row = info["txn"].get(txid)
        updates = request["participants"][name]
        if row is None and not recorded_old:
            # This side never staged anything (for example it answered NO or
            # was never prepared).  Its business rows are untouched, which is
            # equivalent to "old".
            sides[name] = "UNCHANGED"
            any_old = True
            continue
        old_ok = True
        new_ok = True
        for item in updates:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                continue
            key = item["key"]
            expected = item.get("expected")
            if key in recorded_old:
                old_value = recorded_old[key]
            else:
                old_value = None if expected == ABSENT else expected
            got = business.get(key)
            if got != old_value:
                old_ok = False
            if got != item.get("new"):
                new_ok = False
        if old_ok:
            side_outcome = "OLD"
            any_old = True
        elif new_ok:
            side_outcome = "NEW"
            any_new = True
        else:
            side_outcome = "MIXED"
            failures.append(
                f"{name} holds a mixed/partial update: {business!r}"
            )
        sides[name] = side_outcome

    # The requested atomicity property depends on what was actually decided.
    if decision == "COMMIT":
        if not all(v == "NEW" for v in sides.values()):
            failures.append(
                f"decision was COMMIT but sides are {sides}; expected both NEW"
            )
    elif decision == "ABORT":
        if not all(v in ("OLD", "UNCHANGED") for v in sides.values()):
            failures.append(
                f"decision was ABORT but sides are {sides}; expected both OLD"
            )
    else:
        if any(v == "NEW" for v in sides.values()):
            failures.append(
                f"no decision recorded but some side holds new values: {sides}"
            )
    return {
        "txid": txid,
        "decision": decision,
        "sides": sides,
        "atomic": not failures,
        "failures": failures,
    }
