"""端到端验证器。

覆盖：
1. 规则小语料：朴素参考匹配器 vs 流式引擎；
2. 分块长度 1/2/3/7/4096/1MiB 的结果必须逐字节一致（跨块码点、
   组合符号、连续空白、匹配跨块全部被覆盖）；
3. 每条命中**实际读取原字节区间**核查：边界对齐码点、严格可解码，
   且切片规范化中存在一段连续输出恰为关键词、其来源边界与记录的
   字节区间精确重合；小文件逐条核查，大文件流式逐行核查且内存有界；
4. 非法 UTF-8 在各种分块下报同一绝对偏移，且结果文件不发布、
   临时文件被清理（原子性）；
5. 非法配置必须失败；
6. 专项断言：德语展开、预组合/分解重音、全角、中文、多空白首尾
   保留、重叠命中、同形不同编号、同一来源字节产生多个命中；
7. >128 MiB 合成文本上的子进程峰值 RSS（ru_maxrss，只统计进程
   自身驻留集，不含内核页缓存）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from .automaton import AhoCorasick
from .codec import InvalidUTF8
from .config import ConfigError, load_patterns
from .engine import stream_match
from .naive import naive_match, naive_normalize
from .normalize import IncrementalNormalizer

SMALL_CHUNK_SIZES = (1, 2, 3, 7, 4096)
BIG_CHUNK_SIZES = (4096, 1 << 16, 1 << 20)


class VerifyFailure(AssertionError):
    pass


def _load_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            rows.append(json.loads(line))
    return rows


def _assert_order(rows: list[dict], label: str) -> None:
    keys = [(r["norm_start"], r["norm_end"], r["pattern_id"]) for r in rows]
    if keys != sorted(keys):
        raise VerifyFailure(f"{label}: 输出未按 (起点,终点,pattern_id) 排序")
    for r in rows:
        if not (r["byte_start"] < r["byte_end"] and r["norm_start"] < r["norm_end"]):
            raise VerifyFailure(f"{label}: 存在空区间命中 {r}")


def _run_stream(input_path: str, automaton: AhoCorasick, chunk_size: int,
                out_path: str) -> int:
    with open(input_path, "rb") as fh:
        return stream_match(fh, automaton, out_path, chunk_size)


def _normalize_with_sources(raw: bytes):
    """严格解码并规范化一段字节，返回 (规范化字符串, 起点列表, 终点列表)。"""
    text = raw.decode("utf-8")
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    offset = 0
    normalizer = IncrementalNormalizer(
        lambda ch, s, e: (chars.append(ch), starts.append(s), ends.append(e))
    )
    for ch in text:
        length = len(ch.encode("utf-8"))
        normalizer.feed(ch, offset, offset + length)
        offset += length
    normalizer.finish()
    return "".join(chars), starts, ends


def _check_evidence(input_path: str, rows: list[dict],
                    norm_words: dict[str, str], label: str) -> None:
    """逐条命中读取原字节区间并核查证据。

    命中按规范化起点排序，字节起点单调不减（同起点的多个同形编号
    共享相同字节区间，会重新读取同一片证据），因此大文件上读指针
    只在极小范围内回退，不缓存文件内容。

    证据判据（对应引擎的区间构造规则）：
    1. 区间严格可按 UTF-8 解码（边界对齐码点）；
    2. 切片规范化文本中存在一段连续输出恰为命中关键词；
    3. 该段首输出来源字节起点 = 区间起点，末输出来源字节终点 = 区间终点。
    边界原始码点的其它展开输出（如 ß 展开 ss 中只命中一个 s）不参与
    命中，因此不要求整段规范化文本等于关键词。
    """
    size = os.path.getsize(input_path)
    last_start = -1
    with open(input_path, "rb") as fh:
        for index, rec in enumerate(rows):
            bs, be = rec["byte_start"], rec["byte_end"]
            if bs < last_start or be > size or bs >= be:
                raise VerifyFailure(
                    f"{label}: 第 {index} 条字节区间非法 {rec}"
                    f"（last_start={last_start}, size={size}）"
                )
            last_start = bs
            fh.seek(bs)
            raw = fh.read(be - bs)
            try:
                rebuilt, starts, ends = _normalize_with_sources(raw)
            except UnicodeDecodeError as exc:
                raise VerifyFailure(
                    f"{label}: 第 {index} 条证据区间不是合法 UTF-8 切片：{rec} {exc}"
                ) from None
            word = norm_words[rec["pattern_id"]]
            wlen = len(word)
            search_from = 0
            found = False
            while True:
                at = rebuilt.find(word, search_from)
                if at < 0:
                    break
                # 来源是切片内相对字节，与区间比较时加回 bs
                if (
                    bs + starts[at] == bs
                    and bs + ends[at + wlen - 1] == be
                ):
                    found = True
                    break
                search_from = at + 1
            if not found:
                raise VerifyFailure(
                    f"{label}: 第 {index} 条证据不覆盖关键词 {word!r}，"
                    f"切片规范化为 {rebuilt!r}，记录 {rec}"
                )


def _iter_jsonl(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            yield json.loads(line)


def _check_evidence_streaming(input_path: str, result_path: str,
                              norm_words: dict[str, str], label: str) -> int:
    """流式逐条核查证据并校验全局排序，不把结果集载入内存。

    与 _check_evidence 判据相同（严格可解码切片 + 来源边界精确覆盖），
    额外在流式过程中验证 (norm_start, norm_end, pattern_id) 单调有序。
    返回命中条数。
    """
    size = os.path.getsize(input_path)
    last_key = None
    last_start = -1
    count = 0
    with open(input_path, "rb") as fh:
        for rec in _iter_jsonl(result_path):
            key = (rec["norm_start"], rec["norm_end"], rec["pattern_id"])
            if last_key is not None and key < last_key:
                raise VerifyFailure(f"{label}: 输出未全局排序，{last_key} -> {key}")
            last_key = key
            bs, be = rec["byte_start"], rec["byte_end"]
            if bs < last_start or be > size or bs >= be:
                raise VerifyFailure(f"{label}: 字节区间非法 {rec}")
            last_start = bs
            fh.seek(bs)
            raw = fh.read(be - bs)
            try:
                rebuilt, starts, ends = _normalize_with_sources(raw)
            except UnicodeDecodeError as exc:
                raise VerifyFailure(
                    f"{label}: 证据区间不是合法 UTF-8 切片：{rec} {exc}"
                ) from None
            word = norm_words[rec["pattern_id"]]
            wlen = len(word)
            search_from = 0
            found = False
            while True:
                at = rebuilt.find(word, search_from)
                if at < 0:
                    break
                if bs + starts[at] == bs and bs + ends[at + wlen - 1] == be:
                    found = True
                    break
                search_from = at + 1
            if not found:
                raise VerifyFailure(
                    f"{label}: 证据不覆盖关键词 {word!r}，切片规范化 {rebuilt!r}，记录 {rec}"
                )
            count += 1
    return count


def _assert_files_identical(path_a: str, path_b: str, label: str) -> None:
    """两个 JSON Lines 结果逐行完全一致（流式，内存有界）。"""
    with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
        for line_no, (la, lb) in enumerate(zip(fa, fb), 1):
            if la != lb:
                raise VerifyFailure(f"{label}: 第 {line_no} 行不一致")
        if fa.readline() or fb.readline():
            raise VerifyFailure(f"{label}: 行数不一致")


def verify(data_dir: str) -> dict:
    report: dict = {"checks": [], "failures": []}

    def ok(name: str, detail: str = "") -> None:
        report["checks"].append({"name": name, "detail": detail})
        print(f"  [PASS] {name} {detail}")

    patterns = load_patterns(os.path.join(data_dir, "keywords.json"))
    norm_words = dict(patterns)
    automaton = AhoCorasick(patterns)
    manifest = json.load(open(os.path.join(data_dir, "manifest.json"), encoding="utf-8"))

    work_dir = tempfile.mkdtemp(prefix="normmatch-verify-", dir=data_dir)

    # ---------- 1+2+3. 小语料：朴素核对、分块一致、逐条证据 ----------
    for name in manifest["text_files"]:
        input_path = os.path.join(data_dir, name)
        with open(input_path, "rb") as fh:
            normalized = naive_normalize(fh)
        expected = naive_match(patterns, normalized)
        _assert_order(expected, f"naive:{name}")

        outputs = []
        for chunk in SMALL_CHUNK_SIZES + (1 << 20,):
            out_path = os.path.join(work_dir, f"{name}.{chunk}.jsonl")
            count = _run_stream(input_path, automaton, chunk, out_path)
            rows = _load_jsonl(out_path)
            if count != len(rows):
                raise VerifyFailure(f"{name}: 返回计数 {count} 与行数 {len(rows)} 不符")
            outputs.append((chunk, rows))

        base_chunk, base_rows = outputs[0]
        if len(base_rows) != len(expected):
            raise VerifyFailure(
                f"{name}: 流式 {len(base_rows)} 条 != 朴素 {len(expected)} 条"
            )
        for chunk, rows in outputs:
            if rows != base_rows:
                raise VerifyFailure(f"{name}: 分块 {chunk} 与分块 {base_chunk} 结果不一致")
            if rows != expected:
                raise VerifyFailure(f"{name}: 分块 {chunk} 与朴素参考结果不一致")
        _assert_order(base_rows, name)
        _check_evidence(input_path, base_rows, norm_words, name)
        ok(f"小语料 {name}", f"{len(base_rows)} 条命中，分块 {SMALL_CHUNK_SIZES + (1 << 20,)} 一致")

    # ---------- 3b. 首尾空格保留专项 ----------
    spaces_path = os.path.join(data_dir, "text_spaces.txt")
    with open(spaces_path, "rb") as fh:
        spaces_norm = naive_normalize(fh)
    norm_text = "".join(spaces_norm.chars)
    if not norm_text.startswith(" ") or not norm_text.endswith(" "):
        raise VerifyFailure("首尾空格被错误剥离")
    if "  " in norm_text:
        raise VerifyFailure("连续空格未折叠为一个")
    # 折叠空格的来源必须覆盖整段空白（含 tab/全角空格/NEL）
    first_space_end = spaces_norm.ends[0]
    raw_prefix = open(spaces_path, "rb").read(first_space_end)
    if b"  " not in raw_prefix:
        raise VerifyFailure("首段折叠空格来源未覆盖完整空白串")
    ok("空白规则", "首尾空格保留、连续空白折叠且来源覆盖整段")

    # ---------- 4. 非法 UTF-8：偏移一致 + 原子性 ----------
    for name, spec in manifest["invalid_files"].items():
        input_path = os.path.join(data_dir, name)
        out_path = os.path.join(work_dir, f"{name}.out.jsonl")
        for chunk in SMALL_CHUNK_SIZES:
            try:
                _run_stream(input_path, automaton, chunk, out_path)
            except InvalidUTF8 as exc:
                if exc.offset != spec["offset"]:
                    raise VerifyFailure(
                        f"{name}: 分块 {chunk} 报偏移 {exc.offset}，期望 {spec['offset']}"
                    )
                if spec["reason_contains"] not in exc.reason:
                    raise VerifyFailure(f"{name}: 报错原因不符：{exc.reason}")
            else:
                raise VerifyFailure(f"{name}: 分块 {chunk} 未检测出非法 UTF-8")
            if os.path.exists(out_path):
                raise VerifyFailure(f"{name}: 失败后仍发布了结果文件，违反原子性")
        leftovers = [n for n in os.listdir(work_dir) if n.startswith(".normmatch.")]
        if leftovers:
            raise VerifyFailure(f"{name}: 残留临时文件 {leftovers}")
        ok(f"非法 UTF-8 {name}", f"各分块均报绝对偏移 {spec['offset']}，结果未发布")

    # ---------- 5. 非法配置 ----------
    for bad in ("config_bad_empty.json", "config_bad_dupid.json", "config_bad_structure.json"):
        try:
            load_patterns(os.path.join(data_dir, bad))
        except ConfigError:
            pass
        else:
            raise VerifyFailure(f"{bad} 应当配置失败")
        ok(f"非法配置 {bad}", "已拒绝")

    # ---------- 6. 专项语义断言 ----------
    german_rows = _load_jsonl(os.path.join(work_dir, "text_german.txt.1.jsonl"))
    same_source = {}
    for r in german_rows:
        same_source.setdefault((r["byte_start"], r["byte_end"]), set()).add(
            (r["pattern_id"], r["norm_start"], r["norm_end"])
        )
    if not any(len(v) >= 2 for v in same_source.values()):
        raise VerifyFailure("缺少同一原始字节区间产生多个命中的场景")
    ids_pair = {r["pattern_id"] for r in german_rows
                if r["pattern_id"] in ("german", "german_lower")}
    if ids_pair != {"german", "german_lower"}:
        raise VerifyFailure("规范化同形的不同编号未分别报告")
    # 同形编号必须具有完全相同的规范化区间集合
    g1 = sorted((r["norm_start"], r["norm_end"]) for r in german_rows if r["pattern_id"] == "german")
    g2 = sorted((r["norm_start"], r["norm_end"]) for r in german_rows
                if r["pattern_id"] == "german_lower")
    if g1 != g2 or not g1:
        raise VerifyFailure("同形编号命中区间集合不一致")
    ok("德语展开/同形编号/同来源多命中", f"{len(german_rows)} 条")

    overlap_rows = _load_jsonl(os.path.join(work_dir, "text_overlap.txt.1.jsonl"))
    intervals = sorted({(r["norm_start"], r["norm_end"]) for r in overlap_rows})
    if not any(a != b and a[0] < b[0] < a[1] for a in intervals for b in intervals):
        raise VerifyFailure("缺少重叠命中")
    ok("重叠命中", f"{len(intervals)} 个不同区间")

    for corpus, needed in (
        ("text_precomposed.txt", {"cafe", "cafe_decomp"}),
        ("text_decomposed.txt", {"cafe", "cafe_decomp"}),
        ("text_fullwidth.txt", {"abc"}),
        ("text_chinese.txt", {"beijing", "shanghai"}),
        ("text_ligature.txt", {"file"}),
        ("text_marks.txt", {"hindi", "greek", "xyz"}),
    ):
        rows = _load_jsonl(os.path.join(work_dir, f"{corpus}.1.jsonl"))
        got = {r["pattern_id"] for r in rows}
        missing = needed - got
        if missing:
            raise VerifyFailure(f"{corpus} 缺少命中 {missing}，实际 {got}")
        ok(f"规则语料 {corpus}", f"命中编号 {sorted(got)}")

    # Me 标记位于命中之后不得扩张区间
    marks_rows = _load_jsonl(os.path.join(work_dir, "text_marks.txt.1.jsonl"))
    raw_marks = open(os.path.join(data_dir, "text_marks.txt"), "rb").read()
    xyz = [r for r in marks_rows if r["pattern_id"] == "xyz"][0]
    if raw_marks[xyz["byte_start"]:xyz["byte_end"]].decode("utf-8") != "xyz":
        raise VerifyFailure("命中之外的 Me 标记错误扩张了字节区间")
    greek = [r for r in marks_rows if r["pattern_id"] == "greek"][0]
    greek_slice = raw_marks[greek["byte_start"]:greek["byte_end"]].decode("utf-8")
    if "҈" not in greek_slice:
        raise VerifyFailure("夹在命中内部的 Me 标记未被区间覆盖")
    ok("标记区间规则", "内部标记覆盖、外部标记不扩张")

    # ---------- 7. 大文件：分块一致 + 流式逐条证据 + 峰值 RSS ----------
    big_spec = manifest["big_file"]
    big_name = big_spec["name"]
    big_path = os.path.join(data_dir, big_name)
    big_size = os.path.getsize(big_path)
    if big_size <= 128 * 1024 * 1024:
        raise VerifyFailure("合成文本未超过 128 MiB")

    # 大文件使用独立的稀疏长词配置（验证器与 RSS 测量共用）
    big_patterns = load_patterns(os.path.join(data_dir, big_spec["config"]))
    big_norm_words = dict(big_patterns)
    big_automaton = AhoCorasick(big_patterns)

    big_out_paths: list[tuple[int, str]] = []
    for chunk in BIG_CHUNK_SIZES:
        out_path = os.path.join(work_dir, f"big.{chunk}.jsonl")
        count = _run_stream(big_path, big_automaton, chunk, out_path)
        big_out_paths.append((chunk, out_path))
        ok(f"大文件分块 {chunk}", f"{count} 条命中")
    ref_chunk, ref_path = big_out_paths[0]
    for chunk, out_path in big_out_paths[1:]:
        _assert_files_identical(ref_path, out_path, f"big:{ref_chunk} vs {chunk}")
    ok("大文件分块一致", "4096/65536/1048576 三种分块逐字节相同")

    big_hits = _check_evidence_streaming(big_path, ref_path, big_norm_words, "big")
    ok("大文件流式逐条证据", f"{big_hits} 条命中全部核查通过且全局有序")

    # 跨块重叠专项：块边界形成的 4 连 a 必须产生跨边界的 aa/aaa 重叠命中
    cross = 0
    block_bytes = big_spec["block_bytes"]
    for rec in _iter_jsonl(ref_path):
        if rec["pattern_id"] not in ("aa", "aaa"):
            continue
        # 命中字节区间跨越某个 16KiB 块边界
        boundary = (rec["byte_start"] // block_bytes + 1) * block_bytes
        if rec["byte_start"] < boundary < rec["byte_end"]:
            cross += 1
    if cross == 0:
        raise VerifyFailure("未检测到跨块（亦跨读取块）的重叠命中")
    ok("跨块重叠命中", f"{cross} 条命中跨越 16KiB 块边界")

    rss_measurements = _measure_peak_rss(big_path, work_dir, big_spec["config"])
    for chunk, kib in rss_measurements:
        mib = kib / 1024
        # 文件本身 140MiB；若把全文读进内存，RSS 必然接近或超过文件大小
        if mib >= big_size / (1024 * 1024):
            raise VerifyFailure(f"峰值 RSS {mib:.1f} MiB 异常，疑似缓存全文")
        report.setdefault("peak_rss_mib", []).append(
            {"chunk_size": chunk, "peak_rss_mib": round(mib, 2)}
        )
        ok(f"峰值 RSS（chunk={chunk}）", f"{mib:.2f} MiB（文件 {big_size / 1024 / 1024:.0f} MiB）")

    report["total_hits_small"] = sum(
        len(_load_jsonl(os.path.join(work_dir, f"{n}.1.jsonl")))
        for n in manifest["text_files"]
    )
    report["total_hits_big"] = big_hits
    report["work_dir"] = work_dir
    return report


def _measure_peak_rss(input_path: str, work_dir: str, config_name: str) -> list[tuple[int, int]]:
    """子进程运行真实匹配引擎，自报 RUSAGE_SELF.ru_maxrss（Linux 单位 KiB）。

    ru_maxrss 只统计该进程自身的驻留物理页；内核为文件读取维护的
    页缓存属于内核内存，不计入进程 RSS，因此不会被当作应用内存。
    """
    config_path = os.path.join(os.path.dirname(input_path), config_name)

    measurements = []
    for chunk in (4096, 1 << 20):
        out_path = os.path.join(work_dir, f"rss.{chunk}.jsonl")
        code = (
            "import resource, sys;"
            "from normmatch.config import load_patterns;"
            "from normmatch.automaton import AhoCorasick;"
            "from normmatch.engine import stream_match;"
            f"p=load_patterns({config_path!r});"
            "a=AhoCorasick(p);"
            f"f=open({input_path!r},'rb');"
            f"n=stream_match(f,a,{out_path!r},{chunk});"
            "f.close();"
            "print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss);"
            "print(n)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            raise VerifyFailure(f"RSS 子进程失败：{proc.stderr}")
        lines = proc.stdout.strip().splitlines()
        measurements.append((chunk, int(lines[0])))
    return measurements
