"""SQLite 持久化：保存输入历史与规范报告。

库文件由调用方指定路径（默认放在 .runtime/ 等忽略目录中），全部数据本地存储，
不连接任何真实数据库服务。同一 history_id 重复审计时以最新报告覆盖，
并保留输入指纹以便核对。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit_runs (
    history_id              TEXT PRIMARY KEY,
    input_fingerprint_sha256 TEXT NOT NULL,
    status                  TEXT NOT NULL,
    input_json              TEXT NOT NULL,
    report_json             TEXT NOT NULL,
    created_at_utc          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""


class AuditStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def save(self, history_id: str, fingerprint: str, status: str,
             input_json: str, report_json: str) -> None:
        self.conn.execute(
            """
            INSERT INTO audit_runs
                (history_id, input_fingerprint_sha256, status, input_json, report_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(history_id) DO UPDATE SET
                input_fingerprint_sha256 = excluded.input_fingerprint_sha256,
                status = excluded.status,
                input_json = excluded.input_json,
                report_json = excluded.report_json,
                created_at_utc = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (history_id, fingerprint, status, input_json, report_json),
        )
        self.conn.commit()

    def load(self, history_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM audit_runs WHERE history_id = ?", (history_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_runs(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT history_id, input_fingerprint_sha256, status, created_at_utc "
            "FROM audit_runs ORDER BY history_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "AuditStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
