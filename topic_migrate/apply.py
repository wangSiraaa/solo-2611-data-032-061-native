"""Apply human decisions: validate bindings, resolve names, export evidency.

The decision file is authored against a specific old/new input and model
summary. Any change to those sources invalidates it. Matching uses content
fingerprints, never local integer ids, so a consistent renumbering of the new
model cannot move a confirmed business name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import __version__
from .compare import DECISION_SCHEMA
from .data import load_period
from .errors import (
    BindingError,
    DecisionError,
    SourceChangedError,
)
from .model import validate_model
from .names import NamesRegistry, TOPIC_ID_RE, names_for_next_period
from .storage import (
    read_json,
    sha256_file,
    sha256_value,
    write_json_atomic,
    write_text_atomic,
)
APPLIED_SCHEMA = "topic-migrate/applied/v1"
CONFIRMED_ACTIONS = {"continue", "new"}


def _require_dict(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DecisionError(f"{what} must be a JSON object")
    return value


def load_decision(path: Path) -> dict[str, Any]:
    try:
        raw = read_json(path)
    except FileNotFoundError:
        raise DecisionError(f"{path}: decision file not found")
    except ValueError as exc:
        raise DecisionError(str(exc)) from exc
    raw = _require_dict(raw, str(path))
    if raw.get("schema") != DECISION_SCHEMA:
        raise DecisionError(f"{path}: expected schema {DECISION_SCHEMA!r}")
    binding = raw.get("binding")
    if not isinstance(binding, dict):
        raise DecisionError(f"{path}: binding is required")
    for key in (
        "old_period",
        "new_period",
        "old_input_sha256",
        "new_input_sha256",
        "old_model_summary_sha256",
        "new_model_summary_sha256",
        "plan_sha256",
        "old_names_sha256",
        "compare_sha256",
        "compare_config",
    ):
        value = binding.get(key)
        if key == "compare_config":
            if not isinstance(value, dict):
                raise DecisionError(f"{path}: binding.{key} is required")
            continue
        if not isinstance(value, str) or not value:
            raise DecisionError(f"{path}: binding.{key} is required")
    decisions = raw.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise DecisionError(f"{path}: decisions must be a non-empty list")
    retirements = raw.get("retirements", [])
    if not isinstance(retirements, list):
        raise DecisionError(f"{path}: retirements must be a list")
    return raw


def verify_binding(
    decision: dict[str, Any],
    old_model: dict[str, Any],
    new_model: dict[str, Any],
    old_names: NamesRegistry,
) -> None:
    binding = decision["binding"]
    checks: list[tuple[str, str, str]] = [
        (
            "old_period",
            binding["old_period"],
            old_model["period"],
        ),
        (
            "new_period",
            binding["new_period"],
            new_model["period"],
        ),
        (
            "old_input_sha256",
            binding["old_input_sha256"],
            old_model["input"]["sha256"],
        ),
        (
            "new_input_sha256",
            binding["new_input_sha256"],
            new_model["input"]["sha256"],
        ),
        (
            "old_model_summary_sha256",
            binding["old_model_summary_sha256"],
            old_model["summary_sha256"],
        ),
        (
            "new_model_summary_sha256",
            binding["new_model_summary_sha256"],
            new_model["summary_sha256"],
        ),
        (
            "plan_sha256",
            binding["plan_sha256"],
            new_model["plan"]["sha256"],
        ),
        (
            "plan_sha256",
            binding["plan_sha256"],
            old_model["plan"]["sha256"],
        ),
        (
            "old_names_sha256",
            binding["old_names_sha256"],
            old_names.raw_sha256,
        ),
    ]
    for label, expected, actual in checks:
        if expected != actual:
            raise BindingError(
                f"decision binding is stale: {label} expected {expected[:16]}… "
                f"but current value is {actual[:16]}…; redo compare and review"
            )
    if binding["old_period"] == binding["new_period"]:
        raise BindingError("decision binds the same period twice")


def verify_compare_report(
    decision: dict[str, Any], compare_report: dict[str, Any]
) -> None:
    """If the compare report is supplied, it must be the one reviewed."""
    if compare_report.get("compare_sha256") != decision["binding"]["compare_sha256"]:
        raise BindingError(
            "decision binding is stale: compare_sha256 does not match the "
            "supplied compare report; redo compare and review"
        )
    expected = sha256_value(
        {k: v for k, v in compare_report.items() if k != "compare_sha256"}
    )
    if compare_report.get("compare_sha256") != expected:
        raise BindingError(
            "compare report is internally inconsistent (edited after compare)"
        )
    if compare_report.get("config") != decision["binding"]["compare_config"]:
        raise BindingError("compare configuration changed since the decision")


def verify_new_input(
    decision: dict[str, Any],
    new_model: dict[str, Any],
    new_input_path: Path,
    new_input_sha256: str,
) -> None:
    binding = decision["binding"]
    if new_input_sha256 != new_model["input"]["sha256"]:
        raise SourceChangedError(
            f"{new_input_path}: file hash does not match the model input; "
            "the new-period source changed after training"
        )
    if new_input_sha256 != binding["new_input_sha256"]:
        raise BindingError(
            "new-period input changed after the decision was authored; "
            "redo compare and review"
        )


def _fingerprint_index(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {topic["fingerprint"]: topic for topic in model["topics"]}


def apply_names(
    old_model: dict[str, Any],
    new_model: dict[str, Any],
    old_names: NamesRegistry,
    decision: dict[str, Any],
    new_input_path: Path,
    compare_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_model(old_model)
    validate_model(new_model)
    if old_model["period"] == new_model["period"]:
        raise DecisionError("old and new models must be different periods")
    if old_model["plan"]["sha256"] != new_model["plan"]["sha256"]:
        raise DecisionError(
            "old and new models were trained against different plans; the "
            "decision cannot span them"
        )
    # Check the decision's bindings before anything that depends on the
    # current names, so a stale decision is reported as such.
    verify_binding(decision, old_model, new_model, old_names)
    if compare_report is not None:
        verify_compare_report(decision, compare_report)
    old_names.resolve(old_model)
    verify_new_input(
        decision,
        new_model,
        new_input_path,
        sha256_file(new_input_path),
    )

    new_topics = _fingerprint_index(new_model)
    old_entries = old_names.by_topic_id()
    decisions = decision["decisions"]

    by_fingerprint: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(decisions, 1):
        item = _require_dict(item, f"decisions[{index}]")
        fingerprint = item.get("new_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise DecisionError(
                f"decisions[{index}].new_fingerprint is required"
            )
        if fingerprint in by_fingerprint:
            raise DecisionError(
                f"decisions[{index}] duplicates fingerprint {fingerprint[:16]}…"
            )
        if fingerprint not in new_topics:
            raise DecisionError(
                f"decisions[{index}] references a fingerprint absent from the "
                "new model; the decision is stale"
            )
        action = item.get("action")
        if action in ("", "defer", None):
            raise DecisionError(
                f"decisions[{index}] (new topic {fingerprint[:16]}…) is "
                "unresolved: choose 'continue' or 'new' explicitly"
            )
        if action not in CONFIRMED_ACTIONS:
            raise DecisionError(
                f"decisions[{index}].action must be 'continue' or 'new', got "
                f"{action!r}"
            )
        by_fingerprint[fingerprint] = item

    missing = sorted(set(new_topics) - set(by_fingerprint))
    if missing:
        raise DecisionError(
            "decision does not cover every new topic; unresolved: " + ", ".join(
                fp[:16] + "…" for fp in missing
            )
        )
    extra = sorted(set(by_fingerprint) - set(new_topics))
    if extra:  # pragma: no cover - guarded by the per-item check above
        raise DecisionError("decision has topics absent from the new model")

    # Validate continue targets and new topic ids before writing anything.
    continued_old: dict[str, str] = {}
    assigned_new_ids: dict[str, str] = {}
    resolved: dict[str, dict[str, Any]] = {}
    for fingerprint, item in by_fingerprint.items():
        action = item["action"]
        topic = new_topics[fingerprint]
        note = item.get("note", "")
        if not isinstance(note, str):
            raise DecisionError(
                f"decision for {fingerprint[:16]}… has a non-string note"
            )
        flags = item.get("flags", [])
        if isinstance(flags, list) and flags and not note.strip():
            raise DecisionError(
                f"decision for {fingerprint[:16]}… has competition flags "
                f"{flags}; record a note explaining the choice"
            )
        if action == "continue":
            target = item.get("continue_topic_id")
            if not isinstance(target, str) or not target:
                raise DecisionError(
                    f"decision for {fingerprint[:16]}… chose 'continue' but has "
                    "no continue_topic_id"
                )
            if target not in old_entries:
                raise DecisionError(
                    f"continue_topic_id {target!r} is not in the old names file"
                )
            if target in continued_old:
                raise DecisionError(
                    f"old topic {target!r} is continued by two new topics; "
                    "split/merge requires an explicit human decision, not "
                    "automatic dual continuation"
                )
            continued_old[target] = fingerprint
            entry = old_entries[target]
            business_id = entry.topic_id
            name = entry.name
        else:  # action == "new"
            new_id = item.get("new_topic_id")
            new_name = item.get("new_name")
            if not isinstance(new_id, str) or not TOPIC_ID_RE.match(new_id):
                raise DecisionError(
                    f"decision for {fingerprint[:16]}… needs a valid "
                    "new_topic_id"
                )
            if new_id in old_entries:
                raise DecisionError(
                    f"new_topic_id {new_id!r} collides with an existing business "
                    "topic id; use 'continue' to reuse it"
                )
            if new_id in assigned_new_ids.values():
                raise DecisionError(f"new_topic_id {new_id!r} is assigned twice")
            if not isinstance(new_name, str) or not new_name.strip():
                raise DecisionError(
                    f"decision for {fingerprint[:16]}… needs a non-empty new_name"
                )
            assigned_new_ids[fingerprint] = new_id
            business_id = new_id
            name = new_name.strip()
        resolved[fingerprint] = {
            "local_id": topic["local_id"],
            "fingerprint": fingerprint,
            "topic_id": business_id,
            "name": name,
            "action": action,
            "source": "continued" if action == "continue" else "new",
            "note": note,
            "top_terms": topic["top_terms"],
            "n_assigned": topic["n_assigned"],
        }

    # Business ids must remain unique across the applied period.
    ids = [entry["topic_id"] for entry in resolved.values()]
    if len(set(ids)) != len(ids):
        raise DecisionError("two new topics resolved to the same business topic_id")

    retirements = _validate_retirements(
        decision.get("retirements", []), old_entries, set(continued_old)
    )
    changed = _confirmed_changes(
        old_entries, resolved, retirements, continued_old
    )

    return _build_applied(
        old_model=old_model,
        new_model=new_model,
        old_names=old_names,
        decision=decision,
        resolved=resolved,
        retirements=retirements,
        changed=changed,
        new_input_path=new_input_path,
    )


def _validate_retirements(
    raw: list[Any],
    old_entries: dict[str, Any],
    continued: set[str],
) -> list[dict[str, Any]]:
    retirements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, 1):
        item = _require_dict(item, f"retirements[{index}]")
        action = item.get("action", "")
        if action in ("", "keep", "defer", None):
            continue
        if action != "retire":
            raise DecisionError(
                f"retirements[{index}].action must be 'retire' or empty"
            )
        topic_id = item.get("topic_id")
        if not isinstance(topic_id, str) or topic_id not in old_entries:
            raise DecisionError(
                f"retirements[{index}].topic_id is not in the old names file"
            )
        if topic_id in continued:
            raise DecisionError(
                f"old topic {topic_id!r} is both continued and retired"
            )
        if topic_id in seen:
            raise DecisionError(f"retirements duplicates topic {topic_id!r}")
        seen.add(topic_id)
        entry = old_entries[topic_id]
        retirements.append(
            {
                "topic_id": topic_id,
                "name": entry.name,
                "old_local_id": entry.local_id,
                "note": item.get("note", ""),
            }
        )
    return retirements


def _confirmed_changes(
    old_entries: dict[str, Any],
    resolved: dict[str, dict[str, Any]],
    retirements: list[dict[str, Any]],
    continued_old: dict[str, str],
) -> dict[str, Any]:
    continued = [
        {
            "topic_id": topic_id,
            "name": old_entries[topic_id].name,
            "old_local_id": old_entries[topic_id].local_id,
            "new_fingerprint": fingerprint,
            "new_local_id": resolved[fingerprint]["local_id"],
        }
        for topic_id, fingerprint in sorted(continued_old.items())
    ]
    added = [
        {
            "topic_id": entry["topic_id"],
            "name": entry["name"],
            "new_fingerprint": fingerprint,
            "new_local_id": entry["local_id"],
        }
        for fingerprint, entry in sorted(
            resolved.items(), key=lambda kv: kv[1]["local_id"]
        )
        if entry["source"] == "new"
    ]
    continued_ids = set(continued_old)
    retired_ids = {item["topic_id"] for item in retirements}
    unrepresented = [
        {
            "topic_id": topic_id,
            "name": entry.name,
            "local_id": entry.local_id,
            "note": "not continued and not explicitly retired",
        }
        for topic_id, entry in sorted(old_entries.items())
        if topic_id not in continued_ids and topic_id not in retired_ids
    ]
    return {
        "continued": continued,
        "added": added,
        "retired": sorted(retirements, key=lambda item: item["topic_id"]),
        "unrepresented_old": unrepresented,
        "counts": {
            "continued": len(continued),
            "added": len(added),
            "retired": len(retirements),
            "unrepresented_old": len(unrepresented),
            "net_confirmed_change": len(added) - len(retirements),
        },
        "note": (
            "Only explicit human decisions are counted. Candidate similarity "
            "never contributes to these confirmed changes."
        ),
    }


def _build_applied(
    old_model: dict[str, Any],
    new_model: dict[str, Any],
    old_names: NamesRegistry,
    decision: dict[str, Any],
    resolved: dict[str, dict[str, Any]],
    retirements: list[dict[str, Any]],
    changed: dict[str, Any],
    new_input_path: Path,
) -> dict[str, Any]:
    new_ticket_text = {
        ticket.ticket_id: ticket for ticket in load_period(new_input_path).tickets
    }
    assignment_by_id = {
        record["ticket_id"]: record for record in new_model["assignments"]
    }

    topics_out: list[dict[str, Any]] = []
    for fingerprint, entry in sorted(
        resolved.items(), key=lambda kv: kv[1]["local_id"]
    ):
        topic = new_model["topics"][entry["local_id"]]
        members = []
        for ticket_id in topic["assigned_ticket_ids"]:
            record = assignment_by_id[ticket_id]
            ticket = new_ticket_text[ticket_id]
            members.append(
                {
                    "ticket_id": ticket_id,
                    "period": new_model["period"],
                    "channel": ticket.channel,
                    "title": ticket.title,
                    "body": ticket.body,
                    "assignment_score": record["top_score"],
                    "scores": record["scores"],
                    "support": record["support"],
                    "support_terms": record["support_terms"],
                    "margin": record["margin"],
                }
            )
        members.sort(key=lambda m: (-m["assignment_score"], m["ticket_id"]))
        topics_out.append(
            {
                "topic_id": entry["topic_id"],
                "name": entry["name"],
                "local_id": entry["local_id"],
                "fingerprint": fingerprint,
                "action": entry["action"],
                "source": entry["source"],
                "note": entry["note"],
                "keywords": topic["keywords"],
                "top_terms": topic["top_terms"],
                "n_assigned": topic["n_assigned"],
                "representative_tickets": topic["representative_tickets"],
                "members": members,
            }
        )

    unassigned = []
    unmodelable = []
    for record in sorted(new_model["assignments"], key=lambda r: r["ticket_id"]):
        if record["status"] != "assigned":
            ticket = new_ticket_text[record["ticket_id"]]
            unassigned.append(
                {
                    "ticket_id": record["ticket_id"],
                    "period": new_model["period"],
                    "channel": ticket.channel,
                    "title": ticket.title,
                    "body": ticket.body,
                    "reason": record["reason"],
                    "scores": record["scores"],
                    "top_score": record["top_score"],
                    "support": record["support"],
                    "support_terms": record["support_terms"],
                }
            )
    for ticket in new_ticket_text.values():
        if not ticket.is_modelable:
            unmodelable.append(
                {
                    "ticket_id": ticket.ticket_id,
                    "period": new_model["period"],
                    "channel": ticket.channel,
                    "title": ticket.title,
                    "body": ticket.body,
                    "reason": "empty_text",
                }
            )

    old_name_snapshot = [entry.as_dict() for entry in old_names.entries]
    applied = {
        "schema": APPLIED_SCHEMA,
        "period": new_model["period"],
        "tool": {"name": "topic-migrate", "version": __version__},
        "binding": {
            **decision["binding"],
            "decision_sha256": sha256_value(decision),
            "new_input_reverified_sha256": sha256_file(new_input_path),
        },
        "old_period": old_model["period"],
        "old_names": sorted(old_name_snapshot, key=lambda e: e["topic_id"]),
        "topics": topics_out,
        "unassigned_tickets": unassigned,
        "unmodelable_tickets": unmodelable,
        "confirmed_changes": changed,
        "retirements": retirements,
        "counts": {
            "topics": len(topics_out),
            "assigned_tickets": sum(t["n_assigned"] for t in topics_out),
            "unassigned_tickets": len(unassigned),
            "unmodelable_tickets": len(unmodelable),
        },
    }
    applied["applied_sha256"] = sha256_value(applied)
    applied["names_next_period"] = names_for_next_period(new_model, topics_out)
    return applied


def write_applied_outputs(
    applied: dict[str, Any], out_dir: Path
) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    named_path = out_dir / "named_topics.json"
    evidence_path = out_dir / "evidence.jsonl"
    html_path = out_dir / "named.review.html"
    changes_path = out_dir / "confirmed_changes.json"

    write_json_atomic(named_path, applied)
    write_json_atomic(changes_path, applied["confirmed_changes"])
    write_json_atomic(out_dir / "names.next.json", applied["names_next_period"])
    lines: list[str] = []
    for topic in applied["topics"]:
        lines.append(
            {
                "kind": "topic",
                "period": applied["period"],
                "topic_id": topic["topic_id"],
                "name": topic["name"],
                "local_id": topic["local_id"],
                "fingerprint": topic["fingerprint"],
                "action": topic["action"],
                "top_terms": topic["top_terms"],
            }
        )
        for member in topic["members"]:
            lines.append({"kind": "assigned", **member, "topic_id": topic["topic_id"], "name": topic["name"]})
    for item in applied["unassigned_tickets"]:
        lines.append({"kind": "unassigned", **item})
    for item in applied["unmodelable_tickets"]:
        lines.append({"kind": "unmodelable", **item})
    import json

    write_text_atomic(
        evidence_path,
        "\n".join(json.dumps(line, ensure_ascii=False, sort_keys=True) for line in lines)
        + ("\n" if lines else ""),
    )
    write_text_atomic(
        html_path,
        render_applied_html(applied),
    )
    return {
        "named_topics": str(named_path),
        "evidence": str(evidence_path),
        "confirmed_changes": str(changes_path),
        "names_next_period": str(out_dir / "names.next.json"),
        "html": str(html_path),
    }


def render_applied_html(applied: dict[str, Any]) -> str:
    from html import escape

    parts: list[str] = []
    add = parts.append
    add(
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        "<title>Named topics</title><style>"
        "body{font-family:system-ui,sans-serif;margin:2rem auto;max-width:1100px;"
        "padding:0 1rem;line-height:1.5;color:#1d232b}"
        "h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:2rem;"
        "border-bottom:1px solid #dfe3e8;padding-bottom:.3rem}"
        "table{border-collapse:collapse;width:100%;font-size:.9rem;margin:.6rem 0}"
        "th,td{border:1px solid #dfe3e8;padding:.35rem .5rem;vertical-align:top}"
        "th{background:#f5f7fa}.muted{color:#667085;font-size:.85rem}"
        ".evidence{font-family:ui-monospace,monospace;font-size:.85rem;"
        "white-space:pre-wrap}.tag{display:inline-block;background:#eef2f7;"
        "border-radius:4px;padding:.05rem .35rem;margin:.1rem}"
        ".ok{background:#eef6ee;border:1px solid #9ccf9c;padding:.5rem .7rem;"
        "border-radius:6px}</style></head><body>"
    )
    add("<h1>新期主题与证据（命名后）</h1>")
    counts = applied["counts"]
    add(
        f"<p class='ok'>period <b>{escape(applied['period'])}</b> · "
        f"主题 {counts['topics']} · 已归属工单 {counts['assigned_tickets']} · "
        f"弱证据未归属 {counts['unassigned_tickets']} · "
        f"空文本 {counts['unmodelable_tickets']}</p>"
    )
    add("<h2>确认的主题增减</h2>")
    confirmed = applied["confirmed_changes"]["counts"]
    add(
        "<table><tr><th>继承</th><th>新增</th><th>显式删除</th>"
        "<th>未表态旧主题</th><th>净变化</th></tr>"
        f"<tr><td>{confirmed['continued']}</td><td>{confirmed['added']}</td>"
        f"<td>{confirmed['retired']}</td><td>{confirmed['unrepresented_old']}</td>"
        f"<td>{confirmed['net_confirmed_change']}</td></tr></table>"
    )
    for topic in applied["topics"]:
        add(
            f"<h2>topic_id {escape(topic['topic_id'])} · "
            f"{escape(topic['name'])}</h2>"
            f"<p class='muted'>local {topic['local_id']} · "
            f"fingerprint {escape(topic['fingerprint'][:16])}… · "
            f"{escape(topic['source'])} · 已归属 {topic['n_assigned']}</p>"
            + " ".join(f"<span class='tag'>{escape(t)}</span>" for t in topic["top_terms"])
        )
        add("<table><tr><th>ticket_id</th><th>channel</th><th>title</th>"
            "<th>score</th><th>support</th><th>body</th></tr>")
        for member in topic["members"]:
            add(
                f"<tr><td>{escape(member['ticket_id'])}</td>"
                f"<td>{escape(member['channel'])}</td>"
                f"<td>{escape(member['title'])}</td>"
                f"<td>{member['assignment_score']:.3f}</td>"
                f"<td>{member['support']}</td>"
                f"<td class='evidence'>{escape(member['body'])}</td></tr>"
            )
        add("</table>")
    add("<h2>弱证据或空文本（未强制归类）</h2>")
    if applied["unassigned_tickets"] or applied["unmodelable_tickets"]:
        add("<table><tr><th>ticket_id</th><th>reason</th><th>top score</th>"
            "<th>title</th><th>body</th></tr>")
        for item in applied["unassigned_tickets"] + applied["unmodelable_tickets"]:
            add(
                f"<tr><td>{escape(item['ticket_id'])}</td>"
                f"<td>{escape(item['reason'])}</td>"
                f"<td>{item.get('top_score', '—')}</td>"
                f"<td>{escape(item['title'])}</td>"
                f"<td class='evidence'>{escape(item['body'])}</td></tr>"
            )
        add("</table>")
    else:
        add("<p class='muted'>无。</p>")
    add("</body></html>")
    return "".join(parts)
