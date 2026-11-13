"""Command-line interface.

    python -m energyalloc allocate \
        --plan fixtures/plan.json \
        --parcels fixtures/parcels.geojson \
        --zones fixtures/zones.geojson \
        --out out/

    python -m energyalloc explain-parcel P2 \
        --plan ... --parcels ... --zones ... --out out/

Exit status: 0 when every check passes, 1 when output was produced but a
closure check failed, 2 on rejected input.
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import AllocationResult, compute
from .model import Scenario, ValidationError
from .output import parcel_evidence, write_all
from .payload import load_inputs


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="energyalloc",
        description=(
            "Allocate parcel energy to service zones with priority "
            "adjudication and integer Wh conservation."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser, *, require_out: bool = True) -> None:
        p.add_argument("--plan", required=True, help="path to plan.json")
        p.add_argument("--parcels", required=True, help="path to parcels.geojson")
        p.add_argument("--zones", required=True, help="path to zones.geojson")
        p.add_argument(
            "--out",
            required=require_out,
            help=(
                "output directory (created if missing)"
                if require_out
                else "output directory, required with --write-out"
            ),
        )

    alloc = sub.add_parser("allocate", help="run the full allocation")
    add_common(alloc)

    explain = sub.add_parser(
        "explain-parcel",
        help="show evidence for a single parcel",
    )
    explain.add_argument("parcel_id", help="parcel_id to explain")
    add_common(explain, require_out=False)
    explain.add_argument(
        "--write-out",
        action="store_true",
        help="also write the full allocation outputs (requires --out)",
    )

    return parser


def _load(args: argparse.Namespace) -> Scenario:
    return load_inputs(args.plan, args.parcels, args.zones)


def _print_checks(result: AllocationResult) -> None:
    stats = result.stats
    print(
        "parcels: {parcels_total} accepted ({parcels_ok} all-checks-passed), "
        "{rejected_parcels} rejected, {zero} zero-area".format(
            parcels_total=stats["parcels_total"],
            parcels_ok=stats["parcels_ok"],
            rejected_parcels=stats["rejected_parcels"],
            zero=len(stats["zero_area_parcels"]),
        )
    )
    print(
        "fragments: {n} | candidate zone-parcel pairs: {cand} | "
        "exact intersection operations: {exact}".format(
            n=stats["fragments_total"],
            cand=stats["candidate_pairs"],
            exact=stats["exact_intersections"],
        )
    )
    print(
        "energy: accepted parcels {a} Wh, allocated {b} Wh, difference {d} Wh".format(
            a=stats["energy_input_wh_accepted_parcels"],
            b=stats["energy_allocated_wh"],
            d=stats["energy_allocated_wh"]
            - stats["energy_input_wh_accepted_parcels"],
        )
    )
    print(
        "geometry checks: {p}/{n} passed | energy checks: {q}/{n} passed".format(
            p=result.checks["geometry"]["parcels_passed"],
            q=result.checks["energy"]["parcels_passed"],
            n=result.checks["geometry"]["parcels_checked"],
        )
    )
    print(
        "rejected-parcel energy reported separately: {e} Wh".format(
            e=stats["energy_reported_with_rejected_parcels_wh"]
        )
    )
    if stats["energy_unallocated_wh_zero_area_parcels"]:
        print(
            "zero-area parcels left unallocated (not reassigned): {e} Wh".format(
                e=stats["energy_unallocated_wh_zero_area_parcels"]
            )
        )


def cmd_allocate(args: argparse.Namespace) -> int:
    scenario = _load(args)
    result = compute(scenario)
    out = write_all(result, args.out)
    _print_checks(result)
    print(f"wrote {out / 'fragments.geojson'}")
    print(f"wrote {out / 'allocation.csv'}")
    print(f"wrote {out / 'evidence/'}")
    print(f"wrote {out / 'summary.json'}")
    return 0 if result.checks["passed"] else 1


def cmd_explain(args: argparse.Namespace) -> int:
    scenario = _load(args)
    pid = args.parcel_id
    rejected = [
        r for r in scenario.rejected_parcels if r.get("parcel_id") == pid
    ]
    if rejected:
        print(json.dumps(rejected[0], ensure_ascii=False, indent=2))
        return 2

    if scenario.parcel_by_id(pid) is None:
        raise ValidationError(
            "unknown_parcel", f"no parcel with parcel_id {pid!r} in input"
        )

    result = compute(scenario)
    parcel_result = next(p for p in result.parcels if p.parcel_id == pid)
    doc = parcel_evidence(result, parcel_result)
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    if args.write_out:
        if not args.out:
            parser = _build_parser()
            parser.error("--write-out requires --out")
        write_all(result, args.out)
    return 0 if parcel_result.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "allocate":
            return cmd_allocate(args)
        if args.command == "explain-parcel":
            return cmd_explain(args)
        parser.error("unknown command")  # pragma: no cover
    except ValidationError as exc:
        print(
            json.dumps(
                {"error": exc.as_dict()},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
