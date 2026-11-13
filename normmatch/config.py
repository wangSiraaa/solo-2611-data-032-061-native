"""关键词配置加载与校验。

配置为 JSON：{"patterns": [{"pattern_id": "...", "keyword": "..."}, ...]}

约束（题面给定上限）：
* pattern_id 必须唯一、非空；keyword 必须是字符串；
* 关键词数量 <= 1000，规范化后单个词长 <= 4000，总长度 <= 100000；
* 关键词与文本使用完全相同的规范化规则；规范化后为空 -> 配置失败；
* 不同 pattern_id 允许规范化为同一个词，命中分别报告。
"""

from __future__ import annotations

import json

from .normalize import normalize_keyword

MAX_PATTERNS = 1000
MAX_TOTAL_NORM_LEN = 100_000
MAX_SINGLE_NORM_LEN = 4_000


class ConfigError(Exception):
    """关键词配置不合法。"""


def load_patterns(path: str) -> list[tuple[str, str]]:
    """加载配置，返回 [(pattern_id, 规范化关键词), ...]，保持配置顺序。"""
    with open(path, "r", encoding="utf-8") as fh:
        try:
            raw = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置不是合法 JSON：{exc}") from None

    if not isinstance(raw, dict) or not isinstance(raw.get("patterns"), list):
        raise ConfigError('配置顶层必须是对象，且含数组字段 "patterns"')

    entries = raw["patterns"]
    if len(entries) > MAX_PATTERNS:
        raise ConfigError(f"关键词数量 {len(entries)} 超过上限 {MAX_PATTERNS}")
    if not entries:
        raise ConfigError("至少需要一个关键词")

    patterns: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    total_len = 0

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"第 {index} 项不是对象")
        pattern_id = entry.get("pattern_id")
        keyword = entry.get("keyword")
        if not isinstance(pattern_id, str) or not pattern_id:
            raise ConfigError(f"第 {index} 项的 pattern_id 必须是非空字符串")
        if pattern_id in seen_ids:
            raise ConfigError(f"pattern_id 重复：{pattern_id!r}")
        if not isinstance(keyword, str):
            raise ConfigError(f"pattern_id={pattern_id!r} 的 keyword 必须是字符串")

        normalized = normalize_keyword(keyword)
        if not normalized:
            raise ConfigError(
                f"pattern_id={pattern_id!r} 的关键词规范化后为空，配置失败"
            )
        if len(normalized) > MAX_SINGLE_NORM_LEN:
            raise ConfigError(
                f"pattern_id={pattern_id!r} 规范化长度 {len(normalized)} "
                f"超过单词上限 {MAX_SINGLE_NORM_LEN}"
            )

        total_len += len(normalized)
        if total_len > MAX_TOTAL_NORM_LEN:
            raise ConfigError(
                f"规范化总长度超过上限 {MAX_TOTAL_NORM_LEN} 码点"
            )

        seen_ids.add(pattern_id)
        patterns.append((pattern_id, normalized))

    return patterns
