"""朴素参考匹配器（仅用于验证小数据）。

与流式引擎共用同一套逐码点规范化逻辑，但：
* 一次性读入并严格解码整个文件（小数据）；
* 保留完整规范化字符串与每个输出码点的字节来源；
* 对每个模式做朴素子串扫描（允许重叠）；
* 输出按 (起点, 终点, pattern_id 码点序) 排序。

它与流式引擎是两套独立实现（不同的扫描与排序结构），结果一致
才能互相印证；证据字节范围的计算规则保持一致。
"""

from __future__ import annotations

from .codec import iter_codepoints
from .normalize import IncrementalNormalizer


class NaiveResult:
    def __init__(self) -> None:
        self.chars: list[str] = []
        self.starts: list[int] = []
        self.ends: list[int] = []

    def add(self, ch: str, byte_start: int, byte_end: int) -> None:
        self.chars.append(ch)
        self.starts.append(byte_start)
        self.ends.append(byte_end)


def naive_normalize(fileobj, chunk_size: int = 1 << 20) -> NaiveResult:
    result = NaiveResult()
    normalizer = IncrementalNormalizer(result.add)
    for ch, byte_start, byte_end in iter_codepoints(fileobj, chunk_size):
        normalizer.feed(ch, byte_start, byte_end)
    normalizer.finish()
    return result


def naive_match(patterns: list[tuple[str, str]], normalized: NaiveResult) -> list[dict]:
    text = "".join(normalized.chars)
    hits: list[dict] = []
    for pattern_id, word in patterns:
        wlen = len(word)
        start = 0
        while True:
            idx = text.find(word, start)
            if idx < 0:
                break
            end = idx + wlen
            hits.append(
                {
                    "pattern_id": pattern_id,
                    "norm_start": idx,
                    "norm_end": end,
                    "byte_start": normalized.starts[idx],
                    "byte_end": normalized.ends[end - 1],
                }
            )
            start = idx + 1  # 允许重叠
    hits.sort(key=lambda h: (h["norm_start"], h["norm_end"], h["pattern_id"]))
    return hits
