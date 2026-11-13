"""XML QName helpers.

Canonical identity in this tool is always (namespace URI, local name).  Source
prefixes intentionally never leak into identities, so two documents that use
different prefixes still compare equal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from lxml import etree

XBRLI = "http://www.xbrl.org/2003/instance"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
XBRDI = "http://xbrl.org/2006/xbrldi"
XBRLDT = "http://xbrl.org/2005/xbrldt"
ISO4217 = "http://www.xbrl.org/2003/iso4217"
XS = "http://www.w3.org/2001/XMLSchema"
SUMMATION_ARCROLE = "http://www.xbrl.org/2003/arcrole/summation-item"
CALCULATION_LINKBASE_REF = "http://www.xbrl.org/2003/role/calculationLinkbaseRef"
LINKBASE_ARCROLE = "http://www.w3.org/1999/xlink/properties/linkbase"

INFRASTRUCTURE_NAMESPACES = frozenset({XBRLI, LINK, XLINK, XSI, XBRDI, XBRLDT, ISO4217})


class QNameError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class QName:
    namespace: str
    localname: str

    @property
    def clark(self) -> str:
        if self.namespace:
            return "{%s}%s" % (self.namespace, self.localname)
        return self.localname

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.clark


def qname_from_tag(tag: str) -> QName:
    """Convert an lxml tag (``{uri}local``) to a :class:`QName`."""
    if tag.startswith("{"):
        namespace, _, localname = tag[1:].partition("}")
        return QName(namespace, localname)
    return QName("", tag)


def resolve_qname(value: str, nsmap: Mapping[str | None, str], *, use_default: bool = False) -> QName:
    """Resolve a lexical QName using an in-scope namespace map.

    Attribute values and QName text content do not use the default namespace,
    hence ``use_default`` defaults to false.  A prefixed name whose prefix is
    undeclared is an error rather than a silently empty namespace.
    """
    s = value.strip()
    if not s:
        raise QNameError("empty QName")
    if ":" in s:
        prefix, localname = s.split(":", 1)
        if not prefix or not localname:
            raise QNameError("malformed QName %r" % value)
        try:
            namespace = nsmap[prefix]
        except KeyError as exc:
            raise QNameError("undeclared namespace prefix %r in %r" % (prefix, value)) from exc
        return QName(namespace, localname)
    if use_default:
        return QName(nsmap.get(None) or "", s)
    return QName("", s)


def qname_attr(element: etree._Element, name: str, *, required: bool = True) -> QName | None:
    raw = element.get(name)
    if raw is None:
        if required:
            raise QNameError("missing %s attribute on %s" % (name, element.tag))
        return None
    return resolve_qname(raw, element.nsmap)


def text_qname(element: etree._Element) -> QName:
    if element.text is None:
        raise QNameError("empty QName element %s" % element.tag)
    return resolve_qname(element.text, element.nsmap)
