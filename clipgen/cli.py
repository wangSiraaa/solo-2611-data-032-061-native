"""Command-line interface: ``plan`` and ``build`` (and ``verify``)."""

import argparse
import json
import sys
from pathlib import Path

from .audio import ConfigError
from .writer import (
    BuildError,
    build_outputs,
    create_plan,
    load_plan,
    verify_outputs,
    write_plan_outputs,
)


def _default_parser():
    parser = argparse.ArgumentParser(
        prog="clipgen",
        description=(
            "Produce review-length speech clips and local annotations from "
            "16 kHz / 16-bit PCM WAV files.  Channel count is preserved and "
            "no resampling, denoising, or recognition is performed."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser(
        "plan",
        help="Validate annotations and write a deterministic clip plan.",
    )
    plan.add_argument("--annotations", required=True, type=Path, help="annotations.jsonl")
    plan.add_argument("--config", required=True, type=Path, help="config.json")
    plan.add_argument("--recordings", required=True, type=Path, help="directory containing WAV files")
    plan.add_argument("--plan", required=True, type=Path, help="output plan JSON path")
    plan.add_argument(
        "--rejected",
        type=Path,
        help="output rejected-records JSONL path (default: <plan>.rejected.jsonl)",
    )

    build = subparsers.add_parser(
        "build",
        help="Render WAV clips, manifest, and local annotations from a plan.",
    )
    build.add_argument("--plan", required=True, type=Path, help="plan produced by 'plan'")
    build.add_argument("--recordings", required=True, type=Path, help="directory containing WAV files")
    build.add_argument("--out", required=True, type=Path, help="output directory")

    verify = subparsers.add_parser(
        "verify",
        help="Check built WAV bytes and annotation coverage against the plan and sources.",
    )
    verify.add_argument("--plan", required=True, type=Path)
    verify.add_argument("--recordings", required=True, type=Path)
    verify.add_argument("--out", required=True, type=Path)
    return parser


def _fail(message, exit_code=2):
    print(f"error: {message}", file=sys.stderr)
    return exit_code


def main(argv=None):
    parser = _default_parser()
    args = parser.parse_args(argv)

    if args.command == "plan":
        try:
            plan, rejected = create_plan(args.annotations, args.config, args.recordings)
        except (ConfigError, FileNotFoundError) as exc:
            return _fail(str(exc))
        except ValueError as exc:
            return _fail(f"cannot plan: {exc}")
        except BuildError as exc:
            return _fail(f"cannot plan: {exc}")

        rejected_path = args.rejected or Path(str(args.plan) + ".rejected.jsonl")
        try:
            write_plan_outputs(plan, rejected, args.plan, rejected_path)
        except OSError as exc:
            return _fail(f"cannot write plan outputs: {exc}")

        print(
            json.dumps(
                {
                    "plan": str(args.plan),
                    "rejected": str(rejected_path),
                    "clips": len(plan["clips"]),
                    "annotations": len(plan["annotations"]),
                    "rejected_records": len(rejected),
                    "continuations": [
                        item["annotation_id"] for item in plan["continuations"]
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "build":
        try:
            plan = load_plan(args.plan)
            manifest = build_outputs(plan, args.recordings, args.out)
        except (BuildError, ValueError, OSError) as exc:
            return _fail(f"build failed: {exc}", exit_code=1)
        print(
            json.dumps(
                {
                    "out": str(args.out),
                    "clips": len(manifest["clips"]),
                    "manifest": str(Path(args.out) / "manifest.json"),
                    "local_annotations": str(Path(args.out) / "local_annotations.jsonl"),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "verify":
        try:
            report = verify_outputs(args.plan, args.recordings, args.out)
        except (BuildError, ValueError, OSError) as exc:
            return _fail(f"verification failed: {exc}", exit_code=1)
        print(json.dumps({"verified": True, **report}, ensure_ascii=False))
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2
