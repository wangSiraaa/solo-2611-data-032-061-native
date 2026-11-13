"""旧文件签名。

把旧文件按块大小 B 切成完整块，逐块记录：
  * 旧偏移 offset、长度 length（完整块恒为 B）
  * Adler32 弱校验
  * SHA256 强摘要
不足 B 的末块不进入复用候选（不写入 blocks 表），但旧文件的完整大小与
整体 SHA256 仍然记录在 meta 表中。

签名文件是一个 SQLite 数据库，弱校验建立落盘索引，delta 只通过该索引
查找候选，旧文件正文从不参与 delta 阶段。
"""

from __future__ import annotations

import os
import sqlite3

from . import SIGNATURE_VERSION
from .checksums import StreamingHash, adler32, sha256_bytes
from .frozen import FrozenFile


def validate_block_size(block_size: int) -> int:
    if not isinstance(block_size, int) or isinstance(block_size, bool):
        raise TypeError("块大小必须是整数")
    if not (256 <= block_size <= 65536):
        raise ValueError("块大小 B 必须位于 [256, 65536] 字节")
    return block_size


def build_signature(old_path: str, sig_path: str, block_size: int) -> dict:
    """扫描旧文件生成 SQLite 签名，返回统计信息。"""
    block_size = validate_block_size(block_size)

    # 先在临时文件上构建，成功后原子替换，避免留下半截签名。
    tmp_path = sig_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    conn = sqlite3.connect(tmp_path)
    try:
        conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute(
            "CREATE TABLE meta ("
            "key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE blocks ("
            "offset INTEGER PRIMARY KEY,"
            "length INTEGER NOT NULL,"
            "adler32 INTEGER NOT NULL,"
            "sha256 TEXT NOT NULL)"
        )
        conn.execute("CREATE INDEX idx_blocks_adler ON blocks(adler32)")

        total = StreamingHash()
        offset = 0
        block_count = 0
        pending: list[tuple[int, int, int, str]] = []

        with FrozenFile(old_path) as ff:
            identity_size = ff.size
            while True:
                block = ff.read(block_size)
                if not block:
                    break
                total.update(block)
                if len(block) == block_size:
                    pending.append(
                        (offset, block_size, adler32(block), sha256_bytes(block))
                    )
                    block_count += 1
                    if len(pending) >= 4096:
                        conn.executemany(
                            "INSERT INTO blocks VALUES (?, ?, ?, ?)", pending
                        )
                        pending.clear()
                # 不足 B 的末块：仅计入整体摘要，不入复用候选。
                offset += len(block)

        if total.size != identity_size:
            raise RuntimeError("内部错误：读取字节数与文件大小不一致")

        if pending:
            conn.executemany("INSERT INTO blocks VALUES (?, ?, ?, ?)", pending)

        meta = {
            "version": SIGNATURE_VERSION,
            "block_size": block_size,
            "old_size": total.size,
        }
        conn.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [(k, v) for k, v in meta.items()],
        )
        # 整体 SHA256 作为字符串单独存放。
        conn.execute(
            "CREATE TABLE meta_text (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO meta_text VALUES (?, ?)",
            ("old_sha256", total.sha256),
        )
        conn.commit()
    finally:
        conn.close()

    os.replace(tmp_path, sig_path)

    return {
        "signature_path": sig_path,
        "block_size": block_size,
        "old_size": total.size,
        "old_sha256": total.sha256,
        "block_count": block_count,
        "tail_size": total.size % block_size,
    }


def open_signature(sig_path: str) -> sqlite3.Connection:
    """以只读方式打开签名（delta/apply 均不允许改写签名）。"""
    uri = f"file:{os.path.abspath(sig_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def read_meta(conn: sqlite3.Connection) -> dict:
    version = conn.execute(
        "SELECT value FROM meta WHERE key='version'"
    ).fetchone()
    if version is None or version[0] != SIGNATURE_VERSION:
        raise ValueError("不支持的签名格式版本")
    rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    text_rows = dict(
        conn.execute("SELECT key, value FROM meta_text").fetchall()
    )
    return {
        "version": rows["version"],
        "block_size": rows["block_size"],
        "old_size": rows["old_size"],
        "old_sha256": text_rows["old_sha256"],
    }
