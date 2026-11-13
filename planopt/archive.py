"""SQLite 归档：把输入与规范结果存入本地单文件数据库。

纯标准库 sqlite3，不访问任何外部数据库。库文件默认放在运行目录下的
var/planopt_archive.sqlite3（var/ 用于所有本地产物）。
"""

import sqlite3

from .render import canonical_json

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    preset        TEXT,
    input_sha256  TEXT NOT NULL,
    input_json    TEXT NOT NULL,
    result_json   TEXT NOT NULL,
    optimal_order TEXT NOT NULL,
    total_cost    TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(_SCHEMA)
    commit(conn)
    return conn


def commit(conn: sqlite3.Connection) -> None:
    conn.commit()


def save_run(conn: sqlite3.Connection, *, input_payload: dict, result: dict,
             preset: str | None = None) -> int:
    cur = conn.execute(
        """
        INSERT INTO runs (preset, input_sha256, input_json, result_json,
                          optimal_order, total_cost)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            preset,
            result["input_sha256"],
            canonical_json({"input": input_payload}),
            canonical_json(result),
            " -> ".join(result["optimal_plan"]["order"]),
            result["optimal_plan"]["total_cost"],
        ),
    )
    commit(conn)
    return int(cur.lastrowid)


def load_run(conn: sqlite3.Connection, run_id: int) -> dict:
    import json
    row = conn.execute(
        "SELECT input_json, result_json FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise KeyError(run_id)
    return {"input": json.loads(row[0])["input"], "result": json.loads(row[1])}


def list_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return list(conn.execute(
        "SELECT run_id, created_at, preset, input_sha256, optimal_order, total_cost "
        "FROM runs ORDER BY run_id"
    ))
