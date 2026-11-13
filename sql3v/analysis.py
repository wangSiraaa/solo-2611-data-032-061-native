"""核验主流程：解析双谓词、枚举有限域、判定两类等价并选规范反例。"""

from __future__ import annotations

from dataclasses import dataclass

from . import ast_nodes as ast
from .domain import Spec
from .evaluator import Evaluator, snapshot
from .lexer import Token
from .parser import parse_predicate
from .sqlite_check import sqlite_probe
from .trit import Trit, where_accepts


@dataclass(frozen=True)
class ParsedPredicate:
    text: str
    tokens: tuple[Token, ...]
    tree: ast.Expr


@dataclass(frozen=True)
class Counterexample:
    assignment: dict[str, int | None]   # 规范列序（列名字典序）
    ordered_columns: tuple[str, ...]
    result_a: Trit
    result_b: Trit
    steps_a: tuple[dict[str, object], ...]
    steps_b: tuple[dict[str, object], ...]

    def vector(self) -> tuple[str, ...]:
        return tuple("NULL" if self.assignment[c] is None else str(self.assignment[c])
                     for c in self.ordered_columns)


@dataclass(frozen=True)
class PredicateReport:
    counts: dict[str, int]               # TRUE/FALSE/UNKNOWN 计数
    where_count: int                     # WHERE 收录（TRUE）计数


@dataclass(frozen=True)
class CompareResult:
    spec: Spec
    parsed_a: ParsedPredicate
    parsed_b: ParsedPredicate
    traversed: int
    report_a: PredicateReport
    report_b: PredicateReport
    value_equivalent: bool
    filter_equivalent: bool
    value_counterexample: Counterexample | None
    filter_counterexample: Counterexample | None
    sqlite_verified: bool = False


def parse_both(spec: Spec) -> tuple[ParsedPredicate, ParsedPredicate]:
    cols = set(spec.column_names)
    out: list[ParsedPredicate] = []
    for label, text in (("predicate_a", spec.predicate_a), ("predicate_b", spec.predicate_b)):
        try:
            tokens, tree = parse_predicate(text, cols)
        except Exception as exc:
            # 附带是哪个谓词，便于定位
            if hasattr(exc, "message"):
                exc.message = f"[{label}] {exc.message}"  # type: ignore[attr-defined]
            raise
        out.append(ParsedPredicate(text, tuple(tokens), tree))
    return out[0], out[1]


def _make_counterexample(spec: Spec, env: dict[str, int | None],
                         tree_a: ast.Expr, tree_b: ast.Expr,
                         ra: Trit, rb: Trit) -> Counterexample:
    ordered = tuple(c.name for c in spec.canonical_columns())
    return Counterexample(
        assignment=dict(env),
        ordered_columns=ordered,
        result_a=ra,
        result_b=rb,
        steps_a=tuple(snapshot(tree_a, env)),
        steps_b=tuple(snapshot(tree_b, env)),
    )


def compare(spec: Spec, parsed_a: ParsedPredicate, parsed_b: ParsedPredicate,
            cross_check: bool = False) -> CompareResult:
    assignments = spec.enumerate_assignments()
    counts_a = {"TRUE": 0, "FALSE": 0, "UNKNOWN": 0}
    counts_b = {"TRUE": 0, "FALSE": 0, "UNKNOWN": 0}
    where_a = where_b = 0
    value_ce: Counterexample | None = None
    filter_ce: Counterexample | None = None

    ev_a = Evaluator({})
    ev_b = Evaluator({})
    for env in assignments:
        ev_a.env = env
        ev_b.env = env
        ra = ev_a.eval(parsed_a.tree)
        rb = ev_b.eval(parsed_b.tree)
        counts_a[ra.value] += 1
        counts_b[rb.value] += 1
        wa, wb = where_accepts(ra), where_accepts(rb)
        where_a += int(wa)
        where_b += int(wb)

        # 枚举本身按规范顺序进行，第一个反例即“最小反例”。
        if value_ce is None and ra is not rb:
            value_ce = _make_counterexample(spec, env, parsed_a.tree, parsed_b.tree, ra, rb)
        if filter_ce is None and wa != wb:
            filter_ce = _make_counterexample(spec, env, parsed_a.tree, parsed_b.tree, ra, rb)

        if cross_check:
            sra, swa = sqlite_probe(parsed_a.tree, env)
            srb, swb = sqlite_probe(parsed_b.tree, env)
            if sra is not ra or swa != wa or srb is not rb or swb != wb:
                raise RuntimeError(  # VerifyError 已在 sqlite_check 内抛出，这里双保险
                    f"SQLite 交叉核对不一致: 赋值 {env}"
                )

    return CompareResult(
        spec=spec,
        parsed_a=parsed_a,
        parsed_b=parsed_b,
        traversed=len(assignments),
        report_a=PredicateReport(counts_a, where_a),
        report_b=PredicateReport(counts_b, where_b),
        value_equivalent=value_ce is None,
        filter_equivalent=filter_ce is None,
        value_counterexample=value_ce,
        filter_counterexample=filter_ce,
        sqlite_verified=cross_check,
    )


def run(spec: Spec, cross_check: bool = False) -> CompareResult:
    pa, pb = parse_both(spec)
    return compare(spec, pa, pb, cross_check=cross_check)
