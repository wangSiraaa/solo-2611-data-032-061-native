"""测试语料与关键词配置生成器。

在输出目录下生成：
* keywords.json            合法关键词配置（含规范化后同形的不同编号）
* config_bad_*.json        各类非法配置（空关键词、重复 id、结构错误）
* text_*.txt               覆盖各规则的小语料（UTF-8）
* invalid_*.bin            非法 UTF-8 样本，manifest 记录绝对字节偏移
* big.bin                  >128 MiB 的合成文本（流式写出，不占大内存）
* manifest.json            样本说明与非法偏移期望值
"""

from __future__ import annotations

import json
import os

# 大文件目标尺寸（超过 128 MiB）
BIG_TARGET_BYTES = 140 * 1024 * 1024

# 填充语：刻意不含任何大文件关键词，保证命中稀疏、输出规模有界
_BIG_FILLER = (
    "lorem ipsum dolor amet enim veniam quis nostrud exercitation ullamco "
    "laboris nisi aliquip commodo consequat duis aute irure reprehenderit "
    "voluptate velit esse cillum fugiat nulla pariatur excepteur sint "
    "occaecat cupidatat proident sunt culpa officia deserunt mollit anim "
)
# 每块固定携带的“特征 token”，覆盖全部规范化场景；块首与块尾各两个
# a，块与块拼接后边界形成 4 连 a，强制重叠匹配跨块（块边界与读取块
# 边界不对齐，因而也跨读取块）
_BIG_TOKENS = (
    "Strasse CAFÉ ＡＢＣ 北京 上海 किताब ﬁle "
    "Vollmondglühbirne beta\t\u3000\ngamma "
)
_BIG_BLOCK_BYTES = 16384


def _build_big_block() -> str:
    # 块首两个 a
    block = "aa" + _BIG_TOKENS
    while len(block.encode("utf-8")) < _BIG_BLOCK_BYTES - 2:
        block += _BIG_FILLER
    # 块尾两个 a：与下一块块首拼成跨块 4 连 a
    return block + "aa"


# 大文件专用关键词（稀疏长词，不含单字符词，保证结果规模有界）
BIG_PATTERNS = [
    {"pattern_id": "german", "keyword": "STRASSE"},
    {"pattern_id": "cafe", "keyword": "café"},
    {"pattern_id": "abc", "keyword": "Abc"},
    {"pattern_id": "beijing", "keyword": "北京"},
    {"pattern_id": "shanghai", "keyword": "上海"},
    {"pattern_id": "hindi", "keyword": "किताब"},
    {"pattern_id": "file", "keyword": "file"},
    {"pattern_id": "vollmond", "keyword": "Vollmondglühbirne"},
    {"pattern_id": "beta", "keyword": "beta"},
    {"pattern_id": "gamma", "keyword": "GAMMA"},
    {"pattern_id": "aa", "keyword": "aa"},
    {"pattern_id": "aaa", "keyword": "aaa"},
]


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def generate(out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    manifest: dict = {"invalid_files": {}, "text_files": [], "big_file": None}

    # ---------- 合法关键词配置 ----------
    patterns = [
        {"pattern_id": "german", "keyword": "STRASSE"},          # ß/ẞ 展开 ss
        {"pattern_id": "german_lower", "keyword": "strasse"},    # 与 german 规范化同形
        {"pattern_id": "cafe", "keyword": "CAFÉ"},               # 预组合重音
        {"pattern_id": "cafe_decomp", "keyword": "café"},  # 分解写法，同形
        {"pattern_id": "abc", "keyword": "Abc"},                 # 全角命中
        {"pattern_id": "beijing", "keyword": "北京"},
        {"pattern_id": "shanghai", "keyword": "上海"},
        {"pattern_id": "hindi", "keyword": "किताब"},             # 含 Mc 元音符号
        {"pattern_id": "alpha", "keyword": "Alpha"},             # 多空白折叠
        {"pattern_id": "beta", "keyword": "beta"},
        {"pattern_id": "gamma", "keyword": "GAMMA"},
        {"pattern_id": "aa", "keyword": "aa"},                   # 重叠词
        {"pattern_id": "aaa", "keyword": "aaa"},
        {"pattern_id": "a", "keyword": "a"},
        {"pattern_id": "file", "keyword": "file"},               # 连字 ﬁ -> fi
        {"pattern_id": "s", "keyword": "s"},                     # ß 展开后的同来源多命中
        {"pattern_id": "ss", "keyword": "ss"},
        {"pattern_id": "greek", "keyword": "αβγ"},               # Me 标记夹在命中内
        {"pattern_id": "xyz", "keyword": "xyz"},                 # Me/重音位于命中之外
    ]
    with open(os.path.join(out_dir, "keywords.json"), "w", encoding="utf-8") as fh:
        json.dump({"patterns": patterns}, fh, ensure_ascii=False, indent=2)

    # ---------- 非法配置 ----------
    with open(os.path.join(out_dir, "config_bad_empty.json"), "w", encoding="utf-8") as fh:
        json.dump({"patterns": [
            {"pattern_id": "ok", "keyword": "hello"},
            # 仅由组合标记构成：规范化后为空
            {"pattern_id": "empty", "keyword": "́ं̈"},
        ]}, fh, ensure_ascii=False)
    with open(os.path.join(out_dir, "config_bad_dupid.json"), "w", encoding="utf-8") as fh:
        json.dump({"patterns": [
            {"pattern_id": "x", "keyword": "foo"},
            {"pattern_id": "x", "keyword": "bar"},
        ]}, fh, ensure_ascii=False)
    with open(os.path.join(out_dir, "config_bad_structure.json"), "w",
              encoding="utf-8") as fh:
        fh.write("{not valid json")

    # ---------- 小语料 ----------
    texts: dict[str, str] = {
        # 德语：小写 ß 与大写 ẞ 均展开为 ss；同一来源字节产生多个命中
        "text_german.txt": "Straße und ẞSTRASSE\n",
        # 预组合 é
        "text_precomposed.txt": "hier ist ein CAFÉ maison\n",
        # 分解 é（e + U+0301），且命中之外有独立重音
        "text_decomposed.txt": "café́ und xyź ende\n",
        # 全角：NFKD + casefold -> abc
        "text_fullwidth.txt": "ＡＢＣ und ａｂｃ\n",
        # 中文
        "text_chinese.txt": "北京到上海，北京再见\n",
        # 多空白：tab/换行/全角空格/NEL(U+0085)，折叠为一个；首尾空格保留
        "text_spaces.txt": "  alpha\t　\n  beta\u0085gamma  \n",
        # 重叠与同形编号
        "text_overlap.txt": "aaaa aaaaa\n",
        # 连字 ﬁ -> fi
        "text_ligature.txt": "eine ﬁle datei\n",
        # 天城文（Mc 被删除）与 Me 标记夹在命中内/外
        "text_marks.txt": "किताब und αβ҈γ danach xyz҈\n",
        # 空文件
        "text_empty.txt": "",
    }
    for name, content in texts.items():
        _write(os.path.join(out_dir, name), content.encode("utf-8"))
        manifest["text_files"].append(name)

    # ---------- 非法 UTF-8（手工拼装，记录期望偏移） ----------
    def invalid_file(name: str, prefix: bytes, bad: bytes, expected: int, reason: str) -> None:
        _write(os.path.join(out_dir, name), prefix + bad)
        manifest["invalid_files"][name] = {"offset": expected, "reason_contains": reason}

    invalid_file(
        "invalid_continuation.bin", b"abc\xc3\x9fxy", b"\xc3(", 7,
        "invalid continuation byte",
    )
    invalid_file(
        "invalid_stray.bin", b"abc\xc3\x9fxy", b"\x90", 7,
        "invalid start byte",
    )
    invalid_file(
        "invalid_truncated_eof.bin", b"abc\xc3\x9fxy", b"\xc3", 7,
        "UTF-8 序列被截断",
    )
    invalid_file(
        "invalid_overlong.bin", b"abc", b"\xc0\x80def", 3,
        "invalid start byte",
    )
    invalid_file(
        "invalid_surrogate.bin", b"abc", b"\xed\xa0\x80def", 3,
        "invalid continuation byte",
    )
    # 截断点恰好落在读取块边界：3 字节序列只给出前两个字节，
    # 用 1 字节分块读取验证跨块等待与最终报错偏移
    invalid_file(
        "invalid_truncated_split.bin", b"abc", b"\xe4\xb8", 3,
        "UTF-8 序列被截断",
    )

    # ---------- 大文件专用关键词（稀疏长词） ----------
    big_config_path = os.path.join(out_dir, "keywords_big.json")
    with open(big_config_path, "w", encoding="utf-8") as fh:
        json.dump({"patterns": BIG_PATTERNS}, fh, ensure_ascii=False, indent=2)

    # ---------- 大文件（流式写出，逐块构造，不占大内存） ----------
    big_path = os.path.join(out_dir, "big.bin")
    block = _build_big_block().encode("utf-8")
    written = 0
    with open(big_path, "wb") as fh:
        while written < BIG_TARGET_BYTES:
            fh.write(block)
            written += len(block)
    manifest["big_file"] = {
        "name": "big.bin",
        "bytes": os.path.getsize(big_path),
        "block_bytes": len(block),
        "config": "keywords_big.json",
    }

    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return out_dir
