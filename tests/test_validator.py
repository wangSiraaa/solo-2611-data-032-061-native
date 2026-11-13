"""Unit tests for the contract validator, using only the standard library.

Run with ``python3 -m unittest discover -s tests -v`` (or ``python3 run.py verify``
for the richer end-to-end conformance suite).
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from contract_validator import contract_check, instance_check, pointer  # noqa: E402
from contract_validator.baseline import ACCEPT, REJECT, Baseline  # noqa: E402
from contract_validator.generate import (  # noqa: E402
    ESCAPE_SCHEMA,
    REQUIRED_NULL_SCHEMA,
    TREE_SCHEMA,
    TREE_INVALID,
    TREE_VALID_1,
    TREE_VALID_2,
    TWO_BRANCH_MATCH_SCHEMA,
    depth_at_limit_line,
    depth_over_limit_line,
    generate_corpus,
)
from contract_validator.report_json import (  # noqa: E402
    MAX_LINE_BYTES,
    canonical_json,
    iter_physical_lines,
    parse_json_line,
    split_lines,
)


def compile_ok(schema):
    return contract_check.compile_contract(schema)


def one(contract, value):
    results, _ = instance_check.validate_lines(
        contract, (json.dumps(value, ensure_ascii=False) + "\n").encode()
    )
    return results[0]


class PointerTests(unittest.TestCase):
    def test_roundtrip_and_escapes(self):
        self.assertEqual(pointer.parse(""), [])
        self.assertEqual(pointer.parse("/a~1b"), ["a/b"])
        self.assertEqual(pointer.parse("/a~0b"), ["a~b"])
        self.assertEqual(pointer.parse("/~01"), ["~1"])
        self.assertEqual(pointer.render(["a/b", "a~b", ""]), "/a~1b/a~0b/")
        self.assertEqual(pointer.join("/a~1b", "c/d~e"), "/a~1b/c~1d~0e")
        self.assertEqual(pointer.normalize("/"), "")

    def test_bad_escapes_rejected(self):
        for bad in ("a~", "a~2b", "a~b"):
            with self.assertRaises(ValueError):
                pointer.unescape_token(bad)
        with self.assertRaises(ValueError):
            pointer.parse("no-slash")

    def test_unicode_names(self):
        names = ["汉字", "emoji😀", "a/b~c", ""]
        rendered = pointer.ROOT
        for name in names:
            rendered = pointer.join(rendered, name)
        self.assertEqual(pointer.parse(rendered), names)


class ContractCheckTests(unittest.TestCase):
    def assert_error(self, schema, code):
        with self.assertRaises(contract_check.ContractError) as ctx:
            compile_ok(schema)
        self.assertEqual(ctx.exception.code, code, ctx.exception.message)

    def test_minimal_valid(self):
        contract = compile_ok({"type": "integer"})
        self.assertEqual(contract.node_count, 1)
        compile_ok({"oneOf": [{"type": "integer"}, {"type": "null"}]})
        compile_ok({"title": "t", "description": "d", "type": "null"})

    def test_unknown_keywords(self):
        self.assert_error({"type": "object", "minProperties": 1}, "unknown-keyword")
        self.assert_error(
            {"type": "integer", "foo": 1}, "unknown-keyword"
        )
        self.assert_error(
            {"oneOf": [{"type": "integer"}], "title": "x"}, "unknown-keyword"
        )
        self.assert_error(
            {"oneOf": [{"type": "integer"}], "type": "integer"}, "unknown-keyword"
        )

    def test_bad_combinations(self):
        self.assert_error(
            {"type": "string", "properties": {"x": {"type": "string"}}},
            "invalid-combination",
        )
        self.assert_error(
            {"type": "integer", "items": {"type": "integer"}},
            "invalid-combination",
        )
        self.assert_error(
            {"type": "integer", "required": ["x"]}, "invalid-combination"
        )
        self.assert_error(
            {"type": "object", "additionalProperties": {"type": "integer"}},
            "invalid-additional-properties",
        )

    def test_shape_errors(self):
        self.assert_error({"properties": {"x": {"type": "integer"}}}, "invalid-schema")
        self.assert_error({"type": "number"}, "invalid-type")
        self.assert_error({"oneOf": []}, "invalid-oneof")
        self.assert_error({"oneOf": {}}, "invalid-oneof")
        self.assert_error({"type": "object", "properties": []}, "invalid-properties")
        self.assert_error({"type": "object", "required": [1]}, "invalid-required")
        self.assert_error({"type": "object", "required": ["a", "a"]}, "invalid-required")
        self.assert_error({"type": "array", "items": [1]}, "invalid-items")

    def test_ref_forms(self):
        self.assert_error({"$ref": "http://x"}, "invalid-ref")
        self.assert_error({"$ref": "#/$defs/a/b"}, "invalid-ref")
        self.assert_error({"$ref": "#/definitions/x"}, "invalid-ref")
        self.assert_error({"$ref": "#/$defs/nope"}, "unresolved-ref")
        self.assert_error({"$ref": "#/$defs/x", "type": "integer"}, "unknown-keyword")
        compile_ok({"$defs": {"x": {"type": "integer"}}, "$ref": "#/$defs/x"})
        compile_ok({"$defs": {"a/b~c": {"type": "integer"}}, "$ref": "#/$defs/a~1b~0c"})

    def test_cycle_detection(self):
        with self.assertRaises(contract_check.ContractError) as ctx:
            compile_ok(
                {"$defs": {"loop": {"$ref": "#/$defs/loop"}}, "$ref": "#/$defs/loop"}
            )
        self.assertEqual(ctx.exception.code, "zero-progress-cycle")
        self.assertTrue(ctx.exception.extra.get("path"))

        with self.assertRaises(contract_check.ContractError) as ctx:
            compile_ok(
                {
                    "$defs": {
                        "a": {"$ref": "#/$defs/b"},
                        "b": {"oneOf": [{"$ref": "#/$defs/a"}]},
                    },
                    "$ref": "#/$defs/a",
                }
            )
        self.assertEqual(ctx.exception.code, "zero-progress-cycle")
        # Legal recursion descending through properties is accepted.
        compile_ok(TREE_SCHEMA)

    def test_node_limit(self):
        def chain(n):
            root = {"type": "integer"}
            for _ in range(n - 1):
                root = {"type": "object", "properties": {"c": root}}
            return root

        self.assertEqual(compile_ok(chain(200)).node_count, 200)
        self.assert_error(chain(201), "too-many-nodes")


class ValidationTests(unittest.TestCase):
    def test_boolean_is_not_integer(self):
        contract = compile_ok({"type": "integer"})
        self.assertFalse(one(contract, True).accepted)
        self.assertFalse(one(contract, False).accepted)
        self.assertFalse(one(contract, 1.0).accepted)
        self.assertFalse(one(contract, 1.5).accepted)
        self.assertTrue(one(contract, 5).accepted)

    def test_required_vs_explicit_null(self):
        contract = compile_ok(REQUIRED_NULL_SCHEMA)
        self.assertTrue(one(contract, {"value": None}).accepted)
        wrong = one(contract, {"value": 0})
        self.assertFalse(wrong.accepted)
        self.assertEqual(wrong.data_pointer, "/value")
        missing = one(contract, {})
        self.assertEqual(missing.code, "required-property-missing")
        self.assertEqual(missing.data_pointer, "/value")

    def test_oneof_semantics(self):
        contract = compile_ok(TWO_BRANCH_MATCH_SCHEMA)
        both = one(contract, {"a": 1, "b": 2})
        self.assertEqual(both.code, "oneof-multiple-success")
        self.assertEqual(both.successes, [0, 1])
        self.assertTrue(one(contract, {"a": 1}).accepted)
        none = one(contract, {"a": "x"})
        self.assertEqual(none.code, "oneof-no-match")
        self.assertEqual(len(none.representative_errors), 1 + 2)

    def test_pointer_escaping_in_diagnostics(self):
        contract = compile_ok(ESCAPE_SCHEMA)
        self.assertTrue(one(contract, {"a/b": "s", "a~b": 3}).accepted)
        missing = one(contract, {"a/b": "s"})
        self.assertEqual(missing.data_pointer, "/a~0b")
        wrong = one(contract, {"a/b": 1, "a~b": "s"})
        ptrs = {e["dataPointer"] for e in wrong.representative_errors}
        self.assertEqual(ptrs, {"/a~1b", "/a~0b"})

    def test_recursive_tree_and_baseline(self):
        contract = compile_ok(TREE_SCHEMA)
        self.assertTrue(one(contract, TREE_VALID_1).accepted)
        self.assertTrue(one(contract, TREE_VALID_2).accepted)
        self.assertFalse(one(contract, TREE_INVALID).accepted)
        baseline = Baseline(TREE_SCHEMA)
        self.assertEqual(baseline.evaluate(TREE_VALID_1), ACCEPT)
        self.assertEqual(baseline.evaluate(TREE_INVALID), REJECT)

    def test_line_ingress(self):
        contract = compile_ok({"type": "object", "additionalProperties": True})
        raw = b'{"x":1,"x":2}\n{bad\n{"x":1}\n'
        results, summary = instance_check.validate_lines(contract, raw)
        self.assertEqual([r.code for r in results], ["duplicate-key", "invalid-json", None])
        self.assertEqual(summary["inputLines"], 3)
        self.assertEqual(summary["accepted"] + summary["rejected"], 3)

    def test_non_finite_rejected(self):
        contract = compile_ok({"type": "integer"})
        results, _ = instance_check.validate_lines(
            contract, b"NaN\nInfinity\n1e400\n"
        )
        self.assertEqual([r.code for r in results], ["invalid-json"] * 3)

    def test_line_limits(self):
        contract = compile_ok({"type": "integer"})
        results, summary = instance_check.validate_lines(
            contract, b"1\n" + b"a" * (MAX_LINE_BYTES + 1) + b"\n2\n"
        )
        self.assertEqual([r.accepted for r in results], [True, False, True])
        self.assertEqual(results[1].code, "line-too-long")
        self.assertEqual(summary["inputLines"], 3)

    def test_depth_limits(self):
        contract = compile_ok(
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
        results, _ = instance_check.validate_lines(
            contract,
            (depth_at_limit_line() + "\n" + depth_over_limit_line() + "\n").encode(),
        )
        self.assertTrue(results[0].accepted)
        self.assertEqual(results[1].code, "too-deep")

    def test_array_without_items(self):
        contract = compile_ok({"type": "array"})
        self.assertTrue(one(contract, [1, "x", None]).accepted)
        self.assertFalse(one(contract, {"a": 1}).accepted)

    def test_memo_reuse(self):
        contract = compile_ok(
            {
                "$defs": {
                    "inner": {
                        "type": "object",
                        "required": ["z"],
                        "properties": {"z": {"type": "integer"}},
                    },
                },
                "oneOf": [
                    {
                        "type": "object",
                        "required": ["a"],
                        "properties": {"a": {"$ref": "#/$defs/inner"}},
                    },
                    {
                        "type": "object",
                        "required": ["b"],
                        "properties": {"a": {"$ref": "#/$defs/inner"}},
                    },
                    {
                        "type": "object",
                        "required": ["c"],
                        "properties": {"a": {"$ref": "#/$defs/inner"}},
                    },
                ],
            }
        )
        raw = b'{"a":{"z":1}}\n'
        _r, cached = instance_check.validate_lines(contract, raw, use_cache=True)
        _r2, uncached = instance_check.validate_lines(contract, raw, use_cache=False)
        self.assertLess(cached["pairsChecked"], uncached["pairsChecked"])
        self.assertGreaterEqual(cached["memoHits"], 2)


class IngressTests(unittest.TestCase):
    def test_iter_matches_split(self):
        samples = [b"", b"\n", b"a", b"a\n", b"a\nb", b"\n\n", b"x\n\nx\n"]
        for raw in samples:
            got = [line for line, _t, _e in iter_physical_lines(io.BytesIO(raw))]
            self.assertEqual(got, split_lines(raw), raw)

    def test_oversized_streaming(self):
        raw = b"a" * (MAX_LINE_BYTES + 1) + b"\nok\n"
        out = list(iter_physical_lines(io.BytesIO(raw), chunk_size=7))
        self.assertEqual(out[0][1], True)
        self.assertEqual(out[1], (b"ok", False, 0))

    def test_canonical_json_sorted(self):
        self.assertEqual(canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')
        self.assertEqual(canonical_json("é"), '"\\u00e9"')

    def test_error_after_bad_line_continues(self):
        contract = compile_ok({"type": "integer"})
        results, summary = instance_check.validate_lines(
            contract, b'1\n{"x":1}\n2\n'
        )
        self.assertEqual([r.accepted for r in results], [True, False, True])
        self.assertEqual(summary["inputLines"], summary["accepted"] + summary["rejected"])


class CliTests(unittest.TestCase):
    def test_check_and_validate_cli_with_atomic_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = generate_corpus(os.path.join(tmp, "corpus"))
            scenario = info["scenarios"][0]["dir"]
            schema = os.path.join(scenario, "schema.json")
            data = os.path.join(scenario, "data.jsonl")
            out = os.path.join(tmp, "out.jsonl")

            check = subprocess.run(
                [sys.executable, os.path.join(ROOT, "run.py"), "check-schema", schema],
                capture_output=True,
                text=True,
            )
            self.assertEqual(check.returncode, 0, check.stderr)

            proc = subprocess.run(
                [
                    sys.executable,
                    os.path.join(ROOT, "run.py"),
                    "validate",
                    schema,
                    data,
                    out,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 1)  # corpus has a rejected line
            with open(out, "rb") as handle:
                lines = handle.read().splitlines()
            self.assertEqual(len(lines), 3)
            for line in lines:
                json.loads(line)

    def test_bad_contract_rejected_before_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            schema = os.path.join(tmp, "s.json")
            with open(schema, "w", encoding="utf-8") as handle:
                json.dump({"type": "string", "properties": {}}, handle)
            proc = subprocess.run(
                [
                    sys.executable,
                    os.path.join(ROOT, "run.py"),
                    "validate",
                    schema,
                    os.path.join(tmp, "missing.jsonl"),
                    os.path.join(tmp, "out.jsonl"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(os.path.exists(os.path.join(tmp, "out.jsonl")))


if __name__ == "__main__":
    unittest.main()
