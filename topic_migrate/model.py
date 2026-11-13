"""Per-period topic model: preprocessing, NMF training, assignment, artifacts.

Design constraints from the task:

* The vocabulary and preprocessing statistics are fit from *this period only*.
* Topic numbers are local to the model. A content fingerprint identifies a
  topic across runs, and the model summary is invariant under a consistent
  renumbering of local topic ids.
* Documents with too little evidence stay unassigned instead of being forced
  into the nearest topic.
* Original ticket text is preserved in the artifact for human review.
"""

from __future__ import annotations

import math
import re
import unicodedata
import warnings
from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.decomposition import NMF
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction.text import TfidfVectorizer

from . import __version__
from .data import LoadedPeriod, Ticket
from .errors import ModelError
from .storage import sha256_bytes, sha256_file, sha256_value, sorted_terms

MODEL_SCHEMA = "topic-migrate/model/v1"

# A token is a run of CJK ideographs or an ASCII alphanumeric word. The
# fixtures are space-delimited, so this also keeps multi-character Chinese
# terms intact while tolerating punctuation between them.
TOKEN_RE = re.compile(r"[0-9A-Za-z_]+|[㐀-鿿]+")

DEFAULTS = {
    "max_df": 0.8,
    "min_df": 1,
    "sublinear_tf": True,
    "max_iter": 4000,
    "tol": 1e-8,
    "init": "nndsvda",
    "support_terms": 12,
    "keywords": 20,
    "representatives": 5,
    "min_assignment_score": 0.15,
    "min_margin": 0.05,
}


def tokenize(text: str) -> list[str]:
    """Normalize and split text into tokens without external services."""
    normalized = unicodedata.normalize("NFKC", text)
    return [m.group(0).lower() for m in TOKEN_RE.finditer(normalized)]


def _round(value: float, digits: int = 8) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ModelError("model produced a non-finite number")
    rounded = round(value, digits)
    return 0.0 if rounded == 0 else rounded


@dataclass(frozen=True)
class TrainConfig:
    n_topics: int
    seed: int
    min_document_evidence: int
    max_df: float
    min_df: int
    support_terms: int
    keywords: int
    representatives: int
    min_assignment_score: float
    min_margin: float
    max_iter: int
    tol: float
    init: str
    sublinear_tf: bool

    @classmethod
    def from_plan(cls, plan: dict[str, Any], period_key: str) -> "TrainConfig":
        if not isinstance(plan, dict):
            raise ModelError("plan must be a JSON object")
        if period_key not in plan or not isinstance(plan[period_key], dict):
            raise ModelError(f"plan has no object entry for period {period_key!r}")
        period_plan = plan[period_key]
        n_topics = period_plan.get("n_topics")
        if not isinstance(n_topics, int) or isinstance(n_topics, bool) or n_topics < 1:
            raise ModelError(
                f"plan {period_key}.n_topics must be a positive integer"
            )
        seed = plan.get("seed", 0)
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ModelError("plan seed must be an integer")
        min_ev = plan.get("min_document_evidence", 2)
        if not isinstance(min_ev, int) or isinstance(min_ev, bool) or min_ev < 1:
            raise ModelError("plan min_document_evidence must be a positive integer")

        defaults = dict(DEFAULTS)
        for key in (
            "max_df",
            "min_df",
            "support_terms",
            "keywords",
            "representatives",
            "min_assignment_score",
            "min_margin",
            "max_iter",
            "tol",
            "init",
            "sublinear_tf",
        ):
            if key in period_plan:
                defaults[key] = period_plan[key]

        max_df = defaults["max_df"]
        if not isinstance(max_df, (int, float)) or isinstance(max_df, bool):
            raise ModelError("max_df must be a number")
        if not (0.0 < float(max_df) <= 1.0):
            raise ModelError("max_df must be in (0, 1]")
        min_df = defaults["min_df"]
        if not isinstance(min_df, int) or isinstance(min_df, bool) or min_df < 1:
            raise ModelError("min_df must be a positive integer")
        for name in ("support_terms", "keywords", "representatives", "max_iter"):
            value = defaults[name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ModelError(f"{name} must be a positive integer")
        tol = defaults["tol"]
        if not isinstance(tol, (int, float)) or isinstance(tol, bool) or not tol > 0:
            raise ModelError("tol must be positive")
        for name in ("min_assignment_score", "min_margin"):
            value = defaults[name]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ModelError(f"{name} must be a number")
            if not (0.0 <= float(value) <= 1.0):
                raise ModelError(f"{name} must be in [0, 1]")
        init = defaults["init"]
        if not isinstance(init, str):
            raise ModelError("init must be a string")
        sublinear = defaults["sublinear_tf"]
        if not isinstance(sublinear, bool):
            raise ModelError("sublinear_tf must be a boolean")

        return cls(
            n_topics=n_topics,
            seed=seed,
            min_document_evidence=min_ev,
            max_df=float(max_df),
            min_df=min_df,
            support_terms=defaults["support_terms"],
            keywords=defaults["keywords"],
            representatives=defaults["representatives"],
            min_assignment_score=float(defaults["min_assignment_score"]),
            min_margin=float(defaults["min_margin"]),
            max_iter=defaults["max_iter"],
            tol=float(tol),
            init=init,
            sublinear_tf=sublinear,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_topics": self.n_topics,
            "seed": self.seed,
            "min_document_evidence": self.min_document_evidence,
            "max_df": self.max_df,
            "min_df": self.min_df,
            "sublinear_tf": self.sublinear_tf,
            "support_terms": self.support_terms,
            "keywords": self.keywords,
            "representatives": self.representatives,
            "min_assignment_score": self.min_assignment_score,
            "min_margin": self.min_margin,
            "max_iter": self.max_iter,
            "tol": self.tol,
            "init": self.init,
        }


def _build_vectorizer(config: TrainConfig) -> TfidfVectorizer:
    return TfidfVectorizer(
        tokenizer=tokenize,
        token_pattern=None,
        lowercase=False,
        preprocessor=None,
        max_df=config.max_df,
        min_df=config.min_df,
        sublinear_tf=config.sublinear_tf,
        norm="l2",
        dtype=np.float64,
    )


def _validate_capacity(
    config: TrainConfig, modelable: list[Ticket], n_features: int
) -> None:
    if not modelable:
        raise ModelError("no modelable tickets (all bodies are empty)")
    if config.n_topics > len(modelable):
        raise ModelError(
            f"n_topics={config.n_topics} exceeds modelable tickets "
            f"({len(modelable)})"
        )
    if n_features < 1:
        raise ModelError(
            "preprocessing removed every term; max_df/min_df leave no vocabulary"
        )
    if config.n_topics > n_features:
        raise ModelError(
            f"n_topics={config.n_topics} exceeds vocabulary size ({n_features})"
        )


def train_model(
    loaded: LoadedPeriod,
    period: str,
    plan: dict[str, Any],
    period_key: str,
    plan_sha256: str,
    config: TrainConfig | None = None,
) -> dict[str, Any]:
    """Train one period and return a serializable, reviewable model artifact."""
    if config is None:
        config = TrainConfig.from_plan(plan, period_key)

    modelable = loaded.modelable
    if not modelable:
        raise ModelError("no modelable tickets (all bodies are empty)")
    if config.n_topics > len(modelable):
        raise ModelError(
            f"n_topics={config.n_topics} exceeds modelable tickets "
            f"({len(modelable)})"
        )
    texts = [t.model_text() for t in modelable]
    vectorizer = _build_vectorizer(config)
    try:
        matrix = vectorizer.fit_transform(texts)
    except ValueError as exc:
        raise ModelError(f"preprocessing failed: {exc}") from exc
    vocab = list(vectorizer.get_feature_names_out())
    _validate_capacity(config, modelable, len(vocab))

    nmf = NMF(
        n_components=config.n_topics,
        init=config.init,
        random_state=config.seed,
        max_iter=config.max_iter,
        tol=config.tol,
    )
    fit_warnings: list[str] = []
    try:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            weights = nmf.fit_transform(matrix)
            components = nmf.components_
    except (ValueError, RuntimeError) as exc:
        raise ModelError(f"topic model failed to train: {exc}") from exc
    seen_warnings: set[str] = set()
    for entry in captured:
        if issubclass(entry.category, ConvergenceWarning):
            text = "topic model did not fully converge; increase max_iter"
        elif issubclass(entry.category, RuntimeWarning):
            text = "topic model hit a numerically degenerate component"
        else:
            continue
        if text not in seen_warnings:
            seen_warnings.add(text)
            fit_warnings.append(text)

    if not np.all(np.isfinite(weights)) or not np.all(np.isfinite(components)):
        raise ModelError("topic model produced non-finite parameters")
    # reconstruction_err_ is diagnostic only; degenerate all-zero components
    # can make it NaN, which must not abort training.
    reconstruction_error = (
        _round(float(nmf.reconstruction_err_))
        if np.isfinite(nmf.reconstruction_err_)
        else None
    )

    row_sums = weights.sum(axis=1, keepdims=True)
    proportions = np.divide(
        weights,
        row_sums,
        out=np.zeros_like(weights),
        where=row_sums > 0,
    )

    vocabulary = sorted(vocab)
    topical_terms: list[dict[str, float]] = []
    support_term_sets: list[set[str]] = []
    for local_id in range(config.n_topics):
        row = np.maximum(components[local_id], 0.0)
        total = float(row.sum())
        if total <= 0:
            distribute = {term: 0.0 for term in vocab}
        else:
            distribute = {
                term: _round(float(value) / total)
                for term, value in zip(vocab, row)
            }
        topical_terms.append(distribute)
        # Only terms with positive weight are evidence; zero-weight ties would
        # otherwise pad every topic with unrelated terms.
        positive = sorted_terms(
            {term: weight for term, weight in distribute.items() if weight > 0.0},
            config.support_terms,
        )
        support_term_sets.append(set(positive))

    assignments: list[dict[str, Any]] = []
    assigned_by_topic: dict[int, list[dict[str, Any]]] = {
        i: [] for i in range(config.n_topics)
    }
    for row_index, ticket in enumerate(modelable):
        doc_tokens = set(tokenize(ticket.model_text()))
        scores = {
            str(i): _round(float(proportions[row_index, i]))
            for i in range(config.n_topics)
        }
        ordered = sorted(
            ((i, float(proportions[row_index, i])) for i in range(config.n_topics)),
            key=lambda pair: (-pair[1], pair[0]),
        )
        top_id, top_score = ordered[0]
        second_score = ordered[1][1] if len(ordered) > 1 else 0.0
        margin = top_score - second_score
        support_terms = sorted(doc_tokens & support_term_sets[top_id])
        support = len(support_terms)

        reason = "assigned"
        assigned_id: int | None = top_id
        if not doc_tokens:
            reason = "no_modelable_text"
            assigned_id = None
        elif top_score <= 0:
            reason = "no_vocabulary_evidence"
            assigned_id = None
        elif support < config.min_document_evidence:
            reason = "insufficient_evidence"
            assigned_id = None
        elif top_score < config.min_assignment_score:
            reason = "low_assignment_score"
            assigned_id = None
        elif margin < config.min_margin:
            reason = "ambiguous_assignment"
            assigned_id = None

        record = {
            "ticket_id": ticket.ticket_id,
            "status": "assigned" if assigned_id is not None else "unassigned",
            "local_id": assigned_id,
            "scores": scores,
            "top_score": _round(top_score),
            "second_score": _round(second_score),
            "margin": _round(margin),
            "support": support,
            "support_terms": support_terms,
            "reason": reason,
        }
        assignments.append(record)
        if assigned_id is not None:
            assigned_by_topic[assigned_id].append(record)

    topics: list[dict[str, Any]] = []
    warnings_list: list[str] = list(fit_warnings)
    for local_id in range(config.n_topics):
        distribute = topical_terms[local_id]
        keyword_list = [
            {"term": term, "weight": distribute[term]}
            for term in sorted_terms(
                {term: weight for term, weight in distribute.items() if weight > 0.0},
                config.keywords,
            )
        ]
        members = sorted(
            assigned_by_topic[local_id],
            key=lambda rec: (-rec["top_score"], rec["ticket_id"]),
        )
        representative_records = members[: config.representatives]
        ticket_by_id = {t.ticket_id: t for t in modelable}
        representatives = []
        for rec in representative_records:
            ticket = ticket_by_id[rec["ticket_id"]]
            representatives.append(
                {
                    **ticket.review_payload(),
                    "period": period,
                    "assignment_score": rec["top_score"],
                }
            )
        topic = {
            "local_id": local_id,
            "keywords": keyword_list,
            "top_terms": [item["term"] for item in keyword_list[:10]],
            "term_weights": {
                term: distribute[term]
                for term in sorted(distribute)
                if distribute[term] > 0.0
            },
            "support_terms": sorted(support_term_sets[local_id]),
            "n_assigned": len(members),
            "assigned_ticket_ids": sorted(rec["ticket_id"] for rec in members),
            "representative_tickets": representatives,
        }
        topic["fingerprint"] = _fingerprint(topic)
        if not members:
            warnings_list.append(
                f"topic local_id={local_id} has no assigned documents; "
                "review n_topics or evidence thresholds"
            )
        topics.append(topic)

    fingerprints = [t["fingerprint"] for t in topics]
    if len(set(fingerprints)) != len(fingerprints):
        raise ModelError(
            "two topics have identical content; reduce n_topics or adjust preprocessing"
        )

    vocabulary_bytes = "\n".join(vocabulary).encode("utf-8")
    model_artifact: dict[str, Any] = {
        "schema": MODEL_SCHEMA,
        "period": period,
        "created_by": {"tool": "topic-migrate", "version": __version__},
        "input": {
            "file_name": loaded.path.name,
            "sha256": sha256_file(loaded.path),
            "n_records": loaded.n_input_records,
            "n_tickets": len(loaded.tickets),
            "n_modelable": len(modelable),
            "n_unmodelable": len(loaded.unmodelable),
            "duplicate_identical": loaded.duplicate_identical,
        },
        "plan": {
            "period_key": period_key,
            "sha256": plan_sha256,
            "config": config.as_dict(),
        },
        "preprocessing": {
            "tokenizer": "regex:latin-word|cjk-run",
            "normalization": "NFKC+lowercase",
            "text_fields": ["body"],
            "fit_scope": "current_period_only",
            "max_df": config.max_df,
            "min_df": config.min_df,
            "sublinear_tf": config.sublinear_tf,
            "tfidf_norm": "l2",
        },
        "model": {
            "method": "NMF",
            "init": config.init,
            "random_state": config.seed,
            "max_iter": config.max_iter,
            "tol": config.tol,
            "n_topics": config.n_topics,
            "n_features": len(vocabulary),
            "n_documents": len(modelable),
            "reconstruction_error": reconstruction_error,
            "n_iter": int(nmf.n_iter_),
        },
        "vocabulary": vocabulary,
        "vocabulary_sha256": sha256_bytes(vocabulary_bytes),
        "topics": topics,
        "assignments": assignments,
        "tickets": [
            {**t.review_payload(), "modelable": t.is_modelable}
            for t in loaded.tickets
        ],
        "unmodelable": [
            {
                **t.review_payload(),
                "modelable": False,
                "reason": "empty_text",
            }
            for t in loaded.unmodelable
        ],
        "warnings": warnings_list,
    }
    model_artifact["summary_sha256"] = model_summary_hash(model_artifact)
    validate_model(model_artifact)
    return model_artifact


def _fingerprint(topic: dict[str, Any]) -> str:
    payload = {
        "keywords": [[k["term"], k["weight"]] for k in topic["keywords"]],
        "term_weights": topic["term_weights"],
        "support_terms": topic["support_terms"],
        "n_assigned": topic["n_assigned"],
        "assigned_ticket_ids": sorted(topic["assigned_ticket_ids"]),
        "representative_ticket_ids": sorted(
            ticket["ticket_id"] for ticket in topic["representative_tickets"]
        ),
    }
    return sha256_value(payload)


def _summary_payload(model: dict[str, Any]) -> dict[str, Any]:
    """Canonical summary input, independent of local integer topic numbers."""
    topic_by_fp = {t["fingerprint"]: t for t in model["topics"]}
    topics = []
    for fingerprint in sorted(topic_by_fp):
        topic = topic_by_fp[fingerprint]
        topics.append(
            {
                "fingerprint": fingerprint,
                "keywords": [
                    [k["term"], k["weight"]] for k in topic["keywords"]
                ],
                "term_weights": topic["term_weights"],
                "support_terms": topic["support_terms"],
                "n_assigned": topic["n_assigned"],
                "assigned_ticket_ids": sorted(topic["assigned_ticket_ids"]),
                "representative_ticket_ids": sorted(
                    ticket["ticket_id"]
                    for ticket in topic["representative_tickets"]
                ),
            }
        )
    assignments = []
    for record in sorted(model["assignments"], key=lambda r: r["ticket_id"]):
        if record["local_id"] is None:
            topic_fp = None
        else:
            topic_fp = model["topics"][record["local_id"]]["fingerprint"]
        scores_by_fp = {
            model["topics"][int(local)]["fingerprint"]: score
            for local, score in record["scores"].items()
        }
        assignments.append(
            {
                "ticket_id": record["ticket_id"],
                "status": record["status"],
                "topic_fingerprint": topic_fp,
                "scores": scores_by_fp,
                "top_score": record["top_score"],
                "second_score": record["second_score"],
                "margin": record["margin"],
                "support": record["support"],
                "support_terms": sorted(record["support_terms"]),
                "reason": record["reason"],
            }
        )
    return {
        "schema": model["schema"],
        "period": model["period"],
        "input_sha256": model["input"]["sha256"],
        "input_record_counts": {
            "n_records": model["input"]["n_records"],
            "n_modelable": model["input"]["n_modelable"],
            "n_unmodelable": model["input"]["n_unmodelable"],
        },
        "plan": model["plan"],
        "preprocessing": model["preprocessing"],
        "model": {
            key: value
            for key, value in model["model"].items()
            if key not in {"n_iter"}
        },
        "vocabulary_sha256": model["vocabulary_sha256"],
        "topics": topics,
        "assignments": assignments,
        "unmodelable_ticket_ids": sorted(
            ticket["ticket_id"] for ticket in model["unmodelable"]
        ),
    }


def model_summary_hash(model: dict[str, Any]) -> str:
    return sha256_value(_summary_payload(model))


def validate_model(model: dict[str, Any]) -> None:
    """Validate structure and internal consistency of a model artifact.

    A consistent permutation of local ids is valid; an inconsistent one (for
    example reordered topics with stale assignments) is rejected.
    """
    if not isinstance(model, dict) or model.get("schema") != MODEL_SCHEMA:
        raise ModelError(f"not a {MODEL_SCHEMA} artifact")
    period = model.get("period")
    if not isinstance(period, str) or not period:
        raise ModelError("model period is missing")
    topics = model.get("topics")
    assignments = model.get("assignments")
    tickets = model.get("tickets")
    if not isinstance(topics, list) or not topics:
        raise ModelError("model has no topics")
    if not isinstance(assignments, list) or not isinstance(tickets, list):
        raise ModelError("model assignments/tickets are missing")

    k = len(topics)
    if model["model"].get("n_topics") != k:
        raise ModelError("model n_topics does not match topics length")
    local_ids = [t.get("local_id") for t in topics]
    if sorted(local_ids) != list(range(k)):
        raise ModelError("topic local ids must be exactly 0..k-1")
    if local_ids != list(range(k)):
        raise ModelError("topic list must be ordered by local_id (index == local_id)")
    fingerprints = [t.get("fingerprint") for t in topics]
    if any(not isinstance(fp, str) or not fp for fp in fingerprints):
        raise ModelError("topic fingerprint is missing")
    if len(set(fingerprints)) != k:
        raise ModelError("topic fingerprints are not unique")
    for topic in topics:
        if topic.get("n_assigned") != len(topic.get("assigned_ticket_ids", [])):
            raise ModelError(
                f"topic {topic['local_id']} n_assigned is inconsistent"
            )
        if topic["fingerprint"] != _fingerprint(topic):
            raise ModelError(
                f"topic {topic['local_id']} fingerprint does not match its content"
            )

    ticket_ids = {t.get("ticket_id") for t in tickets}
    if len(ticket_ids) != len(tickets):
        raise ModelError("model tickets contain duplicate ticket ids")
    assignment_ids = [a.get("ticket_id") for a in assignments]
    if len(set(assignment_ids)) != len(assignment_ids):
        raise ModelError("assignments contain duplicate ticket ids")
    if not set(assignment_ids) <= ticket_ids:
        raise ModelError("assignment references a ticket absent from tickets")

    expected_scores = {str(i) for i in range(k)}
    seen_by_topic: dict[int, set[str]] = {i: set() for i in range(k)}
    for record in assignments:
        local_id = record.get("local_id")
        scores = record.get("scores")
        if not isinstance(scores, dict) or set(scores) != expected_scores:
            raise ModelError(
                f"assignment {record.get('ticket_id')} has invalid score keys"
            )
        if record.get("status") == "assigned":
            if local_id not in range(k):
                raise ModelError(
                    f"assignment {record['ticket_id']} has invalid local_id"
                )
            seen_by_topic[local_id].add(record["ticket_id"])
        elif record.get("status") == "unassigned":
            if local_id is not None:
                raise ModelError(
                    f"unassigned {record['ticket_id']} has a local_id"
                )
        else:
            raise ModelError("assignment status must be assigned|unassigned")

    for topic in topics:
        expected = set(topic["assigned_ticket_ids"])
        if seen_by_topic[topic["local_id"]] != expected:
            raise ModelError(
                f"topic {topic['local_id']} assignments disagree with topic "
                "assigned_ticket_ids"
            )
        for rep in topic["representative_tickets"]:
            if rep.get("period") != period:
                raise ModelError(
                    f"representative {rep.get('ticket_id')} is not from period "
                    f"{period!r}"
                )
            if rep.get("ticket_id") not in expected:
                raise ModelError(
                    f"representative {rep.get('ticket_id')} is not assigned to "
                    f"topic {topic['local_id']}"
                )

    computed = model_summary_hash(model)
    if model.get("summary_sha256") != computed:
        raise ModelError(
            "model summary hash mismatch; artifact was edited inconsistently"
        )


def permute_model(model: dict[str, Any], order: list[int]) -> dict[str, Any]:
    """Consistently relabel local topic numbers.

    ``order[new_local_id] = old_local_id``. Fingerprints and the summary hash
    are content-based and therefore must not change.
    """
    validate_model(model)
    k = len(model["topics"])
    if sorted(order) != list(range(k)):
        raise ModelError(f"order must be a permutation of 0..{k - 1}")
    mapping = {old: new for new, old in enumerate(order)}
    result = _deep_copy(model)
    result["topics"] = []
    for old_id in order:
        topic = _deep_copy(model["topics"][old_id])
        topic["local_id"] = mapping[old_id]
        result["topics"].append(topic)
    for record in result["assignments"]:
        record["scores"] = {
            str(mapping[int(local)]): score
            for local, score in record["scores"].items()
        }
        if record["local_id"] is not None:
            record["local_id"] = mapping[record["local_id"]]
    result["summary_sha256"] = model_summary_hash(result)
    validate_model(result)
    return result


def _deep_copy(value: Any) -> Any:
    import copy

    return copy.deepcopy(value)
