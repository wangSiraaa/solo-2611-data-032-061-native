"""End-to-end conformance suite: ``verify``.

Checks the contract checker and validator against a curated set of static
cases, ingress cases, and the generated corpus, then cross-checks accepted /
rejected conclusions against the independent :mod:`baseline`.  Also verifies:

* input count == accepted + rejected for every run;
* canonical output is unaffected by data object key order;
* per-line reports are identical when key order changes;
* memoisation reduces (node,data) work versus a no-cache run;
* rejected output files never replace a previous good version;
* contract rejection happens before any data is read (no data file needed).
"""

from __future__ import annotations

import json
import os
import traceback
from dataclasses import dataclass, field

from . import contract_check, instance_check
from .baseline import ACCEPT, REJECT, Baseline
from .generate import generate_corpus
from .report_json import canonical_json, split_lines, parse_json_line


@dataclass
class CaseResult:
    name: str
    ok: bool
    detail: str = ""
    metrics: dict = field(default_factory=dict)


class Suite:
    def __init__(self):
        self.cases: list[CaseResult] = []

    def check(self, name: str, fn):
        try:
            metrics = fn() or {}
            self.cases.append(CaseResult(name, True, metrics=metrics))
        except AssertionError as exc:
            self.cases.append(CaseResult(name, False, str(exc) or "assertion failed"))
        except Exception as exc:  # pragma: no cover - surfaces unexpected bugs
            self.cases.append(
                CaseResult(name, False, f"{type(exc).__name__}: {exc}")
            )
            traceback.print_exc()

    @property
    def failures(self) -> list[CaseResult]:
        return [c for c in self.cases if not c.ok]


def _compile(schema) -> contract_check.Contract:
    return contract_check.compile_contract(schema)


def _expect_contract_error(schema, code: str) -> contract_check.ContractError:
    try:
        _compile(schema)
    except contract_check.ContractError as exc:
        assert exc.code == code, f"expected {code}, got {exc.code}: {exc.message}"
        return exc
    raise AssertionError(f"expected ContractError {code}, but compile succeeded")


def _run(contract, values) -> tuple[list[instance_check.LineResult], dict]:
    lines = [json.dumps(v, ensure_ascii=False).encode() for v in values]
    raw = b"\n".join(lines) + b"\n" if lines else b""
    return instance_check.validate_lines(contract, raw)


def _run_raw(contract, text: str) -> tuple[list[instance_check.LineResult], dict]:
    return instance_check.validate_lines(contract, text.encode("utf-8"))


def _one(contract, value) -> instance_check.LineResult:
    results, _ = _run(contract, [value])
    return results[0]


# ---------------------------------------------------------------------------
# Static contract cases
# ---------------------------------------------------------------------------

def case_static_valid() -> None:
    _compile(
        {
            "$defs": {"leaf": {"type": "null"}},
            "type": "object",
            "properties": {"x": {"$ref": "#/$defs/leaf"}},
            "required": ["x"],
            "additionalProperties": False,
        }
    )
    _compile({"oneOf": [{"type": "integer"}, {"type": "string"}]})
    _compile(
        {"oneOf": [{"type": "integer"}], "$defs": {"a": {"type": "string"}}}
    )


def case_unknown_keyword() -> None:
    _expect_contract_error({"type": "object", "minProperties": 1}, "unknown-keyword")
    _expect_contract_error(
        {"type": "object", "properties": {"x": {"type": "integer", "minimum": 0}}},
        "unknown-keyword",
    )
    _expect_contract_error(
        {"type": "integer", "title": "ok", "description": "ok", "foo": 1},
        "unknown-keyword",
    )


def case_bad_combination() -> None:
    _expect_contract_error(
        {"type": "string", "properties": {"x": {"type": "string"}}},
        "invalid-combination",
    )
    _expect_contract_error(
        {"type": "integer", "items": {"type": "integer"}},
        "invalid-combination",
    )
    _expect_contract_error(
        {"type": "integer", "required": ["x"]}, "invalid-combination"
    )
    _expect_contract_error(
        {"oneOf": [{"type": "integer"}], "type": "integer"}, "unknown-keyword"
    )
    _expect_contract_error(
        {
            "$ref": "#/$defs/x",
            "type": "integer",
            "$defs": {"x": {"type": "integer"}},
        },
        "unknown-keyword",
    )


def case_missing_type() -> None:
    _expect_contract_error({"properties": {"x": {"type": "integer"}}}, "invalid-schema")
    _expect_contract_error({"type": "number"}, "invalid-type")


def case_oneof_forms() -> None:
    _expect_contract_error({"oneOf": []}, "invalid-oneof")
    _expect_contract_error({"oneOf": {}}, "invalid-oneof")
    _expect_contract_error(
        {"oneOf": [{"type": "integer"}], "description": "x"}, "unknown-keyword"
    )
    # Root may carry $defs alongside oneOf.
    _compile({"oneOf": [{"type": "null"}], "$defs": {"x": {"type": "null"}}})


def case_direct_cycle() -> None:
    err = _expect_contract_error(
        {"$defs": {"loop": {"$ref": "#/$defs/loop"}}, "$ref": "#/$defs/loop"},
        "zero-progress-cycle",
    )
    assert err.extra.get("path"), "cycle error should include a reference path"


def case_indirect_cycle() -> None:
    err = _expect_contract_error(
        {
            "$defs": {
                "a": {"$ref": "#/$defs/b"},
                "b": {"oneOf": [{"$ref": "#/$defs/a"}]},
            },
            "$ref": "#/$defs/a",
        },
        "zero-progress-cycle",
    )
    assert "a" in err.extra["path"] and "b" in err.extra["path"]
    # A oneOf reachable cycle across two definitions is likewise rejected.
    _expect_contract_error(
        {
            "$defs": {
                "t": {"oneOf": [{"$ref": "#/$defs/u"}, {"type": "null"}]},
                "u": {"oneOf": [{"$ref": "#/$defs/t"}, {"type": "integer"}]},
            },
            "$ref": "#/$defs/t",
        },
        "zero-progress-cycle",
    )
    # An acyclic $ref chain is not a cycle.
    defs = {f"d{i}": {"$ref": f"#/$defs/d{i + 1}"} for i in range(5)}
    defs["d5"] = {"type": "integer"}
    _compile({"$defs": defs, "$ref": "#/$defs/d0"})


def case_legal_recursion_is_not_cycle() -> None:
    from .generate import TREE_SCHEMA

    _compile(TREE_SCHEMA)
    # Recursion that descends through properties/items is legal.
    _compile(
        {
            "$defs": {
                "n": {
                    "type": "object",
                    "properties": {"next": {"oneOf": [{"$ref": "#/$defs/n"}, {"type": "null"}]}},
                }
            },
            "$ref": "#/$defs/n",
        }
    )


def case_ref_forms() -> None:
    _expect_contract_error({"$ref": "http://x"}, "invalid-ref")
    _expect_contract_error({"$ref": "#/$defs/a/b"}, "invalid-ref")
    _expect_contract_error({"$ref": "#/$defs/nope"}, "unresolved-ref")
    _expect_contract_error({"$ref": "#/definitions/x"}, "invalid-ref")
    # Escaped def names resolve.
    _compile(
        {
            "$defs": {"a/b~c": {"type": "integer"}},
            "$ref": "#/$defs/a~1b~0c",
        }
    )


def case_node_limit() -> None:
    # A chain of 200 schema nodes is fine; 201 is rejected.
    def chain(n):
        root = {"type": "integer"}
        for _ in range(n - 1):
            root = {"type": "object", "properties": {"c": root}}
        return root

    c = _compile(chain(200))
    assert c.node_count == 200
    _expect_contract_error(chain(201), "too-many-nodes")


# ---------------------------------------------------------------------------
# Runtime cases
# ---------------------------------------------------------------------------

def case_boolean_integer() -> None:
    contract = _compile({"type": "integer"})
    assert _one(contract, True).accepted is False
    assert _one(contract, False).accepted is False
    assert _one(contract, 1.0).accepted is False
    assert _one(contract, 1.5).accepted is False
    assert _one(contract, 5).accepted is True
    # Non-finite literals are rejected at ingress.
    results, summary = _run_raw(contract, "NaN\nInfinity\n-Infinity\n1\n")
    codes = [r.code for r in results]
    assert codes[:3] == ["invalid-json"] * 3, codes
    assert results[3].accepted is True
    assert summary["accepted"] == 1 and summary["rejected"] == 3
    # Overflowing literals parse to inf without the bare-token hook; they must
    # still be rejected at ingress (and never reach canonical rendering).
    results, summary = _run_raw(contract, "1e400\n-1e400\n123\n")
    assert [r.code for r in results] == ["invalid-json", "invalid-json", None]


def case_required_null() -> None:
    from .generate import REQUIRED_NULL_SCHEMA

    contract = _compile(REQUIRED_NULL_SCHEMA)
    ok = _one(contract, {"value": None})
    assert ok.accepted, ok.message
    wrong = _one(contract, {"value": 0})
    assert not wrong.accepted
    assert wrong.data_pointer == "/value", wrong.data_pointer
    missing = _one(contract, {})
    assert not missing.accepted
    assert missing.code == "required-property-missing"
    assert missing.data_pointer == "/value", missing.data_pointer
    # Explicit null is NOT the same as absent.
    assert ok.accepted is True


def case_oneof_semantics() -> None:
    from .generate import TWO_BRANCH_MATCH_SCHEMA

    contract = _compile(TWO_BRANCH_MATCH_SCHEMA)
    both = _one(contract, {"a": 1, "b": 2})
    assert not both.accepted
    assert both.code == "oneof-multiple-success"
    assert both.successes == [0, 1], both.successes

    one = _one(contract, {"a": 1})
    assert one.accepted

    none = _one(contract, {"a": "x"})
    assert not none.accepted
    assert none.code == "oneof-no-match"
    assert set(none.successes) == set()
    # Representative error per branch plus the node error.
    assert len(none.representative_errors) == 1 + 2, none.representative_errors
    rep = none.representative_errors
    assert rep[0]["code"] == "oneof-no-match"
    assert rep[1]["branch"] == 0 and rep[2]["branch"] == 1


def case_oneof_representative_selection() -> None:
    # Two failing branches with differing data-path depths: the shallower one
    # must be chosen as the branch representative.
    contract = _compile(
        {
            "oneOf": [
                {
                    "type": "object",
                    "required": ["deep"],
                    "properties": {
                        "deep": {"type": "object", "properties": {"x": {"type": "integer"}}}
                    },
                },
                {"type": "object", "required": ["near", "far"]},
            ]
        }
    )
    result = _one(contract, {"deep": {"x": "no"}})
    assert not result.accepted
    # Branch 1's representative should be its shallow missing requirement.
    branch1 = [e for e in result.representative_errors if e.get("branch") == 1][0]
    assert branch1["dataPointer"] in ("/far", "/near"), branch1
    branch0 = [e for e in result.representative_errors if e.get("branch") == 0][0]
    assert branch0["dataPointer"] == "/deep/x", branch0


def case_additional_properties() -> None:
    strict = _compile(
        {
            "type": "object",
            "required": ["x"],
            "additionalProperties": False,
            "properties": {"x": {"type": "integer"}},
        }
    )
    bad = _one(strict, {"x": 1, "y": 2})
    assert bad.code == "additional-property-forbidden"
    assert bad.data_pointer == "/y"
    assert bad.schema_pointer == "/additionalProperties"
    assert _one(strict, {"x": 1}).accepted


def case_pointer_escaping() -> None:
    from .generate import ESCAPE_SCHEMA

    contract = _compile(ESCAPE_SCHEMA)
    ok = _one(contract, {"a/b": "s", "a~b": 3})
    assert ok.accepted, ok.message
    missing = _one(contract, {"a/b": "s"})
    assert missing.data_pointer == "/a~0b", missing.data_pointer
    wrong_type = _one(contract, {"a/b": 1, "a~b": "s"})
    # Sorted failures; check both escaped pointers are present.
    pointers = {e["dataPointer"] for e in wrong_type.representative_errors}
    assert "/a~1b" in pointers and "/a~0b" in pointers, pointers
    # Invalid name in data is likewise escaped.
    bad_additional = _one(contract, {"a/b": "s", "a~b": 3, "x/y": 1})
    assert bad_additional.data_pointer == "/x~1y", bad_additional.data_pointer


def case_duplicate_keys_and_invalid_json() -> None:
    contract = _compile({"type": "object", "additionalProperties": True})
    results, summary = _run_raw(
        contract,
        '{"x":1,"x":2}\n{bad\n{"x":1}\n',
    )
    assert [r.code for r in results] == [
        "duplicate-key",
        "invalid-json",
        None,
    ], [r.code for r in results]
    assert summary["inputLines"] == 3
    assert summary["accepted"] == 1 and summary["rejected"] == 2


def case_line_and_depth_limits() -> None:
    from .generate import (
        depth_at_limit_line,
        depth_over_limit_line,
        nested_object,
    )

    contract = _compile(
        {
            "$defs": {
                "arr": {
                    "oneOf": [
                        {"type": "array", "items": {"$ref": "#/$defs/arr"}},
                        {"type": "null"},
                    ]
                }
            },
            "$ref": "#/$defs/arr",
        }
    )
    results, summary = _run_raw(
        contract, depth_at_limit_line() + "\n" + depth_over_limit_line() + "\n"
    )
    assert results[0].accepted, results[0].message
    assert results[1].code == "too-deep", results[1].code

    # The object analogue: 64 nested objects accepted, 65 rejected.
    obj_contract = _compile(
        {
            "$defs": {
                "o": {
                    "oneOf": [
                        {
                            "type": "object",
                            "required": ["child"],
                            "additionalProperties": False,
                            "properties": {"child": {"$ref": "#/$defs/o"}},
                        },
                        {
                            "type": "object",
                            "required": ["v"],
                            "additionalProperties": False,
                            "properties": {"v": {"type": "integer"}},
                        },
                    ]
                }
            },
            "$ref": "#/$defs/o",
        }
    )
    at_limit = json.dumps(nested_object(64))
    over = json.dumps(nested_object(65))
    results, summary = _run_raw(obj_contract, at_limit + "\n" + over + "\n")
    assert results[0].accepted, (results[0].code, results[0].message)
    assert results[1].code == "too-deep", results[1].code

    # Oversized single line is isolated without crashing.
    object_contract = _compile({"type": "object"})
    big = "{\"x\":\"" + "a" * (70000) + "\"}"
    results, summary = _run_raw(object_contract, "{}\n" + big + "\n{}\n")
    assert results[0].accepted and results[2].accepted
    assert results[1].code == "line-too-long", results[1].code
    assert summary["inputLines"] == summary["accepted"] + summary["rejected"] == 3


def case_array_without_items() -> None:
    # `type: array` without `items` accepts any element shapes.
    contract = _compile({"type": "array"})
    for value in ([], [1], [1, "x", None, {"a": 1}, [2]]):
        assert _one(contract, value).accepted
    assert not _one(contract, {"a": 1}).accepted


def case_contract_shape_errors() -> None:
    _expect_contract_error(
        {"type": "object", "additionalProperties": {"type": "integer"}},
        "invalid-additional-properties",
    )
    _expect_contract_error(
        {"type": "object", "required": [1]}, "invalid-required"
    )
    _expect_contract_error(
        {"type": "object", "required": ["a", "a"]}, "invalid-required"
    )
    _expect_contract_error(
        {"type": "object", "properties": []}, "invalid-properties"
    )
    _expect_contract_error(
        {"type": "array", "items": [{"type": "integer"}]}, "invalid-items"
    )
    _expect_contract_error({"type": "integer", "title": 5}, "invalid-metadata")
    _expect_contract_error({"$defs": {"x": {"type": "integer"}}, "oneOf": {"x": 1}}, "invalid-oneof")
    _expect_contract_error({"type": ["integer"]}, "invalid-type")
    # A $defs key whose ref uses an unescaped separator is a bad ref form.
    _expect_contract_error(
        {"$defs": {"a/b": {"type": "integer"}}, "$ref": "#/$defs/a/b"},
        "invalid-ref",
    )


def case_pointer_helpers() -> None:
    from . import pointer as ptr

    assert ptr.parse("") == []
    assert ptr.parse("/") == [""]
    assert ptr.parse("/a~1b") == ["a/b"]
    assert ptr.parse("/a~0b") == ["a~b"]
    assert ptr.parse("/~01") == ["~1"]
    assert ptr.parse("/a//b") == ["a", "", "b"]
    assert ptr.render(["a/b", "a~b", ""]) == "/a~1b/a~0b/"
    assert ptr.join("/a~1b", "c/d~e") == "/a~1b/c~1d~0e"
    assert ptr.normalize("/") == ""
    for bad in ("a~", "a~2b", "a~b"):
        try:
            ptr.unescape_token(bad)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"expected ValueError for {bad!r}")
    try:
        ptr.parse("no-slash")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for pointer without leading /")


def case_ingress_edge_cases() -> None:
    import io

    from .report_json import MAX_LINE_BYTES, iter_physical_lines

    # Physical-line splitting: no phantom line after a trailing newline, and a
    # final line without one is still counted.
    contract = _compile({"type": "integer"})
    for raw, expected_count in (
        (b"", 0),
        (b"1", 1),
        (b"1\n", 1),
        (b"1\n2\n", 2),
        (b"1\n2", 2),
        (b"\n", 1),
        (b"\n\n", 2),
    ):
        results, summary = instance_check.validate_lines(contract, raw)
        assert summary["inputLines"] == expected_count, (raw, summary)
        assert summary["inputLines"] == summary["accepted"] + summary["rejected"]

    # Byte-exact line limit (64 KiB), boundaries inclusive.
    results, summary = _run_raw(contract, "1\n" + "a" * MAX_LINE_BYTES + "\n")
    assert results[1].code == "invalid-json"  # exactly at the limit -> parsed
    results, summary = _run_raw(
        contract, "1\n" + "a" * (MAX_LINE_BYTES + 1) + "\n"
    )
    assert results[1].code == "line-too-long", results[1].code
    assert summary["inputLines"] == summary["accepted"] + summary["rejected"] == 2

    # A huge line followed by valid lines is isolated; streaming keeps memory
    # bounded and the following lines still parse.
    huge = "9" * (MAX_LINE_BYTES * 3)
    results, summary = _run_raw(contract, "1\n" + huge + "\n2\n")
    assert [r.accepted for r in results] == [True, False, True]
    assert results[1].code == "line-too-long"

    # Invalid UTF-8 is a per-line invalid-json rejection.
    stream = io.BytesIO(b"1\n\xff\n2\n")
    results, summary = instance_check.validate_stream(contract, stream)
    assert results[1].code == "invalid-json", results[1].code
    assert summary["accepted"] == 2 and summary["rejected"] == 1


def case_large_oneof_memo() -> None:
    # Many overlapping branches: 20 branches, each requiring a different
    # property; a payload with all 20 properties matches all branches.  The
    # validator must still report every success and evaluate each data
    # location a bounded number of times.
    names = [f"k{i}" for i in range(20)]
    schema = {"oneOf": [
        {
            "type": "object",
            "required": [n],
            "properties": {n: {"type": "integer"}},
        }
        for n in names
    ]}
    contract = _compile(schema)
    value = {n: 1 for n in names}
    result = _one(contract, value)
    assert not result.accepted
    assert result.code == "oneof-multiple-success"
    assert result.successes == list(range(20)), result.successes

    # The same contract with no matching branch: each branch contributes
    # exactly one representative error.
    bad = _one(contract, {n: "x" for n in names})
    assert bad.code == "oneof-no-match"
    assert len(bad.representative_errors) == 1 + 20


def case_key_order_invariance() -> None:
    contract = _compile(
        {
            "type": "object",
            "properties": {
                "b": {"type": "integer"},
                "a": {"type": "object", "required": ["z"], "properties": {"z": {"type": "string"}}},
            },
        }
    )
    values_a = [{"b": 1, "a": {"z": "ok"}}, {"a": {"z": 1}, "b": "x"}]
    values_b = [{"a": {"z": "ok"}, "b": 1}, {"b": "x", "a": {"z": 1}}]
    ra, sa = _run(contract, values_a)
    rb, sb = _run(contract, values_b)
    for x, y in zip(ra, rb):
        assert x.accepted == y.accepted
    # Canonical accepted JSON identical.
    assert ra[0].canonical == rb[0].canonical
    # Diagnostics identical.
    assert instance_check.report_line(ra[1])[1] == instance_check.report_line(rb[1])[1]


# ---------------------------------------------------------------------------
# Corpus + baseline cross-check
# ---------------------------------------------------------------------------

def _load_lines(path: str) -> list[str]:
    """Physical lines of a file, using the same split rule as the validator."""
    with open(path, "rb") as handle:
        raw = handle.read()
    return [line.decode("utf-8") for line in split_lines(raw)]


def case_corpus_and_baseline(out_dir: str) -> None:
    info = generate_corpus(out_dir)
    for scenario in info["scenarios"]:
        directory = scenario["dir"]
        with open(os.path.join(directory, "schema.json"), "r", encoding="utf-8") as h:
            raw_schema = json.load(h)
        contract = contract_check.compile_contract(raw_schema)
        with open(os.path.join(directory, "data.jsonl"), "r", encoding="utf-8") as h:
            raw = h.read().encode("utf-8")
        with open(os.path.join(directory, "meta.json"), "r", encoding="utf-8") as h:
            meta = json.load(h)

        results, summary = instance_check.validate_lines(contract, raw)
        assert summary["inputLines"] == summary["accepted"] + summary["rejected"]
        assert summary["inputLines"] == meta["inputLines"], scenario["name"]

        baseline = Baseline(raw_schema)
        for result, expected in zip(results, meta["expected"]):
            assert result.accepted == expected["accepted"], (
                f"{scenario['name']} line {result.lineno}: "
                f"expected {expected['accepted']}, got {result.accepted} "
                f"({result.code} {result.message})"
            )
            # Cross-check against the independent baseline where the baseline
            # can decide (it reports UNKNOWN for dynamically cyclic input).
            line_text = split_lines(raw)[result.lineno - 1].decode("utf-8")
            if result.code in ("line-too-long", "too-deep"):
                continue
            try:
                value = parse_json_line(line_text)
            except Exception:
                continue
            verdict = baseline.evaluate(value)
            if verdict in (ACCEPT, REJECT):
                assert (verdict == ACCEPT) == result.accepted, (
                    f"{scenario['name']} line {result.lineno}: baseline {verdict} "
                    f"vs validator {result.accepted}"
                )


def case_random_differential() -> None:
    """Deterministic pseudo-random contracts/values vs the baseline.

    Uses a fixed seed so the suite is reproducible; only conclusions both
    engines can decide are compared.
    """
    import random

    rng = random.Random(7)

    def rand_schema(depth):
        if depth <= 0:
            return {"type": rng.choice(["integer", "string", "boolean", "null"])}
        kind = rng.choice(["obj", "arr", "oneof", "scalar"])
        if kind == "obj":
            props = {
                c: rand_schema(depth - 1)
                for c in rng.sample("abcdef", rng.randint(0, 3))
            }
            schema = {"type": "object", "properties": props}
            if props and rng.random() < 0.5:
                schema["required"] = list(props)[: rng.randint(0, len(props))]
            if rng.random() < 0.5:
                schema["additionalProperties"] = rng.choice([True, False])
            return schema
        if kind == "arr":
            schema = {"type": "array"}
            if rng.random() < 0.8:
                schema["items"] = rand_schema(depth - 1)
            return schema
        if kind == "oneof":
            return {"oneOf": [rand_schema(depth - 1) for _ in range(rng.randint(1, 3))]}
        return {"type": rng.choice(["integer", "string", "null", "object", "array"])}

    def rand_value(depth=0):
        if depth >= 4:
            return rng.choice([0, 1, -1, None, True, False, "x", "a/b", "a~b"])
        kind = rng.choice(["i", "b", "n", "s", "f", "l", "o", "o"])
        if kind == "i":
            return rng.randint(-3, 3)
        if kind == "b":
            return rng.choice([True, False])
        if kind == "n":
            return None
        if kind == "s":
            return rng.choice(["", "x", "a/b", "a~b"])
        if kind == "f":
            return 1.0
        if kind == "l":
            return [rand_value(depth + 1) for _ in range(rng.randint(0, 2))]
        return {
            rng.choice("abcdef"): rand_value(depth + 1)
            for _ in range(rng.randint(0, 3))
        }

    compared = 0
    for _ in range(150):
        schema = rand_schema(rng.randint(1, 3))
        try:
            contract = contract_check.compile_contract(schema)
        except contract_check.ContractError:
            continue
        baseline = Baseline(schema)
        for _ in range(20):
            value = rand_value()
            result = instance_check.validate_value(
                instance_check.Engine(contract), value
            )
            verdict = baseline.evaluate(value)
            if verdict in (ACCEPT, REJECT):
                compared += 1
                assert (verdict == ACCEPT) == result.success, (
                    schema,
                    value,
                    result.success,
                    verdict,
                )
    assert compared > 1000, compared


def case_memo_reuse(out_dir: str) -> dict:
    from .generate import TREE_SCHEMA, TREE_VALID_1

    schema_text = json.dumps(TREE_SCHEMA)
    raw_schema = json.loads(schema_text)
    contract = contract_check.compile_contract(raw_schema)
    raw = (json.dumps(TREE_VALID_1) + "\n").encode()

    _results, cached = instance_check.validate_lines(contract, raw, use_cache=True)
    _results2, uncached = instance_check.validate_lines(contract, raw, use_cache=False)
    assert cached["pairsChecked"] <= uncached["pairsChecked"], (cached, uncached)
    # A second line validates independently against a fresh memo table; totals
    # are cumulative (per-line work is not shared across lines).
    _r5, two = instance_check.validate_lines(contract, raw + raw, use_cache=True)
    assert two["inputLines"] == 2
    assert two["pairsChecked"] == 2 * cached["pairsChecked"], (cached, two)

    # Cross-check the small recursive tree against the independent baseline.
    baseline = Baseline(TREE_SCHEMA)
    assert baseline.evaluate(TREE_VALID_1) == ACCEPT
    from .generate import TREE_INVALID

    assert baseline.evaluate(TREE_INVALID) == REJECT
    assert baseline.stats.pairs > 0

    # A many-branch oneOf where every branch descends into the same shared
    # subtree at the same data location: without memoisation that subtree is
    # evaluated once per branch; with it, once per line.
    contract2 = contract_check.compile_contract(
        {
            "$defs": {
                "inner": {
                    "type": "object",
                    "required": ["z"],
                    "properties": {"z": {"type": "integer"}},
                },
                "b0": {
                    "type": "object",
                    "required": ["a"],
                    "properties": {"a": {"$ref": "#/$defs/inner"}},
                },
                "b1": {
                    "type": "object",
                    "required": ["b"],
                    "properties": {"a": {"$ref": "#/$defs/inner"}},
                },
                "b2": {
                    "type": "object",
                    "required": ["c"],
                    "properties": {"a": {"$ref": "#/$defs/inner"}},
                },
            },
            "oneOf": [
                {"$ref": "#/$defs/b0"},
                {"$ref": "#/$defs/b1"},
                {"$ref": "#/$defs/b2"},
            ],
        }
    )
    raw2 = b'{"a":{"z":1}}\n'
    _r, c2 = instance_check.validate_lines(contract2, raw2, use_cache=True)
    _r2, u2 = instance_check.validate_lines(contract2, raw2, use_cache=False)
    assert c2["pairsChecked"] < u2["pairsChecked"], (c2, u2)
    assert c2["memoHits"] >= 2, c2

    # A diamond-shaped oneOf ladder: each level's two branches both lead to
    # the same next-level node at the same data location.  Without memo the
    # work grows exponentially with depth; with memo each distinct pair is
    # evaluated once.
    depth = 8
    defs: dict = {}
    for i in range(depth):
        defs[f"L{i}"] = {
            "oneOf": [{"$ref": f"#/$defs/A{i}"}, {"$ref": f"#/$defs/B{i}"}]
        }
        defs[f"A{i}"] = {
            "type": "object",
            "properties": {"c": {"$ref": f"#/$defs/L{i + 1}"}},
            "additionalProperties": True,
        }
        defs[f"B{i}"] = {
            "type": "object",
            "properties": {
                "c": {"$ref": f"#/$defs/A{i}"},
                "x": {"type": "integer"},
            },
            "additionalProperties": True,
        }
    defs[f"L{depth}"] = {
        "oneOf": [
            {
                "type": "object",
                "properties": {"leaf": {"type": "integer"}},
                "additionalProperties": True,
            },
            {"$ref": f"#/$defs/A{depth}"},
        ]
    }
    defs[f"A{depth}"] = {
        "type": "object",
        "properties": {"leaf": {"type": "integer"}},
        "additionalProperties": True,
    }
    ladder = contract_check.compile_contract({"$defs": defs, "$ref": "#/$defs/L0"})
    value: dict = {}
    cursor = value
    for _ in range(depth):
        cursor["c"] = {}
        cursor = cursor["c"]
    cursor["leaf"] = 1
    ladder_raw = (json.dumps(value) + "\n").encode()
    _r3, lc = instance_check.validate_lines(ladder, ladder_raw, use_cache=True)
    _r4, lu = instance_check.validate_lines(ladder, ladder_raw, use_cache=False)
    assert lc["pairsChecked"] < lu["pairsChecked"], (lc, lu)
    assert lc["memoHits"] > 0 and lu["memoHits"] == 0, (lc, lu)
    assert lc["pairsChecked"] == lc["memoMisses"], (lc, lu)
    return {
        "treeCachedPairs": cached["pairsChecked"],
        "treeUncachedPairs": uncached["pairsChecked"],
        "threeBranchCachedPairs": c2["pairsChecked"],
        "threeBranchUncachedPairs": u2["pairsChecked"],
        "threeBranchMemoHits": c2["memoHits"],
        "diamondCachedPairs": lc["pairsChecked"],
        "diamondUncachedPairs": lu["pairsChecked"],
        "diamondMemoHits": lc["memoHits"],
    }


def case_atomic_report(out_dir: str) -> None:
    from .cli import _atomic_write

    path = os.path.join(out_dir, "atomic-target.txt")
    _atomic_write(path, b"v1\n")
    _atomic_write(path, b"v2\n")
    with open(path, "rb") as h:
        assert h.read() == b"v2\n"
    # Failure path: writing into a non-existent directory leaves no file.
    missing_dir = os.path.join(out_dir, "no-such-dir", "x")
    try:
        _atomic_write(missing_dir, b"data")
    except OSError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected OSError")
    assert not os.path.exists(missing_dir)


def case_contract_precedes_data(out_dir: str) -> None:
    """A bad contract is rejected without any data file existing."""
    path = os.path.join(out_dir, "bad-contract.json")
    with open(path, "w", encoding="utf-8") as h:
        json.dump({"type": "string", "properties": {}}, h)
    try:
        contract_check.load_contract_file(path)
    except contract_check.ContractError as exc:
        assert exc.code == "invalid-combination"
    else:  # pragma: no cover
        raise AssertionError("expected contract error")


def run_verify(args) -> int:
    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    suite = Suite()

    suite.check("static-valid", case_static_valid)
    suite.check("unknown-keyword", case_unknown_keyword)
    suite.check("bad-combination", case_bad_combination)
    suite.check("missing-type", case_missing_type)
    suite.check("oneof-forms", case_oneof_forms)
    suite.check("direct-cycle", case_direct_cycle)
    suite.check("indirect-cycle", case_indirect_cycle)
    suite.check("legal-recursion", case_legal_recursion_is_not_cycle)
    suite.check("ref-forms", case_ref_forms)
    suite.check("node-limit", case_node_limit)
    suite.check("boolean-integer", case_boolean_integer)
    suite.check("required-null", case_required_null)
    suite.check("oneof-semantics", case_oneof_semantics)
    suite.check("oneof-representative", case_oneof_representative_selection)
    suite.check("additional-properties", case_additional_properties)
    suite.check("pointer-escaping", case_pointer_escaping)
    suite.check("duplicate-invalid-json", case_duplicate_keys_and_invalid_json)
    suite.check("array-without-items", case_array_without_items)
    suite.check("contract-shape-errors", case_contract_shape_errors)
    suite.check("pointer-helpers", case_pointer_helpers)
    suite.check("line-depth-limits", case_line_and_depth_limits)
    suite.check("ingress-edge-cases", case_ingress_edge_cases)
    suite.check("large-oneof-memo", case_large_oneof_memo)
    suite.check("key-order-invariance", case_key_order_invariance)
    suite.check("corpus-baseline", lambda: case_corpus_and_baseline(os.path.join(out_dir, "corpus")))
    suite.check("random-differential", case_random_differential)
    suite.check("memo-reuse", lambda: case_memo_reuse(os.path.join(out_dir, "memo")))
    suite.check("atomic-report", lambda: case_atomic_report(out_dir))
    suite.check("contract-before-data", lambda: case_contract_precedes_data(out_dir))

    report = {
        "ok": not suite.failures,
        "cases": len(suite.cases),
        "passed": len(suite.cases) - len(suite.failures),
        "failed": len(suite.failures),
        "failures": [
            {"name": c.name, "detail": c.detail} for c in suite.failures
        ],
        "metrics": {
            c.name: c.metrics for c in suite.cases if c.metrics
        },
    }
    print(canonical_json(report))
    return 0 if not suite.failures else 1


__all__ = ["run_verify"]
