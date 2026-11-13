"""SHA256 摘要规则（题面固定，不做任何 Unicode 归一化）。

- 叶摘要   = SHA256(0x00 || u32be(len(key)) || key || u32be(len(value)) || value)
- 内部节点 = SHA256(0x01 || left_digest(32) || right_digest(32))
- 补齐叶   = SHA256(0x03)，固定常量
- 空数据根 = SHA256(0x02)，固定常量

key/value 均按原始 UTF-8 字节解释，长度即字节长度。
"""

from __future__ import annotations

import hashlib

DIGEST_SIZE = 32
MAX_FIELD_BYTES = 64 * 1024  # 单字段不超过 64 KiB

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"
EMPTY_ROOT_PREFIX = b"\x02"
PAD_PREFIX = b"\x03"

# 空数据根与补齐叶是确定常量，进程内只算一次。
EMPTY_ROOT = hashlib.sha256(EMPTY_ROOT_PREFIX).digest()
PAD_LEAF = hashlib.sha256(PAD_PREFIX).digest()


def u32be(n: int) -> bytes:
    """非负整数的 4 字节大端编码。"""
    if not isinstance(n, int) or n < 0 or n > 0xFFFFFFFF:
        raise ValueError(f"u32 长度越界: {n!r}")
    return n.to_bytes(4, "big")


def leaf_digest(key: bytes, value: bytes) -> bytes:
    """计算真实记录的叶摘要。"""
    h = hashlib.sha256()
    h.update(LEAF_PREFIX)
    h.update(u32be(len(key)))
    h.update(key)
    h.update(u32be(len(value)))
    h.update(value)
    return h.digest()


def internal_digest(left: bytes, right: bytes) -> bytes:
    """计算内部节点摘要，左右次序固定。"""
    if len(left) != DIGEST_SIZE or len(right) != DIGEST_SIZE:
        raise ValueError("内部节点的子摘要必须各为 32 字节")
    return hashlib.sha256(NODE_PREFIX + left + right).digest()


def next_power_of_two(n: int) -> int:
    """不小于 n 的最小二次幂（n >= 1）。"""
    if n < 1:
        raise ValueError("next_power_of_two 仅适用于正整数")
    return 1 << (n - 1).bit_length()


def tree_height(record_count: int) -> int:
    """按记录数推导树高（根所在层号）。空树为 0。"""
    if record_count == 0:
        return 0
    leaf_count = next_power_of_two(record_count)
    return leaf_count.bit_length() - 1
