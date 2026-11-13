"""Local synthetic corpus generation (no network, no external data).

Everything here is deterministic given a seed and is produced entirely from
the contract literals below.  The generated corpus intentionally covers the
required boundary cases:

* a recursive binary tree whose leaves terminate through a legal ``oneOf``;
* direct and indirect zero-progress cycles (contracts that must be rejected);
* two ``oneOf`` branches that simultaneously match;
* ``required`` combined with explicit ``null``;
* a boolean masquerading as an integer;
* unknown / additional fields;
* names requiring ``~0`` and ``~1`` pointer escaping;
* the 64-level nesting ceiling (exactly at, one under and one over).

Each scenario lives in its own directory: ``schema.json`` (the contract as an
explicit two-space-indented JSON file), ``data.jsonl`` (the input lines) and
``meta.json`` (which lines are expected to pass and why lines are expected to
be rejected).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile


def _write_atomic(path: str, payload: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _schema_text(obj) -> str:
    return json.dumps(obj, ensure_ascii=True, indent=2, sort_keys=True) + "\n"


def _data_line(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

# A recursive tree: objects have a string label, an integer count and an
# optional "leaf" (null) OR a required "children" array of child trees.  A null
# leaf is represented by the node type being present... we use a oneOf so a
# node is either a leaf (with label+count, no children) or a branch node.
TREE_SCHEMA = {
    "$defs": {
        "tree": {
            "oneOf": [
                {
                    "type": "object",
                    "required": ["label", "count", "leaf"],
                    "additionalProperties": False,
                    "properties": {
                        "label": {"type": "string"},
                        "count": {"type": "integer"},
                        "leaf": {"type": "null"},
                    },
                },
                {
                    "type": "object",
                    "required": ["label", "count", "children"],
                    "additionalProperties": False,
                    "properties": {
                        "label": {"type": "string"},
                        "count": {"type": "integer"},
                        "children": {
                            "type": "array",
                            "items": {"$ref": "#/$defs/tree"},
                        },
                    },
                },
            ]
        }
    },
    "$ref": "#/$defs/tree",
}

TREE_VALID_1 = {
    "label": "root",
    "count": 0,
    "children": [
        {"label": "a", "count": 1, "leaf": None},
        {"label": "b", "count": 2, "children": [{"label": "b1", "count": 0, "leaf": None}]},
    ],
}

TREE_VALID_2 = {"label": "only", "count": 7, "leaf": None}

TREE_INVALID = {
    "label": "root",
    "count": 0,
    "children": [{"label": "broken", "count": 1}],  # child matches neither branch
}

# required + explicit null: "value" is required and must be null when present.
REQUIRED_NULL_SCHEMA = {
    "type": "object",
    "required": ["value"],
    "additionalProperties": False,
    "properties": {"value": {"type": "null"}},
}

# Boolean masquerading as an integer.
INTEGER_SCHEMA = {"type": "integer"}

# oneOf with two independently-matchable branches (both accept any integer).
TWO_BRANCH_MATCH_SCHEMA = {
    "oneOf": [
        {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}},
        {"type": "object", "required": ["b"], "properties": {"b": {"type": "integer"}}},
    ]
}

# Unknown fields with additionalProperties=false and with true.
STRICT_OBJECT_SCHEMA = {
    "type": "object",
    "required": ["x"],
    "additionalProperties": False,
    "properties": {"x": {"type": "integer"}},
}
OPEN_OBJECT_SCHEMA = {
    "type": "object",
    "required": ["x"],
    "additionalProperties": True,
    "properties": {"x": {"type": "integer"}},
}

# Pointer escaping: names "a/b" and "a~b".
ESCAPE_SCHEMA = {
    "type": "object",
    "required": ["a/b", "a~b"],
    "additionalProperties": False,
    "properties": {"a/b": {"type": "string"}, "a~b": {"type": "integer"}},
}

# Zero-progress cycles that must be rejected by check-schema.
DIRECT_CYCLE_SCHEMA = {
    "$defs": {"loop": {"$ref": "#/$defs/loop"}},
    "$ref": "#/$defs/loop",
}
INDIRECT_CYCLE_SCHEMA = {
    "$defs": {
        "a": {"$ref": "#/$defs/b"},
        "b": {"oneOf": [{"$ref": "#/$defs/a"}]},
    },
    "$ref": "#/$defs/a",
}
ONEOF_ROOT_CYCLE_SCHEMA = {
    "$defs": {
        "t": {
            "oneOf": [
                {"$ref": "#/$defs/u"},
                {"type": "null"},
            ]
        },
        "u": {"oneOf": [{"$ref": "#/$defs/t"}, {"type": "integer"}]},
    },
    "$ref": "#/$defs/t",
}

# Unknown keyword / illegal combination contracts for static rejection.
UNKNOWN_KEYWORD_SCHEMA = {"type": "object", "minProperties": 1}
BAD_COMBINATION_SCHEMA = {"type": "string", "properties": {"x": {"type": "string"}}}
REF_WITH_CONSTRAINT_SCHEMA = {
    "$defs": {"x": {"type": "integer"}},
    "$ref": "#/$defs/x",
    "type": "integer",
}
UNRESOLVED_REF_SCHEMA = {"$ref": "#/$defs/does-not-exist"}


def TOO_MANY_NODES_SCHEMA() -> dict:
    """A 201-node contract: the static node ceiling must reject it."""
    root: dict = {"type": "integer"}
    for _ in range(200):
        root = {"type": "object", "properties": {"c": root}}
    return root


# ---------------------------------------------------------------------------
# Nesting-ceiling lines
# ---------------------------------------------------------------------------

def nested_array(depth: int, leaf=None) -> str:
    """JSON text for ``depth`` nested arrays around ``leaf`` (JSON-encoded)."""
    return "[" * depth + ("null" if leaf is None else json.dumps(leaf)) + "]" * depth


def depth_at_limit_line(limit: int = 64) -> str:
    """A payload exactly at the nesting limit (64 bracket levels)."""
    return nested_array(limit)


def depth_over_limit_line(limit: int = 64) -> str:
    return nested_array(limit + 1)


def depth_under_limit_line(limit: int = 64) -> str:
    return nested_array(limit - 1)


def nested_object(depth: int) -> dict:
    node: dict = {"v": 0}
    for _ in range(depth - 1):
        node = {"child": node}
    return node


def _validate_depth_scenarios(limit: int = 64) -> tuple[dict, list[tuple[str, bool]]]:
    # A finite nested-array value terminates through a legal null leaf; the
    # recursive array edge consumes one data level per step.
    schema = {
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
    lines = [
        (depth_under_limit_line(limit), True),
        (depth_at_limit_line(limit), True),
        (depth_over_limit_line(limit), False),
    ]
    return schema, lines


# ---------------------------------------------------------------------------
# Public generation API
# ---------------------------------------------------------------------------

def _scenario(
    out_root: str,
    name: str,
    schema,
    lines: list[tuple[str, bool]],
    notes: str,
) -> dict:
    directory = os.path.join(out_root, name)
    os.makedirs(directory, exist_ok=True)
    schema_text = _schema_text(schema) if not isinstance(schema, str) else schema + "\n"
    data_text = "".join(line + "\n" for line, _expect in lines)
    meta = {
        "name": name,
        "notes": notes,
        "inputLines": len(lines),
        "expected": [
            {"line": i + 1, "accepted": expect} for i, (_l, expect) in enumerate(lines)
        ],
    }
    _write_atomic(os.path.join(directory, "schema.json"), schema_text)
    _write_atomic(os.path.join(directory, "data.jsonl"), data_text)
    _write_atomic(
        os.path.join(directory, "meta.json"),
        _schema_text(meta),
    )
    return {"name": name, "dir": directory, "lineCount": len(lines)}


def generate_corpus(out_root: str) -> dict:
    scenarios = []
    scenarios.append(
        _scenario(
            out_root,
            "recursive-tree",
            TREE_SCHEMA,
            [
                (_data_line(TREE_VALID_1), True),
                (_data_line(TREE_VALID_2), True),
                (_data_line(TREE_INVALID), False),
            ],
            "recursive tree; legal leaf termination; child matching neither branch",
        )
    )
    scenarios.append(
        _scenario(
            out_root,
            "required-null",
            REQUIRED_NULL_SCHEMA,
            [
                (_data_line({"value": None}), True),
                (_data_line({"value": 1}), False),
                (_data_line({}), False),
            ],
            "required with explicit null; missing property is distinct from null",
        )
    )
    scenarios.append(
        _scenario(
            out_root,
            "integer-not-boolean",
            INTEGER_SCHEMA,
            [
                ("1", True),
                ("-2", True),
                ("true", False),
                ("1.0", False),
                ("1.5", False),
            ],
            "exact integers only; booleans and floats are rejected",
        )
    )
    scenarios.append(
        _scenario(
            out_root,
            "oneof-two-success",
            TWO_BRANCH_MATCH_SCHEMA,
            [
                (_data_line({"a": 1, "b": 2}), False),
                (_data_line({"a": 1}), True),
                (_data_line({"b": 2}), True),
                (_data_line({"a": "x"}), False),
            ],
            "both branches match -> rejected; exactly one -> accepted",
        )
    )
    scenarios.append(
        _scenario(
            out_root,
            "strict-and-open-objects",
            STRICT_OBJECT_SCHEMA,
            [
                (_data_line({"x": 1}), True),
                (_data_line({"x": 1, "y": 2}), False),
                (_data_line({}), False),
            ],
            "additionalProperties=false rejects unknown fields",
        )
    )
    scenarios.append(
        _scenario(
            out_root,
            "open-object",
            OPEN_OBJECT_SCHEMA,
            [
                (_data_line({"x": 1}), True),
                (_data_line({"x": 1, "y": 2}), True),
                (_data_line({"x": True}), False),
            ],
            "additionalProperties=true permits unknown fields",
        )
    )
    scenarios.append(
        _scenario(
            out_root,
            "pointer-escaping",
            ESCAPE_SCHEMA,
            [
                (_data_line({"a/b": "s", "a~b": 3}), True),
                (_data_line({"a/b": "s"}), False),  # missing a~b
                (_data_line({"a/b": 1, "a~b": "s"}), False),
            ],
            "names requiring ~0/~1 escaping in diagnostics",
        )
    )

    depth_schema, depth_lines = _validate_depth_scenarios()
    scenarios.append(
        _scenario(
            out_root,
            "depth-limit",
            depth_schema,
            depth_lines,
            "63/deep <= 64 levels accepted; 65 levels rejected",
        )
    )

    # Raw (non-object) line-level ingress cases.
    line_schema = OPEN_OBJECT_SCHEMA
    raw_lines = [
        ('{"x": 1, "x": 2}', False),   # duplicate key
        ("{bad json", False),           # invalid JSON
        ('{"x": 1}', True),
    ]
    scenarios.append(
        _scenario(
            out_root,
            "ingress-errors",
            line_schema,
            raw_lines,
            "duplicate object keys and malformed JSON are per-line rejections",
        )
    )

    # static rejections: kept as contracts only, check-schema must reject.
    rejected = []
    for name, schema in (
        ("direct-cycle", DIRECT_CYCLE_SCHEMA),
        ("indirect-cycle", INDIRECT_CYCLE_SCHEMA),
        ("oneof-root-cycle", ONEOF_ROOT_CYCLE_SCHEMA),
        ("unknown-keyword", UNKNOWN_KEYWORD_SCHEMA),
        ("bad-combination", BAD_COMBINATION_SCHEMA),
        ("ref-with-constraint", REF_WITH_CONSTRAINT_SCHEMA),
        ("unresolved-ref", UNRESOLVED_REF_SCHEMA),
        ("too-many-nodes", TOO_MANY_NODES_SCHEMA()),
    ):
        directory = os.path.join(out_root, "rejected-schemas", name)
        _write_atomic(
            os.path.join(directory, "schema.json"), _schema_text(schema)
        )
        rejected.append({"name": name, "dir": directory})

    return {"scenarios": scenarios, "rejectedSchemas": rejected}


def generate_from_cli(args) -> int:
    info = generate_corpus(args.out_dir)
    sys.stdout.write(json.dumps(info, ensure_ascii=True, sort_keys=True) + "\n")
    return 0


__all__ = [
    "DIRECT_CYCLE_SCHEMA",
    "INDIRECT_CYCLE_SCHEMA",
    "ONEOF_ROOT_CYCLE_SCHEMA",
    "TREE_SCHEMA",
    "UNRESOLVED_REF_SCHEMA",
    "generate_corpus",
]
