"""Independent finite-expansion baseline used only by the verifier.

This evaluator shares no evaluation logic with :mod:`instance_check`.  It works
directly on the raw contract dictionary, resolves ``$ref`` lazily and expands
``oneOf`` branches with an explicit fuel budget.  If the budget runs out, or a
zero-progress reference loop is detected at runtime, it returns ``unknown``
instead of guessing.

It also counts *every* (schema pointer, data pointer) visit (no memoisation), so
the conformance suite can compare these visit counts with the validator's
distinct-pair check count and demonstrate that results are reused.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import pointer as ptr

ACCEPT = "accept"
REJECT = "reject"
UNKNOWN = "unknown"


def _is_integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _matches(type_name: str, value) -> bool:
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return _is_integer(value)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "null":
        return value is None
    return False


@dataclass
class BaselineStats:
    visits: int = 0
    pairs: int = 0
    unknowns: int = 0


class Baseline:
    def __init__(self, raw_contract: dict, fuel: int = 200_000):
        self.raw = raw_contract
        self.defs = raw_contract.get("$defs", {}) if isinstance(raw_contract, dict) else {}
        self.fuel = fuel
        self.stats = BaselineStats()

    def evaluate(self, value) -> str:
        self.stats = BaselineStats()
        result = self._eval(self.raw, value, "", "", set(), 0)
        return result

    def _resolve(self, ref: str):
        prefix = "#/$defs/"
        if not ref.startswith(prefix):
            return None
        tail = ref[len(prefix):]
        if "/" in tail:
            return None
        try:
            name = ptr.unescape_token(tail)
        except ValueError:
            return None
        return self.defs.get(name)

    def _eval(
        self,
        schema,
        value,
        data_path: str,
        schema_path: str,
        active: set,
        depth: int,
    ) -> str:
        if self.fuel <= 0:
            self.stats.unknowns += 1
            return UNKNOWN
        self.fuel -= 1
        if not isinstance(schema, dict):
            return UNKNOWN

        self.stats.visits += 1
        pair = (schema_path, data_path)
        # Count pair membership: distinct pairs observed by the baseline.
        if pair not in active:
            self.stats.pairs += 1

        if "$ref" in schema:
            ref = schema["$ref"]
            target = self._resolve(ref)
            if target is None:
                return UNKNOWN
            # A zero-progress loop at the same data location cannot ever make
            # progress; treat it as unknown rather than spin.
            marker = (ref, data_path)
            if marker in active:
                self.stats.unknowns += 1
                return UNKNOWN
            target_name = ptr.unescape_token(ref[len("#/$defs/"):])
            target_path = "/$defs/" + ptr.escape_token(target_name)
            return self._eval(
                target, value, data_path, target_path, active | {marker}, depth
            )

        if "oneOf" in schema:
            successes = 0
            unknown = False
            for index, branch in enumerate(schema["oneOf"]):
                verdict = self._eval(
                    branch,
                    value,
                    data_path,
                    schema_path + f"/oneOf/{index}",
                    active,
                    depth,
                )
                if verdict == ACCEPT:
                    successes += 1
                elif verdict == UNKNOWN:
                    unknown = True
            if unknown and successes == 0:
                self.stats.unknowns += 1
                return UNKNOWN
            return ACCEPT if successes == 1 else REJECT

        type_name = schema.get("type")
        if type_name is None or not _matches(type_name, value):
            return REJECT

        if type_name == "object":
            if not isinstance(value, dict):
                return REJECT
            verdict = ACCEPT
            for name in schema.get("required", []):
                if name not in value:
                    verdict = REJECT
            if schema.get("additionalProperties", True) is False:
                known = set(schema.get("properties", {}))
                if any(key not in known for key in value):
                    verdict = REJECT
            for name, child_schema in sorted(schema.get("properties", {}).items()):
                if name not in value:
                    continue
                child_verdict = self._eval(
                    child_schema,
                    value[name],
                    ptr.join(data_path, name),
                    schema_path
                    + "/properties/"
                    + ptr.escape_token(name),
                    active,
                    depth + 1,
                )
                if child_verdict == UNKNOWN:
                    verdict = UNKNOWN if verdict == ACCEPT else verdict
                elif child_verdict == REJECT:
                    verdict = REJECT
            return verdict

        if type_name == "array":
            if not isinstance(value, list):
                return REJECT
            items = schema.get("items")
            if items is None:
                # No `items`: element shapes are unconstrained.
                return ACCEPT
            verdict = ACCEPT
            for index, element in enumerate(value):
                child_verdict = self._eval(
                    items,
                    element,
                    ptr.join(data_path, str(index)),
                    schema_path + "/items",
                    active,
                    depth + 1,
                )
                if child_verdict == UNKNOWN:
                    verdict = UNKNOWN if verdict == ACCEPT else verdict
                elif child_verdict == REJECT:
                    verdict = REJECT
            return verdict

        return ACCEPT


__all__ = ["ACCEPT", "Baseline", "BaselineStats", "REJECT", "UNKNOWN"]
