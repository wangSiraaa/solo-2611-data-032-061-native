"""Core value objects shared by parsing, comparison and calculation checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from .precision import dec_str, interval_str
from .qname import QName


@dataclass(frozen=True)
class SourceLocation:
    file: str
    line: int | None
    ordinal: int

    def as_json(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "ordinal": self.ordinal}


@dataclass(frozen=True)
class Period:
    """An instant or duration period.  ``forever`` is outside the subset."""

    kind: str  # "instant" | "duration"
    instant: str | None = None
    start: str | None = None
    end: str | None = None

    def key(self) -> tuple[str, str | None, str | None, str | None]:
        return (self.kind, self.instant, self.start, self.end)

    def display(self) -> str:
        if self.kind == "instant":
            return "instant %s" % self.instant
        return "duration %s/%s" % (self.start, self.end)

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.kind == "instant":
            out["instant"] = self.instant
        else:
            out["start"] = self.start
            out["end"] = self.end
        return out


@dataclass(frozen=True)
class ContextIdentity:
    """Identity of a context, independent of its XML ``id`` and prefixes.

    Entity, period and the *set* of explicit dimension members are included.
    Dimension order is normalised by sorting (axis URI, axis local, member URI,
    member local), so document order and prefixes have no effect.
    """

    entity_scheme: str
    entity_id: str
    period: Period
    dimensions: tuple[tuple[QName, QName], ...] = ()

    def key(self) -> tuple[Any, ...]:
        return (self.entity_scheme, self.entity_id, self.period.key(), self.dimensions)

    def canonical(self) -> str:
        dims = ", ".join("%s=%s" % (a.clark, m.clark) for a, m in self.dimensions)
        return "entity(%s|%s);period(%s);dims(%s)" % (
            self.entity_scheme,
            self.entity_id,
            self.period.display(),
            dims,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "entity": {"scheme": self.entity_scheme, "identifier": self.entity_id},
            "period": self.period.as_json(),
            "dimensions": [{"axis": a.clark, "member": m.clark} for a, m in self.dimensions],
            "canonical": self.canonical(),
        }


@dataclass(frozen=True)
class UnitIdentity:
    """A simple numerator-only unit; identity is the sorted measure set."""

    measures: tuple[QName, ...]

    def key(self) -> tuple[QName, ...]:
        return self.measures

    def canonical(self) -> str:
        return "unit(%s)" % ";".join(m.clark for m in self.measures)

    def currency(self) -> str | None:
        if len(self.measures) == 1:
            return self.measures[0].localname
        return None

    def as_json(self) -> dict[str, Any]:
        return {
            "measures": [m.clark for m in self.measures],
            "currency": self.currency(),
            "canonical": self.canonical(),
        }


@dataclass(frozen=True)
class FactKey:
    """Canonical fact identity: concept + context identity + unit identity."""

    concept: QName
    context: ContextIdentity
    unit: UnitIdentity

    def canonical(self) -> str:
        return "%s;%s;%s" % (self.concept.clark, self.context.canonical(), self.unit.canonical())


@dataclass
class Fact:
    key: FactKey
    raw_value: str
    value: Decimal | None
    nil: bool
    decimals: str | None
    interval: tuple[Decimal, Decimal] | None
    source: SourceLocation
    context_id: str
    unit_id: str
    index: int = 0

    @property
    def concept(self) -> QName:
        return self.key.concept

    @property
    def is_numeric(self) -> bool:
        return not self.nil and self.value is not None

    def as_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "concept": {
                "namespace": self.concept.namespace,
                "local": self.concept.localname,
                "clark": self.concept.clark,
            },
            "context": self.key.context.as_json(),
            "unit": self.key.unit.as_json(),
            "raw_value": self.raw_value,
            "value": dec_str(self.value),
            "nil": self.nil,
            "decimals": self.decimals,
            "interval": (
                [dec_str(self.interval[0]), dec_str(self.interval[1])]
                if self.interval is not None
                else None
            ),
            "source": self.source.as_json(),
            "context_id": self.context_id,
            "unit_id": self.unit_id,
        }


@dataclass
class GroupInfo:
    """All facts sharing one canonical key, classified for consistency."""

    key: FactKey
    facts: list[Fact]
    status: str  # single | identical | compatible | conflict | nil
    interval: tuple[Decimal, Decimal] | None
    reason: str | None = None

    @property
    def nil(self) -> bool:
        return self.status == "nil"

    @property
    def conflict(self) -> bool:
        return self.status == "conflict"

    @property
    def numeric(self) -> bool:
        return self.interval is not None

    def as_json(self, *, include_facts: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "interval": (
                [dec_str(self.interval[0]), dec_str(self.interval[1])]
                if self.interval is not None
                else None
            ),
            "reason": self.reason,
            "fact_count": len(self.facts),
        }
        if include_facts:
            out["facts"] = [f.as_json() for f in self.facts]
        return out

    def summary(self) -> str:
        if self.status == "nil":
            return "nil"
        if self.interval is None:
            return self.status
        return "%s %s" % (self.status, interval_str(self.interval))


@dataclass
class Diagnostic:
    severity: str  # "error" | "warning"
    code: str
    message: str
    location: SourceLocation | None = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.location is not None:
            out["location"] = self.location.as_json()
        return out

    def render(self) -> str:
        where = ""
        if self.location is not None:
            line = ":%s" % self.location.line if self.location.line else ""
            where = " [%s%s]" % (self.location.file, line)
        return "%s: %s: %s%s" % (self.severity.upper(), self.code, self.message, where)


@dataclass
class RejectedFact:
    concept: QName | None
    raw: str
    reason: str
    location: SourceLocation

    def as_json(self) -> dict[str, Any]:
        return {
            "concept": self.concept.clark if self.concept else None,
            "raw": self.raw,
            "reason": self.reason,
            "location": self.location.as_json(),
        }


@dataclass
class ContextRecord:
    identity: ContextIdentity
    context_id: str
    location: SourceLocation

    def as_json(self) -> dict[str, Any]:
        out = self.identity.as_json()
        out["context_id"] = self.context_id
        out["source"] = self.location.as_json()
        return out


@dataclass
class UnitRecord:
    identity: UnitIdentity
    unit_id: str
    location: SourceLocation

    def as_json(self) -> dict[str, Any]:
        out = self.identity.as_json()
        out["unit_id"] = self.unit_id
        out["source"] = self.location.as_json()
        return out


@dataclass
class Report:
    path: Path
    contexts: list[ContextRecord] = field(default_factory=list)
    units: list[UnitRecord] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    groups: dict[FactKey, GroupInfo] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    rejected: list[RejectedFact] = field(default_factory=list)
    schema_refs: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(d.severity == "error" for d in self.diagnostics)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    def sorted_facts(self) -> list[Fact]:
        return sorted(
            self.facts,
            key=lambda f: (
                f.key.concept.namespace,
                f.key.concept.localname,
                f.key.context.canonical(),
                f.key.unit.canonical(),
                dec_str(f.value) or "",
                f.decimals or "",
                f.raw_value,
                f.source.file,
                f.source.line or 0,
                f.source.ordinal,
            ),
        )


@dataclass
class ConceptInfo:
    qname: QName
    type_qname: QName | None
    period_type: str | None
    abstract: bool
    nillable: bool
    substitution_group: QName | None
    kind: str  # item | dimension | tuple | other
    schema_path: str

    @property
    def supported_numeric(self) -> bool:
        return (
            self.kind == "item"
            and not self.abstract
            and self.type_qname is not None
            and self.type_qname.namespace == "http://www.xbrl.org/2003/instance"
            and self.type_qname.localname in {"monetaryItemType", "decimalItemType"}
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "qname": self.qname.clark,
            "type": self.type_qname.clark if self.type_qname else None,
            "period_type": self.period_type,
            "abstract": self.abstract,
            "nillable": self.nillable,
            "substitution_group": (
                self.substitution_group.clark if self.substitution_group else None
            ),
            "kind": self.kind,
            "schema": self.schema_path,
        }
