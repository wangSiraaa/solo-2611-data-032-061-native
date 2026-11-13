"""Line-level JSONL contract validation.

The validator evaluates a *compiled* contract graph against one parsed JSON
value.  It memoises every (contract node, data node) pair so repeated work in
``oneOf`` branch combinations is performed once per line.

Diagnostics use RFC 6901 pointers, with ``~0``/``~1`` escaping for ``~`` and
``/`` in property names.  A missing required property is reported with a
pointer that addresses the *absent* property location.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import pointer as ptr
from .contract_check import Contract, Node
from .report_json import (
    IngressError,
    LineResult,
    MAX_LINE_BYTES,
    canonical_json,
    decode_line,
    measure_nesting,
    parse_json_line,
)

MAX_DATA_DEPTH = 64


@dataclass
class ErrorRec:
    code: str
    data_pointer: str
    schema_pointer: str
    message: str
    branch: int | None = None
    oneof_successes: int | None = None
    successes: tuple[int, ...] | None = None

    def sort_key(self) -> tuple[int, str, str, str]:
        depth = 0 if self.data_pointer == "" else self.data_pointer.count("/")
        return (depth, self.data_pointer, self.schema_pointer, self.code)

    def to_dict(self) -> dict:
        out = {
            "code": self.code,
            "dataPointer": self.data_pointer,
            "schemaPointer": self.schema_pointer,
            "message": self.message,
        }
        if self.branch is not None:
            out["branch"] = self.branch
        if self.oneof_successes is not None:
            out["oneofSuccesses"] = self.oneof_successes
        if self.successes is not None:
            out["successBranches"] = list(self.successes)
        return out


@dataclass
class EvalResult:
    success: bool
    failures: list[ErrorRec] = field(default_factory=list)
    successes: list[int] = field(default_factory=list)


def representative(errors: list[ErrorRec]) -> ErrorRec:
    """Pick a representative error by depth, then codepoint order of pointers."""
    return min(errors, key=lambda e: e.sort_key())


class Engine:
    """Evaluator for one compiled contract with a per-line memo table.

    Evaluation uses an explicit work stack rather than Python recursion, so
    no contract shape (including very long ``$ref`` chains) can trip the
    interpreter's recursion guard.  The stack size is bounded by the contract
    graph and the (pre-limited) input depth.
    """

    def __init__(self, contract: Contract, use_cache: bool = True):
        self.contract = contract
        self.use_cache = use_cache
        self._cache: dict[tuple[int, str], EvalResult] = {}
        # Per-line counters plus cumulative totals across the whole input.
        self.checks = 0       # distinct (node,data) evaluations actually run
        self.cache_hits = 0
        self.cache_misses = 0
        self.total_checks = 0
        self.total_hits = 0
        self.total_misses = 0

    def start_line(self) -> None:
        """Roll per-line counters into totals and begin a fresh memo table."""
        self.total_checks += self.checks
        self.total_hits += self.cache_hits
        self.total_misses += self.cache_misses
        self._cache.clear()
        self.checks = 0
        self.cache_hits = 0
        self.cache_misses = 0

    def reset(self) -> None:
        """Clear everything (cache and all counters)."""
        self._cache.clear()
        self.checks = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.total_checks = 0
        self.total_hits = 0
        self.total_misses = 0

    def evaluate(self, node_id: int, value, data_pointer: str) -> EvalResult:
        self._run(node_id, value, data_pointer)
        # _run memoises every node it evaluates, including the root pair.
        return self._cache[(node_id, data_pointer)]

    # -- iterative work-stack evaluator -----------------------------------
    # Frames are [node_id, value, data_pointer, mode, payload].
    #   mode "eval": evaluate the node now.
    #   mode "finish": children have been evaluated and memoised; combine
    #                  them into this node's EvalResult.
    #   mode "finish_ref": copy the referenced node's result onto a $ref node.
    # The stack grows at most by the contract node count (alias/oneOf edges)
    # plus input nesting, both statically bounded above (200 nodes, depth 64).
    def _run(self, root_node: int, root_value, root_pointer: str) -> None:
        cache = self._cache
        nodes = self.contract.nodes
        # In "finish" mode payload is a list of ("error", ErrorRec) or
        # ("child", (node_id, data_pointer)) entries in deterministic order.
        stack: list[list] = [[root_node, root_value, root_pointer, "eval", None]]
        while stack:
            frame = stack[-1]
            node_id, value, data_pointer, mode = frame[0], frame[1], frame[2], frame[3]
            key = (node_id, data_pointer)

            if mode == "finish_ref":
                # Alias the referenced node's result onto this $ref node.
                target_id = frame[4]
                cache[key] = cache[(target_id, data_pointer)]
                stack.pop()
                continue

            if mode == "finish":
                node = nodes[node_id]
                pending = frame[4]
                if node.kind == "oneOf":
                    result = self._combine_oneof(node, pending, data_pointer)
                elif node.type == "object":
                    result = self._combine_object(node, pending)
                else:
                    result = self._combine_array(pending)
                cache[key] = result
                stack.pop()
                continue

            if self.use_cache:
                hit = cache.get(key)
                if hit is not None:
                    self.cache_hits += 1
                    stack.pop()
                    continue
                self.cache_misses += 1
            self.checks += 1

            node = nodes[node_id]
            if node.kind == "ref":
                assert node.ref_target is not None
                # $ref is an alias in this subset: evaluate the target at the
                # same data location, then copy its result.  This uses O(1)
                # stack per alias link rather than one frame per chain hop,
                # and the static zero-progress check guarantees termination.
                frame[3] = "finish_ref"
                frame[4] = node.ref_target
                stack.append([node.ref_target, value, data_pointer, "eval", None])
                continue

            if node.kind == "oneOf":
                frame[3] = "finish"
                frame[4] = [
                    ("child", (branch, value, data_pointer))
                    for branch in node.oneof
                ]
                for branch in reversed(node.oneof):
                    stack.append([branch, value, data_pointer, "eval", None])
                continue

            if not _type_matches(node.type, value):
                cache[key] = EvalResult(
                    success=False,
                    failures=[
                        ErrorRec(
                            code="type-mismatch",
                            data_pointer=data_pointer,
                            schema_pointer=node.pointer + "/type",
                            message=(
                                f"expected {node.type}, got {_json_type_name(value)}"
                            ),
                        )
                    ],
                )
                stack.pop()
                continue

            if node.type == "object":
                pending: list[tuple[str, object]] = [
                    ("error", rec)
                    for rec in self._object_failures(node, value, data_pointer)
                ]
                for name in sorted(node.properties):
                    if name in value:
                        child_ptr = ptr.join(data_pointer, name)
                        pending.append(("child", (node.properties[name], child_ptr)))
                frame[3] = "finish"
                frame[4] = pending
                # Push surviving children in reverse order so the first
                # property (code-point order) is evaluated first.
                for name in reversed(sorted(node.properties)):
                    if name in value:
                        stack.append(
                            [
                                node.properties[name],
                                value[name],
                                ptr.join(data_pointer, name),
                                "eval",
                                None,
                            ]
                        )
                continue

            if node.type == "array":
                if node.items is None:
                    # No `items`: arrays of any element shape are accepted.
                    cache[key] = EvalResult(success=True)
                    stack.pop()
                    continue
                pending = []
                for index in range(len(value)):
                    child_ptr = ptr.join(data_pointer, str(index))
                    pending.append(("child", (node.items, child_ptr)))
                frame[3] = "finish"
                frame[4] = pending
                for index in range(len(value) - 1, -1, -1):
                    stack.append(
                        [
                            node.items,
                            value[index],
                            ptr.join(data_pointer, str(index)),
                            "eval",
                            None,
                        ]
                    )
                continue

            # A matching scalar type.
            cache[key] = EvalResult(success=True)
            stack.pop()
            continue

    def _object_failures(self, node: Node, value: dict, data_pointer: str):
        for name in node.required:
            if name not in value:
                child_pointer = ptr.join(data_pointer, name)
                child_schema = node.properties.get(name)
                schema_pointer = (
                    self.contract.nodes[child_schema].pointer
                    if child_schema is not None
                    else node.pointer
                )
                yield ErrorRec(
                    code="required-property-missing",
                    data_pointer=child_pointer,
                    schema_pointer=schema_pointer,
                    message=f"required property {name!r} is absent",
                )
        if not node.additional_properties:
            for name in sorted(value):
                if name not in node.properties:
                    yield ErrorRec(
                        code="additional-property-forbidden",
                        data_pointer=ptr.join(data_pointer, name),
                        schema_pointer=node.pointer + "/additionalProperties",
                        message=f"property {name!r} is not allowed",
                    )

    def _combine_oneof(self, node: Node, pending: list, data_pointer: str) -> EvalResult:
        results = [self._cache[(child_id, child_ptr)] for _k, (child_id, _v, child_ptr) in pending]
        successes = [i for i, r in enumerate(results) if r.success]
        if len(successes) == 1:
            return EvalResult(success=True, successes=successes)
        if len(successes) == 0:
            node_error = ErrorRec(
                code="oneof-no-match",
                data_pointer=data_pointer,
                schema_pointer=node.pointer,
                message="oneOf matched no branch; exactly one is required",
                oneof_successes=0,
            )
            failures: list[ErrorRec] = [node_error]
            for index, result in enumerate(results):
                rep = representative(result.failures)
                failures.append(
                    ErrorRec(
                        code=rep.code,
                        data_pointer=rep.data_pointer,
                        schema_pointer=rep.schema_pointer,
                        message=f"branch {index}: {rep.message}",
                        branch=index,
                    )
                )
            return EvalResult(success=False, failures=failures, successes=[])
        node_error = ErrorRec(
            code="oneof-multiple-success",
            data_pointer=data_pointer,
            schema_pointer=node.pointer,
            message=(
                "oneOf matched "
                + ", ".join(f"branch {i}" for i in successes)
                + "; exactly one is required"
            ),
            oneof_successes=len(successes),
            successes=tuple(successes),
        )
        return EvalResult(success=False, failures=[node_error], successes=successes)

    def _combine_object(self, node: Node, pending: list) -> EvalResult:
        failures: list[ErrorRec] = []
        for kind, payload in pending:
            if kind == "error":
                failures.append(payload)
            else:
                child_id, child_ptr = payload
                child_result = self._cache[(child_id, child_ptr)]
                if not child_result.success:
                    failures.extend(child_result.failures)
        return EvalResult(success=not failures, failures=failures)

    def _combine_array(self, pending: list) -> EvalResult:
        failures: list[ErrorRec] = []
        for _kind, (child_id, child_ptr) in pending:
            child_result = self._cache[(child_id, child_ptr)]
            if not child_result.success:
                failures.extend(child_result.failures)
        return EvalResult(success=not failures, failures=failures)



def _type_matches(expected: str, value) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "integer":
        # bool is a subclass of int in Python; JSON true/false are not integers.
        # Only an exact integer is accepted: floats (including 1.0), non-finite
        # numbers and booleans are rejected.
        return isinstance(value, int) and not isinstance(value, bool)
    return False


def _json_type_name(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def validate_value(engine: Engine, value, data_pointer: str = "") -> EvalResult:
    return engine.evaluate(engine.contract.root, value, data_pointer)


def process_line(
    engine: Engine,
    lineno: int,
    raw_line: bytes,
    first_line: bool,
    too_long: bool = False,
    excess: int = 0,
) -> LineResult:
    """Validate one raw physical line; never raises for a data-level problem."""
    if too_long:
        return LineResult(
            lineno=lineno,
            accepted=False,
            code="line-too-long",
            message=(
                f"input line is {excess} bytes; limit is {MAX_LINE_BYTES} "
                f"({MAX_LINE_BYTES // 1024} KiB)"
            ),
        )

    text, decoded = decode_line(raw_line)
    if not decoded:
        return LineResult(
            lineno=lineno,
            accepted=False,
            code="invalid-json",
            message="input line is not valid UTF-8",
        )
    if first_line and text.startswith("﻿"):
        text = text[1:]
    line_len = len(raw_line) + excess
    if line_len > MAX_LINE_BYTES:
        return LineResult(
            lineno=lineno,
            accepted=False,
            code="line-too-long",
            message=f"input line is {line_len} bytes; limit is {MAX_LINE_BYTES}",
        )
    if measure_nesting(text) > MAX_DATA_DEPTH:
        return LineResult(
            lineno=lineno,
            accepted=False,
            code="too-deep",
            message=f"input nesting exceeds the {MAX_DATA_DEPTH}-level limit",
        )
    try:
        value = parse_json_line(text)
    except IngressError as exc:
        return LineResult(
            lineno=lineno, accepted=False, code=exc.code, message=exc.message
        )

    engine.start_line()
    try:
        result = validate_value(engine, value)
    except RecursionError:
        # Defensive: all depth limits are checked before parsing, so this
        # should be unreachable; isolate the line rather than crash.
        return LineResult(
            lineno=lineno,
            accepted=False,
            code="too-deep",
            message="interpreter recursion guard tripped while validating",
        )

    if result.success:
        return LineResult(
            lineno=lineno,
            accepted=True,
            canonical=canonical_json(value),
        )

    errors = sorted(result.failures, key=lambda e: e.sort_key())
    # Failures may come from nested oneOf nodes; aggregate the success branch
    # lists of any "multiple-success" records so a top-level report always names
    # the offending branch numbers, alongside the top-level result's own list.
    merged_successes = list(result.successes)
    for error in errors:
        if error.successes:
            for branch in error.successes:
                if branch not in merged_successes:
                    merged_successes.append(branch)
    primary = errors[0]
    return LineResult(
        lineno=lineno,
        accepted=False,
        code=primary.code,
        data_pointer=primary.data_pointer,
        schema_pointer=primary.schema_pointer,
        message=primary.message,
        successes=sorted(merged_successes),
        representative_errors=[e.to_dict() for e in errors],
    )


def validate_lines(
    contract: Contract, raw: bytes, use_cache: bool = True
) -> tuple[list[LineResult], dict]:
    """Validate all physical lines of an input byte string.

    Returns per-line results and a summary including cumulative memo stats.
    Input count always equals accepted count plus rejected count.
    """
    import io

    return validate_stream(contract, io.BytesIO(raw), use_cache=use_cache)


def validate_stream(
    contract: Contract, stream, use_cache: bool = True
) -> tuple[list[LineResult], dict]:
    """Validate every physical line of an open binary stream.

    Lines are consumed incrementally (see :func:`iter_physical_lines`) and an
    over-long line is isolated without affecting subsequent lines.  The
    iterator yields excess bytes separately so the exact byte length can be
    reported without buffering the whole line.
    """
    from .report_json import iter_physical_lines

    engine = Engine(contract, use_cache=use_cache)
    results: list[LineResult] = []
    count = 0
    for index, (raw_line, too_long, excess) in enumerate(iter_physical_lines(stream)):
        count += 1
        results.append(
            process_line(
                engine, index + 1, raw_line, index == 0, too_long, excess
            )
        )
    summary = {
        "inputLines": count,
        "accepted": sum(1 for r in results if r.accepted),
        "rejected": sum(1 for r in results if not r.accepted),
        # Cumulative across all lines; per-line memo tables are cleared between
        # lines, so totals equal the sum of the per-line counters.
        "pairsChecked": engine.total_checks + engine.checks,
        "memoHits": engine.total_hits + engine.cache_hits,
        "memoMisses": engine.total_misses + engine.cache_misses,
        "useCache": use_cache,
    }
    return results, summary


def rejected_report(result: LineResult) -> dict:
    out = {
        "status": "rejected",
        "line": result.lineno,
        "code": result.code,
        "dataPointer": result.data_pointer,
        "schemaPointer": result.schema_pointer,
        "message": result.message,
    }
    if result.successes:
        out["successBranches"] = list(result.successes)
    if result.representative_errors:
        out["errors"] = result.representative_errors
    return out


def accepted_report(result: LineResult) -> dict:
    import json

    return {
        "status": "ok",
        "line": result.lineno,
        "value": json.loads(result.canonical),
    }


def report_line(result: LineResult) -> tuple[bool, str]:
    """Return ``(accepted, report_text)`` for a result.

    Accepted lines are canonical JSON (sorted keys, compact separators).
    Rejected lines are canonical JSON reports retaining the original line
    number and both pointers.
    """
    if result.accepted:
        return True, result.canonical or ""
    return False, canonical_json(rejected_report(result))


__all__ = [
    "Engine",
    "ErrorRec",
    "EvalResult",
    "MAX_DATA_DEPTH",
    "accepted_report",
    "process_line",
    "rejected_report",
    "report_line",
    "validate_lines",
    "validate_stream",
    "validate_value",
]
