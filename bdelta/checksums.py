"""Adler32 滚动弱校验与 SHA256 强摘要。

Adler32（与 :func:`zlib.adler32` 完全一致的模数 65521 定义），窗口长度 n：

    a = 1 + sum(x_i)                        (mod M)
    b = n + sum((n-i) * x_i),  i 从 0 开始   (mod M)

窗口右移一个字节（移出 x_out、移入 x_in，窗口长度恒为 n）时，新窗口的
加权和与旧窗口之差恰好等于新窗口的字节和 a' - 1，因此：

    a' = a - x_out + x_in        (mod M)
    b' = b - n * x_out + a' - 1  (mod M)

两次更新都是 O(1)，不需要为每移动一个字节重算整个窗口。
每个候选窗口仍会用 :func:`zlib.adler32` 重新核对弱值、用 SHA256 核对强摘要。
"""

from __future__ import annotations

import hashlib
import zlib

_MOD = 65521


class RollingAdler:
    """定长窗口的可滚动 Adler32 计算器。"""

    __slots__ = ("_a", "_b", "_n")

    def __init__(self, data: bytes) -> None:
        self._a = 1
        self._b = 0
        self._n = len(data)
        for byte in data:
            self._a += byte
            self._b += self._a
        self._a %= _MOD
        self._b %= _MOD

    @property
    def value(self) -> int:
        return (self._b << 16) | self._a

    def roll(self, outgoing: int, incoming: int) -> int:
        """窗口右移一个字节，返回新的 Adler32。"""
        n = self._n
        a = (self._a - outgoing + incoming) % _MOD
        # b' = b - n*x_out + 新窗口字节和；新窗口字节和 = a' - 1。
        b = (self._b - n * outgoing + a - 1) % _MOD
        self._a = a
        self._b = b
        return self.value


def adler32(data: bytes) -> int:
    """与 zlib 一致的 Adler32（非负整数）。"""
    return zlib.adler32(data) & 0xFFFFFFFF


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class StreamingHash:
    """同时累积 SHA256 与字节数的流式摘要器。"""

    __slots__ = ("_h", "_size")

    def __init__(self) -> None:
        self._h = hashlib.sha256()
        self._size = 0

    def update(self, data: bytes) -> None:
        self._h.update(data)
        self._size += len(data)

    @property
    def size(self) -> int:
        return self._size

    @property
    def sha256(self) -> str:
        return self._h.hexdigest()
