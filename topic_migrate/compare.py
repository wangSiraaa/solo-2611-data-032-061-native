"""Cross-period comparison and name-continuation candidates.

Two periods are trained independently, so their vocabularies and local topic
numbers are not shared. Comparison therefore happens in an explicitly
comparable space:

* **terms** — only the intersection of the two vocabularies, with each topic's
  term distribution re-normalized inside that shared space. Coverage (how much
  of a topic's mass falls in the shared space) discounts the cosine, so two
  topics are never declared similar merely because their vocabularies are
  disjoint.
* **tickets** — tickets whose ``ticket_id`` occurs in both periods, scored by
  the Bhattacharyya coefficient of their topic proportions in each period, plus
  co-assignment counts.
* **local ids are never compared.** Candidates are scored from content only.

Ambiguous, split, and merge situations are reported as competing candidates
and remain unconfirmed; this module never applies an inheritance rule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from .errors import CompareError
from .model import validate_model
from .names import NamesRegistry
from .storage import sha256_value, write_json_atomic, write_text_atomic

COMPARE_SCHEMA = "topic-migrate/compare/v1"
DECISION_SCHEMA = "topic-migrate/decision/v1"

# Deliberately conservative, interpretable defaults.
DEFAULTS = {
    "candidate_floor": 0.20,
    "near_delta": 0.10,
    "strong_threshold": 0.32,
    "weight_term": 0.50,
    "weight_document": 0.30,
    "weight_support": 0.20,
}


def _norm_distribution(weights: dict[str, float]) -> dict[str, float]:
    total = sum(float(v) for v in weights.values())
    if total <= 0:
        return {}
    return {term: float(value) / total for term, value in weights.items()}


def _cosine_shared(
    p: dict[str, float], q: dict[str, float], shared: set[str]
) -> tuple[float, float, float]:
    """Cosine over shared terms and each side's coverage of the shared space."""
    dot = pn = qn = 0.0
    for term in shared:
        pv = p.get(term, 0.0)
        qv = q.get(term, 0.0)
        dot += pv * qv
        pn += pv * pv
        qn += qv * qv
    coverage_p = sum(p.get(term, 0.0) for term in shared)
    coverage_q = sum(q.get(term, 0.0) for term in shared)
    if pn <= 0 or qn <= 0:
        return 0.0, coverage_p, coverage_q
    cosine = dot / (math.sqrt(pn) * math.sqrt(qn))
    return cosine, coverage_p, coverage_q


@dataclass
class TopicView:
    period: str
    local_id: int
    fingerprint: str
    term_weights: dict[str, float]
    support_terms: set[str]
    keyword_terms: list[str]
    n_assigned: int
    assigned_ticket_ids: set[str]
    representatives: list[dict[str, Any]]
    name_entry: Any | None

    @property
    def topic_id(self) -> str | None:
        return self.name_entry.topic_id if self.name_entry else None

    @property
    def name(self) -> str | None:
        return self.name_entry.name if self.name_entry else None

    def label(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "local_id": self.local_id,
            "fingerprint": self.fingerprint,
            "topic_id": self.topic_id,
            "name": self.name,
            "top_terms": self.keyword_terms,
            "n_assigned": self.n_assigned,
        }


def _topic_views(
    model: dict[str, Any], registry: NamesRegistry | None
) -> dict[str, TopicView]:
    resolved = registry.resolve(model)["by_fingerprint"] if registry else {}
    views: dict[str, TopicView] = {}
    for topic in model["topics"]:
        views[topic["fingerprint"]] = TopicView(
            period=model["period"],
            local_id=topic["local_id"],
            fingerprint=topic["fingerprint"],
            term_weights=_norm_distribution(topic["term_weights"]),
            support_terms=set(topic["support_terms"]),
            keyword_terms=[item["term"] for item in topic["keywords"][:10]],
            n_assigned=topic["n_assigned"],
            assigned_ticket_ids=set(topic["assigned_ticket_ids"]),
            representatives=topic["representative_tickets"],
            name_entry=resolved.get(topic["fingerprint"]),
        )
    return views


def _assignment_index(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {record["ticket_id"]: record for record in model["assignments"]}


def _proportions_by_fingerprint(
    model: dict[str, Any],
) -> dict[str, dict[str, dict[str, float]]]:
    fp_by_local = {topic["local_id"]: topic["fingerprint"] for topic in model["topics"]}
    result: dict[str, dict[str, dict[str, float]]] = {}
    for record in model["assignments"]:
        result[record["ticket_id"]] = {
            fp_by_local[int(local)]: float(score)
            for local, score in record["scores"].items()
        }
    return result


def compare_models(
    old_model: dict[str, Any],
    new_model: dict[str, Any],
    old_names: NamesRegistry,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_model(old_model)
    validate_model(new_model)
    if old_model["period"] == new_model["period"]:
        raise CompareError("old and new models are for the same period")
    if old_names.period != old_model["period"]:
        raise CompareError(
            f"old names are for period {old_names.period!r}, old model is "
            f"{old_model['period']!r}"
        )
    old_names.resolve(old_model)
    if (
        old_model["plan"]["sha256"] != new_model["plan"]["sha256"]
        or old_model["plan"]["period_key"] == new_model["plan"]["period_key"]
    ):
        raise CompareError(
            "the two models must come from the same plan with distinct period "
            "keys; retrain both periods against one plan"
        )

    cfg = dict(DEFAULTS)
    if config:
        for key, value in config.items():
            if key in cfg:
                cfg[key] = value
    for name in ("candidate_floor", "near_delta", "strong_threshold"):
        if not isinstance(cfg[name], (int, float)) or isinstance(cfg[name], bool):
            raise CompareError(f"{name} must be a number")
    weights = [cfg["weight_term"], cfg["weight_document"], cfg["weight_support"]]
    if any(not isinstance(w, (int, float)) or w < 0 for w in weights):
        raise CompareError("comparison weights must be non-negative numbers")
    if abs(sum(weights) - 1.0) > 1e-9:
        raise CompareError("comparison weights must sum to 1.0")

    old_views = _topic_views(old_model, old_names)
    new_views = _topic_views(new_model, None)
    old_vocab = set(old_model["vocabulary"])
    new_vocab = set(new_model["vocabulary"])
    shared_vocab = old_vocab & new_vocab
    if not shared_vocab:
        raise CompareError(
            "the periods share no vocabulary; no interpretable comparable space"
        )

    old_props = _proportions_by_fingerprint(old_model)
    new_props = _proportions_by_fingerprint(new_model)
    old_by_id = _assignment_index(old_model)
    new_by_id = _assignment_index(new_model)
    shared_tickets = sorted(set(old_props) & set(new_props))
    old_modelable = {r["ticket_id"] for r in old_model["assignments"]}
    new_modelable = {r["ticket_id"] for r in new_model["assignments"]}
    if not shared_tickets:
        warning = (
            "no ticket_id occurs in both periods; candidates rest on term "
            "evidence only and should be reviewed manually"
        )
    else:
        warning = None

    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for new_fp, new_view in new_views.items():
        for old_fp, old_view in old_views.items():
            cosine, cov_old, cov_new = _cosine_shared(
                old_view.term_weights, new_view.term_weights, shared_vocab
            )
            coverage = min(cov_old, cov_new)
            term_score = cosine * coverage
            union = old_view.support_terms | new_view.support_terms
            jaccard = (
                len(old_view.support_terms & new_view.support_terms) / len(union)
                if union
                else 0.0
            )
            affinity_sum = 0.0
            co_assigned: list[str] = []
            shared_modelable = 0
            for ticket_id in shared_tickets:
                if ticket_id not in old_modelable or ticket_id not in new_modelable:
                    continue
                shared_modelable += 1
                p = old_props[ticket_id].get(old_fp, 0.0)
                q = new_props[ticket_id].get(new_fp, 0.0)
                affinity_sum += math.sqrt(max(p, 0.0) * max(q, 0.0))
                old_rec = old_by_id[ticket_id]
                new_rec = new_by_id[ticket_id]
                old_fp_of_assignment = (
                    old_model["topics"][old_rec["local_id"]]["fingerprint"]
                    if old_rec["local_id"] is not None
                    else None
                )
                new_fp_of_assignment = (
                    new_model["topics"][new_rec["local_id"]]["fingerprint"]
                    if new_rec["local_id"] is not None
                    else None
                )
                if (
                    old_fp_of_assignment == old_fp
                    and new_fp_of_assignment == new_fp
                ):
                    co_assigned.append(ticket_id)
            doc_score = (
                affinity_sum / shared_modelable if shared_modelable else 0.0
            )
            composite = (
                cfg["weight_term"] * term_score
                + cfg["weight_document"] * doc_score
                + cfg["weight_support"] * jaccard
            )
            pairs[(new_fp, old_fp)] = {
                "old_fingerprint": old_fp,
                "new_fingerprint": new_fp,
                "old_topic": old_view.label(),
                "new_topic": {
                    "period": new_view.period,
                    "local_id": new_view.local_id,
                    "fingerprint": new_view.fingerprint,
                    "top_terms": new_view.keyword_terms,
                    "n_assigned": new_view.n_assigned,
                },
                "scores": {
                    "composite": round(composite, 6),
                    "term_cosine": round(cosine, 6),
                    "term_coverage_old": round(cov_old, 6),
                    "term_coverage_new": round(cov_new, 6),
                    "term_score": round(term_score, 6),
                    "document_affinity": round(doc_score, 6),
                    "support_jaccard": round(jaccard, 6),
                },
                "evidence": {
                    "shared_vocabulary_terms": len(shared_vocab),
                    "shared_modelable_tickets": shared_modelable,
                    "co_assigned_ticket_ids": sorted(co_assigned),
                    "co_assigned_count": len(co_assigned),
                    "old_name_available": old_view.name is not None,
                },
            }

    # Candidate lists per new topic, plus reverse maps for split/merge detection.
    floor = float(cfg["candidate_floor"])
    near_delta = float(cfg["near_delta"])
    strong = float(cfg["strong_threshold"])
    candidates_by_new: dict[str, list[dict[str, Any]]] = {}
    for new_fp in new_views:
        ranked = sorted(
            (pairs[(new_fp, old_fp)] for old_fp in old_views),
            key=lambda item: (-item["scores"]["composite"], item["old_fingerprint"]),
        )
        candidates_by_new[new_fp] = ranked

    old_to_new: dict[str, list[dict[str, Any]]] = {fp: [] for fp in old_views}
    for new_fp, ranked in candidates_by_new.items():
        for item in ranked:
            if item["scores"]["composite"] >= floor:
                old_to_new[item["old_fingerprint"]].append(item)

    proposals: list[dict[str, Any]] = []
    for new_fp, ranked in candidates_by_new.items():
        above = [item for item in ranked if item["scores"]["composite"] >= floor]
        flags: list[str] = []
        status = "new_topic_candidate"
        chosen = None

        if above:
            status = "continuation_candidate"
            chosen = above[0]
            if (
                len(above) >= 2
                and above[1]["scores"]["composite"]
                >= above[0]["scores"]["composite"] - near_delta
            ):
                flags.append("ambiguous_candidates")
                status = "ambiguous_candidates"
            if len(above) >= 2 and above[1]["scores"]["composite"] >= strong:
                flags.append("possible_merge_of_old_topics")

            # Splits: this new topic's top old topic also strongly matches
            # another new topic.
            top_old = above[0]["old_fingerprint"]
            competing_new = [
                item
                for item in old_to_new[top_old]
                if item["new_fingerprint"] != new_fp
                and item["scores"]["composite"] >= floor
            ]
            if competing_new:
                best_other = max(
                    item["scores"]["composite"] for item in competing_new
                )
                if best_other >= (
                    above[0]["scores"]["composite"] - near_delta
                ):
                    flags.append("possible_split_from_old_topic")

        proposal = {
            "new_local_id": new_views[new_fp].local_id,
            "new_fingerprint": new_fp,
            "new_top_terms": new_views[new_fp].keyword_terms,
            "n_assigned": new_views[new_fp].n_assigned,
            "status": status,
            "flags": sorted(set(flags)),
            "suggested_old_fingerprint": (
                chosen["old_fingerprint"] if chosen else None
            ),
            "suggested_topic_id": (
                chosen["old_topic"]["topic_id"] if chosen else None
            ),
            "suggested_name": chosen["old_topic"]["name"] if chosen else None,
            "candidates": [item for item in ranked[:4]],
        }
        proposals.append(proposal)

    proposals.sort(key=lambda p: p["new_local_id"])

    # Old topics with no plausible successor are reported but not "retired".
    unresolved_old: list[dict[str, Any]] = []
    for old_fp, items in old_to_new.items():
        if not items:
            view = old_views[old_fp]
            unresolved_old.append(
                {
                    **view.label(),
                    "note": "no new topic reached the candidate floor",
                }
            )

    competition: list[dict[str, Any]] = []
    for proposal in proposals:
        if proposal["flags"]:
            competition.append(
                {
                    "new_local_id": proposal["new_local_id"],
                    "new_fingerprint": proposal["new_fingerprint"],
                    "flags": proposal["flags"],
                    "candidates": [
                        {
                            "old_local_id": item["old_topic"]["local_id"],
                            "old_topic_id": item["old_topic"]["topic_id"],
                            "old_name": item["old_topic"]["name"],
                            "composite": item["scores"]["composite"],
                        }
                        for item in proposal["candidates"]
                        if item["scores"]["composite"] >= floor
                    ],
                    "decision": "pending_human",
                }
            )

    report: dict[str, Any] = {
        "schema": COMPARE_SCHEMA,
        "old": {
            "period": old_model["period"],
            "input_sha256": old_model["input"]["sha256"],
            "model_summary_sha256": old_model["summary_sha256"],
            "plan_sha256": old_model["plan"]["sha256"],
            "n_topics": len(old_model["topics"]),
            "names_sha256": old_names.raw_sha256,
        },
        "new": {
            "period": new_model["period"],
            "input_sha256": new_model["input"]["sha256"],
            "model_summary_sha256": new_model["summary_sha256"],
            "plan_sha256": new_model["plan"]["sha256"],
            "n_topics": len(new_model["topics"]),
            "input_file": new_model["input"]["file_name"],
        },
        "comparable_space": {
            "shared_vocabulary_terms": len(shared_vocab),
            "old_vocabulary_terms": len(old_vocab),
            "new_vocabulary_terms": len(new_vocab),
            "shared_vocabulary_sha256": sha256_value(sorted(shared_vocab)),
            "shared_tickets": len(shared_tickets),
            "shared_ticket_ids": shared_tickets,
        },
        "config": cfg,
        "old_topics": [
            old_views[fp].label()
            for fp in sorted(old_views, key=lambda f: old_views[f].local_id)
        ],
        "new_topics": [
            new_views[fp].label()
            for fp in sorted(new_views, key=lambda f: new_views[f].local_id)
        ],
        "new_representatives": [
            {
                "fingerprint": view.fingerprint,
                "local_id": view.local_id,
                "representatives": view.representatives[:5],
            }
            for view in sorted(new_views.values(), key=lambda v: v.local_id)
        ],
        "proposals": proposals,
        "competition": competition,
        "unresolved_old_topics": unresolved_old,
        "warnings": [warning] if warning else [],
        "confirmed_changes": {
            "note": (
                "No relationships are confirmed at compare time. Candidate "
                "relationships never count toward topic increase/decrease "
                "rankings."
            ),
            "continued": [],
            "added": [],
            "retired": [],
            "net_change": None,
        },
    }
    report["compare_sha256"] = sha256_value(report)
    return report


def build_decision_template(
    report: dict[str, Any], old_model: dict[str, Any], tool_version: str
) -> dict[str, Any]:
    decisions = []
    for proposal in report["proposals"]:
        suggestions = []
        for item in proposal["candidates"]:
            scores = item["scores"]
            suggestions.append(
                {
                    "old_topic_id": item["old_topic"]["topic_id"],
                    "old_name": item["old_topic"]["name"],
                    "old_local_id": item["old_topic"]["local_id"],
                    "composite": scores["composite"],
                    "term_score": scores["term_score"],
                    "document_affinity": scores["document_affinity"],
                    "support_jaccard": scores["support_jaccard"],
                    "co_assigned_count": item["evidence"]["co_assigned_count"],
                }
            )
        decisions.append(
            {
                "new_local_id": proposal["new_local_id"],
                "new_fingerprint": proposal["new_fingerprint"],
                "new_top_terms": proposal["new_top_terms"],
                "status": proposal["status"],
                "flags": proposal["flags"],
                "suggested_topic_id": proposal["suggested_topic_id"],
                "suggested_name": proposal["suggested_name"],
                "candidates": suggestions,
                "action": "",
                "continue_topic_id": "",
                "new_topic_id": "",
                "new_name": "",
                "note": "",
            }
        )
    retirements = [
        {
            "topic_id": topic["topic_id"],
            "name": topic["name"],
            "local_id": topic["local_id"],
            "action": "",
        }
        for topic in report["old_topics"]
    ]
    return {
        "schema": DECISION_SCHEMA,
        "tool_version": tool_version,
        "binding": {
            "old_period": report["old"]["period"],
            "new_period": report["new"]["period"],
            "old_input_sha256": report["old"]["input_sha256"],
            "new_input_sha256": report["new"]["input_sha256"],
            "old_model_summary_sha256": report["old"]["model_summary_sha256"],
            "new_model_summary_sha256": report["new"]["model_summary_sha256"],
            "plan_sha256": report["old"]["plan_sha256"],
            "old_names_sha256": report["old"]["names_sha256"],
            "compare_sha256": report["compare_sha256"],
            "compare_config": report["config"],
        },
        "instructions": (
            "For every decisions[] entry choose exactly one action: "
            "'continue' with continue_topic_id copied from an old topic "
            "(name is inherited), or 'new' with a fresh new_topic_id and "
            "new_name. Empty or 'defer' actions are rejected at apply time. "
            "Retirements are optional; only action='retire' entries count. "
            "Do not edit binding, fingerprints, or snapshots."
        ),
        "decisions": decisions,
        "retirements": retirements,
    }


def write_compare_outputs(
    report: dict[str, Any],
    decision_template: dict[str, Any],
    out_dir: Path,
) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    compare_path = out_dir / "compare.json"
    decision_path = out_dir / "decision.template.json"
    html_path = out_dir / "compare.review.html"
    write_json_atomic(compare_path, report)
    write_json_atomic(decision_path, decision_template)
    write_text_atomic(html_path, render_report_html(report, decision_template))
    return {
        "compare": str(compare_path),
        "decision_template": str(decision_path),
        "html": str(html_path),
    }


def _fmt(value: float | int | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_report_html(
    report: dict[str, Any], template: dict[str, Any]
) -> str:
    representative_by_fp = {
        item["fingerprint"]: item["representatives"]
        for item in report.get("new_representatives", [])
    }
    parts: list[str] = []
    add = parts.append

    add(
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        "<title>Topic name review</title><style>"
        "body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
        "margin:2rem auto;max-width:1100px;padding:0 1rem;color:#1d232b;"
        "line-height:1.5}"
        "h1{font-size:1.5rem}h2{font-size:1.2rem;margin-top:2rem;"
        "border-bottom:1px solid #dfe3e8;padding-bottom:.3rem}"
        "table{border-collapse:collapse;width:100%;margin:.8rem 0;font-size:.9rem}"
        "th,td{border:1px solid #dfe3e8;padding:.4rem .5rem;text-align:left;"
        "vertical-align:top}th{background:#f5f7fa}"
        ".muted{color:#667085;font-size:.85rem}"
        ".tag{display:inline-block;background:#eef2f7;border-radius:4px;"
        "padding:.05rem .35rem;margin:0 .2rem .2rem 0;font-size:.8rem}"
        ".warn{background:#fff7e6;border:1px solid #f5c26b;padding:.6rem .8rem;"
        "border-radius:6px;margin:.8rem 0}"
        ".readonly{background:#eef6ee;border:1px solid #9ccf9c;padding:.6rem .8rem;"
        "border-radius:6px}"
        ".evidence{font-family:ui-monospace,monospace;font-size:.85rem;"
        "white-space:pre-wrap}"
        "code{background:#f3f4f6;padding:.05rem .25rem;border-radius:3px}"
        "</style></head><body>"
    )
    add("<h1>主题名称迁移审阅（只读）</h1>")
    add(
        "<p class='readonly'>此 HTML 仅为只读审阅。请在 "
        f"<code>{escape('decision.template.json')}</code> 中填写决定；"
        "工具不会在本页修改任何名称。</p>"
    )
    add(
        f"<p class='muted'>旧期 <b>{escape(report['old']['period'])}</b> "
        f"→ 新期 <b>{escape(report['new']['period'])}</b>；"
        f"共享词项 {report['comparable_space']['shared_vocabulary_terms']} 个，"
        f"共享工单 {report['comparable_space']['shared_tickets']} 条。</p>"
    )
    for warning in report["warnings"]:
        add(f"<div class='warn'>{escape(warning)}</div>")

    add("<h2>绑定摘要</h2><table><tr><th>项</th><th>旧期</th><th>新期</th></tr>")
    for label, key in (
        ("period", "period"),
        ("input sha256", "input_sha256"),
        ("model summary", "model_summary_sha256"),
        ("plan sha256", "plan_sha256"),
        ("topics", "n_topics"),
    ):
        add(
            f"<tr><td>{escape(label)}</td>"
            f"<td class='evidence'>{escape(str(report['old'][key]))}</td>"
            f"<td class='evidence'>{escape(str(report['new'][key]))}</td></tr>"
        )
    add("</table>")

    add("<h2>旧期业务名称（人工回查）</h2>")
    add("<table><tr><th>local</th><th>business topic_id</th><th>名称</th>"
        "<th>关键词</th><th>已归属</th><th>fingerprint</th></tr>")
    for topic in report["old_topics"]:
        add(
            f"<tr><td>{topic['local_id']}</td>"
            f"<td>{escape(topic['topic_id'] or '（未命名）')}</td>"
            f"<td>{escape(topic['name'] or '')}</td>"
            f"<td>{' '.join(escape(t) for t in topic['top_terms'])}</td>"
            f"<td>{topic['n_assigned']}</td>"
            f"<td class='evidence'>{escape(topic['fingerprint'][:16])}…</td></tr>"
        )
    add("</table>")

    add("<h2>新期主题与候选（候选不等于确认）</h2>")
    for proposal in sorted(report["proposals"], key=lambda p: p["new_local_id"]):
        add(
            f"<div style='margin-top:1.2rem'><b>新 topic {proposal['new_local_id']}</b> "
            f"<span class='muted'>fingerprint {escape(proposal['new_fingerprint'][:16])}… · "
            f"已归属 {proposal['n_assigned']}</span><br>"
            f"<span class='muted'>关键词：</span>"
            + " ".join(
                f"<span class='tag'>{escape(term)}</span>"
                for term in proposal["new_top_terms"]
            )
            + "<br><span class='muted'>状态：</span>"
            f"<code>{escape(proposal['status'])}</code>"
        )
        for flag in proposal["flags"]:
            add(f"<span class='tag'>{escape(flag)}</span>")
        if proposal["suggested_topic_id"]:
            add(
                f"<div>建议延续：<b>{escape(proposal['suggested_topic_id'])}</b> "
                f"（{escape(proposal['suggested_name'] or '')}）</div>"
            )
        else:
            add("<div class='warn'>没有达到候选阈值的旧主题；只能人工新建名称。</div>")
        add(
            "<table><tr><th>旧 local</th><th>business topic_id</th><th>名称</th>"
            "<th>composite</th><th>term</th><th>doc</th><th>jaccard</th>"
            "<th>共同归属工单</th></tr>"
        )
        for item in proposal["candidates"]:
            scores = item["scores"]
            old = item["old_topic"]
            add(
                f"<tr><td>{old['local_id']}</td>"
                f"<td>{escape(old['topic_id'] or '（未命名）')}</td>"
                f"<td>{escape(old['name'] or '')}</td>"
                f"<td>{_fmt(scores['composite'])}</td>"
                f"<td>{_fmt(scores['term_score'])}</td>"
                f"<td>{_fmt(scores['document_affinity'])}</td>"
                f"<td>{_fmt(scores['support_jaccard'])}</td>"
                f"<td class='evidence'>{escape(', '.join(item['evidence']['co_assigned_ticket_ids']) or '—')}</td></tr>"
            )
        add("</table>")
        add("<div class='muted'>代表工单（来自新期，含原文）：</div>")
        for rep in representative_by_fp.get(proposal["new_fingerprint"], [])[:3]:
            add(
                f"<div class='evidence'>[{escape(rep['period'])}] "
                f"{escape(rep['ticket_id'])} · {escape(rep['channel'])} · "
                f"{escape(rep['title'])} · score {_fmt(rep.get('assignment_score'))}\n"
                f"{escape(rep['body'])}</div>"
            )
        add("</div>")

    add("<h2>竞争关系（待人工决定）</h2>")
    if report["competition"]:
        add("<ul>")
        for item in report["competition"]:
            cand = "；".join(
                f"{c['old_topic_id']}（{c['old_name'] or ''}）={c['composite']:.3f}"
                for c in item["candidates"]
            )
            add(
                f"<li>新 topic {item['new_local_id']}："
                f"{escape(', '.join(item['flags']))}；候选 {escape(cand)}</li>"
            )
        add("</ul>")
        add(
            "<div class='warn'>拆分/合并/多近邻候选不会自动继承名称；"
            "请在 decision 模板中逐项明确选择。</div>"
        )
    else:
        add("<p class='muted'>没有检测到竞争关系。</p>")

    add("<h2>未决旧主题</h2>")
    if report["unresolved_old_topics"]:
        add("<ul>")
        for item in report["unresolved_old_topics"]:
            add(
                f"<li>{escape(item['topic_id'] or '（未命名）')} "
                f"（{escape(item['name'] or '')}）local={item['local_id']}："
                f"{escape(item['note'])}</li>"
            )
        add("</ul>")
        add(
            "<div class='muted'>这些旧主题没有候选，但不等于已确认删除；"
            "只有 decision 中显式 retire 才计入确认变化。</div>"
        )
    else:
        add("<p class='muted'>没有未决旧主题。</p>")

    add("<h2>确认的主题增减</h2>")
    add(
        "<p class='muted'>候选关系不计入排名。当前比较阶段没有任何确认关系；"
        "在 apply-names 后由 decision 文件产生确认计数。</p>"
    )
    add(
        f"<p class='muted'>decision 模板：<code>"
        f"{escape(template['schema'])}</code>，"
        f"共 {len(template['decisions'])} 个新主题待决定。</p>"
    )
    add("</body></html>")
    return "".join(parts)
