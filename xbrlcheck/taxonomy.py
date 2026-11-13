"""Local-only taxonomy loading.

Only the declared subset is understood:

* ``xs:element`` declarations reachable from the instance's ``link:schemaRef``;
* ``xs:import`` with a *local* ``schemaLocation``;
* a calculation linkbase referenced from ``link:linkbaseRef`` in the schema
  ``appinfo``.

No network access, no generic DTS, no presentation/label handling, and no XSD
validation.  Anything outside the subset is reported as unsupported instead of
being guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

from lxml import etree

from .model import ConceptInfo, Diagnostic, SourceLocation
from .qname import (
    CALCULATION_LINKBASE_REF,
    INFRASTRUCTURE_NAMESPACES,
    LINK,
    LINKBASE_ARCROLE,
    SUMMATION_ARCROLE,
    XBRLI,
    XBRLDT,
    XS,
    QName,
    QNameError,
    qname_from_tag,
    resolve_qname,
)

CALC_ROLE = "http://www.xbrl.org/2003/role/calculation"
CALC_LINK_ROLE = "http://www.xbrl.org/2003/role/link"
MAX_SCHEMA_DEPTH = 32


class TaxonomyError(ValueError):
    pass


@dataclass
class CalcArc:
    parent: QName
    child: QName
    weight: Decimal
    order: float
    arc_id: str | None
    source: SourceLocation


@dataclass
class CalcRelation:
    role: str
    arcs: list[CalcArc]
    source: SourceLocation
    linkbase: str

    def by_parent(self) -> dict[QName, list[CalcArc]]:
        out: dict[QName, list[CalcArc]] = {}
        for arc in self.arcs:
            out.setdefault(arc.parent, []).append(arc)
        for children in out.values():
            children.sort(key=lambda a: (a.order, a.child.namespace, a.child.localname))
        return out


@dataclass
class Taxonomy:
    concepts: dict[QName, ConceptInfo] = field(default_factory=dict)
    calc_relations: list[CalcRelation] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    loaded_schemas: list[str] = field(default_factory=list)
    loaded_linkbases: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(d.severity == "error" for d in self.diagnostics)

    def concept(self, qname: QName) -> ConceptInfo | None:
        return self.concepts.get(qname)

    def axis(self, qname: QName) -> ConceptInfo | None:
        info = self.concepts.get(qname)
        if info is not None and info.kind == "dimension":
            return info
        return None

    def member(self, qname: QName) -> ConceptInfo | None:
        return self.concepts.get(qname)


class _SchemaLoader:
    def __init__(self, search_dirs: Iterable[Path], diagnostics: list[Diagnostic]):
        self.search_dirs = [Path(d) for d in search_dirs]
        self.diagnostics = diagnostics
        self.concepts: dict[QName, ConceptInfo] = {}
        self.linkbase_refs: list[tuple[Path, str, str | None, str | None]] = []
        self.loaded: list[str] = []
        self._visited: set[Path] = set()
        self._element_index: dict[tuple[Path, str], QName] = {}
        self._element_ids: dict[Path, dict[str, QName]] = {}

    # ---- resolution -------------------------------------------------
    def resolve_href(self, base: Path, href: str) -> Path | None:
        if not href:
            return None
        if "://" in href and not href.startswith("file:"):
            self.diagnostics.append(
                Diagnostic(
                    "error",
                    "remote-reference",
                    "remote taxonomy reference is not supported: %s" % href,
                )
            )
            return None
        target = Path(href)
        candidates: list[Path] = []
        if target.is_absolute():
            candidates.append(target)
        else:
            candidates.append((base.parent / target))
            candidates.extend(d / target for d in self.search_dirs)
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        self.diagnostics.append(
            Diagnostic(
                "error",
                "missing-local-schema",
                "cannot resolve local taxonomy reference %r from %s" % (href, base),
            )
        )
        return None

    # ---- schemas ----------------------------------------------------
    def load_schema(self, path: Path, depth: int = 0) -> None:
        path = path.resolve()
        if path in self._visited:
            return
        if depth > MAX_SCHEMA_DEPTH:
            self.diagnostics.append(
                Diagnostic("error", "schema-depth", "schema import depth exceeded at %s" % path)
            )
            return
        self._visited.add(path)
        self.loaded.append(str(path))
        try:
            tree = etree.parse(str(path))
        except (OSError, etree.XMLSyntaxError) as exc:
            self.diagnostics.append(
                Diagnostic("error", "schema-parse", "cannot parse schema %s: %s" % (path, exc))
            )
            return
        root = tree.getroot()
        if qname_from_tag(root.tag) != QName(XS, "schema"):
            self.diagnostics.append(
                Diagnostic("error", "schema-root", "not an xs:schema: %s" % path)
            )
            return
        target_ns = root.get("targetNamespace") or ""
        element_index: dict[str, QName] = {}
        for el in root.iter(QName(XS, "element").clark):
            self._register_element(el, target_ns, path, element_index)
        self._element_ids[path] = element_index
        for imp in root.iter(QName(XS, "import").clark):
            loc = imp.get("schemaLocation")
            ns = imp.get("namespace") or ""
            if not loc:
                if ns not in INFRASTRUCTURE_NAMESPACES:
                    self.diagnostics.append(
                        Diagnostic(
                            "warning",
                            "import-without-location",
                            "xs:import for %r has no local schemaLocation" % ns,
                        )
                    )
                continue
            target = self.resolve_href(path, loc)
            if target is not None:
                self.load_schema(target, depth + 1)
        self._collect_linkbase_refs(root, path)

    def _register_element(
        self, el: etree._Element, target_ns: str, path: Path, index: dict[str, QName]
    ) -> None:
        name = el.get("name")
        if not name:
            return
        qname = QName(target_ns, name)
        el_id = el.get("id")
        if el_id:
            index[el_id] = qname
        index.setdefault(name, qname)
        self._element_index[(path, name)] = qname
        if qname in self.concepts:
            return

        type_qname: QName | None = None
        raw_type = el.get("type")
        if raw_type:
            try:
                type_qname = resolve_qname(raw_type, el.nsmap)
            except QNameError as exc:
                self.diagnostics.append(
                    Diagnostic("error", "bad-type-qname", str(exc), _loc(path, el))
                )
        sub_group: QName | None = None
        raw_sub = el.get("substitutionGroup")
        if raw_sub:
            try:
                sub_group = resolve_qname(raw_sub, el.nsmap)
            except QNameError as exc:
                self.diagnostics.append(
                    Diagnostic("error", "bad-substitution-qname", str(exc), _loc(path, el))
                )

        kind = "other"
        if sub_group == QName(XBRLI, "item"):
            kind = "item"
        elif sub_group == QName(XBRLDT, "dimensionItem"):
            kind = "dimension"
        elif sub_group == QName(XBRLI, "tuple"):
            kind = "tuple"

        info = ConceptInfo(
            qname=qname,
            type_qname=type_qname,
            period_type=_attr_no_ns(el, XBRLI, "periodType") or el.get("periodType"),
            abstract=(el.get("abstract") or "false").strip() == "true",
            nillable=(el.get("nillable") or "false").strip() == "true",
            substitution_group=sub_group,
            kind=kind,
            schema_path=str(path),
        )
        self.concepts[qname] = info

    def _collect_linkbase_refs(self, root: etree._Element, path: Path) -> None:
        for el in root.iter(QName(LINK, "linkbaseRef").clark):
            href = el.get(QName("http://www.w3.org/1999/xlink", "href").clark)
            role = el.get(QName("http://www.w3.org/1999/xlink", "role").clark)
            arcrole = el.get(QName("http://www.w3.org/1999/xlink", "arcrole").clark)
            if href:
                self.linkbase_refs.append((path, href, role, arcrole))

    def lookup_locator(self, base: Path, href: str) -> QName | None:
        file_part, _, fragment = href.partition("#")
        target = (base.parent / file_part).resolve() if file_part else base.resolve()
        if not target.is_file():
            target = self.resolve_href(base, file_part) if file_part else None
        if target is None:
            return None
        if target not in self._element_ids:
            self.load_schema(target)
        ids = self._element_ids.get(target, {})
        if fragment in ids:
            return ids[fragment]
        return None


def _attr_no_ns(el: etree._Element, namespace: str, localname: str) -> str | None:
    return el.get(QName(namespace, localname).clark)


def _loc(path: Path, el: etree._Element) -> SourceLocation:
    return SourceLocation(str(path), el.sourceline, 0)


def _load_calculation_linkbase(
    taxonomy: Taxonomy, loader: _SchemaLoader, path: Path, href: str
) -> None:
    target = loader.resolve_href(path, href)
    if target is None:
        return
    if str(target) in taxonomy.loaded_linkbases:
        return
    taxonomy.loaded_linkbases.append(str(target))
    try:
        tree = etree.parse(str(target))
    except (OSError, etree.XMLSyntaxError) as exc:
        taxonomy.diagnostics.append(
            Diagnostic("error", "linkbase-parse", "cannot parse linkbase %s: %s" % (target, exc))
        )
        return
    root = tree.getroot()
    if qname_from_tag(root.tag) != QName(LINK, "linkbase"):
        taxonomy.diagnostics.append(
            Diagnostic("error", "linkbase-root", "not a link:linkbase: %s" % target)
        )
        return

    for calc_link in root.iter(QName(LINK, "calculationLink").clark):
        role = calc_link.get(QName("http://www.w3.org/1999/xlink", "role").clark) or CALC_LINK_ROLE
        locators: dict[str, QName] = {}
        for loc in calc_link.iter(QName(LINK, "loc").clark):
            label = loc.get(QName("http://www.w3.org/1999/xlink", "label").clark)
            loc_href = loc.get(QName("http://www.w3.org/1999/xlink", "href").clark)
            if not label or not loc_href:
                taxonomy.diagnostics.append(
                    Diagnostic("error", "bad-locator", "locator missing label/href", _loc(target, loc))
                )
                continue
            concept = loader.lookup_locator(target, loc_href)
            if concept is None:
                taxonomy.diagnostics.append(
                    Diagnostic(
                        "error",
                        "unresolved-locator",
                        "cannot resolve locator %r (%s)" % (label, loc_href),
                        _loc(target, loc),
                    )
                )
                continue
            locators[label] = concept

        arcs: list[CalcArc] = []
        for arc in calc_link.iter(QName(LINK, "calculationArc").clark):
            arcrole = arc.get(QName("http://www.w3.org/1999/xlink", "arcrole").clark)
            if arcrole != SUMMATION_ARCROLE:
                taxonomy.diagnostics.append(
                    Diagnostic(
                        "error",
                        "unsupported-arcrole",
                        "only summation-item arcs are supported, got %r" % arcrole,
                        _loc(target, arc),
                    )
                )
                continue
            if arc.get("priority") is not None:
                taxonomy.diagnostics.append(
                    Diagnostic(
                        "error",
                        "unsupported-priority",
                        "calculation arcs with @priority are outside the supported subset",
                        _loc(target, arc),
                    )
                )
                continue
            if (arc.get("use") or "optional").strip() == "prohibited":
                taxonomy.diagnostics.append(
                    Diagnostic(
                        "error",
                        "unsupported-prohibited",
                        "prohibited calculation arcs are outside the supported subset",
                        _loc(target, arc),
                    )
                )
                continue
            from_label = arc.get(QName("http://www.w3.org/1999/xlink", "from").clark)
            to_label = arc.get(QName("http://www.w3.org/1999/xlink", "to").clark)
            if from_label not in locators or to_label not in locators:
                taxonomy.diagnostics.append(
                    Diagnostic(
                        "error",
                        "unresolved-arc",
                        "arc endpoints %r -> %r are not declared locators"
                        % (from_label, to_label),
                        _loc(target, arc),
                    )
                )
                continue
            raw_weight = arc.get("weight")
            try:
                weight = Decimal(raw_weight) if raw_weight is not None else Decimal(1)
            except InvalidOperation:
                taxonomy.diagnostics.append(
                    Diagnostic(
                        "error",
                        "bad-weight",
                        "invalid calculation weight %r" % raw_weight,
                        _loc(target, arc),
                    )
                )
                continue
            if not weight.is_finite():
                taxonomy.diagnostics.append(
                    Diagnostic("error", "bad-weight", "weight must be finite", _loc(target, arc))
                )
                continue
            raw_order = arc.get("order") or "0"
            try:
                order = float(raw_order)
            except ValueError:
                taxonomy.diagnostics.append(
                    Diagnostic(
                        "error",
                        "bad-order",
                        "invalid arc order %r" % raw_order,
                        _loc(target, arc),
                    )
                )
                continue
            arcs.append(
                CalcArc(
                    parent=locators[from_label],
                    child=locators[to_label],
                    weight=weight,
                    order=order,
                    arc_id=arc.get("id"),
                    source=_loc(target, arc),
                )
            )
        taxonomy.calc_relations.append(
            CalcRelation(role=role, arcs=arcs, source=_loc(target, calc_link), linkbase=str(target))
        )


def _check_single_role(taxonomy: Taxonomy) -> None:
    roles = {rel.role for rel in taxonomy.calc_relations}
    if len(roles) > 1:
        taxonomy.diagnostics.append(
            Diagnostic(
                "error",
                "multiple-calculation-roles",
                "calculation relations use multiple roles (%s); only a single role is supported"
                % ", ".join(sorted(roles)),
            )
        )
        return
    for rel in taxonomy.calc_relations:
        by_child: dict[QName, list[QName]] = {}
        for arc in rel.arcs:
            by_child.setdefault(arc.child, []).append(arc.parent)
        # duplicate arcs between the same parent/child
        seen: set[tuple[QName, QName]] = set()
        for arc in rel.arcs:
            pair = (arc.parent, arc.child)
            if pair in seen:
                taxonomy.diagnostics.append(
                    Diagnostic(
                        "error",
                        "duplicate-calculation-arc",
                        "duplicate calculation arc %s -> %s"
                        % (arc.parent.clark, arc.child.clark),
                        arc.source,
                    )
                )
            seen.add(pair)
        # cycle detection over the parent -> child graph
        graph: dict[QName, list[QName]] = {}
        for arc in rel.arcs:
            graph.setdefault(arc.parent, []).append(arc.child)
        state: dict[QName, int] = {}

        def visit(node: QName) -> bool:
            mark = state.get(node, 0)
            if mark == 1:
                return True
            if mark == 2:
                return False
            state[node] = 1
            for nxt in graph.get(node, []):
                if visit(nxt):
                    return True
            state[node] = 2
            return False

        for node in list(graph) + list(by_child):
            if visit(node):
                taxonomy.diagnostics.append(
                    Diagnostic(
                        "error",
                        "cyclic-calculation",
                        "calculation graph contains a cycle involving %s" % node.clark,
                        rel.source,
                    )
                )
                break
        for arc in rel.arcs:
            for qn in (arc.parent, arc.child):
                info = taxonomy.concepts.get(qn)
                if info is None:
                    taxonomy.diagnostics.append(
                        Diagnostic(
                            "error",
                            "unknown-calculation-concept",
                            "calculation concept %s is not declared in the local taxonomy"
                            % qn.clark,
                            arc.source,
                        )
                    )
                elif not info.supported_numeric:
                    taxonomy.diagnostics.append(
                        Diagnostic(
                            "error",
                            "non-numeric-calculation-concept",
                            "calculation concept %s is not a supported monetary/decimal item"
                            % qn.clark,
                            arc.source,
                        )
                    )


def load_taxonomy(
    schema_paths: Iterable[Path], search_dirs: Iterable[Path] = ()
) -> Taxonomy:
    taxonomy = Taxonomy()
    loader = _SchemaLoader(search_dirs, taxonomy.diagnostics)
    for schema in schema_paths:
        if not Path(schema).is_file():
            taxonomy.diagnostics.append(
                Diagnostic(
                    "error",
                    "missing-local-schema",
                    "schema not found: %s" % schema,
                )
            )
            continue
        loader.load_schema(Path(schema))
    for path, href, role, arcrole in loader.linkbase_refs:
        if arcrole and arcrole != LINKBASE_ARCROLE:
            continue
        if role and role != CALCULATION_LINKBASE_REF:
            # Presentation/label/etc. references are outside the subset.
            continue
        _load_calculation_linkbase(taxonomy, loader, path, href)
    taxonomy.concepts = loader.concepts
    taxonomy.loaded_schemas = loader.loaded
    _check_single_role(taxonomy)
    return taxonomy
