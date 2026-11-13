"""可随机访问的有序 Merkle 树存储（SQLite 外排）。

构建阶段不在 Python 内存中保存全量键，而是把原始记录写入 SQLite 的
WITHOUT ROWID 表，利用其 B 树（BINARY 排序规则，即原始字节序）完成
外部排序与去重：重复键会触发 PRIMARY KEY 冲突，整次构建失败。

数据库布局：
- records(staging): 写入阶段的暂存表，主键冲突负责检测重复键。
- records(ord, key PRIMARY KEY, value): 排序迁移后的正式表，ord 为
  原始叶下标（从 0 开始），由窗口函数一次性生成。
- nodes(level, idx, digest): 全树节点摘要，level 0 为叶层，根在最高层。
- meta(key PRIMARY KEY, value): 版本、记录数、叶数、树高、根摘要。

加载时不只看文件是否存在：会重算全部叶摘要与各层内部摘要，核验元数据、
记录数、排序关系、补齐数量以及最终根，任一不符即拒绝。
"""

from __future__ import annotations

import json
import os
import sqlite3
from types import TracebackType
from typing import Iterator, Optional

from . import FORMAT_VERSION
from .errors import InputError, TreeIntegrityError
from .hashing import (
    DIGEST_SIZE,
    EMPTY_ROOT,
    MAX_FIELD_BYTES,
    PAD_LEAF,
    internal_digest,
    leaf_digest,
    next_power_of_two,
    tree_height,
)
from .manifest import Manifest, build_manifest

_SCHEMA_STAGING = """
CREATE TABLE staging (
    key   BLOB PRIMARY KEY,
    value BLOB NOT NULL
) WITHOUT ROWID
"""

_SCHEMA_RECORDS = """
CREATE TABLE records (
    ord   INTEGER PRIMARY KEY,
    key   BLOB NOT NULL UNIQUE,
    value BLOB NOT NULL
) WITHOUT ROWID
"""

_SCHEMA_NODES = """
CREATE TABLE nodes (
    level  INTEGER NOT NULL,
    idx    INTEGER NOT NULL,
    digest BLOB NOT NULL,
    PRIMARY KEY (level, idx)
) WITHOUT ROWID
"""

_SCHEMA_META = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID
"""


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    # BLOB 列使用 BINARY 排序规则，与 Python bytes 的字节序一致。
    conn.execute("PRAGMA case_sensitive_like = ON")
    return conn


def _decode_field(obj: object, field: str, lineno: int) -> bytes:
    if not isinstance(obj, dict):
        raise InputError(f"第 {lineno} 行：每条记录必须是 JSON 对象")
    if set(obj.keys()) != {"key", "value"}:
        raise InputError(f"第 {lineno} 行：记录必须且只能包含 key、value 两个字段")
    raw = obj[field]
    if not isinstance(raw, str):
        raise InputError(f"第 {lineno} 行：字段 {field} 必须是字符串")
    data = raw.encode("utf-8")
    if len(data) > MAX_FIELD_BYTES:
        raise InputError(
            f"第 {lineno} 行：字段 {field} 为 {len(data)} 字节，超过 64 KiB 上限"
        )
    return data


def _iter_records(jsonl_path: str) -> Iterator[tuple[bytes, bytes]]:
    """逐行解析 JSONL，严格校验，不把全量数据载入内存。"""
    try:
        f = open(jsonl_path, "r", encoding="utf-8")
    except OSError as exc:
        raise InputError(f"无法打开输入文件 {jsonl_path}: {exc}") from exc
    with f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                raise InputError(f"第 {lineno} 行：不允许空行")
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise InputError(f"第 {lineno} 行：JSON 解析失败: {exc}") from exc
            key = _decode_field(obj, "key", lineno)
            if len(key) == 0:
                raise InputError(f"第 {lineno} 行：key 必须为非空字符串")
            value = _decode_field(obj, "value", lineno)
            yield key, value


def build_tree(jsonl_path: str, db_path: str) -> Manifest:
    """从 JSONL 构建树数据库，返回根清单。任何非法输入都不会留下产物。"""
    tmp_path = f"{db_path}.tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    conn = _connect(tmp_path)
    try:
        conn.execute("PRAGMA journal_mode = MEMORY")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute(_SCHEMA_STAGING)
        conn.execute(_SCHEMA_RECORDS)
        conn.execute(_SCHEMA_NODES)
        conn.execute(_SCHEMA_META)

        seen = 0
        batch: list[tuple[bytes, bytes]] = []
        for key, value in _iter_records(jsonl_path):
            batch.append((key, value))
            seen += 1
            if len(batch) >= 1000:
                _insert_staging(conn, batch)
                batch.clear()
        if batch:
            _insert_staging(conn, batch)

        # 借助 SQLite B 树完成外排，窗口函数一次性分配原始叶下标。
        conn.execute(
            "INSERT INTO records(ord, key, value) "
            "SELECT ROW_NUMBER() OVER (ORDER BY key) - 1, key, value FROM staging"
        )
        conn.execute("DROP TABLE staging")
        record_count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        if record_count != seen:  # 理论上主键约束已保证，双保险。
            raise InputError("记录数与排序迁移结果不一致")

        root = _build_node_levels(conn, record_count)
        height = tree_height(record_count)

        meta = {
            "format_version": str(FORMAT_VERSION),
            "record_count": str(record_count),
            "leaf_count": str(next_power_of_two(record_count) if record_count else 0),
            "height": str(height),
            "root_hex": root.hex(),
        }
        conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", meta.items())
        conn.commit()
    except Exception:
        conn.close()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    conn.close()
    os.replace(tmp_path, db_path)
    return build_manifest(record_count, root)


def _insert_staging(
    conn: sqlite3.Connection, batch: list[tuple[bytes, bytes]]
) -> None:
    try:
        conn.executemany("INSERT INTO staging(key, value) VALUES (?, ?)", batch)
    except sqlite3.IntegrityError as exc:
        raise InputError(f"发现重复 key，构建失败: {exc}") from exc


def _build_node_levels(conn: sqlite3.Connection, record_count: int) -> bytes:
    """自叶向根折叠，补齐叶使用固定常量 SHA256(0x03)。"""
    if record_count == 0:
        # 空数据不生成任何节点，根固定为 SHA256(0x02)。
        return EMPTY_ROOT

    leaf_count = next_power_of_two(record_count)
    height = leaf_count.bit_length() - 1

    # level 0：真实记录按 ord 写入叶摘要，剩余位置写补齐叶。
    level_rows: list[tuple[int, int, bytes]] = []
    for ord_, key, value in conn.execute(
        "SELECT ord, key, value FROM records ORDER BY ord"
    ):
        level_rows.append((0, ord_, leaf_digest(key, value)))
    for idx in range(record_count, leaf_count):
        level_rows.append((0, idx, PAD_LEAF))
    conn.executemany("INSERT INTO nodes(level, idx, digest) VALUES (?, ?, ?)", level_rows)

    # 逐层折叠；游标流式读取，避免整层同时驻留 Python 内存。
    current_count = leaf_count
    for level in range(1, height + 1):
        parent_rows: list[tuple[int, int, bytes]] = []
        cur = conn.execute(
            "SELECT idx, digest FROM nodes WHERE level = ? ORDER BY idx",
            (level - 1,),
        )
        pending: Optional[tuple[int, bytes]] = None
        for idx, digest in cur:
            if pending is None:
                pending = (idx, digest)
            else:
                left_idx, left = pending
                if idx != left_idx + 1:
                    raise TreeIntegrityError("层内节点下标不连续，无法折叠")
                parent_rows.append(
                    (level, left_idx // 2, internal_digest(left, digest))
                )
                pending = None
        if pending is not None or current_count % 2 != 0:
            raise TreeIntegrityError("折叠父节点时子节点数量为奇数")
        conn.executemany(
            "INSERT INTO nodes(level, idx, digest) VALUES (?, ?, ?)", parent_rows
        )
        current_count //= 2

    if current_count != 1:
        raise TreeIntegrityError("折叠结束后根节点数量不为 1")
    root = conn.execute(
        "SELECT digest FROM nodes WHERE level = ? AND idx = 0", (height,)
    ).fetchone()[0]
    return root


def _require_meta(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.DatabaseError as exc:
        raise TreeIntegrityError(f"数据库结构损坏: {exc}") from exc
    meta = dict(rows)
    for key in ("format_version", "record_count", "leaf_count", "height", "root_hex"):
        if key not in meta:
            raise TreeIntegrityError(f"元数据缺失: {key}")
    return meta


class Tree:
    """只读随机访问句柄。构造即执行完整完整性核验。"""

    def __init__(self, db_path: str, expected_manifest: Optional[Manifest] = None):
        if not os.path.exists(db_path):
            raise TreeIntegrityError(f"树数据库不存在: {db_path}")
        self.conn = _connect(db_path)
        try:
            self.meta = _require_meta(self.conn)
            self._verify_integrity()
        except Exception:
            self.conn.close()
            raise
        self.manifest = build_manifest(self.record_count, self.root)
        if expected_manifest is not None and self.manifest != expected_manifest:
            raise TreeIntegrityError("树与可信根清单不一致（版本/记录数/根摘要）")

    # ---- 基本属性 -------------------------------------------------
    @property
    def record_count(self) -> int:
        return int(self.meta["record_count"])

    @property
    def height(self) -> int:
        return int(self.meta["height"])

    @property
    def leaf_count(self) -> int:
        return int(self.meta["leaf_count"])

    @property
    def root(self) -> bytes:
        return bytes.fromhex(self.meta["root_hex"])

    # ---- 完整性核验 -----------------------------------------------
    def _verify_integrity(self) -> None:
        meta = self.meta
        if meta["format_version"] != str(FORMAT_VERSION):
            raise TreeIntegrityError(
                f"格式版本不符: {meta['format_version']} != {FORMAT_VERSION}"
            )
        try:
            count = int(meta["record_count"])
            leaf_count = int(meta["leaf_count"])
            height = int(meta["height"])
            root = bytes.fromhex(meta["root_hex"])
        except ValueError as exc:
            raise TreeIntegrityError(f"元数据取值非法: {exc}") from exc
        if len(root) != DIGEST_SIZE or count < 0 or leaf_count < 0 or height < 0:
            raise TreeIntegrityError("元数据取值越界")
        if height != tree_height(count):
            raise TreeIntegrityError("树高与记录数不匹配")
        expected_leaves = next_power_of_two(count) if count else 0
        if leaf_count != expected_leaves:
            raise TreeIntegrityError("叶数与记录数不匹配")

        # records 表：数量、连续下标、字节序、无重复键。
        rows = self.conn.execute(
            "SELECT ord, key, value FROM records ORDER BY ord"
        ).fetchall()
        if len(rows) != count:
            raise TreeIntegrityError("records 行数与记录数不符")
        prev_key: Optional[bytes] = None
        for expected_ord, (ord_, key, value) in enumerate(rows):
            if ord_ != expected_ord:
                raise TreeIntegrityError("叶下标不连续或未从零开始")
            if not isinstance(key, bytes) or len(key) == 0:
                raise TreeIntegrityError("存在空键或键类型损坏")
            if not isinstance(value, bytes):
                raise TreeIntegrityError("值类型损坏")
            if prev_key is not None and not (prev_key < key):
                raise TreeIntegrityError("记录未按键字节序严格升序排列")
            prev_key = key

        if count == 0:
            if root != EMPTY_ROOT:
                raise TreeIntegrityError("空树根必须为 SHA256(0x02)")
            if self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] != 0:
                raise TreeIntegrityError("空树不应包含任何节点")
            return

        # 重算 level 0：真实叶摘要 + 固定补齐叶。
        recomputed: list[bytes] = [leaf_digest(k, v) for _, k, v in rows]
        recomputed.extend(PAD_LEAF for _ in range(count, leaf_count))
        stored_level0 = [
            d for (d,) in self.conn.execute(
                "SELECT digest FROM nodes WHERE level = 0 ORDER BY idx"
            )
        ]
        if len(stored_level0) != leaf_count or stored_level0 != recomputed:
            raise TreeIntegrityError("叶层摘要与记录重算结果不符")

        # 逐层重算内部节点直到根。
        current = recomputed
        for level in range(1, height + 1):
            nxt = [
                internal_digest(current[i], current[i + 1])
                for i in range(0, len(current), 2)
            ]
            stored = [
                d for (d,) in self.conn.execute(
                    "SELECT digest FROM nodes WHERE level = ? ORDER BY idx", (level,)
                )
            ]
            if stored != nxt:
                raise TreeIntegrityError(f"第 {level} 层内部摘要核验失败")
            current = nxt
        if len(current) != 1 or current[0] != root:
            raise TreeIntegrityError("根摘要与重算结果不符")

    # ---- 随机访问 -------------------------------------------------
    def lookup(self, key: bytes) -> Optional[tuple[int, bytes]]:
        """精确命中返回 (原始叶下标, value)，否则 None。"""
        row = self.conn.execute(
            "SELECT ord, value FROM records WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), bytes(row[1])

    def predecessor(self, key: bytes) -> Optional[tuple[int, bytes, bytes]]:
        """严格小于 key 的最大键：(下标, key, value)。"""
        row = self.conn.execute(
            "SELECT ord, key, value FROM records WHERE key < ? ORDER BY key DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), bytes(row[1]), bytes(row[2])

    def successor(self, key: bytes) -> Optional[tuple[int, bytes, bytes]]:
        """严格大于 key 的最小键：(下标, key, value)。"""
        row = self.conn.execute(
            "SELECT ord, key, value FROM records WHERE key > ? ORDER BY key ASC LIMIT 1",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), bytes(row[1]), bytes(row[2])

    def node_digest(self, level: int, idx: int) -> bytes:
        row = self.conn.execute(
            "SELECT digest FROM nodes WHERE level = ? AND idx = ?", (level, idx)
        ).fetchone()
        if row is None:
            raise TreeIntegrityError(f"请求了不存在的节点: level={level} idx={idx}")
        return bytes(row[0])

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Tree":
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()


def load_tree(db_path: str, expected_manifest: Optional[Manifest] = None) -> Tree:
    return Tree(db_path, expected_manifest=expected_manifest)
