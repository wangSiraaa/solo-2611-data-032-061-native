"""verify：独立验证结果副本的文件语义。

两条相互独立的读取入口：
1. 直接 ZIP/XML（lxml）：核对非工作表部件字节一致、公式节点（含共享
   主格/跟随格结构）逐格一致、合并区域/样式/坐标一致，且每个公式格的
   新缓存与引擎结果逐格相等，旧缓存不再残留；
2. openpyxl 两种模式：data_only=True 的值与值视图一致；普通模式公式
   文本仍在（跟随格经 openpyxl 展开后应等于我们的位移展开结果）。

另核对源文件 sha256 在处理前后未变化（源文件只读约束）。
"""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass, field

from lxml import etree

from .ooxml import NS, NSMAP, WorkbookData, split_coord
from .values import BOOLEAN, EMPTY_STRING, ERROR, NUMBER, TEXT

VERIFY_OK = "PASS"
VERIFY_FAIL = "FAIL"


class VerificationError(AssertionError):
    pass


@dataclass
class VerifyReport:
    checks: list[dict] = field(default_factory=list)
    source_sha256_before: str = ""
    source_sha256_after: str = ""

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "result": VERIFY_OK if ok else VERIFY_FAIL, "detail": detail})
        if not ok:
            raise VerificationError(f"[{name}] {detail}")

    @property
    def passed(self) -> bool:
        return all(c["result"] == VERIFY_OK for c in self.checks)

    def to_json_data(self) -> dict:
        return {
            "source_sha256_before": self.source_sha256_before,
            "source_sha256_after": self.source_sha256_after,
            "all_passed": self.passed,
            "checks": self.checks,
        }


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_copy(
    source_path: str,
    dest_path: str,
    wb: WorkbookData,
    results: dict,
    value_view: dict,
    digest_before: str,
) -> VerifyReport:
    report = VerifyReport(
        source_sha256_before=digest_before,
        source_sha256_after=sha256_file(source_path),
    )
    report.add(
        "源文件摘要未变",
        report.source_sha256_before == report.source_sha256_after,
        f"{report.source_sha256_before} vs {report.source_sha256_after}",
    )
    _raw_zip_checks(source_path, dest_path, wb, results, report)
    _openpyxl_checks(dest_path, wb, results, value_view, report)
    return report


# ---------------- 入口一：原始 ZIP/XML ----------------
def _raw_zip_checks(source_path, dest_path, wb, results, report: VerifyReport) -> None:
    with zipfile.ZipFile(source_path) as zs, zipfile.ZipFile(dest_path) as zd:
        src_names = zs.namelist()
        dst_names = zd.namelist()
        report.add("部件清单一致", src_names == dst_names,
                   f"src={len(src_names)} dst={len(dst_names)}")

        sheet_paths = {wb.sheets[n].path: n for n in wb.sheet_order}
        for name in src_names:
            if name not in sheet_paths:
                same = zs.read(name) == zd.read(name)
                report.add(f"非工作表部件字节一致: {name}", same)

        for path, sheet_name in sheet_paths.items():
            src_root = etree.fromstring(zs.read(path))
            dst_root = etree.fromstring(zd.read(path))
            _worksheet_structure(src_root, dst_root, sheet_name, report)
            _formula_nodes_preserved(src_root, dst_root, sheet_name, report)
            _caches_match_engine(dst_root, sheet_name, wb, results, report)
            _no_stale_cache(zs.read(path), zd.read(path), sheet_name, report)


def _worksheet_structure(src_root, dst_root, sheet_name, report) -> None:
    def coords(root):
        return [c.get("r") for c in root.findall("m:sheetData/m:row/m:c", NSMAP)]

    report.add(f"[{sheet_name}] 单元格坐标序列一致",
               coords(src_root) == coords(dst_root))

    def styles(root):
        return {c.get("r"): c.get("s") for c in root.findall(".//m:c", NSMAP)}

    report.add(f"[{sheet_name}] 样式 s 属性逐格一致",
               styles(src_root) == styles(dst_root))

    def merges(root):
        return [m.get("ref") for m in root.findall("m:mergeCells/m:mergeCell", NSMAP)]

    report.add(f"[{sheet_name}] 合并区域一致",
               merges(src_root) == merges(dst_root),
               str(merges(dst_root)))

    def dim(root):
        el = root.find("m:dimension", NSMAP)
        return el.get("ref") if el is not None else None

    report.add(f"[{sheet_name}] dimension 一致", dim(src_root) == dim(dst_root))

    # 非公式单元格整体 XML 必须完全一致
    for sc, dc in zip(
        src_root.findall("m:sheetData/m:row/m:c", NSMAP),
        dst_root.findall("m:sheetData/m:row/m:c", NSMAP),
    ):
        if sc.find("m:f", NSMAP) is None:
            same = etree.tostring(sc) == etree.tostring(dc)
            report.add(f"[{sheet_name}] 静态格 {sc.get('r')} XML 未改动", same)


def _formula_nodes_preserved(src_root, dst_root, sheet_name, report) -> None:
    src_f = {
        c.get("r"): etree.tostring(c.find("m:f", NSMAP))
        for c in src_root.findall(".//m:c", NSMAP)
        if c.find("m:f", NSMAP) is not None
    }
    dst_f = {
        c.get("r"): etree.tostring(c.find("m:f", NSMAP))
        for c in dst_root.findall(".//m:c", NSMAP)
        if c.find("m:f", NSMAP) is not None
    }
    report.add(
        f"[{sheet_name}] 公式节点（含共享主格/跟随格）逐格保留",
        src_f == dst_f,
        f"公式格数 {len(dst_f)}",
    )


def _caches_match_engine(dst_root, sheet_name, wb, results, report) -> None:
    mismatches: list[str] = []
    for c_el in dst_root.findall(".//m:c", NSMAP):
        if c_el.find("m:f", NSMAP) is None:
            continue
        coord = c_el.get("r")
        col, row = split_coord(coord)
        result = results[(sheet_name, col, row)]
        v_el = c_el.find("m:v", NSMAP)
        t_attr = c_el.get("t")
        vtype = result.value.type
        if vtype == "blank":
            ok = v_el is None and t_attr is None
        elif vtype == NUMBER:
            ok = t_attr is None and v_el is not None and float(v_el.text) == float(result.value.value)
        elif vtype == BOOLEAN:
            ok = t_attr == "b" and v_el is not None and v_el.text == ("1" if result.value.value else "0")
        elif vtype in (TEXT, EMPTY_STRING):
            ok = t_attr == "str" and v_el is not None and v_el.text == result.value.value
        elif vtype == ERROR:
            ok = t_attr == "e" and v_el is not None and v_el.text == result.value.value
        else:
            ok = False
        if not ok:
            mismatches.append(f"{coord}: t={t_attr} v={v_el.text if v_el is not None else None}")
    report.add(f"[{sheet_name}] 公式格缓存与引擎结果逐格一致",
               not mismatches, "; ".join(mismatches[:10]))


def _no_stale_cache(src_bytes: bytes, dst_bytes: bytes, sheet_name: str, report) -> None:
    # 通用规则：源文件公式格的旧 <v> 不得原样残留（结果副本必须由本引擎重算）
    src_root = etree.fromstring(src_bytes)
    dst_root = etree.fromstring(dst_bytes)
    stale: list[str] = []
    for sc in src_root.findall(".//m:c", NSMAP):
        if sc.find("m:f", NSMAP) is None:
            continue
        sv = sc.find("m:v", NSMAP)
        if sv is None:
            continue
        coord = sc.get("r")
        dv = dst_root.xpath(
            ".//m:c[@r=$coord]/m:v", coord=coord, namespaces={"m": NS}
        )
        if dv and dv[0].text == sv.text:
            stale.append(coord + "=" + sv.text)
    report.add(f"[{sheet_name}] 不存在原样残留的旧缓存",
               not stale, ", ".join(stale))


# ---------------- 入口二：openpyxl ----------------
def _openpyxl_checks(dest_path, wb, results, value_view, report) -> None:
    import openpyxl

    wb_data = openpyxl.load_workbook(dest_path, data_only=True)
    wb_formula = openpyxl.load_workbook(dest_path, data_only=False)
    report.add("openpyxl 工作表顺序一致",
        wb_data.sheetnames == wb.sheet_order,
        str(wb_data.sheetnames),
    )
    value_index = {
        (sv["sheet"], coord): cell
        for sv in value_view["sheets"]
        for coord, cell in sv["cells"].items()
    }
    # 防呆：值视图覆盖的格数应等于工作簿记录格数
    assert len(value_index) == sum(
        len(wb.sheets[sv["sheet"]].cells) for sv in value_view["sheets"]
    ), "值视图存在跨表坐标覆盖"
    mismatches: list[str] = []
    formula_missing: list[str] = []
    for sheet_name in wb.sheet_order:
        ws_d = wb_data[sheet_name]
        ws_f = wb_formula[sheet_name]
        for (col, row), record in wb.sheets[sheet_name].cells.items():
            coord = record.coord
            got = ws_d[coord].value
            expected = value_index[(sheet_name, coord)]
            if not _openpyxl_value_matches(got, expected):
                mismatches.append(f"{sheet_name}!{coord}: openpyxl={got!r} expected={expected}")
            if record.formula is not None:
                ftext = ws_f[coord].value
                if not (isinstance(ftext, str) and ftext.startswith("=")):
                    formula_missing.append(f"{sheet_name}!{coord}: {ftext!r}")
    report.add("openpyxl(data_only) 值视图逐格一致", not mismatches,
               "; ".join(mismatches[:10]))
    report.add("openpyxl 普通模式公式文本仍保留（未替换成常数）",
               not formula_missing, "; ".join(formula_missing[:10]))

    # 共享公式跟随格：openpyxl 展开的文本应等于本工具位移展开结果
    shift_mismatch: list[str] = []
    for sheet_name in wb.sheet_order:
        ws_f = wb_formula[sheet_name]
        for (col, row), record in wb.sheets[sheet_name].cells.items():
            f = record.formula
            if f is None or f.kind != "shared_follower":
                continue
            result = results[(sheet_name, col, row)]
            openpyxl_text = (ws_f[record.coord].value or "").lstrip("=")
            if openpyxl_text.replace(" ", "") != (result.expanded_formula or "").replace(" ", ""):
                shift_mismatch.append(
                    f"{sheet_name}!{record.coord}: {openpyxl_text} != {result.expanded_formula}"
                )
    report.add("共享跟随格位移展开与 openpyxl 展开一致",
               not shift_mismatch, "; ".join(shift_mismatch[:10]))


def _openpyxl_value_matches(got, expected: dict) -> bool:
    etype = expected["type"]
    if etype == "blank":
        return got is None
    if etype == "empty_string":
        return got == ""
    if etype == "text":
        return got == expected["value"]
    if etype == "boolean":
        return bool(got) == bool(expected["value"]) and isinstance(got, (bool, int))
    if etype == "number":
        return isinstance(got, (int, float)) and not isinstance(got, bool) \
            and float(got) == float(expected["value"])
    if etype == "error":
        return got == expected["value"]
    return False
