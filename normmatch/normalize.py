"""逐原始 Unicode 码点的规范化器，带逐输出码点的原始字节来源。

规范化规则（关键词与文本完全相同）：

1. 对每个**原始**码点单独执行 NFKD（unicodedata.normalize("NFKD", ch)）；
2. 对展开结果执行 str.casefold()；
3. 删除类别为 Mn / Mc / Me 的码点（unicodedata.category）；
4. 其余字符若满足 str.isspace()，统一替换为 ASCII 空格 " "；
5. 连续空格折叠为一个；**不**剥离文件首尾空格。

NFKD、casefold 都按原始码点独立进行，绝不跨输入码点做整体规范化，
因此一个原始码点展开产生多个输出码点时，这些输出共享同一原始字节区间。

每个输出码点附带来源 [byte_start, byte_end)：
* 普通输出：产生它的原始码点的 UTF-8 字节区间；
* 折叠空格：构成这一整段连续空白的所有原始码点字节并集
  （即从第一个空白码点起点到最后一个空白码点终点的连续区间）。
"""

from __future__ import annotations

import unicodedata

_MARK_CATEGORIES = frozenset(("Mn", "Mc", "Me"))


def transform_codepoint(ch: str) -> list[str]:
    """单个原始码点 -> 规则 1~4 处理后的输出序列（尚未做空格折叠）。

    返回结果中空白已统一为 " "，标记类码点已删除；空格折叠由
    IncrementalNormalizer 跨码点完成（折叠必须知道整段空白范围）。
    """
    out: list[str] = []
    # 规则 1：逐原始码点 NFKD；规则 2：casefold（同样按码点独立进行）
    for part in unicodedata.normalize("NFKD", ch).casefold():
        category = unicodedata.category(part)
        # 规则 3：删除 Mn / Mc / Me
        if category in _MARK_CATEGORIES:
            continue
        # 规则 4：其余字符中的空白统一为 ASCII 空格
        if part.isspace():
            out.append(" ")
        else:
            out.append(part)
    return out


class IncrementalNormalizer:
    """增量规范化器：逐码点喂入，逐输出码点回调来源区间。

    只保留极少量跨块状态（当前空格折叠段），不保存任何历史输出，
    可以安全地在远大于内存的输入上运行。

    sink 接口：sink(norm_char: str, byte_start: int, byte_end: int) -> None
    """

    __slots__ = ("_sink", "_space_open", "_space_end", "_space_seen")

    def __init__(self, sink) -> None:
        self._sink = sink
        # 正在折叠、尚未发出的空格段
        self._space_open: int | None = None  # 空格段第一个原始码点字节起点
        self._space_end: int = 0             # 空格段当前覆盖到的字节终点
        self._space_seen: bool = False

    def feed(self, ch: str, byte_start: int, byte_end: int) -> None:
        # ASCII 快路径：NFKD/casefold 对 ASCII 仅改变 A-Z 大小写，
        # 且 ASCII 中只有 0x09~0x0D 与 0x20 是空白，无标记类码点。
        o = ord(ch)
        if o < 0x80:
            if 0x41 <= o <= 0x5A:
                self._flush_space()
                self._sink(chr(o + 0x20), byte_start, byte_end)
            elif o in (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20):
                self._extend_space(byte_start, byte_end)
            else:
                self._flush_space()
                self._sink(ch, byte_start, byte_end)
            return

        parts = transform_codepoint(ch)
        if not parts:
            # 整个码点被规则 3 删除（例如孤立组合重音）：它不产生输出，
            # 也不打断空白折叠段。其字节是否进入命中区间，完全取决于
            # 命中的首尾来源是否把它夹在中间。
            return
        if all(part == " " for part in parts):
            # 该码点（可能展开为多个空白）整体并入当前空格段
            self._extend_space(byte_start, byte_end)
            return
        # 至少有一个非空格输出：先结束此前的空格段
        self._flush_space()
        for part in parts:
            if part == " ":
                # 同一码点内部先出现的空格，与后续空格并为一段，
                # 来源都属于当前原始码点
                self._extend_space(byte_start, byte_end)
            else:
                self._flush_space()
                self._sink(part, byte_start, byte_end)

    def finish(self) -> None:
        """输入结束：发出尾部空格（若有）。首尾空格均保留。"""
        self._flush_space()

    def _extend_space(self, byte_start: int, byte_end: int) -> None:
        if not self._space_seen:
            self._space_open = byte_start
            self._space_seen = True
        self._space_end = byte_end

    def _flush_space(self) -> None:
        if self._space_seen:
            # 折叠空格的来源覆盖整段原始空白（含中间被删除的标记）
            self._sink(" ", self._space_open, self._space_end)
            self._space_seen = False
            self._space_open = None
            self._space_end = 0


def normalize_keyword(keyword: str) -> str:
    """对关键词应用同一套规则。调用方负责拒绝规范化后为空的词。

    关键词的字节来源不参与定位，这里只需要规范化字符串本身。
    """
    pieces: list[str] = []
    normalizer = IncrementalNormalizer(lambda ch, _s, _e: pieces.append(ch))
    for ch in keyword:
        normalizer.feed(ch, 0, len(ch.encode("utf-8")))
    normalizer.finish()
    return "".join(pieces)
