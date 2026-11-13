"""Instance parsing for the declared XBRL subset.

Supported: numeric (monetary/decimal) items, instant and duration contexts,
simple units, and explicit dimensions.  Tuples, typed dimensions, divide units
and ``precision`` are rejected without attempting to reinterpret them.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

from .model import (
    ContextIdentity,
    ContextRecord,
    Diagnostic,
    Fact,
    FactKey,
    GroupInfo,
    Period,
    RejectedFact,
    Report,
    SourceLocation,
    UnitIdentity,
    UnitRecord,
)
from .precision import (
    PrecisionError,
    intersect_intervals,
    parse_decimals,
    parse_numeric,
    precision_interval,
)
from .qname import (
    INFRASTRUCTURE_NAMESPACES,
    LINK,
    XBRDI,
    XBRLI,
    XSI,
    QName,
    QNameError,
    qname_from_tag,
    resolve_qname,
)
from .taxonomy import Taxonomy

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$"
)


class ReportError(ValueError):
    pass


def normalize_datetime(text: str) -> str:
    """Validate and canonicalise an ``xs:date``/``xs:dateTime``.

    Timezone-aware values are converted to UTC so that equal instants with
    different offsets compare equal.  Naive values remain naive.
    """
    s = text.strip()
    if _DATE_RE.match(s):
        datetime.strptime(s, "%Y-%m-%d")
        return s + "T00:00:00"
    if not _DATETIME_RE.match(s):
        raise ValueError("invalid date/dateTime %r" % text)
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError("invalid date/dateTime %r" % text) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.isoformat() + "Z"
    return parsed.isoformat()


def _loc(path: Path, el: etree._Element, ordinal: int) -> SourceLocation:
    return SourceLocation(str(path), el.sourceline, ordinal)


def _split_qname_lexical(raw: str, nsmap, attribute: bool = True) -> QName:
    return resolve_qname(raw, nsmap, use_default=not attribute)


class _ContextBuilder:
    """Parse one ``xbrli:context`` element into a canonical identity."""

    def __init__(self, path: Path, taxonomy: Taxonomy, diagnostics: list[Diagnostic]):
        self.path = path
        self.taxonomy = taxonomy
        self.diagnostics = diagnostics

    def error(self, el, code: str, message: str) -> None:
        self.diagnostics.append(Diagnostic("error", code, message, _loc(self.path, el, 0)))

    def parse(self, el: etree._Element) -> ContextIdentity | None:
        ident = self._entity_identifier(el)
        period = self._period(el)
        dims = self._dimensions(el)
        if ident is None or period is None or dims is None:
            return None
        scheme, entity_id = ident
        dims_sorted = tuple(sorted(dims, key=lambda p: (p[0].namespace, p[0].localname)))
        return ContextIdentity(
            entity_scheme=scheme,
            entity_id=entity_id,
            period=period,
            dimensions=dims_sorted,
        )

    def _entity_identifier(self, el: etree._Element) -> tuple[str, str] | None:
        entity = el.find(QName(XBRLI, "entity").clark)
        if entity is None:
            self.error(el, "context-entity", "context is missing xbrli:entity")
            return None
        idents = entity.findall(QName(XBRLI, "identifier").clark)
        if len(idents) != 1:
            self.error(
                el,
                "context-identifier",
                "context must have exactly one xbrli:identifier, found %d" % len(idents),
            )
            return None
        ident = idents[0]
        scheme = ident.get("scheme")
        if scheme is None:
            self.error(el, "context-identifier-scheme", "entity identifier requires @scheme")
            return None
        text = (ident.text or "").strip()
        if not text:
            self.error(el, "context-identifier-value", "entity identifier is empty")
            return None
        return (scheme, text)

    def _period(self, el: etree._Element) -> Period | None:
        period = el.find(QName(XBRLI, "period").clark)
        if period is None:
            self.error(el, "context-period", "context is missing xbrli:period")
            return None
        instant = period.find(QName(XBRLI, "instant").clark)
        forever = period.find(QName(XBRLI, "forever").clark)
        start = period.find(QName(XBRLI, "startDate").clark)
        end = period.find(QName(XBRLI, "endDate").clark)
        if forever is not None:
            self.error(el, "unsupported-period", "forever periods are outside the supported subset")
            return None
        if instant is not None:
            if start is not None or end is not None:
                self.error(el, "context-period", "context mixes instant and duration")
                return None
            try:
                normalized = normalize_datetime(instant.text or "")
            except ValueError as exc:
                self.error(el, "context-period", str(exc))
                return None
            return Period(kind="instant", instant=normalized)
        if start is None or end is None:
            self.error(el, "context-period", "duration context needs startDate and endDate")
            return None
        try:
            start_norm = normalize_datetime(start.text or "")
            end_norm = normalize_datetime(end.text or "")
        except ValueError as exc:
            self.error(el, "context-period", str(exc))
            return None
        try:
            if datetime.fromisoformat(start_norm.rstrip("Z")) > datetime.fromisoformat(
                end_norm.rstrip("Z")
            ):
                self.error(el, "context-period", "duration startDate is after endDate")
                return None
        except ValueError:
            pass
        return Period(kind="duration", start=start_norm, end=end_norm)

    def _dimensions(self, el: etree._Element) -> list[tuple[QName, QName]] | None:
        dims: list[tuple[QName, QName]] = []
        seen_axes: dict[QName, QName] = {}
        # xbrli:segment is a child of xbrli:entity; xbrli:scenario is a child
        # of xbrli:context.  Both may carry explicit members.
        wrappers: list[tuple[etree._Element | None, str]] = []
        entity = el.find(QName(XBRLI, "entity").clark)
        if entity is not None:
            wrappers.append((entity.find(QName(XBRLI, "segment").clark), "segment"))
        wrappers.append((el.find(QName(XBRLI, "scenario").clark), "scenario"))
        for wrapper, container in wrappers:
            direct = self._dimension_children(el, wrapper, container)
            if direct is None:
                return None
            for axis, member in direct:
                if axis in seen_axes:
                    if seen_axes[axis] != member:
                        self.error(
                            el,
                            "duplicate-dimension-axis",
                            "axis %s has multiple members in one context" % axis.clark,
                        )
                        return None
                    continue
                seen_axes[axis] = member
                dims.append((axis, member))
        return dims

    def _dimension_children(self, context_el, wrapper, container):
        """Dimensions may sit directly in segment/scenario or nested; only
        direct explicit members are part of the subset."""
        found: list[tuple[QName, QName]] = []
        if wrapper is None:
            return found
        for child in wrapper:
            if not isinstance(child.tag, str):
                continue
            tag = qname_from_tag(child.tag)
            if tag.namespace != XBRDI:
                # Anything other than explicit dimensions is outside the subset.
                self.error(
                    context_el,
                    "unsupported-context-content",
                    "unsupported content in xbrli:%s: %s" % (container, tag.clark),
                )
                return None
            if tag.localname == "typedMember":
                self.error(
                    context_el,
                    "unsupported-typed-dimension",
                    "typed dimensions are outside the supported subset",
                )
                return None
            if tag.localname != "explicitMember":
                self.error(
                    context_el,
                    "unsupported-dimension-member",
                    "unsupported xbrldi element: %s" % tag.clark,
                )
                return None
            raw_dim = child.get("dimension")
            if not raw_dim:
                self.error(context_el, "dimension-axis", "explicitMember requires @dimension")
                return None
            try:
                axis = _split_qname_lexical(raw_dim, child.nsmap, attribute=True)
                member = resolve_qname(
                    (child.text or ""), child.nsmap, use_default=False
                )
            except QNameError as exc:
                self.error(context_el, "dimension-qname", str(exc))
                return None
            axis_info = self.taxonomy.axis(axis)
            if axis_info is None:
                self.error(
                    context_el,
                    "unknown-dimension-axis",
                    "dimension axis %s is not a declared dimensionItem in the local taxonomy"
                    % axis.clark,
                )
                return None
            if self.taxonomy.member(member) is None:
                self.error(
                    context_el,
                    "unknown-dimension-member",
                    "dimension member %s is not declared in the local taxonomy" % member.clark,
                )
                return None
            found.append((axis, member))
        return found


class _UnitBuilder:
    def __init__(self, path: Path, diagnostics: list[Diagnostic]):
        self.path = path
        self.diagnostics = diagnostics

    def parse(self, el: etree._Element) -> UnitIdentity | None:
        if el.find(QName(XBRLI, "divide").clark) is not None:
            self.diagnostics.append(
                Diagnostic(
                    "error",
                    "unsupported-divide-unit",
                    "divide units are outside the supported subset",
                    _loc(self.path, el, 0),
                )
            )
            return None
        measures: list[QName] = []
        for measure in el.findall(QName(XBRLI, "measure").clark):
            try:
                measures.append(resolve_qname((measure.text or ""), measure.nsmap))
            except QNameError as exc:
                self.diagnostics.append(
                    Diagnostic("error", "unit-measure", str(exc), _loc(self.path, measure, 0))
                )
                return None
        other = [
            qname_from_tag(c.tag)
            for c in el
            if isinstance(c.tag, str)
            and qname_from_tag(c.tag) not in {QName(XBRLI, "measure"), QName(XBRLI, "divide")}
        ]
        if other:
            self.diagnostics.append(
                Diagnostic(
                    "error",
                    "unsupported-unit-content",
                    "unsupported unit content: %s" % ", ".join(q.clark for q in other),
                    _loc(self.path, el, 0),
                )
            )
            return None
        if not measures:
            self.diagnostics.append(
                Diagnostic("error", "unit-measure", "unit has no measure", _loc(self.path, el, 0))
            )
            return None
        return UnitIdentity(measures=tuple(sorted(measures, key=lambda q: (q.namespace, q.localname))))


def _is_nil(el: etree._Element) -> bool:
    raw = el.get(QName(XSI, "nil").clark)
    return raw is not None and raw.strip().lower() in {"true", "1"}


def _classify_group(key: FactKey, facts: list[Fact]) -> GroupInfo:
    if not facts:
        raise ValueError("empty group")
    nils = [f for f in facts if f.nil]
    numbers = [f for f in facts if not f.nil]
    if nils and numbers:
        return GroupInfo(
            key=key,
            facts=facts,
            status="conflict",
            interval=None,
            reason="nil and numeric facts share the same concept/context/unit",
        )
    if nils:
        return GroupInfo(key=key, facts=facts, status="nil", interval=None)
    intervals = [f.interval for f in numbers]
    if any(i is None for i in intervals):
        return GroupInfo(
            key=key, facts=facts, status="conflict", interval=None, reason="missing precision"
        )
    common = intersect_intervals(intervals)  # type: ignore[arg-type]
    if common is None:
        return GroupInfo(
            key=key,
            facts=facts,
            status="conflict",
            interval=None,
            reason="declared precision intervals do not overlap",
        )
    values = {(f.value, f.decimals) for f in numbers}
    if len(facts) == 1:
        status = "single"
    elif len(values) == 1:
        status = "identical"
    else:
        status = "compatible"
    return GroupInfo(key=key, facts=facts, status=status, interval=common)


class InstanceParser:
    def __init__(self, taxonomy: Taxonomy):
        self.taxonomy = taxonomy

    def parse(self, path: Path) -> Report:
        path = Path(path)
        report = Report(path=path)
        try:
            tree = etree.parse(str(path))
        except (OSError, etree.XMLSyntaxError) as exc:
            report.diagnostics.append(
                Diagnostic("error", "instance-parse", "cannot parse instance %s: %s" % (path, exc))
            )
            return report
        root = tree.getroot()
        if qname_from_tag(root.tag) != QName(XBRLI, "xbrl"):
            report.diagnostics.append(
                Diagnostic("error", "instance-root", "root element is not xbrli:xbrl", SourceLocation(str(path), root.sourceline, 0))
            )
            return report

        for ref in root.findall(QName(LINK, "schemaRef").clark):
            href = ref.get(QName("http://www.w3.org/1999/xlink", "href").clark)
            if href:
                report.schema_refs.append(href)

        context_ids: dict[str, ContextIdentity] = {}
        unit_ids: dict[str, UnitIdentity] = {}
        ordinal = 0
        fact_entries: list[Fact] = []

        ctx_builder = _ContextBuilder(path, self.taxonomy, report.diagnostics)
        unit_builder = _UnitBuilder(path, report.diagnostics)

        for child in root:
            if not isinstance(child.tag, str):
                # comments and processing instructions carry no facts
                continue
            tag = qname_from_tag(child.tag)
            if tag == QName(XBRLI, "context"):
                ordinal += 1
                ctx_id = child.get("id")
                if not ctx_id:
                    report.diagnostics.append(
                        Diagnostic("error", "context-id", "context has no id", _loc(path, child, ordinal))
                    )
                    continue
                identity = ctx_builder.parse(child)
                if identity is None:
                    continue
                existing = context_ids.get(ctx_id)
                if existing is not None and existing != identity:
                    report.diagnostics.append(
                        Diagnostic(
                            "error",
                            "duplicate-context-id",
                            "context id %r is reused for a different context identity" % ctx_id,
                            _loc(path, child, ordinal),
                        )
                    )
                    continue
                context_ids[ctx_id] = identity
                report.contexts.append(
                    ContextRecord(identity=identity, context_id=ctx_id, location=_loc(path, child, ordinal))
                )
                continue
            if tag == QName(XBRLI, "unit"):
                ordinal += 1
                unit_id = child.get("id")
                if not unit_id:
                    report.diagnostics.append(
                        Diagnostic("error", "unit-id", "unit has no id", _loc(path, child, ordinal))
                    )
                    continue
                identity = unit_builder.parse(child)
                if identity is None:
                    continue
                existing = unit_ids.get(unit_id)
                if existing is not None and existing != identity:
                    report.diagnostics.append(
                        Diagnostic(
                            "error",
                            "duplicate-unit-id",
                            "unit id %r is reused for a different unit identity" % unit_id,
                            _loc(path, child, ordinal),
                        )
                    )
                    continue
                unit_ids[unit_id] = identity
                report.units.append(
                    UnitRecord(identity=identity, unit_id=unit_id, location=_loc(path, child, ordinal))
                )
                continue
            if tag.namespace in INFRASTRUCTURE_NAMESPACES or tag == QName(LINK, "schemaRef"):
                # linkbase refs, footnote links, etc. are outside the subset.
                if tag.namespace == LINK and tag.localname not in {"schemaRef", "linkbaseRef"}:
                    report.diagnostics.append(
                        Diagnostic(
                            "warning",
                            "ignored-link-element",
                            "ignored link element %s" % tag.clark,
                            _loc(path, child, ordinal),
                        )
                    )
                continue
            ordinal += 1
            self._parse_fact(
                report,
                child,
                tag,
                path,
                ordinal,
                context_ids,
                unit_ids,
                fact_entries,
            )

        for index, fact in enumerate(fact_entries):
            fact.index = index
        report.facts = fact_entries
        grouped: dict[FactKey, list[Fact]] = {}
        for fact in fact_entries:
            grouped.setdefault(fact.key, []).append(fact)
        for key, facts in grouped.items():
            report.groups[key] = _classify_group(key, facts)
        return report

    def _parse_fact(
        self,
        report: Report,
        el: etree._Element,
        tag: QName,
        path: Path,
        ordinal: int,
        context_ids: dict[str, ContextIdentity],
        unit_ids: dict[str, UnitIdentity],
        out: list[Fact],
    ) -> None:
        location = _loc(path, el, ordinal)
        raw_text = (el.text or "").strip()
        concept = self.taxonomy.concept(tag)

        def reject(reason: str, code: str = "fact-rejected") -> None:
            report.rejected.append(RejectedFact(concept=tag, raw=raw_text, reason=reason, location=location))
            report.diagnostics.append(Diagnostic("error", code, reason, location))

        if concept is None:
            reject("concept %s is not declared in the local taxonomy" % tag.clark, "unknown-concept")
            return
        if concept.kind == "tuple":
            reject("tuples are outside the supported subset", "unsupported-tuple")
            return
        if concept.kind != "item":
            reject("concept %s is not an item" % tag.clark, "unsupported-concept-kind")
            return
        if concept.abstract:
            reject("abstract concept %s cannot carry a fact" % tag.clark, "abstract-concept")
            return
        if el.get("precision") is not None:
            reject("precision attribute is outside the supported subset", "unsupported-precision")
            return
        if not concept.supported_numeric:
            reject(
                "concept %s is not a supported monetary/decimal item" % tag.clark,
                "unsupported-item-type",
            )
            return

        context_ref = el.get("contextRef")
        unit_ref = el.get("unitRef")
        if not context_ref:
            reject("fact is missing contextRef", "missing-contextref")
            return
        if not unit_ref:
            reject("fact is missing unitRef", "missing-unitref")
            return
        context = context_ids.get(context_ref)
        if context is None:
            reject("contextRef %r is not a declared context" % context_ref, "unresolved-context")
            return
        unit = unit_ids.get(unit_ref)
        if unit is None:
            reject("unitRef %r is not a declared unit" % unit_ref, "unresolved-unit")
            return

        if concept.period_type == "duration" and context.period.kind != "duration":
            reject(
                "concept %s is a duration item but context %r is an instant" % (tag.clark, context_ref),
                "period-type-mismatch",
            )
            return
        if concept.period_type == "instant" and context.period.kind != "instant":
            reject(
                "concept %s is an instant item but context %r is a duration" % (tag.clark, context_ref),
                "period-type-mismatch",
            )
            return

        nil = _is_nil(el)
        raw_decimals = el.get("decimals")
        try:
            decimals = parse_decimals(raw_decimals)
        except PrecisionError as exc:
            reject(str(exc), "bad-decimals")
            return

        if nil:
            if raw_text:
                reject("nil fact must not contain a value", "nil-with-value")
                return
            if decimals is None and raw_decimals is not None:
                reject("invalid decimals on nil fact", "bad-decimals")
                return
            key = FactKey(concept=tag, context=context, unit=unit)
            out.append(
                Fact(
                    key=key,
                    raw_value="",
                    value=None,
                    nil=True,
                    decimals=decimals,
                    interval=None,
                    source=location,
                    context_id=context_ref,
                    unit_id=unit_ref,
                )
            )
            return

        if decimals is None:
            reject("numeric fact requires a decimals attribute", "missing-decimals")
            return
        try:
            value = parse_numeric(raw_text)
        except PrecisionError as exc:
            reject(str(exc), "bad-value")
            return
        interval = precision_interval(value, decimals)
        key = FactKey(concept=tag, context=context, unit=unit)
        out.append(
            Fact(
                key=key,
                raw_value=raw_text,
                value=value,
                nil=False,
                decimals=decimals,
                interval=interval,
                source=location,
                context_id=context_ref,
                unit_id=unit_ref,
            )
        )


def parse_report(path: Path, taxonomy: Taxonomy) -> Report:
    return InstanceParser(taxonomy).parse(Path(path))
