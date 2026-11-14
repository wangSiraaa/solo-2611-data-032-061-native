"""extract.py 的验证测试：重复表头、换行、跨页续接、表外数字、写入顺序打乱、坐标回查、诊断。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "fixtures"))

import pymupdf  # noqa: E402

import extract as ex  # noqa: E402
import make_pdf  # noqa: E402  fixtures/ 下的输入生成材料，仅用于构造输入变体

FIXTURE = ROOT / "fixtures" / "report.pdf"


def build_pdf(path, pages):
    """与 fixtures/make_pdf.py 同款的最小 PDF 写出器，仅用于构造诊断测试输入。"""
    objects = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for items in pages:
        stream = "\n".join(f"BT /F1 11 Tf {x} {y} Td ({t}) Tj ET" for x, y, t in items).encode()
        content_id = len(objects) + 1
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
        page_id = len(objects) + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        kids.append(page_id)
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = ("<< /Type /Pages /Count " + str(len(kids)) + " /Kids ["
                  + " ".join(f"{k} 0 R" for k in kids) + "] >>").encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for o in offsets[1:]:
        out.extend(f"{o:010} 00000 n \n".encode())
    out.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    Path(path).write_bytes(out)


HEADER = [(40, 800, "Synthetic expense report"),
          (40, 760, "Date"), (155, 760, "Description"), (455, 760, "Amount")]


def page_with(*body):
    return HEADER + list(body) + [(40, 45, "Page 1")]


class ExtractCase(unittest.TestCase):
    def extract_tmp(self, pages):
        with tempfile.TemporaryDirectory() as td:
            pdf = Path(td) / "t.pdf"
            build_pdf(pdf, pages)
            return ex.extract(pdf)

    @staticmethod
    def row_recs(records):
        return [r for r in records if r["type"] == "row"]

    @staticmethod
    def reviews(records, issue=None):
        rs = [r for r in records if r["type"] == "review"]
        return [r for r in rs if r["issue"] == issue] if issue else rs


class TestFixture(ExtractCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.records = ex.extract(FIXTURE)

    def test_rows_and_values(self):
        got = [(r["date"], r["description"], r["amount"]) for r in self.row_recs(self.records)]
        self.assertEqual(got, [
            ("2026-01-01", "Office supplies batch 12", "120.50"),
            ("2026-01-02", "Field visit on 2026-01-01 includes route 42 materials", "-15.00"),
            ("2026-01-03", "Equipment maintenance and replacement components", "210.25"),
            ("2026-01-04", "Travel claim 2026-01-03 two receipts 18 and 19", "55.00"),
        ])

    def test_repeated_header_not_extracted(self):
        # 两页各有一套 Date/Description/Amount 表头，均不得成为数据行
        self.assertEqual(len(self.row_recs(self.records)), 4)
        blob = json.dumps(self.records, ensure_ascii=False)
        for header_word in ('"Date"', '"Description"', '"Amount"'):
            self.assertNotIn(header_word, blob)

    def test_multiline_description(self):
        rec = self.row_recs(self.records)[1]
        frags = rec["cells"]["description"]["fragments"]
        ys = {f["bbox"][1] for f in frags}
        self.assertGreater(len(ys), 1)  # 说明来自两个视觉行
        self.assertIn("includes route 42 materials", rec["description"])

    def test_cross_page_continuation(self):
        rec = self.row_recs(self.records)[2]
        desc = rec["cells"]["description"]
        pages = [f["page"] for f in desc["fragments"]]
        self.assertEqual(sorted(set(pages)), [1, 2])          # 跨页说明保留各页片段
        self.assertEqual(len({(f["page"], tuple(f["bbox"])) for f in desc["fragments"]}),
                         len(desc["fragments"]))              # 每段各自 bbox，非跨页大矩形
        amount = rec["cells"]["amount"]
        self.assertEqual([f["page"] for f in amount["fragments"]], [2])  # 金额来自第 2 页
        self.assertEqual(rec["amount"], "210.25")

    def test_out_of_table_numbers_ignored(self):
        blob = json.dumps(self.records, ensure_ascii=False)
        for s in ("999.99", "2026-01-05", "Table ends", "Synthetic expense report"):
            self.assertNotIn(s, blob)
        self.assertEqual(self.reviews(self.records), [])      # 样例应干净提取，无待复核

    def test_page_furniture_ignored(self):
        for rec in self.row_recs(self.records):
            for cell in rec["cells"].values():
                self.assertNotIn("Page", cell["text"])

    def test_shuffle_invariance(self):
        # 文字对象写入顺序打乱、几何位置不变 → 结果逐字节一致
        with tempfile.TemporaryDirectory() as td:
            shuffled = Path(td) / "shuffled.pdf"
            make_pdf.build(shuffled, shuffled=True)
            rows_a, recs_a = ex.extract(FIXTURE)
            rows_b, recs_b = ex.extract(shuffled)
            self.assertEqual(ex.format_csv(rows_a), ex.format_csv(rows_b))
            self.assertEqual(ex.format_jsonl(recs_a), ex.format_jsonl(recs_b))

    def test_coordinate_spotcheck(self):
        # 抽样若干单元格，拿 JSONL 中的 bbox 回到原页核对文字
        doc = pymupdf.open(FIXTURE)
        by_id = {r["row_id"]: r for r in self.row_recs(self.records)}
        samples = [(1, "date"), (1, "amount"), (3, "date"), (3, "description"),
                   (3, "amount"), (4, "description")]
        for rid, col in samples:
            cell = by_id[rid]["cells"][col]
            self.assertTrue(cell["fragments"], f"{rid}/{col} 无来源片段")
            for frag in cell["fragments"]:
                page = doc[frag["page"] - 1]
                clip = pymupdf.Rect(frag["bbox"])
                self.assertTrue(page.rect.contains(clip), f"bbox 越出页面: {frag}")
                words = page.get_text("words", clip=clip)
                got = "".join(w[4] for w in words)
                self.assertEqual(got, frag["text"].replace(" ", ""),
                                 f"row {rid} {col} 坐标回查不符: {frag}")


class TestDiagnostics(ExtractCase):
    def test_amount_missing(self):
        rows, records = self.extract_tmp([page_with(
            (40, 730, "2026-02-01"), (155, 730, "No amount here"),
            (40, 700, "Table ends"),
        )])
        rec = self.row_recs(records)[0]
        self.assertEqual(rec["amount"], "")
        self.assertEqual(rec["cells"]["amount"]["status"], "absent")   # 原文缺失
        self.assertEqual(len(self.reviews(records, "amount_missing")), 1)

    def test_amount_unparsed(self):
        rows, records = self.extract_tmp([page_with(
            (40, 730, "2026-02-01"), (155, 730, "Bad amount"), (455, 730, "12.5"),
        )])
        rec = self.row_recs(records)[0]
        self.assertEqual(rec["amount"], "")                            # 不静默猜值
        self.assertEqual(rec["cells"]["amount"]["status"], "unparsed")  # 提取失败≠原文缺失
        self.assertEqual(rec["cells"]["amount"]["text"], "12.5")       # 原文保留待查
        self.assertEqual(len(self.reviews(records, "amount_unparsed")), 1)

    def test_amount_sign_anomaly(self):
        rows, records = self.extract_tmp([page_with(
            (40, 730, "2026-02-01"), (155, 730, "Odd sign"), (455, 730, "15.00-"),
        )])
        self.assertEqual(self.row_recs(records)[0]["amount"], "")
        self.assertEqual(len(self.reviews(records, "amount_unparsed")), 1)

    def test_cross_page_amount_conflict(self):
        p1 = page_with((40, 730, "2026-02-01"), (155, 730, "Alpha"), (455, 730, "10.00"))
        p2 = HEADER + [(155, 730, "orphan tail"), (455, 730, "99.99"), (40, 45, "Page 2")]
        rows, records = self.extract_tmp([p1, p2])
        rec = self.row_recs(records)[0]
        self.assertEqual(rec["amount"], "10.00")          # 不覆盖、不合并
        self.assertEqual(rec["description"], "Alpha")
        self.assertNotIn("99.99", json.dumps(self.row_recs(records)))
        self.assertEqual(len(self.reviews(records, "amount_conflict")), 1)

    def test_orphan_continuation(self):
        rows, records = self.extract_tmp([page_with(
            (155, 730, "lonely tail"), (455, 730, "12.00"),          # 无归属的续行型内容
            (40, 690, "2026-02-02"), (155, 690, "Real row"), (455, 690, "1.00"),
        )])
        recs = self.row_recs(records)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["date"], "2026-02-02")
        self.assertEqual(len(self.reviews(records, "orphan_continuation")), 1)

    def test_carried_row_closed_when_header_not_repeated(self):
        p1 = page_with((40, 730, "2026-02-01"), (155, 730, "Unfinished row"))  # 缺金额，页末未完结
        p2 = [(40, 800, "Something else entirely"), (155, 700, "stray tail"), (455, 700, "9.99")]
        rows, records = self.extract_tmp([p1, p2])
        rec = self.row_recs(records)[0]
        self.assertEqual(rec["amount"], "")
        self.assertEqual(len(self.reviews(records, "amount_missing")), 1)
        # 第二页无重复表头：疑似续行留痕为待复核，候选归属指向被封闭的行
        orphans = self.reviews(records, "orphan_continuation")
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["candidates"], [rec["row_id"]])
        self.assertNotIn("stray tail", json.dumps(self.row_recs(records)))

    def test_description_keeps_numbers_and_dates(self):
        rows, records = self.extract_tmp([page_with(
            (40, 730, "2026-02-01"), (155, 730, "cost 99.99 on 2026-02-03 total"),
            (455, 730, "10.00"),
        )])
        rec = self.row_recs(records)[0]
        self.assertEqual(rec["description"], "cost 99.99 on 2026-02-03 total")  # 不因金额解析截断
        self.assertEqual(rec["amount"], "10.00")

    def test_invalid_date_does_not_start_row(self):
        rows, records = self.extract_tmp([page_with(
            (40, 730, "2026-13-99"), (155, 730, "not a row"), (455, 730, "5.00"),
        )])
        self.assertEqual(self.row_recs(records), [])
        self.assertEqual(len(self.reviews(records, "row_not_recognized")), 1)


if __name__ == "__main__":
    unittest.main()
