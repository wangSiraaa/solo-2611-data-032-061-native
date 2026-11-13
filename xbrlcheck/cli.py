"""Command line interface: ``inspect``, ``compare`` and ``check``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, Sequence

from lxml import etree

from .calc import check_calculations
from .compare import compare_reports
from .model import Report
from .parser import parse_report
from .precision import dec_str, interval_str
from .qname import LINK, QName, qname_from_tag
from .taxonomy import Taxonomy, load_taxonomy


def _schema_refs(path: Path) -> list[str]:
    try:
        root = etree.parse(str(path)).getroot()
    except (OSError, etree.XMLSyntaxError):
        return []
    if qname_from_tag(root.tag) != QName("http://www.xbrl.org/2003/instance", "xbrl"):
        return []
    hrefs = []
    for ref in root.findall(QName(LINK, "schemaRef").clark):
        href = ref.get(QName("http://www.w3.org/1999/xlink", "href").clark)
        if href:
            hrefs.append(href)
    return hrefs


def _resolve_refs(report_path: Path) -> list[Path]:
    out: list[Path] = []
    for href in _schema_refs(report_path):
        if "://" in href:
            continue
        candidate = (report_path.parent / href).resolve()
        out.append(candidate)
    return out


def build_taxonomy(
    report_paths: Sequence[Path],
    explicit: Iterable[Path] = (),
    search_dirs: Iterable[Path] = (),
) -> tuple[Taxonomy, list[Report]]:
    reports = [Path(p) for p in report_paths]
    if explicit:
        schema_paths = [Path(p) for p in explicit]
    else:
        seen: list[Path] = []
        for report in reports:
            for ref in _resolve_refs(report):
                if ref not in seen:
                    seen.append(ref)
        schema_paths = seen
    dirs = list(search_dirs)
    for report in reports:
        dirs.append(report.parent)
    for schema in schema_paths:
        dirs.append(schema.parent)
    taxonomy = load_taxonomy(schema_paths, dirs)
    parsed = [parse_report(p, taxonomy) for p in reports]
    return taxonomy, parsed


# ---------------------------------------------------------------------------
# rendering helpers
# ---------------------------------------------------------------------------


def _fact_line(fact) -> str:
    location = "%s:%s" % (Path(fact.source.file).name, fact.source.line)
    value = "nil" if fact.nil else fact.raw_value
    interval = interval_str(fact.interval) if fact.interval is not None else "-"
    return "%s  %s  %s  value=%s decimals=%s interval=%s  [%s]" % (
        fact.concept.clark,
        fact.context_id,
        fact.key.unit.canonical(),
        value,
        fact.decimals,
        interval,
        location,
    )


def render_diagnostics(report: Report, out) -> None:
    for diag in report.diagnostics:
        print(diag.render(), file=out)


def render_inspect(report: Report, taxonomy: Taxonomy, out=sys.stdout) -> None:
    print("== Report: %s ==" % report.path, file=out)
    if report.schema_refs:
        print("schemaRef: %s" % ", ".join(report.schema_refs), file=out)
    print("taxonomy schemas: %s" % ", ".join(Path(p).name for p in taxonomy.loaded_schemas), file=out)
    print("taxonomy calculation linkbases: %s" % (
        ", ".join(Path(p).name for p in taxonomy.loaded_linkbases) or "-"
    ), file=out)
    print("", file=out)

    print("-- Contexts (%d) --" % len(report.contexts), file=out)
    for ctx in report.contexts:
        print("  %s -> %s" % (ctx.context_id, ctx.identity.canonical()), file=out)
    print("", file=out)

    print("-- Units (%d) --" % len(report.units), file=out)
    for unit in report.units:
        print("  %s -> %s" % (unit.unit_id, unit.identity.canonical()), file=out)
    print("", file=out)

    print("-- Canonical facts (%d) --" % len(report.facts), file=out)
    for fact in report.sorted_facts():
        print("  " + _fact_line(fact), file=out)
    if report.rejected:
        print("", file=out)
        print("-- Rejected facts (%d) --" % len(report.rejected), file=out)
        for r in report.rejected:
            line = r.location.line
            print("  %s [%s:%s]: %s" % (
                r.concept.clark if r.concept else "<unknown>",
                Path(r.location.file).name,
                line,
                r.reason,
            ), file=out)

    duplicate_groups = [g for g in report.groups.values() if len(g.facts) > 1]
    if duplicate_groups:
        print("", file=out)
        print("-- Duplicate fact groups (%d) --" % len(duplicate_groups), file=out)
        for group in duplicate_groups:
            print("  %s" % group.key.canonical(), file=out)
            print("    status=%s interval=%s" % (
                group.status,
                interval_str(group.interval) if group.interval else "-",
            ), file=out)
            if group.reason:
                print("    reason=%s" % group.reason, file=out)
            for fact in group.facts:
                print("    " + _fact_line(fact), file=out)

    summary = {
        "contexts": len(report.contexts),
        "units": len(report.units),
        "facts": len(report.facts),
        "nil_facts": sum(1 for f in report.facts if f.nil),
        "rejected": len(report.rejected),
        "duplicate_groups": len(duplicate_groups),
        "conflicts": sum(1 for g in report.groups.values() if g.conflict),
    }
    print("", file=out)
    print("summary: " + json.dumps(summary, ensure_ascii=False), file=out)
    render_diagnostics(report, out)


def render_compare(rows, report_a: Report, report_b: Report, out=sys.stdout) -> None:
    print("== Compare ==", file=out)
    print("A: %s" % report_a.path, file=out)
    print("B: %s" % report_b.path, file=out)
    print("", file=out)
    print(
        "%-12s %-26s %-24s %-14s %-14s %-14s %s"
        % ("status", "concept", "context", "A", "B", "delta", "common interval"),
        file=out,
    )
    for row in rows:
        a_val = "-" if row.left is None else _group_value(row.left)
        b_val = "-" if row.right is None else _group_value(row.right)
        ctx = row.key.context.canonical()
        print(
            "%-12s %-26s %-24s %-14s %-14s %-14s %s"
            % (
                row.status,
                row.key.concept.localname,
                _short(ctx, 24),
                a_val,
                b_val,
                dec_str(row.delta) or "-",
                interval_str(row.interval) if row.interval else "-",
            ),
            file=out,
        )
        if row.detail:
            print("             %s" % row.detail, file=out)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    print("", file=out)
    print("counts: " + json.dumps(counts, ensure_ascii=False), file=out)
    print(
        "note: 'compatible' only means the declared precision intervals overlap; "
        "it does not establish that the reports agree.",
        file=out,
    )


def _group_value(group) -> str:
    if group.nil:
        return "nil"
    values = [dec_str(f.value) for f in group.facts]
    if len(values) == 1:
        return values[0]
    return "{%s}" % ",".join(values)


def _short(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def render_check(report: Report, result, out=sys.stdout) -> None:
    print("== Check: %s ==" % report.path, file=out)
    print("calculation role: %s" % (result.role or "-"), file=out)
    if result.diagnostics:
        for diag in result.diagnostics:
            print(diag.render() if hasattr(diag, "render") else diag, file=out)
    print("", file=out)
    if not result.conclusions:
        print("no calculation relations declared in the supported subset.", file=out)
        return
    ordered = sorted(
        result.conclusions,
        key=lambda c: (c.depth, c.parent.namespace, c.parent.localname, c.context_canonical),
    )
    for conclusion in ordered:
        print(conclusion.render(), file=out)
        print("", file=out)
    print("counts: " + json.dumps(result.counts(), ensure_ascii=False), file=out)
    print(
        "note: a consistent relation is only a statement about that relation's "
        "facts and precision; it is not a claim that the report is correct.",
        file=out,
    )


# ---------------------------------------------------------------------------
# JSON rendering
# ---------------------------------------------------------------------------


def inspect_json(report: Report, taxonomy: Taxonomy) -> dict:
    return {
        "report": str(report.path),
        "schema_refs": report.schema_refs,
        "taxonomy": {
            "schemas": taxonomy.loaded_schemas,
            "calculation_linkbases": taxonomy.loaded_linkbases,
            "diagnostics": [d.as_json() for d in taxonomy.diagnostics],
        },
        "contexts": [c.as_json() for c in report.contexts],
        "units": [u.as_json() for u in report.units],
        "facts": [f.as_json() for f in report.sorted_facts()],
        "groups": [
            dict(
                concept=g.key.concept.clark,
                context=g.key.context.canonical(),
                unit=g.key.unit.canonical(),
                **g.as_json(),
            )
            for g in report.groups.values()
        ],
        "rejected": [r.as_json() for r in report.rejected],
        "diagnostics": [d.as_json() for d in report.diagnostics],
    }


def compare_json(rows, report_a: Report, report_b: Report) -> dict:
    return {
        "a": str(report_a.path),
        "b": str(report_b.path),
        "rows": [r.as_json() for r in rows],
        "counts": _counts(rows),
        "diagnostics": {
            "a": [d.as_json() for d in report_a.diagnostics],
            "b": [d.as_json() for d in report_b.diagnostics],
        },
    }


def check_json(report: Report, result) -> dict:
    return {
        "report": str(report.path),
        "calculation": result.as_json(),
        "diagnostics": [d.as_json() for d in report.diagnostics],
    }


def _counts(rows) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[row.status] = out.get(row.status, 0) + 1
    return out


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_inspect(args: argparse.Namespace) -> int:
    taxonomy, reports = build_taxonomy([args.report], args.taxonomy, args.search_dir)
    report = reports[0]
    if args.json:
        data = inspect_json(report, taxonomy)
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        render_inspect(report, taxonomy)
        for diag in taxonomy.diagnostics:
            print(diag.render())
    return 2 if report.has_errors or taxonomy.has_errors else 0


def cmd_compare(args: argparse.Namespace) -> int:
    taxonomy, reports = build_taxonomy([args.report_a, args.report_b], args.taxonomy, args.search_dir)
    rows = compare_reports(reports[0], reports[1])
    if args.json:
        print(json.dumps(compare_json(rows, reports[0], reports[1]), ensure_ascii=False, indent=2))
    else:
        for diag in taxonomy.diagnostics:
            print(diag.render())
        render_diagnostics(reports[0], sys.stdout)
        render_diagnostics(reports[1], sys.stdout)
        render_compare(rows, reports[0], reports[1])
    bad = any(
        r.status in {"conflict", "ambiguous", "only_a", "only_b", "nil-vs-value", "value-vs-nil"}
        for r in rows
    )
    return 1 if bad or reports[0].has_errors or reports[1].has_errors else 0


def cmd_check(args: argparse.Namespace) -> int:
    taxonomy, reports = build_taxonomy(args.report, args.taxonomy, args.search_dir)
    if taxonomy.has_errors:
        # The calculation linkbase itself is outside the supported subset; do
        # not draw conclusions from a relation we cannot interpret faithfully.
        if args.json:
            print(
                json.dumps(
                    {
                        "taxonomy_diagnostics": [d.as_json() for d in taxonomy.diagnostics],
                        "calculations_skipped": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            for diag in taxonomy.diagnostics:
                print(diag.render())
            print("calculation relations rejected: the linkbase is outside the supported subset.")
        return 2
    exit_code = 0
    payload = []
    for report in reports:
        result = check_calculations(report, taxonomy)
        if args.json:
            payload.append(check_json(report, result))
        else:
            render_diagnostics(report, sys.stdout)
            render_check(report, result)
            print("")
        if report.has_errors or not result.ok:
            exit_code = 1
    if args.json:
        print(json.dumps(payload if len(payload) != 1 else payload[0], ensure_ascii=False, indent=2))
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xbrlcheck",
        description="Local XBRL fact inspection, comparison and calculation checks.",
    )
    parser.add_argument(
        "--taxonomy",
        action="append",
        default=[],
        metavar="XSD",
        help="local schema to load instead of the instance schemaRef (repeatable)",
    )
    parser.add_argument(
        "--search-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="extra directory used to resolve local schema/linkbase references (repeatable)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect", help="show canonical facts, contexts, units and diagnostics")
    p_inspect.add_argument("report", type=Path)
    p_inspect.add_argument("--json", action="store_true")
    p_inspect.set_defaults(func=cmd_inspect)

    p_compare = sub.add_parser("compare", help="compare two reports fact-by-fact on canonical identity")
    p_compare.add_argument("report_a", type=Path)
    p_compare.add_argument("report_b", type=Path)
    p_compare.add_argument("--json", action="store_true")
    p_compare.set_defaults(func=cmd_compare)

    p_check = sub.add_parser("check", help="check calculation relations against precision intervals")
    p_check.add_argument("report", type=Path, nargs="+")
    p_check.add_argument("--json", action="store_true")
    p_check.set_defaults(func=cmd_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
