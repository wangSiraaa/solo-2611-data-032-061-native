"""直接读取 OOXML 包（ZIP + lxml），不经过 openpyxl 的公式加工。

关键点：
- 共享公式主格 <f t="shared" si="0" ref="D2:D5"> 携带公式文本；
  跟随格只有 <f t="shared" si="0"/>，没有文本。二者必须分开建模，
  跟随格在 engine 中按位移展开，绝不能当成同一公式直接求值。
- 数组公式 t="array"、数据表 t="dataTable"、外部链接、命名区域一律
  显式标记 unsupported，不读取旧缓存冒充结果。
- 静态值区分 inlineStr / sharedStrings / 数字 / 布尔 / 错误 / 空白。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field

from lxml import etree

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NSMAP = {"m": NS, "r": R_NS}

CELL_REF_RE = re.compile(r"^([A-Z]+)([0-9]+)$")


@dataclass
class FormulaInfo:
    kind: str  # normal / shared_master / shared_follower / array / data_table / other
    text: str | None          # 主格或普通公式的原文；跟随格为 None
    si: str | None = None     # 共享公式索引
    master_ref: str | None = None  # 主格声明的适用范围 ref
    unsupported_reason: str | None = None


@dataclass
class StaticValue:
    """非公式单元格的静态值（六类值模型的原始形态，engine 再转换）。"""

    kind: str  # blank / number / text / boolean / error
    value: object = None
    reason: str | None = None


@dataclass
class CellRecord:
    coord: str
    col: int
    row: int
    style: str | None
    formula: FormulaInfo | None
    static: StaticValue
    cached_type: str | None  # 原缓存 t 属性，仅供核对，不参与求值


@dataclass
class SheetData:
    name: str
    path: str
    cells: dict[tuple[int, int], CellRecord]
    merges: list[str] = field(default_factory=list)


@dataclass
class WorkbookData:
    path: str
    sheet_order: list[str]
    sheets: dict[str, SheetData]
    shared_strings: list[str]
    defined_names: list[str]
    has_external_links: bool
    external_link_parts: list[str]
    all_parts: list[str]


# ---------------- 坐标工具 ----------------
def split_coord(coord: str) -> tuple[int, int]:
    m = CELL_REF_RE.match(coord)
    if not m:
        raise ValueError(f"非法单元格坐标: {coord!r}")
    letters, row = m.groups()
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - 64)
    return col, int(row)


def coord_key(coord: str) -> tuple[int, int]:
    return split_coord(coord)


# ---------------- 读取入口 ----------------
def read_workbook(path: str) -> WorkbookData:
    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()
        workbook_xml = zf.read("xl/workbook.xml")
        rels_xml = zf.read("xl/_rels/workbook.xml.rels")
        shared_strings = _read_shared_strings(zf, names)
        external_parts = sorted(
            n for n in names if n.startswith("xl/externalLinks/") and n.endswith(".xml")
        )
        sheet_order, rid_to_name, defined_names = _parse_workbook(workbook_xml)
        rid_to_target = _parse_rels(rels_xml)
        has_external = _rels_reference_external(rels_xml) or bool(external_parts)

        sheets: dict[str, SheetData] = {}
        for name in sheet_order:
            rid = rid_to_name[name]
            target = rid_to_target[rid]
            sheet_path = _normalize_target(target)
            root = etree.fromstring(zf.read(sheet_path))
            sheets[name] = _parse_sheet(name, sheet_path, root, shared_strings)

    return WorkbookData(
        path=path,
        sheet_order=sheet_order,
        sheets=sheets,
        shared_strings=shared_strings,
        defined_names=defined_names,
        has_external_links=has_external,
        external_link_parts=external_parts,
        all_parts=names,
    )


def _normalize_target(target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return "xl/" + target


def _parse_workbook(xml_bytes: bytes):
    root = etree.fromstring(xml_bytes)
    order: list[str] = []
    rid_to_name: dict[str, str] = {}
    for sheet in root.findall("m:sheets/m:sheet", NSMAP):
        name = sheet.get("name")
        rid = sheet.get(f"{{{R_NS}}}id")
        order.append(name)
        rid_to_name[name] = rid
    defined: list[str] = []
    dn = root.find("m:definedNames", NSMAP)
    if dn is not None:
        for d in dn.findall("m:definedName", NSMAP):
            defined.append(d.get("name") or "(未命名)")
    return order, rid_to_name, defined


def _parse_rels(xml_bytes: bytes) -> dict[str, str]:
    root = etree.fromstring(xml_bytes)
    mapping: dict[str, str] = {}
    for rel in root.findall(f"{{{PKG_REL_NS}}}Relationship"):
        mapping[rel.get("Id")] = rel.get("Target")
    return mapping


def _rels_reference_external(xml_bytes: bytes) -> bool:
    root = etree.fromstring(xml_bytes)
    for rel in root.findall(f"{{{PKG_REL_NS}}}Relationship"):
        rtype = rel.get("Type", "")
        if rtype.endswith("/externalLink"):
            return True
    return False


def _read_shared_strings(zf: zipfile.ZipFile, names: list[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    root = etree.fromstring(zf.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for si in root.findall("m:si", NSMAP):
        # si 可能含单个 t，或富文本 r/t 多段
        parts = [t.text or "" for t in si.findall(".//m:t", NSMAP)]
        strings.append("".join(parts))
    return strings


def _parse_sheet(
    name: str, path: str, root: etree._Element, shared_strings: list[str]
) -> SheetData:
    cells: dict[tuple[int, int], CellRecord] = {}
    for c in root.findall("m:sheetData/m:row/m:c", NSMAP):
        coord = c.get("r")
        col, row = split_coord(coord)
        style = c.get("s")
        ctype = c.get("t")  # n / b / e / s / inlineStr / str
        f_el = c.find("m:f", NSMAP)
        formula = _parse_formula(f_el) if f_el is not None else None
        static = _parse_static(c, ctype, shared_strings)
        cells[(col, row)] = CellRecord(
            coord=coord, col=col, row=row, style=style,
            formula=formula, static=static, cached_type=ctype,
        )
    merges = [
        mc.get("ref")
        for mc in root.findall("m:mergeCells/m:mergeCell", NSMAP)
    ]
    return SheetData(name=name, path=path, cells=cells, merges=merges)


def _parse_formula(f_el: etree._Element) -> FormulaInfo:
    ftype = f_el.get("t", "normal")
    si = f_el.get("si")
    ref = f_el.get("ref")
    text = f_el.text
    if ftype == "shared":
        if text is not None and text != "":
            return FormulaInfo("shared_master", text, si=si, master_ref=ref)
        return FormulaInfo(
            "shared_follower", None, si=si,
            unsupported_reason=None,
        )
    if ftype == "array":
        return FormulaInfo(
            "array", text, si=si, master_ref=ref,
            unsupported_reason="数组公式（t=\"array\"）不在支持范围内",
        )
    if ftype == "dataTable":
        return FormulaInfo(
            "data_table", text,
            unsupported_reason="模拟运算表公式（t=\"dataTable\"）不在支持范围内",
        )
    if ftype != "normal":
        return FormulaInfo(
            "other", text,
            unsupported_reason=f"未知公式类型 t=\"{ftype}\" 不在支持范围内",
        )
    if text is None or text == "":
        # 正常公式理论上必有文本；保守标记
        return FormulaInfo(
            "other", None,
            unsupported_reason="公式节点缺少公式文本",
        )
    return FormulaInfo("normal", text)


def _parse_static(
    c_el: etree._Element, ctype: str | None, shared_strings: list[str]
) -> StaticValue:
    # 内联字符串
    if ctype == "inlineStr":
        t = c_el.find("m:is/m:t", NSMAP)
        if t is None:
            parts = c_el.findall("m:is/m:r/m:t", NSMAP)
            return StaticValue("text", "".join(p.text or "" for p in parts))
        return StaticValue("text", t.text or "")
    v_el = c_el.find("m:v", NSMAP)
    if v_el is None or v_el.text is None:
        return StaticValue("blank")
    raw = v_el.text
    if ctype in (None, "n"):
        try:
            return StaticValue("number", float(raw))
        except ValueError:
            return StaticValue("error", raw, f"数字缓存无法解析: {raw!r}")
    if ctype == "b":
        return StaticValue("boolean", raw == "1")
    if ctype == "e":
        return StaticValue("error", raw, f"单元格本身存储错误值 {raw}")
    if ctype == "s":
        try:
            idx = int(raw)
            return StaticValue("text", shared_strings[idx])
        except (ValueError, IndexError):
            return StaticValue("error", raw, f"共享字符串索引无效: {raw}")
    if ctype == "str":
        # 公式缓存的字符串；无公式节点出现时按静态文本处理
        return StaticValue("text", raw)
    return StaticValue("error", raw, f"未知单元格类型 t=\"{ctype}\"")
