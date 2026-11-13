"""值视图与单元格解释（explain）的 JSON 构建。

- 值视图：每个有记录的单元格给出六类值模型之一，静态文本（哪怕以
  等号开头）保持 text，不执行。
- explain：公式格列出原始公式、共享跟随格的展开公式、实际依赖、
  计算值或失败来源（错误码 + 原因）；静态格给出值与类型。
"""

from __future__ import annotations

from .engine import CellResult, Engine
from .ooxml import WorkbookData


def build_value_view(wb: WorkbookData, results: dict) -> dict:
    sheets_out = []
    for sheet_name in wb.sheet_order:
        sheet = wb.sheets[sheet_name]
        cells = {}
        for (col, row), record in sorted(sheet.cells.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            result = results[(sheet_name, col, row)]
            cells[record.coord] = result.value.to_json_data()
        sheets_out.append({"sheet": sheet_name, "cells": cells})
    return {"workbook": wb.path, "sheets": sheets_out}


def build_explain_view(wb: WorkbookData, results: dict) -> dict:
    sheets_out = []
    for sheet_name in wb.sheet_order:
        sheet = wb.sheets[sheet_name]
        cells = {}
        for (col, row), record in sorted(sheet.cells.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            result: CellResult = results[(sheet_name, col, row)]
            if result.is_formula:
                entry = {
                    "kind": "formula",
                    "coord": f"{sheet_name}!{record.coord}",
                    "formula": result.formula_text,
                    "expanded_formula": result.expanded_formula,
                    "shared_follower": record.formula.kind == "shared_follower"
                    if record.formula else False,
                    "dependencies": result.dependencies,
                    "value": result.value.to_json_data(),
                    "status": result.status,
                }
                if result.reason is not None:
                    entry["failure"] = {
                        "code": result.value.value if result.value.is_error else None,
                        "reason": result.reason,
                    }
            else:
                entry = {
                    "kind": "static",
                    "coord": f"{sheet_name}!{record.coord}",
                    "value": result.value.to_json_data(),
                    "status": result.status,
                }
                if record.static.kind == "text" and isinstance(record.static.value, str) \
                        and record.static.value.startswith("="):
                    entry["note"] = "以等号开头的普通文本，按文本保存，不作为公式执行"
                if result.reason is not None:
                    entry["failure"] = {
                        "code": result.value.value if result.value.is_error else None,
                        "reason": result.reason,
                    }
            cells[record.coord] = entry
        sheets_out.append({"sheet": sheet_name, "cells": cells})
    unsupported_notes = _workbook_level_notes(wb)
    return {
        "workbook": wb.path,
        "unsupported_workbook_features": unsupported_notes,
        "sheets": sheets_out,
    }


def _workbook_level_notes(wb: WorkbookData) -> list[dict]:
    notes: list[dict] = []
    if wb.has_external_links:
        notes.append({
            "feature": "external_link",
            "reason": "工作簿包含外部链接部件，本工具不跨工作簿取值",
            "parts": wb.external_link_parts,
        })
    if wb.defined_names:
        notes.append({
            "feature": "defined_name",
            "reason": "工作簿包含命名区域，本工具不解析命名区域",
            "names": wb.defined_names,
        })
    return notes


def explain_one(engine: Engine, wb: WorkbookData, sheet_name: str, coord: str) -> dict:
    from .ooxml import split_coord

    col, row = split_coord(coord)
    result = engine.evaluate_cell(sheet_name, col, row)
    view = build_explain_view(wb, engine.results())
    for sheet_view in view["sheets"]:
        if sheet_view["sheet"] == sheet_name and coord in sheet_view["cells"]:
            return sheet_view["cells"][coord]
    # 空白格（工作簿中不存在的格）也允许解释
    return {
        "kind": "blank",
        "coord": f"{sheet_name}!{coord}",
        "value": result.value.to_json_data(),
        "status": "ok",
        "note": "该格在工作表中不存在，按空白处理",
    }
