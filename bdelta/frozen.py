"""冻结输入检查。

signature/delta 只允许在扫描期间保持不变的输入上运行。每次读取前后都比对
文件大小与纳秒级修改时间（st_mtime_ns），一旦发现变化立即抛出
:class:`InputChangedError`，避免在悄悄变化的文件上产生不一致结果。

注意：如果修改发生在两次 stat 之间且大小、mtime_ns 恰好都未变化（极端竞态），
任何仅靠 stat 的方案都无法察觉；因此题面同时要求使用者提供冻结输入。
"""

from __future__ import annotations

import os
from typing import BinaryIO


class InputChangedError(RuntimeError):
    """扫描期间输入文件的大小或修改时间发生了变化。"""


class FrozenFile:
    """只读二进制文件包装器，读取时校验大小与 mtime_ns 不变。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh: BinaryIO = open(path, "rb")
        st = os.fstat(self._fh.fileno())
        self._size = st.st_size
        self._mtime_ns = st.st_mtime_ns

    @property
    def size(self) -> int:
        return self._size

    @property
    def mtime_ns(self) -> int:
        return self._mtime_ns

    def _check(self) -> None:
        st = os.fstat(self._fh.fileno())
        if st.st_size != self._size or st.st_mtime_ns != self._mtime_ns:
            raise InputChangedError(
                f"输入文件在扫描期间发生变化: {self.path} "
                f"(size {self._size}->{st.st_size}, "
                f"mtime_ns {self._mtime_ns}->{st.st_mtime_ns})"
            )

    def read(self, size: int = -1) -> bytes:
        self._check()
        data = self._fh.read(size)
        self._check()
        return data

    def seek(self, pos: int) -> None:
        self._fh.seek(pos)

    def tell(self) -> int:
        return self._fh.tell()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "FrozenFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
