"""大于 64 MiB、仅少量插入的端到端演示与指标采集。

报告：补丁体积、复用字节、强校验次数、峰值 RSS、各阶段耗时，
并用逐字节比对与 SHA256 验证正确性（不以压缩率作为唯一正确性判断）。
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import time
from pathlib import Path

from bdelta.apply import apply_patch
from bdelta.delta import make_delta
from bdelta.generator import LARGE_SIZE, make_large_pair
from bdelta.signature import build_signature


def _rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def run(work_dir: str, size: int, block_size: int) -> dict:
    d = Path(work_dir)
    d.mkdir(parents=True, exist_ok=True)
    rss_before = _rss_mib()

    t0 = time.perf_counter()
    pair = make_large_pair(str(d), size)
    t_gen = time.perf_counter() - t0

    sig = d / "large.sig"
    patch = d / "large.patch"
    out = d / "large.out"

    rss = {}
    rss["after_generate_mib"] = round(_rss_mib(), 3)

    t0 = time.perf_counter()
    sig_stats = build_signature(pair["old_path"], str(sig), block_size)
    t_sig = time.perf_counter() - t0
    rss["after_signature_mib"] = round(_rss_mib(), 3)

    t0 = time.perf_counter()
    delta_stats = make_delta(str(sig), pair["new_path"], str(patch))
    t_delta = time.perf_counter() - t0
    rss["after_delta_mib"] = round(_rss_mib(), 3)

    t0 = time.perf_counter()
    apply_stats = apply_patch(pair["old_path"], str(patch), str(out))
    t_apply = time.perf_counter() - t0
    rss["after_apply_mib"] = round(_rss_mib(), 3)

    # 逐字节正确性（流式比对，不把整文件装入内存；不只看压缩率）。
    byte_equal = True
    chunk_n = 1 << 20
    with open(pair["new_path"], "rb") as ef, open(out, "rb") as af:
        while True:
            ec, ac = ef.read(chunk_n), af.read(chunk_n)
            if ec != ac:
                byte_equal = False
                break
            if not ec:
                break
    peak_rss = max(_rss_mib(), rss_before)

    report = {
        "block_size": block_size,
        "old_size": pair["old_size"],
        "new_size": pair["new_size"],
        "inserted_bytes": pair["inserted_bytes"],
        "signature_blocks": sig_stats["block_count"],
        "signature_size_bytes": sig.stat().st_size,
        "patch_size_bytes": patch.stat().st_size,
        "reused_copy_bytes": delta_stats.copy_bytes,
        "add_bytes": delta_stats.add_bytes,
        "reuse_ratio": round(delta_stats.copy_bytes / pair["new_size"], 6),
        "strong_checks": delta_stats.strong_checks,
        "weak_hits": delta_stats.weak_hits,
        "bytes_scanned": delta_stats.bytes_scanned,
        "copy_instructions": delta_stats.copy_instructions,
        "add_chunks": delta_stats.add_chunks,
        "peak_rss_mib": round(peak_rss, 3),
        "rss_checkpoints_mib": rss,
        "timing_seconds": {
            "generate": round(t_gen, 3),
            "signature": round(t_sig, 3),
            "delta": round(t_delta, 3),
            "apply": round(t_apply, 3),
        },
        "rebuild_byte_identical": byte_equal,
        "rebuild_sha256": apply_stats["new_sha256"],
    }
    if not byte_equal:
        raise AssertionError("大文件重建结果与目标不一致")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="bdelta 大文件 (>64MiB) 演示")
    ap.add_argument("--dir", default=".runtime/large")
    ap.add_argument("--size", type=int, default=LARGE_SIZE)
    ap.add_argument("--block-size", type=int, default=4096)
    args = ap.parse_args()
    report = run(args.dir, args.size, args.block_size)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
