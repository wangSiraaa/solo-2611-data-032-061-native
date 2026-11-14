#!/usr/bin/env python3
"""extract — 从文本型 PDF 提取固定三列表（日期 / 说明 / 金额），输出 CSV + 来源 JSONL。

设计原则
--------
* 只依赖文字块的几何坐标：先按行高容差把词聚成视觉行，行内按 x 排序，
  绝不把 PDF 内容流中文字对象的存储顺序当作阅读顺序（对写入顺序打乱免疫）。
* 不预置每页坐标：每页的列布局由该页重复出现的表头行
  （Date / Description / Amount）的 x 位置现场识别。
* 新明细只能由日期列中的合法 ISO 日期开启；没有日期列内容的行视为续行；
  日期列出现非日期内容、或续行垂直间距过大，都视为本页表格结束。
* 跨页续接要求下一页重复表头且列锚点漂移在容差内，且新页首个正文行为
  续行型；金额冲突等证据不足的情况输出待复核项，不无条件拼给上一行。
* 金额只取自金额列；说明列中的日期、数字原样保留，不参与金额解析。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

import pymupdf

# ---------------------------------------------------------------- 语义与容差
COLS = ("date", "description", "amount")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")        # 仅支持样例中的 ISO 日期
AMOUNT_RE = re.compile(r"^-?\d+\.\d{2}$")           # 仅支持两位小数金额（可带负号）

LINE_TOL_FACTOR = 0.35   # 行聚合：词心 y 差 ≤ 0.35 × 中位词高 → 同一视觉行
BOUNDARY_TOL = 3.0       # 列归属：词心距列边界 ≤ 3pt → 两列皆可行，转待复核
CONT_GAP_FACTOR = 2.5    # 同页续行：与上一正文行的行心距 ≤ 2.5 × 中位词高
ANCHOR_TOL = 8.0         # 跨页续接：重复表头三个列锚点的允许水平漂移


# ---------------------------------------------------------------- 几何基础
@dataclass
class Token:
    page: int  # 0 基
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class Line:
    tokens: list[Token]
    y: float = 0.0  # 行心 y（聚类时维护的均值）


def cluster_lines(tokens: list[Token], med_h: float) -> list[Line]:
    """按 y 坐标把词聚成视觉行（几何排序，不依赖内容流顺序）。"""
    tol = max(2.0, LINE_TOL_FACTOR * med_h)
    lines: list[Line] = []
    for t in sorted(tokens, key=lambda t: (t.yc, t.x0)):
        if lines and abs(t.yc - lines[-1].y) <= tol:
            lines[-1].tokens.append(t)
            lines[-1].y = sum(x.yc for x in lines[-1].tokens) / len(lines[-1].tokens)
        else:
            lines.append(Line(tokens=[t], y=t.yc))
    for ln in lines:
        ln.tokens.sort(key=lambda t: t.x0)
    return lines


@dataclass
class Layout:
    """由表头行识别的列布局：anchors = 各列表头词的左边缘 x。"""

    anchors: dict[str, float]

    def matches(self, other: "Layout") -> bool:
        return all(abs(self.anchors[c] - other.anchors[c]) <= ANCHOR_TOL for c in COLS)

    @property
    def bounds(self) -> tuple[float, float]:
        xs = [self.anchors[c] for c in COLS]
        return ((xs[0] + xs[1]) / 2.0, (xs[1] + xs[2]) / 2.0)

    def column_of(self, tok: Token):
        """按词心 x 归列。返回 (列名, None)；落入边界歧义区返回 (None, (左列, 右列))。"""
        c = tok.xc
        b0, b1 = self.bounds
        for i, b in enumerate((b0, b1)):
            if abs(c - b) <= BOUNDARY_TOL:
                return None, (COLS[i], COLS[i + 1])
        if c < b0:
            return "date", None
        if c < b1:
            return "description", None
        return "amount", None


def match_header(line: Line) -> Layout | None:
    """一行同时含 Date/Description/Amount 三个词（大小写不敏感）→ 表头。"""
    anchors: dict[str, float] = {}
    for t in line.tokens:
        key = t.text.strip().casefold()
        if key in COLS and key not in anchors:
            anchors[key] = t.x0
    if all(c in anchors for c in COLS):
        return Layout(anchors)
    return None


def is_iso_date(s: str) -> bool:
    if not DATE_RE.match(s):
        return False
    try:
        _date.fromisoformat(s)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------- 数据模型
@dataclass
class Fragment:
    """一个单元格的一段原文：页码（1 基）、原文、该页内的 bbox。跨页单元格由多个片段组成。"""

    page: int
    text: str
    bbox: tuple[float, float, float, float]

    def as_json(self) -> dict:
        return {"page": self.page, "text": self.text, "bbox": [round(v, 2) for v in self.bbox]}


@dataclass
class Cell:
    frags: list[Fragment] = field(default_factory=list)
    status: str = "absent"  # ok | absent（原文缺失） | unparsed（有原文但解析失败）
    value: str = ""         # 写入 CSV 的值；absent / unparsed 时为空

    @property
    def text(self) -> str:
        return " ".join(f.text for f in self.frags)


@dataclass
class Row:
    row_id: int
    date: Cell
    desc: Cell
    amount: Cell


def frag_of(tok: Token) -> Fragment:
    return Fragment(page=tok.page + 1, text=tok.text, bbox=(tok.x0, tok.y0, tok.x1, tok.y1))


# ---------------------------------------------------------------- 提取器
class Extractor:
    def __init__(self) -> None:
        self.rows: list[Row] = []
        self.records: list[dict] = []   # JSONL 记录流（row 与 review 按产生顺序交错）
        self._next_id = 1
        self._last_row_id: int | None = None
        # 跨页状态
        self.open_row: Row | None = None
        self.layout: Layout | None = None      # 当前（或最近）表格的列布局
        self.page_active = False               # 本页已见表头、表格在本页有效
        self.header_seen = False               # 本页是否见过表头
        self.cross_pending = False             # 下一个正文行按跨页续接候选处理
        self.carried: Row | None = None        # 进入本页时等待续接证据的行
        self.prev_y: float | None = None       # 上一正文行的行心 y
        self._orphan_lines: list[Line] = []    # 本页表格区外、疑似被遗弃的续行

    # ---- 待复核项 ----
    def add_review(self, issue, message, row_id=None, fragments=None, candidates=None):
        rec = {"type": "review", "issue": issue, "row_id": row_id, "message": message}
        if fragments:
            rec["fragments"] = [f.as_json() for f in fragments]
        if candidates:
            rec["candidates"] = candidates
        self.records.append(rec)

    # ---- 页级状态机 ----
    def begin_page(self):
        self.page_active = False
        self.header_seen = False
        self.cross_pending = False
        self.prev_y = None
        self.carried = self.open_row
        self._orphan_lines = []

    def end_page(self):
        # 本页没有重复表头 → 携带行失去续接证据，封闭（金额缺失会在封闭时诊断）；
        # 同时把本页疑似被遗弃的续行留痕，候选归属指向被封闭的行。
        # 若本页见过表头，则表格正常运作过，表后说明静默忽略，不留痕。
        if self.carried is not None and self.open_row is self.carried and not self.header_seen:
            self.close_row("下一页未重复表头，跨页续接证据不足")
            for line in self._orphan_lines:
                self.flag_orphan_line(line)
        self._orphan_lines = []

    def adopt_header(self, layout: Layout):
        if self.open_row is not None and self.layout is not None and not layout.matches(self.layout):
            self.close_row("新页表头列锚点漂移超过容差，放弃跨页续接")
        self.layout = layout
        self.page_active = True
        self.header_seen = True
        self.cross_pending = self.open_row is not None
        self.prev_y = None

    def flag_orphan_line(self, line: Line):
        """无重复表头页面上的续行型内容：留痕为待复核，候选归属为被封闭的携带行。"""
        assert self.layout is not None
        cols: dict[str, list[Token]] = {c: [] for c in COLS}
        for tok in line.tokens:
            col, amb = self.layout.column_of(tok)
            if col is not None:
                cols[col].append(tok)
        if not cols["date"] and (cols["description"] or cols["amount"]):
            toks = cols["description"] + cols["amount"]
            self.add_review(
                "orphan_continuation",
                "本页未重复表头，疑似续行内容无法确认归属，未拼给任何明细",
                fragments=[frag_of(t) for t in toks],
                candidates=[self.carried.row_id] if self.carried else [],
            )

    # ---- 行处理 ----
    def handle_body_line(self, line: Line, med_h: float):
        assert self.layout is not None
        cols: dict[str, list[Token]] = {c: [] for c in COLS}
        for tok in line.tokens:
            col, amb = self.layout.column_of(tok)
            if amb is not None:
                self.add_review(
                    "ambiguous_column",
                    f"词 {tok.text!r} 的横向中心距 {amb[0]}/{amb[1]} 列边界不足 "
                    f"{BOUNDARY_TOL}pt，两个列归属皆可行，未静默归列",
                    row_id=self.open_row.row_id if self.open_row else None,
                    fragments=[frag_of(tok)],
                    candidates=[amb[0], amb[1]],
                )
                continue
            cols[col].append(tok)

        date_toks = cols["date"]
        if date_toks:
            if len(date_toks) == 1 and is_iso_date(date_toks[0].text):
                self.start_row(date_toks[0], cols["description"], cols["amount"])
                self.prev_y = line.y
            else:
                self.end_table(line, cols)
            return

        if cols["description"] or cols["amount"]:
            self.handle_continuation(line, cols["description"], cols["amount"], med_h)
        # 全部词都落入歧义区的行：已逐词给出待复核，不再参与行聚合

    def start_row(self, date_tok: Token, desc_toks: list[Token], amount_toks: list[Token]):
        self.close_row("新明细开始")
        row = Row(
            row_id=self._next_id,
            date=Cell([frag_of(date_tok)]),
            desc=Cell([frag_of(t) for t in desc_toks]),
            amount=Cell([frag_of(t) for t in amount_toks]),
        )
        self._next_id += 1
        self.open_row = row

    def end_table(self, line: Line, cols: dict[str, list[Token]]):
        """日期列出现非日期内容 → 本页表格结束（页脚、'Table ends'、表后说明同此路径）。

        注意：不封闭携带行——它可能在下页重复表头后续接。
        """
        dateish = any(DATE_RE.match(t.text) for t in cols["date"])
        if cols["description"] or cols["amount"] or dateish:
            frags = [frag_of(t) for t in line.tokens]
            self.add_review(
                "row_not_recognized",
                "表格内一行的日期列不是合法 ISO 日期，且行内含其他列内容；"
                "未作为明细提取，表格在此结束",
                row_id=self.open_row.row_id if self.open_row else None,
                fragments=frags,
            )
        self.page_active = False
        self.cross_pending = False

    def handle_continuation(self, line: Line, desc_toks: list[Token], amount_toks: list[Token], med_h: float):
        if self.open_row is None:
            self.add_review(
                "orphan_continuation",
                "续行型内容（日期列为空）出现时没有可归属的未完结明细，未静默拼接",
                fragments=[frag_of(t) for t in desc_toks + amount_toks],
                candidates=[self._last_row_id] if self._last_row_id else [],
            )
            return
        if self.cross_pending:
            # 跨页续接：上一页未完结行 + 本页重复表头（锚点已校验）+ 首个正文行为续行型。
            self.cross_pending = False
            self.merge_continuation(desc_toks, amount_toks, cross_page=True)
            self.prev_y = line.y
            return
        gap = line.y - self.prev_y if self.prev_y is not None else float("inf")
        if gap <= CONT_GAP_FACTOR * med_h:
            self.merge_continuation(desc_toks, amount_toks, cross_page=False)
            self.prev_y = line.y
        else:
            self.add_review(
                "continuation_rejected",
                f"续行型内容与上一正文行的行心距 {gap:.1f}pt 超过容差 "
                f"{CONT_GAP_FACTOR}×中位词高，不作为续行；表格在此结束",
                row_id=self.open_row.row_id,
                fragments=[frag_of(t) for t in desc_toks + amount_toks],
            )
            self.page_active = False
            self.cross_pending = False

    def merge_continuation(self, desc_toks: list[Token], amount_toks: list[Token], cross_page: bool):
        row = self.open_row
        assert row is not None
        where = "跨页续行" if cross_page else "续行"
        if amount_toks and row.amount.frags:
            self.add_review(
                "amount_conflict",
                f"{where}带有金额，但上一条明细已有金额 {row.amount.text!r}；"
                "证据不足，未合并，候选归属见 fragments",
                row_id=row.row_id,
                fragments=[frag_of(t) for t in desc_toks + amount_toks],
            )
            return
        row.desc.frags.extend(frag_of(t) for t in desc_toks)
        row.amount.frags.extend(frag_of(t) for t in amount_toks)

    # ---- 行封闭与诊断 ----
    def close_row(self, context: str):
        row = self.open_row
        self.open_row = None
        if row is None:
            return
        pending: list[dict] = []
        # 金额：区分“原文缺失”与“有原文但解析失败”，都不静默猜值。
        if not row.amount.frags:
            row.amount.status = "absent"
            pending.append(
                {"type": "review", "issue": "amount_missing", "row_id": row.row_id,
                 "message": f"第 {row.row_id} 行（日期 {row.date.text}）金额列无原文"
                            f"（{context}）；CSV 中金额留空"}
            )
        else:
            raw = row.amount.text
            if AMOUNT_RE.match(raw):
                row.amount.status = "ok"
                row.amount.value = raw
            else:
                row.amount.status = "unparsed"
                pending.append(
                    {"type": "review", "issue": "amount_unparsed", "row_id": row.row_id,
                     "message": f"第 {row.row_id} 行金额原文 {raw!r} 不是两位小数金额"
                                f"（{context}）；未猜值，CSV 中金额留空",
                     "fragments": [f.as_json() for f in row.amount.frags]}
                )
        row.date.status = "ok"
        row.date.value = row.date.text
        row.desc.status = "ok" if row.desc.frags else "absent"
        row.desc.value = row.desc.text
        self.rows.append(row)
        self._last_row_id = row.row_id
        self.records.append(row_record(row))
        self.records.extend(pending)

    def finish(self):
        self.close_row("文档结束")


def row_record(row: Row) -> dict:
    return {
        "type": "row",
        "row_id": row.row_id,
        "date": row.date.value,
        "description": row.desc.value,
        "amount": row.amount.value,
        "cells": {name: cell_json(cell) for name, cell in
                  (("date", row.date), ("description", row.desc), ("amount", row.amount))},
    }


def cell_json(cell: Cell) -> dict:
    return {
        "status": cell.status,
        "text": cell.text,
        "fragments": [f.as_json() for f in cell.frags],
    }


# ---------------------------------------------------------------- 主流程
def extract(pdf_path: str | Path):
    """返回 (rows, records)：rows 为 Row 列表，records 为 JSONL 记录（row/review）。"""
    ex = Extractor()
    doc = pymupdf.open(pdf_path)
    for pno in range(doc.page_count):
        page = doc[pno]
        tokens = [
            Token(pno, w[0], w[1], w[2], w[3], w[4])
            for w in page.get_text("words")
        ]
        ex.begin_page()
        if tokens:
            med_h = statistics.median(t.height for t in tokens)
            for line in cluster_lines(tokens, med_h):
                hdr = match_header(line)
                if hdr is not None:
                    ex.adopt_header(hdr)
                    continue
                if not ex.page_active:
                    # 页眉/页脚/表后说明：不进入表格的内容先记下；
                    # 若本页最终没出现重复表头，end_page 会把疑似续行留痕。
                    if ex.carried is not None and ex.layout is not None:
                        ex._orphan_lines.append(line)
                    continue
                ex.handle_body_line(line, med_h)
        ex.end_page()
    ex.finish()
    return ex.rows, ex.records


def format_csv(rows: list[Row]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["row_id", "date", "description", "amount"])
    for r in rows:
        w.writerow([r.row_id, r.date.value, r.desc.value, r.amount.value])
    return buf.getvalue()


def format_jsonl(records: list[dict]) -> str:
    return "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in records)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="extract",
        description="从文本型 PDF 提取固定三列表（日期/说明/金额）为 CSV + 来源 JSONL",
    )
    p.add_argument("pdf", help="输入 PDF（文本型，非扫描件）")
    p.add_argument("--csv", help="CSV 输出路径（默认 <输入名>.csv）")
    p.add_argument("--jsonl", help="来源 JSONL 输出路径（默认 <输入名>.jsonl）")
    a = p.parse_args(argv)

    csv_path = a.csv or f"{Path(a.pdf).stem}.csv"
    jsonl_path = a.jsonl or f"{Path(a.pdf).stem}.jsonl"

    rows, records = extract(a.pdf)
    Path(csv_path).write_text(format_csv(rows), encoding="utf-8")
    Path(jsonl_path).write_text(format_jsonl(records), encoding="utf-8")

    reviews = [r for r in records if r["type"] == "review"]
    print(f"extract: {len(rows)} 行明细, {len(reviews)} 条待复核 -> {csv_path}, {jsonl_path}",
          file=sys.stderr)
    for r in reviews:
        print(f"  [{r['issue']}] {r['message']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
