"""本地只读 Web 界面（标准库 http.server，零第三方依赖）。

按需读取：所有列表接口强制 LIMIT 分页；时间线按 trace 窗口返回切片而不是原始全量；
“点击定位原始事件”走单条 raw 接口。10 万 span 也不会把原文推给浏览器。
"""
from __future__ import annotations

import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .report import build_report
from .storage import get_raw

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
MAX_LIMIT = 500


def _db_connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def make_handler(db_path: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TraceRelay/1.0"

        def log_message(self, fmt, *args):  # 安静一点
            pass

        def _send(self, obj, status=200, ctype="application/json; charset=utf-8"):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                self._route(u.path, q)
            except Exception as exc:  # 只读界面，异常回 JSON 不泄栈到页面外
                self._send({"error": str(exc)}, status=500)

        def _limit(self, q):
            return max(1, min(int(q.get("limit", 200)), MAX_LIMIT))

        def _route(self, path, q):
            conn = _db_connect(db_path)
            try:
                if path in ("/", "/index.html"):
                    return self._static("index.html", "text/html; charset=utf-8")
                if path == "/app.js":
                    return self._static("app.js", "application/javascript; charset=utf-8")
                if path == "/app.css":
                    return self._static("app.css", "text/css; charset=utf-8")
                if path == "/api/health":
                    return self._send({"ok": True, "db": db_path})
                if path == "/api/traces":
                    return self._traces(conn, q)
                if path == "/api/trace":
                    return self._trace_detail(conn, q)
                if path == "/api/timeline":
                    return self._timeline(conn, q)
                if path == "/api/awaits":
                    return self._awaits(conn, q)
                if path == "/api/rpc":
                    return self._rpc(conn, q)
                if path == "/api/diagnostics":
                    return self._diagnostics(conn, q)
                if path == "/api/lanes":
                    return self._lanes(conn, q)
                if path == "/api/raw":
                    return self._raw(conn, q)
                if path == "/api/ref-loc":
                    return self._ref_loc(conn, q)
                if path == "/api/report":
                    return self._send(build_report(conn))
                if path == "/api/issues":
                    return self._issues(conn, q)
                self._send({"error": "not found"}, status=404)
            finally:
                conn.close()

        def _static(self, name, ctype):
            fp = os.path.join(WEB_DIR, name)
            if not os.path.isfile(fp):
                return self._send({"error": "missing static asset"}, 404)
            with open(fp, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _traces(self, conn, q):
            where, params = [], []
            if q.get("complete") == "1":
                where.append("complete_flag=1")
            if q.get("gaps") == "1":
                where.append("(has_gap=1 OR has_unknown_time=1 OR complete_flag=0)")
            if q.get("q"):
                where.append("(trace_id LIKE ? OR request_type LIKE ?)")
                params += [f"%{q['q']}%", f"%{q['q']}%"]
            wsql = (" WHERE " + " AND ".join(where)) if where else ""
            limit, offset = self._limit(q), int(q.get("offset", 0))
            total = conn.execute(f"SELECT COUNT(*) c FROM analysis_trace{wsql}",
                                 params).fetchone()["c"]
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM analysis_trace{wsql} ORDER BY trace_id LIMIT ? OFFSET ?",
                params + [limit, offset])]
            self._send({"total": total, "limit": limit, "offset": offset, "rows": rows})

        def _trace_detail(self, conn, q):
            tid = q["trace_id"]
            summ = conn.execute("SELECT * FROM analysis_trace WHERE trace_id=?",
                                (tid,)).fetchone()
            if not summ:
                return self._send({"error": "trace not found"}, 404)
            issues = [dict(r) for r in conn.execute(
                "SELECT severity, code, evidence FROM validation_issues WHERE trace_id=?",
                (tid,))]
            self._send({"summary": dict(summ), "issues": issues})

        def _timeline(self, conn, q):
            tid = q["trace_id"]
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM timeline_slices WHERE trace_id=? ORDER BY slice_no", (tid,))]
            self._send({"trace_id": tid, "slices": rows})

        def _lanes(self, conn, q):
            """泳道：按服务分组返回 span（分页/限量），事件单独返回供打点。"""
            tid = q["trace_id"]
            limit = self._limit(q)
            spans = [dict(r) for r in conn.execute(
                "SELECT span_id, service, operation, span_type, start_ns, end_ns, parent_id, "
                "retry_of, attempt_no FROM spans WHERE trace_id=? "
                "ORDER BY service, start_ns LIMIT ?", (tid, limit))]
            events = [dict(r) for r in conn.execute(
                "SELECT event_id, span_id, name, at_ns FROM events WHERE trace_id=? "
                "AND at_ns IS NOT NULL ORDER BY at_ns LIMIT ?", (tid, limit))]
            awaits = [dict(r) for r in conn.execute(
                "SELECT await_id, waiter_span_id, start_ns, end_ns, mode FROM awaits "
                "WHERE trace_id=? LIMIT ?", (tid, limit))]
            self._send({"trace_id": tid, "spans": spans, "events": events,
                        "awaits": awaits, "truncated": len(spans) == limit})

        def _awaits(self, conn, q):
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM await_causes WHERE trace_id=? ORDER BY start_ns",
                (q["trace_id"],))]
            self._send({"rows": rows})

        def _rpc(self, conn, q):
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM rpc_pairs WHERE trace_id=? ORDER BY client_span_id",
                (q["trace_id"],))]
            self._send({"rows": rows})

        def _diagnostics(self, conn, q):
            sql = "SELECT * FROM diagnostics"
            params = []
            if q.get("trace_id"):
                sql += " WHERE trace_id=?"
                params.append(q["trace_id"])
            sql += " ORDER BY diag_code LIMIT ?"
            params.append(self._limit(q))
            self._send({"rows": [dict(r) for r in conn.execute(sql, params)]})

        def _issues(self, conn, q):
            limit, offset = self._limit(q), int(q.get("offset", 0))
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM validation_issues ORDER BY trace_id LIMIT ? OFFSET ?",
                (limit, offset))]
            self._send({"rows": rows, "limit": limit, "offset": offset})

        def _raw(self, conn, q):
            rec = get_raw(conn, q["source_file"], int(q["source_line"]))
            self._send(rec or {"error": "raw record not found"},
                       status=200 if rec else 404)

        def _ref_loc(self, conn, q):
            """证据 id -> 原始 JSONL 定位（source_file, line_no），再由前端取原文。"""
            kind, rid, tid = q.get("kind", "span"), q["id"], q["trace_id"]
            table, col = ("awaits", "await_id") if kind == "await" else (
                ("events", "event_id") if kind == "event" else ("spans", "span_id"))
            row = conn.execute(
                f"SELECT source_file, source_line FROM {table} WHERE trace_id=? AND {col}=?",
                (tid, rid)).fetchone()
            if not row:
                return self._send({"error": "ref not found", "id": rid}, 404)
            self._send(get_raw(conn, row["source_file"], row["source_line"])
                       or {"error": "raw missing"})

    return Handler


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path))
    print(f"TraceRelay 界面: http://{host}:{port}/  (db={db_path}, Ctrl+C 退出)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止")
