"""严格 UTF-8 流式解码：逐码点产出 (字符, 字节起点, 字节终点)。

设计要点：
* 按块读取，块边界可能截断多字节序列——只有“结构上不完整”的后缀
  才允许留到下一块继续；真正非法的字节立即报错并给出**绝对**偏移。
* 不使用 errors="replace"，非法输入绝不产生 U+FFFD 伪造定位。
* 正常路径每块只做一次 bytes.decode（C 实现），逐码点的字节长度
  通过码点序数直接计算，避免对每个字符重新 encode。
"""

from __future__ import annotations

from collections.abc import Iterator


class InvalidUTF8(Exception):
    """非法 UTF-8。offset 为非法字节在原文件中的绝对字节偏移。"""

    def __init__(self, offset: int, reason: str) -> None:
        super().__init__(f"非法 UTF-8，字节偏移 {offset}（0x{offset:x}）：{reason}")
        self.offset = offset
        self.reason = reason


def _utf8_lead_length(lead: int) -> int:
    """根据首字节返回序列长度；非首字节（0x80-0xBF）返回 0。"""
    if lead < 0x80:
        return 1
    if 0xC2 <= lead <= 0xDF:
        return 2
    if 0xE0 <= lead <= 0xEF:
        return 3
    if 0xF0 <= lead <= 0xF4:
        return 4
    return 0


def _incomplete_suffix_length(data: bytes) -> int:
    """返回 data 末尾“结构上不完整的 UTF-8 序列”长度，没有则返回 0。

    仅检查序列形状（首字节 + 连续字节是否齐全），不完整是唯一可以
    跨块等待后续字节的合法情形；overlong/代理等语义问题留给拼齐后
    由 bytes.decode 严格判定，报错偏移仍然精确。
    """
    n = len(data)
    max_tail = min(3, n)
    for k in range(1, max_tail + 1):
        lead = data[n - k]
        need = _utf8_lead_length(lead)
        if need == 0:
            # 不是首字节：继续向前找，直到找到首字节为止
            continue
        available = k  # 含首字节本身
        if need <= available:
            return 0  # 序列是完整的，末尾不存在截断
        # 首字节之后、data 之内的字节必须全是连续字节 0x80-0xBF
        for j in range(n - k + 1, n):
            if not (0x80 <= data[j] <= 0xBF):
                return 0
        return k
    return 0


def iter_codepoints(fileobj, chunk_size: int) -> Iterator[tuple[str, int, int]]:
    """从二进制 fileobj 流式读取并严格解码。

    产出 (字符, 绝对字节起点, 绝对字节终点)，区间左闭右开。
    遇到非法或文件结束时仍截断的序列，抛 InvalidUTF8（含绝对偏移）。
    """
    pending = b""
    base = 0  # pending 在原文件中的绝对起点

    while True:
        chunk = fileobj.read(chunk_size)
        if not chunk:
            if pending:
                # 文件结束仍不完整：错误指向该序列首字节
                raise InvalidUTF8(base, "文件结束处 UTF-8 序列被截断")
            return

        data = pending + chunk
        data_start = base
        pending = b""

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            # 只有“末尾序列被块边界截断”可以等待下一块
            cut = _incomplete_suffix_length(data)
            if cut and exc.start == len(data) - cut and exc.end >= len(data):
                pending = data[len(data) - cut:]
                base = data_start + len(data) - cut
                text = data[: len(data) - cut].decode("utf-8")
            else:
                # 真正的非法字节：绝对偏移 = 块基址 + 解码错误相对偏移
                raise InvalidUTF8(data_start + exc.start, exc.reason) from None

        offset = data_start
        for ch in text:
            o = ord(ch)
            if o < 0x80:
                length = 1
            elif o < 0x800:
                length = 2
            elif o < 0x10000:
                length = 3
            else:
                length = 4
            yield ch, offset, offset + length
            offset += length

        if pending:
            base = data_start + len(data) - len(pending)
        else:
            base = data_start + len(data)
