"""SQLite 存储与 JSONL 幂等导入。

设计要点
--------
- 原始记录全文存 ``raw_records``，界面“点击定位原始事件”按 (file,line_no) 精确回查；
  浏览器端永远走分页/单条接口，不一次性拉取原文（10 万 span 也不塞给前端）。
- 完全相同的记录按内容哈希折叠（dedup 计数 +1）；同 ID 内容冲突不折叠，写验证问题并保留首条。
- 缺失时间戳存 NULL，全链路保持 NULL，不填零。
- 分析结果表每次 analyze 整体重建，可复算、可复现。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any, Iterator, Optional

from .model import SPAN_TYPES

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
  trace_id      TEXT NOT NULL,
  span_id       TEXT NOT NULL,
  service       TEXT NOT NULL,
  operation     TEXT NOT NULL,
  span_type     TEXT NOT NULL,
  start_ns      INTEGER,
  end_ns        INTEGER,
  parent_id     TEXT,
  retry_of      TEXT,
  attempt_no    INTEGER,
  dedup_count   INTEGER NOT NULL DEFAULT 1,
  source_file   TEXT NOT NULL,
  source_line   INTEGER NOT NULL,
  PRIMARY KEY (trace_id, span_id)
);
CREATE TABLE IF NOT EXISTS span_links (
  trace_id    TEXT NOT NULL,
  source_id   TEXT NOT NULL,
  target_id   TEXT NOT NULL,
  rel         TEXT NOT NULL DEFAULT 'link'
);
CREATE TABLE IF NOT EXISTS events (
  trace_id    TEXT NOT NULL,
  event_id    TEXT NOT NULL,
  span_id     TEXT,
  name        TEXT NOT NULL,
  at_ns       INTEGER,
  dedup_count INTEGER NOT NULL DEFAULT 1,
  source_file TEXT NOT NULL,
  source_line INTEGER NOT NULL,
  PRIMARY KEY (trace_id, event_id)
);
CREATE TABLE IF NOT EXISTS awaits (
  trace_id        TEXT NOT NULL,
  await_id        TEXT NOT NULL,
  waiter_span_id  TEXT NOT NULL,
  start_ns        INTEGER NOT NULL,
  end_ns          INTEGER,
  mode            TEXT NOT NULL,
  result_ids      TEXT NOT NULL,
  resolved_by     TEXT,
  open_flag       INTEGER NOT NULL DEFAULT 0,
  dedup_count     INTEGER NOT NULL DEFAULT 1,
  source_file     TEXT NOT NULL,
  source_line     INTEGER NOT NULL,
  PRIMARY KEY (trace_id, await_id)
);
CREATE TABLE IF NOT EXISTS raw_records (
  source_file TEXT NOT NULL,
  source_line INTEGER NOT NULL,
  kind        TEXT NOT NULL,
  record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS validation_issues (
  trace_id  TEXT NOT NULL,
  severity  TEXT NOT NULL,          -- error / warning / info
  code      TEXT NOT NULL,
  evidence  TEXT NOT NULL           -- JSON 证据
);
CREATE TABLE IF NOT EXISTS import_runs (
  run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  source_file TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  n_spans     INTEGER NOT NULL,
  n_events    INTEGER NOT NULL,
  n_awaits    INTEGER NOT NULL,
  n_dup       INTEGER NOT NULL,
  n_conflict  INTEGER NOT NULL
);

-- 分析结果表（每次 analyze 重建）
CREATE TABLE IF NOT EXISTS rpc_pairs (
  trace_id TEXT NOT NULL, client_span_id TEXT, server_span_id TEXT,
  match_basis TEXT NOT NULL, confidence TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_trace (
  trace_id TEXT PRIMARY KEY,
  root_span_id TEXT, root_service TEXT, root_operation TEXT, request_type TEXT,
  root_start_ns INTEGER, root_end_ns INTEGER, root_window_ns INTEGER,
  complete_flag INTEGER NOT NULL,
  has_gap INTEGER NOT NULL, has_unknown_time INTEGER NOT NULL,
  n_spans INTEGER, n_events INTEGER, n_awaits INTEGER,
  self_ns INTEGER, await_ns INTEGER, queue_ns INTEGER, gap_ns INTEGER,
  gap_lower_ns INTEGER, gap_upper_ns INTEGER,
  conservation_ok INTEGER NOT NULL,
  background_ns INTEGER, background_open INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS timeline_slices (
  trace_id TEXT NOT NULL, slice_no INTEGER NOT NULL,
  start_ns INTEGER NOT NULL, end_ns INTEGER NOT NULL, duration_ns INTEGER NOT NULL,
  category TEXT NOT NULL, sublabel TEXT NOT NULL,
  ref_kind TEXT NOT NULL, ref_id TEXT NOT NULL,
  service TEXT NOT NULL, operation TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS await_causes (
  trace_id TEXT NOT NULL, await_id TEXT NOT NULL,
  waiter_span_id TEXT NOT NULL, mode TEXT NOT NULL,
  start_ns INTEGER NOT NULL, end_ns INTEGER,
  resolved_by TEXT NOT NULL, blocker_phase TEXT NOT NULL,
  blocker_detail TEXT NOT NULL, evidence TEXT NOT NULL,
  upper_await_id TEXT
);
CREATE TABLE IF NOT EXISTS resource_ledger (
  trace_id TEXT NOT NULL, service TEXT NOT NULL, operation TEXT NOT NULL,
  work_kind TEXT NOT NULL, exec_ns INTEGER, queue_ns INTEGER,
  await_ns INTEGER, background_ns INTEGER, open_flag INTEGER NOT NULL DEFAULT 0,
  lower_bound_ns INTEGER, upper_bound_ns INTEGER
);
CREATE TABLE IF NOT EXISTS diagnostics (
  trace_id TEXT NOT NULL, diag_code TEXT NOT NULL, certainty TEXT NOT NULL,
  title TEXT NOT NULL, detail TEXT NOT NULL, evidence TEXT NOT NULL
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_spans_trace ON spans(trace_id)",
    "CREATE INDEX IF NOT EXISTS ix_spans_parent ON spans(trace_id, parent_id)",
    "CREATE INDEX IF NOT EXISTS ix_spans_type ON spans(trace_id, span_type)",
    "CREATE INDEX IF NOT EXISTS ix_spans_time ON spans(trace_id, start_ns)",
    "CREATE INDEX IF NOT EXISTS ix_spans_svc ON spans(service, operation)",
    "CREATE INDEX IF NOT EXISTS ix_events_trace ON events(trace_id)",
    "CREATE INDEX IF NOT EXISTS ix_events_span ON events(trace_id, span_id)",
    "CREATE INDEX IF NOT EXISTS ix_awaits_trace ON awaits(trace_id)",
    "CREATE INDEX IF NOT EXISTS ix_issues_trace ON validation_issues(trace_id)",
    "CREATE INDEX IF NOT EXISTS ix_slices_trace ON timeline_slices(trace_id, slice_no)",
    "CREATE INDEX IF NOT EXISTS ix_ledger_svc ON resource_ledger(service, operation)",
    "CREATE INDEX IF NOT EXISTS ix_diag_trace ON diagnostics(trace_id)",
    "CREATE INDEX IF NOT EXISTS ix_rpc_trace ON rpc_pairs(trace_id)",
    "CREATE INDEX IF NOT EXISTS ix_raw_lookup ON raw_records(source_file, source_line)",
    "CREATE INDEX IF NOT EXISTS ix_atrace_type ON analysis_trace(request_type)",
]

ANALYSIS_TABLES = ["rpc_pairs", "analysis_trace", "timeline_slices", "await_causes",
                   "resource_ledger", "diagnostics"]


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-65536")  # 64MB 页缓存，大批量导入/分析用
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for stmt in INDEXES:
        conn.execute(stmt)
    conn.commit()


def reset_analysis(conn: sqlite3.Connection) -> None:
    for t in ANALYSIS_TABLES:
        conn.execute(f"DELETE FROM {t}")
    conn.commit()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _content_hash(obj: dict) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()[:16]


def _issue(conn: sqlite3.Connection, trace_id: str, severity: str, code: str, evidence: dict) -> None:
    conn.execute(
        "INSERT INTO validation_issues(trace_id, severity, code, evidence) VALUES(?,?,?,?)",
        (trace_id, severity, code, json.dumps(evidence, ensure_ascii=False)),
    )


def import_jsonl(conn: sqlite3.Connection, path: str) -> dict:
    """流式读取 JSONL 并导入。返回计数统计。输入顺序不影响后续分析（全部按 trace_id/时间重排）。"""
    init_db(conn)
    counts = {"span": 0, "event": 0, "await": 0, "dup": 0, "conflict": 0, "bad": 0}
    f = os.path.basename(path)
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
                kind = rec.get("kind")
            except json.JSONDecodeError:
                counts["bad"] += 1
                _issue(conn, "?", "error", "unparseable_json",
                       {"source_file": f, "source_line": line_no})
                continue
            conn.execute("INSERT INTO raw_records(source_file, source_line, kind, record_json) "
                         "VALUES(?,?,?,?)", (f, line_no, kind or "?", raw))
            if kind == "span":
                _import_span(conn, f, line_no, rec, counts)
            elif kind == "event":
                _import_event(conn, f, line_no, rec, counts)
            elif kind == "await":
                _import_await(conn, f, line_no, rec, counts)
            else:
                counts["bad"] += 1
                _issue(conn, str(rec.get("trace_id", "?")), "error", "unknown_record_kind",
                       {"source_file": f, "source_line": line_no, "kind": kind})
    conn.execute(
        "INSERT INTO import_runs(source_file, imported_at, n_spans, n_events, n_awaits, n_dup, n_conflict) "
        "VALUES(?, datetime('now'), ?,?,?,?,?)",
        (f, counts["span"], counts["event"], counts["await"], counts["dup"], counts["conflict"]))
    conn.commit()
    return counts


def _check_time(rec: dict, conn: sqlite3.Connection, trace_id: str, who: str) -> None:
    s, e = rec.get("start_ns"), rec.get("end_ns")
    if s is not None and e is not None and e < s:
        _issue(conn, trace_id, "error", "negative_duration",
               {"ref": who, "start_ns": s, "end_ns": e})


def _import_span(conn: sqlite3.Connection, f: str, line_no: int, rec: dict, counts: dict) -> None:
    trace_id = str(rec.get("trace_id") or "")
    span_id = str(rec.get("span_id") or "")
    if not trace_id or not span_id:
        counts["bad"] += 1
        _issue(conn, trace_id or "?", "error", "span_missing_identity",
               {"source_file": f, "source_line": line_no})
        return
    if rec.get("span_type") not in SPAN_TYPES:
        _issue(conn, trace_id, "warning", "unknown_span_type",
               {"span_id": span_id, "span_type": rec.get("span_type")})
    _check_time(rec, conn, trace_id, f"span:{span_id}")
    ident = _content_hash({k: v for k, v in rec.items() if k != "kind"})
    row = conn.execute("SELECT * FROM spans WHERE trace_id=? AND span_id=?",
                       (trace_id, span_id)).fetchone()
    if row is not None:
        # 内容身份只比较固定的业务字段（两侧用同一组键，缺列按 None 处理），
        # 不含来源位置/折叠计数/links，保证跨文件同内容可折叠
        identity_cols = ("trace_id", "span_id", "service", "operation", "span_type",
                         "start_ns", "end_ns", "parent_id", "retry_of", "attempt_no")
        old_sig = _content_hash({k: row[k] for k in identity_cols})
        new_sig = _content_hash({k: rec.get(k) for k in identity_cols})
        if old_sig == new_sig:
            conn.execute("UPDATE spans SET dedup_count=dedup_count+1 WHERE trace_id=? AND span_id=?",
                         (trace_id, span_id))
            counts["dup"] += 1
            return
        counts["conflict"] += 1
        _issue(conn, trace_id, "error", "conflicting_span_identity",
               {"span_id": span_id, "kept": {"source_file": row["source_file"],
                                             "source_line": row["source_line"]},
                "rejected": {"source_file": f, "source_line": line_no,
                             "service": rec.get("service"), "operation": rec.get("operation"),
                             "start_ns": rec.get("start_ns"), "end_ns": rec.get("end_ns")}})
        return
    conn.execute(
        "INSERT INTO spans(trace_id, span_id, service, operation, span_type, start_ns, end_ns, "
        "parent_id, retry_of, attempt_no, source_file, source_line) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (trace_id, span_id, rec.get("service", "?"), rec.get("operation", "?"),
         rec.get("span_type", "internal"), rec.get("start_ns"), rec.get("end_ns"),
         rec.get("parent_id"), rec.get("retry_of"), rec.get("attempt_no"), f, line_no))
    for link in rec.get("links", []) or []:
        if isinstance(link, dict):
            target, rel = link.get("span_id"), link.get("rel", "link")
        else:
            target, rel = link, "link"
        if target:
            conn.execute("INSERT INTO span_links(trace_id, source_id, target_id, rel) VALUES(?,?,?,?)",
                         (trace_id, span_id, str(target), rel))
    counts["span"] += 1


def _import_event(conn: sqlite3.Connection, f: str, line_no: int, rec: dict, counts: dict) -> None:
    trace_id = str(rec.get("trace_id") or "")
    event_id = str(rec.get("event_id") or f"auto@{f}:{line_no}")
    row = conn.execute("SELECT 1 FROM events WHERE trace_id=? AND event_id=?",
                       (trace_id, event_id)).fetchone()
    if row is not None:
        counts["dup"] += 1
        conn.execute("UPDATE events SET dedup_count=dedup_count+1 WHERE trace_id=? AND event_id=?",
                     (trace_id, event_id))
        return
    conn.execute(
        "INSERT INTO events(trace_id, event_id, span_id, name, at_ns, source_file, source_line) "
        "VALUES(?,?,?,?,?,?,?)",
        (trace_id, event_id, rec.get("span_id"), rec.get("name", "?"),
         rec.get("at_ns"), f, line_no))
    counts["event"] += 1


def _import_await(conn: sqlite3.Connection, f: str, line_no: int, rec: dict, counts: dict) -> None:
    trace_id = str(rec.get("trace_id") or "")
    await_id = str(rec.get("await_id") or f"auto@{f}:{line_no}")
    row = conn.execute("SELECT 1 FROM awaits WHERE trace_id=? AND await_id=?",
                       (trace_id, await_id)).fetchone()
    if row is not None:
        counts["dup"] += 1
        conn.execute("UPDATE awaits SET dedup_count=dedup_count+1 WHERE trace_id=? AND await_id=?",
                     (trace_id, await_id))
        return
    mode = rec.get("mode", "all")
    if mode not in ("all", "any"):
        _issue(conn, trace_id, "warning", "unknown_await_mode",
               {"await_id": await_id, "mode": mode})
    conn.execute(
        "INSERT INTO awaits(trace_id, await_id, waiter_span_id, start_ns, end_ns, mode, "
        "result_ids, resolved_by, open_flag, source_file, source_line) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (trace_id, await_id, str(rec.get("waiter_span_id") or ""), rec.get("start_ns"),
         rec.get("end_ns"), mode, json.dumps(rec.get("result_ids", [])),
         json.dumps(rec.get("resolved_by")) if rec.get("resolved_by") is not None else None,
         1 if rec.get("end_ns") is None else 0, f, line_no))
    counts["await"] += 1


def iter_traces(conn: sqlite3.Connection) -> Iterator[str]:
    for row in conn.execute("SELECT DISTINCT trace_id FROM spans ORDER BY trace_id"):
        yield row["trace_id"]


def get_raw(conn: sqlite3.Connection, source_file: str, source_line: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM raw_records WHERE source_file=? AND source_line=?",
                       (source_file, source_line)).fetchone()
    if not row:
        return None
    return {"source_file": source_file, "source_line": source_line, "kind": row["kind"],
            "record": json.loads(row["record_json"])}
