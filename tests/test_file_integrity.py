"""文件语义完整性测试。

- 独立 ZIP/XML 读取：公式节点（含共享主格/跟随格）原样、合并区域、
  数字格式 s、工作表顺序保留；只有公式格的 <v>/<t> 被更新；
- 旧缓存（9999）不残留，错误格写 t="e"，文本结果写 t="str"；
- openpyxl 第二入口：data_only 值与值视图一致，普通模式公式仍在；
- 源文件 sha256 处理前后不变；
- 公式绝不被替换成常数（<f> 节点存在且文本未改）。
"""

from __future__ import annotations

import json
import zipfile

import pytest
from lxml import etree
from openpyxl import Workbook

from xlcalc.cli import cmd_calculate
from xlcalc.engine import Engine
from xlcalc.ooxml import NSMAP, read_workbook
from xlcalc.verify import sha256_file, verify_copy
from xlcalc.views import build_value_view
from xlcalc.writer import write_result_copy

from .builders import make_shared_formula, save_wb


@pytest.fixture()
def integrity_wb(tmp_path):
    src = str(tmp_path / "src.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "Detail"
    ws["A1"] = "数量"
    ws["B1"] = "单价"
    ws["A2"] = 3
    ws["B2"] = 4
    ws["A3"] = 5
    ws["B3"] = 6
    for coord in ("C2", "C3"):
        ws[coord] = None
    ws["D2"] = "=SUM(C2:C3)"
    ws.merge_cells("A1:C1")  # 合并区域
    out = wb.create_sheet("Out")
    out["A1"] = '=IFERROR(1/0,"错")'
    out["A2"] = "=Detail!C2"
    save_wb(wb, src)
    make_shared_formula(src, 1, "C2", ["C3"], "A2*B2", "C2:C3")
    digest = sha256_file(src)
    return src, digest, tmp_path


def _run_calculation(src, tmp_path):
    outdir = str(tmp_path / "out")
    wb = read_workbook(src)
    results = Engine(wb).evaluate_all()
    dest = str(tmp_path / "out" / "src.calculated.xlsx")
    import os

    os.makedirs(outdir, exist_ok=True)
    stats = write_result_copy(src, dest, wb, results)
    value_view = build_value_view(wb, results)
    report = verify_copy(src, dest, wb, results, value_view, sha256_file(src))
    assert report.passed, [c for c in report.checks if c["result"] != "PASS"]
    return dest, wb, results, value_view, stats


def test_source_digest_unchanged_and_parts_preserved(integrity_wb):
    src, digest, tmp_path = integrity_wb
    dest, *_ = _run_calculation(src, tmp_path)
    assert sha256_file(src) == digest  # 源文件只读
    with zipfile.ZipFile(src) as zs, zipfile.ZipFile(dest) as zd:
        assert zs.namelist() == zd.namelist()
        # 非 worksheet 部件字节完全一致
        for name in zs.namelist():
            if "worksheets/sheet" not in name:
                assert zs.read(name) == zd.read(name), name


def test_formula_nodes_and_shared_structure_preserved(integrity_wb):
    src, _, tmp_path = integrity_wb
    dest, *_ = _run_calculation(src, tmp_path)
    with zipfile.ZipFile(src) as zs, zipfile.ZipFile(dest) as zd:
        for path in ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"):
            s_root = etree.fromstring(zs.read(path))
            d_root = etree.fromstring(zd.read(path))
            sf = {c.get("r"): etree.tostring(c.find("m:f", NSMAP))
                  for c in s_root.xpath(".//m:c[m:f]", namespaces=NSMAP)}
            df = {c.get("r"): etree.tostring(c.find("m:f", NSMAP))
                  for c in d_root.xpath(".//m:c[m:f]", namespaces=NSMAP)}
            assert sf == df  # 共享主格文本、跟随格空 f 节点全部原样


def test_merged_cells_and_styles_preserved(integrity_wb):
    src, _, tmp_path = integrity_wb
    dest, *_ = _run_calculation(src, tmp_path)
    with zipfile.ZipFile(dest) as zd:
        root = etree.fromstring(zd.read("xl/worksheets/sheet1.xml"))
        merges = [m.get("ref") for m in root.xpath(".//m:mergeCell", namespaces=NSMAP)]
        assert merges == ["A1:C1"]
        # 样式 s 属性逐格对比源
        with zipfile.ZipFile(src) as zs:
            sroot = etree.fromstring(zs.read("xl/worksheets/sheet1.xml"))
            s_styles = {c.get("r"): c.get("s") for c in sroot.xpath(".//m:c", namespaces=NSMAP)}
            d_styles = {c.get("r"): c.get("s") for c in root.xpath(".//m:c", namespaces=NSMAP)}
            assert s_styles == d_styles


def test_caches_rewritten_with_correct_types_and_no_stale(integrity_wb):
    src, _, tmp_path = integrity_wb
    dest, wb, results, *_ = _run_calculation(src, tmp_path)
    with zipfile.ZipFile(dest) as zd:
        root = etree.fromstring(zd.read("xl/worksheets/sheet1.xml"))
        for c in root.xpath(".//m:c[m:f]", namespaces=NSMAP):
            v = c.find("m:v", NSMAP)
            assert v is not None and v.text != "9999"  # 旧缓存被替换
        # C2 = 3*4 = 12，数字格无 t 属性
        c2 = root.xpath(".//m:c[@r='C2']", namespaces=NSMAP)[0]
        assert c2.get("t") is None and c2.find("m:v", NSMAP).text == "12"
        # sheet2 A1 是 IFERROR 文本回退 => t="str"
        s2 = etree.fromstring(zd.read("xl/worksheets/sheet2.xml"))
        a1 = s2.xpath(".//m:c[@r='A1']", namespaces=NSMAP)[0]
        assert a1.get("t") == "str"
        assert a1.find("m:v", NSMAP).text == "错"


def test_openpyxl_second_reader_matches_value_view(integrity_wb):
    src, _, tmp_path = integrity_wb
    dest, wb, results, value_view, _ = _run_calculation(src, tmp_path)
    import openpyxl

    wb_data = openpyxl.load_workbook(dest, data_only=True)
    wb_formula = openpyxl.load_workbook(dest, data_only=False)
    # 值视图逐格一致
    index = {(sv["sheet"], coord): cell
             for sv in value_view["sheets"] for coord, cell in sv["cells"].items()}
    for sheet_name in wb.sheet_order:
        ws = wb_data[sheet_name]
        for (col, row), record in wb.sheets[sheet_name].cells.items():
            got = ws[record.coord].value
            expected = index[(sheet_name, record.coord)]
            if expected["type"] == "number":
                assert float(got) == float(expected["value"])
            elif expected["type"] == "blank":
                assert got is None
            else:
                assert got == expected["value"]
    # 普通模式公式仍是字符串公式，不是常数
    assert wb_formula["Detail"]["C2"].value.startswith("=")
    assert wb_formula["Out"]["A1"].value.startswith("=IFERROR")


def test_cli_calculate_writes_three_artifacts_and_verify(integrity_wb, monkeypatch):
    src, _, tmp_path = integrity_wb
    outdir = str(tmp_path / "cli_out")
    rc = cmd_calculate(type(
        "Args", (),
        {"source": src, "outdir": outdir, "copy_out": None,
         "values_out": None, "explain_out": None, "no_verify": False},
    )())
    assert rc == 0
    import os

    names = os.listdir(outdir)
    assert any(n.endswith(".calculated.xlsx") for n in names)
    assert any(n.endswith(".values.json") for n in names)
    assert any(n.endswith(".explain.json") for n in names)
    assert any(n.endswith(".verify.json") for n in names)
    # verify 报告全部 PASS
    verify_json = [n for n in names if n.endswith(".verify.json")][0]
    report = json.load(open(os.path.join(outdir, verify_json)))
    assert report["all_passed"] is True


def test_verify_command_return_codes(integrity_wb, tmp_path, monkeypatch):
    import os
    from xlcalc.cli import build_parser

    src, _, _ = integrity_wb
    dest, _, _, value_view, _ = _run_calculation(src, tmp_path)
    values_path = str(tmp_path / "v.json")
    with open(values_path, "w", encoding="utf-8") as fh:
        json.dump(value_view, fh, ensure_ascii=False)

    good = build_parser().parse_args(["verify", src, dest, values_path])
    assert good.func(good) == 0  # 合格副本返回 0

    # 篡改缓存后必须返回 1
    import zipfile
    from lxml import etree
    from xlcalc.ooxml import NS
    bad = str(tmp_path / "bad.xlsx")
    tmp = bad + ".t"
    with zipfile.ZipFile(dest) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith("sheet1.xml"):
                root = etree.fromstring(data)
                v = root.xpath(".//m:v", namespaces={"m": NS})
                if v:
                    v[0].text = "9999"
                    data = etree.tostring(root)
            zout.writestr(item, data)
    os.replace(tmp, bad)
    bad_args = build_parser().parse_args(["verify", src, bad, values_path])
    assert bad_args.func(bad_args) == 1
