"""导出分析输入、诊断与可复算中间片段（JSONL/JSON，便于离线复算）。"""
from __future__ import annotations

import json
import os
import sqlite3


def _dump_rows(conn: sqlite3.Connection, sql: str, path: str, params=()) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for r in conn.execute(sql, params):
            fh.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
            n += 1
    return n


def export_bundle(conn: sqlite3.Connection, out_dir: str,
                  trace_id: str | None = None) -> int:
    os.makedirs(out_dir, exist_ok=True)
    where = "WHERE trace_id=?" if trace_id else ""
    params = (trace_id,) if trace_id else ()
    files = {
        "timeline_slices.jsonl":
            f"SELECT * FROM timeline_slices {where} ORDER BY trace_id, slice_no",
        "await_causes.jsonl":
            f"SELECT * FROM await_causes {where} ORDER BY trace_id",
        "resource_ledger.jsonl":
            f"SELECT * FROM resource_ledger {where} ORDER BY trace_id, service",
        "diagnostics.jsonl":
            f"SELECT * FROM diagnostics {where} ORDER BY trace_id, diag_code",
        "rpc_pairs.jsonl":
            f"SELECT * FROM rpc_pairs {where} ORDER BY trace_id",
        "analysis_trace.jsonl":
            f"SELECT * FROM analysis_trace {where} ORDER BY trace_id",
        "validation_issues.jsonl":
            f"SELECT * FROM validation_issues {where} ORDER BY trace_id",
    }
    n_files = 0
    for name, sql in files.items():
        _dump_rows(conn, sql, os.path.join(out_dir, name), params)
        n_files += 1
    # 原始输入（仅该 trace 时按 raw 行回查）
    raw_path = os.path.join(out_dir, "input_records.jsonl")
    n_raw = 0
    with open(raw_path, "w", encoding="utf-8") as fh:
        if trace_id:
            for tbl in ("spans", "events", "awaits"):
                for r in conn.execute(f"SELECT source_file, source_line FROM {tbl} "
                                      f"WHERE trace_id=?", (trace_id,)):
                    row = conn.execute(
                        "SELECT record_json FROM raw_records WHERE source_file=? AND source_line=?",
                        (r["source_file"], r["source_line"])).fetchone()
                    if row:
                        fh.write(row["record_json"] + "\n")
                        n_raw += 1
        else:
            for r in conn.execute("SELECT record_json FROM raw_records"):
                fh.write(r["record_json"] + "\n")
                n_raw += 1
    n_files += 1
    manifest = {"note": "可复算中间片段；时间单位纳秒；墙钟口径，非 CPU",
                "trace_id": trace_id, "files": list(files) + ["input_records.jsonl"],
                "n_files": n_files, "n_raw_input_records": n_raw}
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    n_files += 1  # manifest
    return n_files
