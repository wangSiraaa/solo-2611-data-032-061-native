"""Command-line entry points: train, compare, apply-names.

Exit codes: 0 success, 2 input/model error, 3 stale binding, 4 invalid decision.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .apply import apply_names, load_decision, write_applied_outputs
from .compare import (
    build_decision_template,
    compare_models,
    write_compare_outputs,
)
from .data import load_period
from .errors import (
    BindingError,
    DecisionError,
    SourceChangedError,
    TopicMigrateError,
)
from .model import TrainConfig, train_model, validate_model
from .names import build_names_template, load_names
from .storage import (
    read_json,
    sha256_file,
    write_json_atomic,
)


def _load_json(path: Path, what: str) -> Any:
    try:
        return read_json(path)
    except FileNotFoundError:
        raise TopicMigrateError(f"{what} file not found: {path}")
    except ValueError as exc:
        raise TopicMigrateError(str(exc))


def _load_plan(path: Path) -> dict[str, Any]:
    plan = _load_json(path, "plan")
    if not isinstance(plan, dict):
        raise TopicMigrateError(f"{path}: plan must be a JSON object")
    return plan


def _load_model(path: Path) -> dict[str, Any]:
    model = _load_json(path, "model")
    validate_model(model)
    return model


def _period_keys(plan: dict[str, Any]) -> list[str]:
    keys = sorted(k for k, v in plan.items() if isinstance(v, dict) and "n_topics" in v)
    if not keys:
        raise TopicMigrateError(
            "plan must declare at least one period object with n_topics"
        )
    return keys


def _resolve_period_key(plan: dict[str, Any], requested: str) -> str:
    keys = _period_keys(plan)
    if requested in plan and isinstance(plan.get(requested), dict):
        return requested
    matches = [
        key
        for key in keys
        if requested in {key, key.replace("period_", ""), key.split("_")[-1]}
    ]
    if len(matches) == 1:
        return matches[0]
    raise TopicMigrateError(
        f"period {requested!r} is not declared in plan; available: {keys}"
    )


def run_train(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan)
    plan = _load_plan(plan_path)
    plan_sha = sha256_file(plan_path)
    period_key = _resolve_period_key(plan, args.period)
    period = period_key[len("period_"):] if period_key.startswith("period_") else period_key

    input_path = Path(args.input)
    loaded = load_period(input_path)
    model = train_model(
        loaded=loaded,
        period=period,
        plan=plan,
        period_key=period_key,
        plan_sha256=plan_sha,
        config=TrainConfig.from_plan(plan, period_key),
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "model.json"
    names_path = out_dir / "names.template.json"
    summary_path = out_dir / "summary.json"
    write_json_atomic(model_path, model)
    write_json_atomic(names_path, build_names_template(model))
    write_json_atomic(summary_path, _train_summary(model))

    print(
        f"trained period {period!r}: {len(model['topics'])} topics, "
        f"{model['input']['n_modelable']} modelable, "
        f"{model['input']['n_unmodelable']} empty"
    )
    print(f"model:   {model_path}")
    print(f"names:   {names_path}  (fill topic_id/name, then re-run train with --names)")
    print(f"summary: {summary_path}")
    for warning in model["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    if args.names:
        registry = load_names(Path(args.names))
        resolution = registry.resolve(model)
        named_path = out_dir / "names.json"
        write_json_atomic(
            named_path,
            {
                "schema": "topic-migrate/names/v1",
                "period": model["period"],
                "input_sha256": model["input"]["sha256"],
                "model_summary_sha256": model["summary_sha256"],
                "topics": [
                    {
                        "topic_id": registry.by_fingerprint()[t["fingerprint"]].topic_id,
                        "name": registry.by_fingerprint()[t["fingerprint"]].name,
                        "local_id": t["local_id"],
                        "fingerprint": t["fingerprint"],
                        "note": registry.by_fingerprint()[t["fingerprint"]].note,
                    }
                    for t in model["topics"]
                ],
            },
        )
        remapped = resolution["remap"]
        print(f"names resolved by fingerprint -> {named_path}")
        if remapped:
            print(f"local-id remap applied: {remapped}")
    return 0


def _train_summary(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "period": model["period"],
        "input": model["input"],
        "plan": model["plan"],
        "model": model["model"],
        "vocabulary_sha256": model["vocabulary_sha256"],
        "summary_sha256": model["summary_sha256"],
        "topics": [
            {
                "local_id": t["local_id"],
                "fingerprint": t["fingerprint"],
                "top_terms": t["top_terms"],
                "n_assigned": t["n_assigned"],
                "representative_ticket_ids": [
                    rep["ticket_id"] for rep in t["representative_tickets"]
                ],
            }
            for t in model["topics"]
        ],
        "warnings": model["warnings"],
    }


def run_compare(args: argparse.Namespace) -> int:
    old_model = _load_model(Path(args.old_model))
    new_model = _load_model(Path(args.new_model))
    old_names = load_names(Path(args.old_names))
    config = None
    if args.config:
        config = _load_json(Path(args.config), "compare config")
        if not isinstance(config, dict):
            raise TopicMigrateError("compare config must be a JSON object")
    report = compare_models(old_model, new_model, old_names, config)
    template = build_decision_template(report, old_model, __version__)
    outputs = write_compare_outputs(report, template, Path(args.out))
    print(
        f"compared {report['old']['period']!r} -> {report['new']['period']!r}: "
        f"{len(report['proposals'])} new topics, "
        f"{len(report['competition'])} with competing candidates, "
        f"{len(report['unresolved_old_topics'])} unresolved old topics"
    )
    for name, path in outputs.items():
        print(f"{name}: {path}")
    print("review the HTML, then edit the decision template and run apply-names")
    return 0


def run_apply(args: argparse.Namespace) -> int:
    old_model = _load_model(Path(args.old_model))
    new_model = _load_model(Path(args.new_model))
    old_names = load_names(Path(args.old_names))
    decision = load_decision(Path(args.decision))
    new_input = Path(args.new_input)
    report = None
    if args.compare_report:
        report = _load_json(Path(args.compare_report), "compare report")
        if not isinstance(report, dict):
            raise TopicMigrateError("compare report must be a JSON object")
    applied = apply_names(
        old_model, new_model, old_names, decision, new_input, report
    )
    outputs = write_applied_outputs(applied, Path(args.out))
    counts = applied["counts"]
    changes = applied["confirmed_changes"]["counts"]
    print(
        f"applied names for period {applied['period']!r}: "
        f"{counts['topics']} topics, {counts['assigned_tickets']} assigned, "
        f"{counts['unassigned_tickets']} unassigned, "
        f"{counts['unmodelable_tickets']} empty"
    )
    print(
        "confirmed changes: "
        f"continued={changes['continued']} added={changes['added']} "
        f"retired={changes['retired']} net={changes['net_confirmed_change']}"
    )
    for name, path in outputs.items():
        print(f"{name}: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="topic-migrate",
        description=(
            "Local topic analysis with fingerprint-bound business names: "
            "train, compare, apply-names."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    train = sub.add_parser("train", help="train one period's topic model")
    train.add_argument("--input", required=True, help="period JSONL")
    train.add_argument("--plan", required=True, help="plan JSON")
    train.add_argument("--period", required=True, help="period key, e.g. period_a or a")
    train.add_argument("--out", required=True, help="output directory")
    train.add_argument("--names", help="optional existing names to resolve by fingerprint")
    train.set_defaults(func=run_train)

    compare = sub.add_parser("compare", help="propose name continuations")
    compare.add_argument("--old-model", required=True)
    compare.add_argument("--new-model", required=True)
    compare.add_argument("--old-names", required=True)
    compare.add_argument("--out", required=True)
    compare.add_argument("--config", help="optional comparison weights JSON")
    compare.set_defaults(func=run_compare)

    apply_cmd = sub.add_parser("apply-names", help="apply explicit human decisions")
    apply_cmd.add_argument("--old-model", required=True)
    apply_cmd.add_argument("--new-model", required=True)
    apply_cmd.add_argument("--old-names", required=True)
    apply_cmd.add_argument("--decision", required=True)
    apply_cmd.add_argument("--new-input", required=True)
    apply_cmd.add_argument(
        "--compare-report",
        help="optional compare.json to verify against the decision binding",
    )
    apply_cmd.add_argument("--out", required=True)
    apply_cmd.set_defaults(func=run_apply)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BindingError as exc:
        print(f"binding error: {exc}", file=sys.stderr)
        return exc.exit_code
    except SourceChangedError as exc:
        print(f"source changed: {exc}", file=sys.stderr)
        return exc.exit_code
    except DecisionError as exc:
        print(f"decision error: {exc}", file=sys.stderr)
        return exc.exit_code
    except (TopicMigrateError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 2)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
