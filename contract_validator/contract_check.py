"""Static contract checking / compilation for the supported JSON Schema subset.

Supported keywords
------------------
* ``type``: ``"object"``, ``"array"``, ``"string"``, ``"integer"``,
  ``"boolean"``, ``"null"``.  ``type`` is mandatory on every schema node.
* object: ``properties`` (name -> schema), ``required`` (array of names),
  ``additionalProperties`` (JSON boolean).
* array: ``items`` (one schema; applied to every element).
* ``oneOf``: non-empty array of schemas; exactly one must match at runtime.
* root-only ``$defs`` and local ``$ref`` of the exact form
  ``#/$defs/<name>`` (single reference token, ``~0``/``~1`` escapes).
* ``title`` and ``description`` are accepted annotations on ``type``/``$ref``
  nodes and ignored.  A ``oneOf`` node contains only its branch array (plus
  root-level ``$defs``).

Every contract is compiled into a finite node graph *before* any data is
read.  Unknown keywords, impossible keyword/type combinations, malformed or
unresolved references, graphs above 200 nodes and zero-progress cycles
(cycles made only of ``$ref``/``oneOf`` edges) are static contract errors.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from . import pointer as ptr
from .report_json import IngressError, measure_nesting, parse_json_line

MAX_CONTRACT_NODES = 200
# A valid contract with <=200 schema nodes cannot exceed this raw JSON nesting;
# higher values are rejected before the decoder so a hostile file can never
# trip the interpreter recursion guard.
MAX_CONTRACT_JSON_DEPTH = 1000

ALLOWED_TYPES = ("object", "array", "string", "integer", "boolean", "null")
_META_KEYS = frozenset({"title", "description"})
_SCHEMA_KEYS = frozenset(
    {"type", "properties", "required", "additionalProperties", "items"}
)
_REF_KEYS = frozenset({"$ref"})
_ONEOF_KEYS = frozenset({"oneOf"})


class ContractError(Exception):
    """A static contract error with a stable code and contract location."""

    def __init__(self, code: str, pointer: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.pointer = pointer
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        out = {
            "ok": False,
            "code": self.code,
            "pointer": self.pointer,
            "message": self.message,
        }
        out.update(self.extra)
        return out


@dataclass
class Node:
    id: int
    pointer: str
    kind: str  # "schema" | "ref" | "oneOf"
    type: str | None = None
    properties: dict[str, int] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    additional_properties: bool = True
    items: int | None = None
    oneof: tuple[int, ...] = ()
    ref_raw: str | None = None
    ref_target: int | None = None
    title: str | None = None
    description: str | None = None


@dataclass
class Contract:
    root: int
    nodes: list[Node]
    defs: dict[str, int]
    source: str = ""

    @property
    def node_count(self) -> int:
        return len(self.nodes)


class _Builder:
    def __init__(self):
        self.nodes: list[Node] = []
        self._pending_refs: list[int] = []

    def _count(self, pointer: str) -> int:
        if len(self.nodes) >= MAX_CONTRACT_NODES:
            raise ContractError(
                "too-many-nodes",
                pointer,
                f"contract graph exceeds the {MAX_CONTRACT_NODES}-node limit",
                limit=MAX_CONTRACT_NODES,
            )
        nid = len(self.nodes)
        self.nodes.append(Node(id=nid, pointer=pointer, kind="schema"))
        return nid

    def build(
        self, raw, pointer: str, defs_names: set[str], is_root: bool
    ) -> int:
        if not isinstance(raw, dict):
            raise ContractError(
                "invalid-schema", pointer, "schema node must be a JSON object"
            )
        nid = self._count(pointer)
        node = self.nodes[nid]

        keys = set(raw)
        if "$ref" in keys and "oneOf" in keys:
            raise ContractError(
                "invalid-combination",
                pointer,
                "$ref and oneOf are mutually exclusive on one node",
            )

        if "$ref" in keys:
            allowed = _REF_KEYS | _META_KEYS
            if is_root:
                allowed = allowed | {"$defs"}
            self._reject_unknown(raw, allowed, pointer)
            node.kind = "ref"
            node.ref_raw = raw["$ref"]
            node.title, node.description = _metadata(raw, pointer)
            self._pending_refs.append(nid)
            return nid

        if "oneOf" in keys:
            # "oneOf nodes contain only the branch array" (plus root $defs).
            allowed = _ONEOF_KEYS | ({"$defs"} if is_root else set())
            self._reject_unknown(raw, allowed, pointer)
            branches_raw = raw["oneOf"]
            if not isinstance(branches_raw, list):
                raise ContractError(
                    "invalid-oneof",
                    pointer + "/oneOf",
                    "oneOf must be a JSON array of schemas",
                )
            if not branches_raw:
                raise ContractError(
                    "invalid-oneof",
                    pointer + "/oneOf",
                    "oneOf must contain at least one branch",
                )
            node.kind = "oneOf"
            branch_ids = []
            for i, branch_raw in enumerate(branches_raw):
                branch_ids.append(
                    self.build(
                        branch_raw,
                        pointer + f"/oneOf/{i}",
                        defs_names,
                        False,
                    )
                )
            node.oneof = tuple(branch_ids)
            return nid

        allowed = _SCHEMA_KEYS | _META_KEYS
        if is_root:
            allowed = allowed | {"$defs"}
        self._reject_unknown(raw, allowed, pointer)
        node.kind = "schema"
        node.title, node.description = _metadata(raw, pointer)

        if "type" not in raw:
            raise ContractError(
                "invalid-schema",
                pointer,
                "a schema node must declare one of the supported types",
            )
        type_value = raw["type"]
        if not isinstance(type_value, str) or type_value not in ALLOWED_TYPES:
            raise ContractError(
                "invalid-type",
                pointer + "/type",
                f"type must be one of {', '.join(ALLOWED_TYPES)}",
            )
        node.type = type_value

        has_props = "properties" in raw
        has_req = "required" in raw
        has_add = "additionalProperties" in raw
        has_items = "items" in raw
        if type_value != "object" and (has_props or has_req or has_add):
            bad = "properties" if has_props else "required" if has_req else "additionalProperties"
            raise ContractError(
                "invalid-combination",
                pointer + "/" + bad,
                f"{bad} is only meaningful for type 'object'",
            )
        if type_value != "array" and has_items:
            raise ContractError(
                "invalid-combination",
                pointer + "/items",
                "items is only meaningful for type 'array'",
            )

        if has_props:
            props_raw = raw["properties"]
            if not isinstance(props_raw, dict):
                raise ContractError(
                    "invalid-properties",
                    pointer + "/properties",
                    "properties must be a JSON object",
                )
            for name in sorted(props_raw):
                child_id = self.build(
                    props_raw[name],
                    pointer + "/properties/" + ptr.escape_token(name),
                    defs_names,
                    False,
                )
                node.properties[name] = child_id
        if has_req:
            req_raw = raw["required"]
            if not isinstance(req_raw, list) or not all(
                isinstance(x, str) for x in req_raw
            ):
                raise ContractError(
                    "invalid-required",
                    pointer + "/required",
                    "required must be an array of strings",
                )
            if len(set(req_raw)) != len(req_raw):
                raise ContractError(
                    "invalid-required",
                    pointer + "/required",
                    "required property names must be unique",
                )
            node.required = tuple(sorted(req_raw))
        if has_add:
            add_raw = raw["additionalProperties"]
            if not isinstance(add_raw, bool):
                raise ContractError(
                    "invalid-additional-properties",
                    pointer + "/additionalProperties",
                    "additionalProperties must be a JSON boolean",
                )
            node.additional_properties = add_raw
        if has_items:
            items_raw = raw["items"]
            if not isinstance(items_raw, dict):
                raise ContractError(
                    "invalid-items",
                    pointer + "/items",
                    "items must be a single schema object",
                )
            node.items = self.build(
                items_raw, pointer + "/items", defs_names, False
            )
        return nid

    @staticmethod
    def _reject_unknown(raw: dict, allowed: set, pointer: str) -> None:
        unknown = sorted(set(raw) - allowed)
        if unknown:
            key = unknown[0]
            raise ContractError(
                "unknown-keyword",
                pointer + "/" + ptr.escape_token(key),
                f"unknown keyword {key!r}; allowed here: "
                + ", ".join(sorted(allowed)),
                keyword=key,
            )

    # -- references -------------------------------------------------------
    def resolve_references(self, defs: dict[str, int]) -> None:
        for nid in self._pending_refs:
            node = self.nodes[nid]
            raw = node.ref_raw
            if not isinstance(raw, str):
                raise ContractError(
                    "invalid-ref", node.pointer, "$ref must be a string"
                )
            prefix = "#/$defs/"
            if not raw.startswith(prefix):
                raise ContractError(
                    "invalid-ref",
                    node.pointer,
                    "$ref must use the local form '#/$defs/<name>'",
                    ref=raw,
                )
            tail = raw[len(prefix):]
            if tail == "" or "/" in tail:
                raise ContractError(
                    "invalid-ref",
                    node.pointer,
                    "$ref must name exactly one $defs entry via a single "
                    "JSON Pointer token",
                    ref=raw,
                )
            try:
                name = ptr.unescape_token(tail)
            except ValueError as exc:
                raise ContractError(
                    "invalid-ref",
                    node.pointer,
                    f"invalid JSON Pointer escape in $ref: {exc}",
                    ref=raw,
                )
            if name not in defs:
                raise ContractError(
                    "unresolved-ref",
                    node.pointer,
                    f"$ref target '#/$defs/{tail}' does not exist",
                    ref=raw,
                )
            node.ref_target = defs[name]


def _metadata(raw: dict, pointer: str) -> tuple[str | None, str | None]:
    title = raw.get("title")
    description = raw.get("description")
    for key, value in (("title", title), ("description", description)):
        if value is not None and not isinstance(value, str):
            raise ContractError(
                "invalid-metadata",
                pointer + "/" + key,
                f"{key} must be a string when present",
            )
    return title, description


def _cycle_edges(nodes: list[Node]) -> list[list[tuple[str, int]]]:
    edges = []
    for node in nodes:
        out = []
        if node.kind == "ref" and node.ref_target is not None:
            out.append((f"$ref {node.ref_raw}", node.ref_target))
        elif node.kind == "oneOf":
            for i, branch in enumerate(node.oneof):
                out.append((f"oneOf[{i}]", branch))
        edges.append(out)
    return edges


def _display(node: Node) -> str:
    return "<root>" if node.pointer == "" else node.pointer


def find_zero_progress_cycle(nodes: list[Node]) -> dict | None:
    """Find a cycle made only of $ref/oneOf edges.

    Properties and items edges consume one data level and therefore cannot
    close a zero-progress loop.  Returns a deterministic description with the
    reference path, or ``None``.
    """
    edges = _cycle_edges(nodes)
    n = len(nodes)
    color = [0] * n  # 0 white, 1 gray, 2 black
    for start in range(n):
        if color[start] != 0:
            continue
        path_nodes = [start]
        path_edges: list[tuple[str, int]] = []  # labels used to enter each node
        color[start] = 1
        stack = [(start, 0)]
        while stack:
            node_id, edge_index = stack[-1]
            out = edges[node_id]
            if edge_index >= len(out):
                color[node_id] = 2
                stack.pop()
                if path_nodes and path_nodes[-1] == node_id:
                    path_nodes.pop()
                    if path_edges:
                        path_edges.pop()
                continue
            stack[-1] = (node_id, edge_index + 1)
            label, target = out[edge_index]
            if color[target] == 1:
                # The gray target is on the current DFS path.
                try:
                    pos = path_nodes.index(target)
                except ValueError:  # pragma: no cover - defensive
                    pos = 0
                cycle_nodes = path_nodes[pos:] + [target]
                cycle_path_edges = path_edges[pos:] + [(label, target)]
                parts = [_display(nodes[cycle_nodes[0]])]
                edge_dicts = []
                for i, (edge_label, _tgt) in enumerate(cycle_path_edges):
                    to_node = cycle_nodes[i + 1]
                    parts.append(f"-[{edge_label}]-> {_display(nodes[to_node])}")
                    edge_dicts.append(
                        {
                            "from": nodes[cycle_nodes[i]].pointer,
                            "label": edge_label,
                            "to": nodes[to_node].pointer,
                        }
                    )
                return {
                    "pointer": nodes[cycle_nodes[0]].pointer,
                    "message": "zero-progress cycle through $ref/oneOf: "
                    + " ".join(parts),
                    "path": " ".join(parts),
                    "cycleNodes": [nodes[x].pointer for x in cycle_nodes],
                    "cycleEdges": edge_dicts,
                }
            if color[target] == 0:
                color[target] = 1
                path_nodes.append(target)
                path_edges.append((label, target))
                stack.append((target, 0))
    return None


def compile_contract(raw, source: str = "") -> Contract:
    """Compile a parsed JSON contract, raising ContractError on any problem."""
    if not isinstance(raw, dict):
        raise ContractError(
            "invalid-schema", "", "the contract root must be a JSON object"
        )
    builder = _Builder()

    defs_names: set[str] = set()
    defs_raw = raw.get("$defs")
    if "$defs" in raw and not isinstance(defs_raw, dict):
        raise ContractError(
            "invalid-defs", "/$defs", "$defs must be a JSON object"
        )
    if isinstance(defs_raw, dict):
        defs_names = set(defs_raw)
    root_id = builder.build(raw, "", defs_names, True)

    defs: dict[str, int] = {}
    if isinstance(defs_raw, dict):
        for name in sorted(defs_raw):
            defs[name] = builder.build(
                defs_raw[name],
                "/$defs/" + ptr.escape_token(name),
                defs_names,
                False,
            )
    builder.resolve_references(defs)

    cycle = find_zero_progress_cycle(builder.nodes)
    if cycle is not None:
        raise ContractError(
            "zero-progress-cycle",
            cycle["pointer"],
            cycle["message"],
            path=cycle["path"],
            cycleNodes=cycle["cycleNodes"],
            cycleEdges=cycle["cycleEdges"],
        )
    return Contract(
        root=root_id, nodes=builder.nodes, defs=defs, source=source
    )


def load_contract_text(text: str, source: str = "") -> Contract:
    """Compile a contract from JSON text, with pre-parse depth isolation."""
    if measure_nesting(text) > MAX_CONTRACT_JSON_DEPTH:
        raise ContractError(
            "contract-too-deep",
            "",
            f"contract JSON nesting exceeds {MAX_CONTRACT_JSON_DEPTH} levels",
        )
    # Even below the (generous) pre-parse bound, a contract that deep is far
    # beyond the 200-node graph limit.  Parse with a temporarily raised
    # recursion limit so the structural error is reported rather than an
    # opaque JSON parse failure.
    old_limit = sys.getrecursionlimit()
    needed = 2 * measure_nesting(text) + 200
    if needed > old_limit:
        sys.setrecursionlimit(needed)
    try:
        try:
            raw = parse_json_line(text)
        except RecursionError:
            raise ContractError(
                "contract-too-deep",
                "",
                "contract JSON is too deep to parse",
            ) from None
        except IngressError as exc:
            code = (
                "duplicate-key" if exc.code == "duplicate-key" else "contract-invalid-json"
            )
            raise ContractError(code, "", exc.message) from None
    finally:
        sys.setrecursionlimit(old_limit)
    return compile_contract(raw, source=source)


def load_contract_file(path: str) -> Contract:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise ContractError(
            "contract-unreadable", "", f"cannot read contract file: {exc}"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ContractError(
            "contract-invalid-json", "", "contract file is not valid UTF-8"
        )
    if text.startswith("﻿"):
        text = text[1:]
    return load_contract_text(text, source=path)


__all__ = [
    "ALLOWED_TYPES",
    "Contract",
    "ContractError",
    "MAX_CONTRACT_NODES",
    "Node",
    "compile_contract",
    "find_zero_progress_cycle",
    "load_contract_file",
    "load_contract_text",
]
