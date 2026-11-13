"""十万条合成记录的端到端演示脚本。

流程：
1. generate 生成 100,000 条 JSONL（含中文键）；
2. build 经 SQLite 外排构建可随机访问树与可信根清单；
3. 取一组查询键生成批量共享证明，与“逐条机械复制完整路径”的
   体积口径对比（不要求固定压缩比例，只展示实际复用效果）；
4. verify 仅凭可信清单 + 查询集合 + 证明完成独立验证。

所有产物落在 .runtime/demo/（已被 .gitignore 忽略），全程离线。
用法：python3.11 demo_100k.py
"""

from __future__ import annotations

import json
import os
import time

from merkle_dataset.cli import main as cli_main
from merkle_dataset.manifest import read_manifest
from merkle_dataset.proving import (
    generate_proof,
    naive_path_digest_count,
    proof_byte_size,
    read_proof,
    shared_node_count,
    write_proof,
)
from merkle_dataset.storage import load_tree
from merkle_dataset.verifier import verify_proof

DEMO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".runtime", "demo")
N = 100_000


def run(argv: list[str]) -> None:
    code = cli_main(argv)
    if code != 0:
        raise SystemExit(f"命令失败: {argv}")


def main() -> None:
    os.makedirs(DEMO_DIR, exist_ok=True)
    jsonl = os.path.join(DEMO_DIR, "data-100k.jsonl")
    db = os.path.join(DEMO_DIR, "tree-100k.db")
    manifest_path = os.path.join(DEMO_DIR, "manifest.json")
    queries_path = os.path.join(DEMO_DIR, "queries.json")
    proof_path = os.path.join(DEMO_DIR, "proof.json")

    t0 = time.perf_counter()
    run(["generate", "--count", str(N), "--output", jsonl])
    t1 = time.perf_counter()
    run(["build", "--input", jsonl, "--db", db, "--manifest", manifest_path])
    t2 = time.perf_counter()

    # 混合查询：存在键（含连续相邻键以制造共享）、最小键之前、最大键之后、键间缺失。
    with open(jsonl, encoding="utf-8") as f:
        all_keys = [json.loads(line)["key"] for line in f]
    queries = (
        [all_keys[0], all_keys[1], all_keys[2]]          # 连续相邻，路径大量共享
        + [all_keys[100], all_keys[101], all_keys[500]]
        + [all_keys[N // 2], all_keys[N // 2 + 1]]
        + [all_keys[-3], all_keys[-2], all_keys[-1]]
        + ["\x00-min-boundary", "zzz-max-boundary", "key-0000000500-gap"]
    )
    # 查询必须互不重复。
    queries = list(dict.fromkeys(queries))
    with open(queries_path, "w", encoding="utf-8") as f:
        json.dump(queries, f, ensure_ascii=False)

    manifest = read_manifest(manifest_path)
    with load_tree(db, expected_manifest=manifest) as tree:
        proof = generate_proof(tree, manifest, queries)
    write_proof(proof, proof_path)
    t3 = time.perf_counter()

    conclusions = verify_proof(manifest, queries, read_proof(proof_path))
    t4 = time.perf_counter()

    size = proof_byte_size(proof)
    shared = shared_node_count(proof)
    naive_digests = naive_path_digest_count(proof)
    # 逐条路径的对照口径：同样的 JSON 答案体积 + 未共享的全部摘要（按证明中的
    # 摘要编码开销折算），给出可比的字节估算。
    per_digest_overhead = len(json.dumps({"level": 0, "index": 0, "digest_hex": "ab" * 32}))
    naive_node_bytes = naive_digests * per_digest_overhead
    shared_node_bytes = sum(
        len(json.dumps(n, ensure_ascii=False, sort_keys=True).encode()) for n in proof["nodes"]
    )
    answer_bytes = sum(
        len(json.dumps(a, ensure_ascii=False, sort_keys=True).encode()) for a in proof["answers"]
    )
    naive_total = answer_bytes + naive_node_bytes

    print("\n================ 十万条记录演示结果 ================")
    print(f"记录数                 : {N}")
    print(f"generate 耗时          : {t1 - t0:6.2f}s")
    print(f"build（SQLite 外排）   : {t2 - t1:6.2f}s")
    print(f"prove（{len(queries):2d} 个查询）        : {t3 - t2:6.2f}s")
    print(f"verify（独立验证）     : {t4 - t3:6.2f}s")
    print(f"树数据库体积           : {os.path.getsize(db):,} 字节")
    print(f"输入 JSONL 体积        : {os.path.getsize(jsonl):,} 字节")
    print("----------------------------------------------------")
    print(f"查询数                 : {len(queries)}（存在 "
          f"{sum(1 for c in conclusions if c.present)} / 缺失 "
          f"{sum(1 for c in conclusions if not c.present)}）")
    print(f"共享证明体积（实际）   : {size:,} 字节")
    print(f"  其中答案部分         : {answer_bytes:,} 字节")
    print(f"  共享节点（去重后）   : {shared} 个 / {shared_node_bytes:,} 字节")
    print(f"逐条路径对照（不共享） : {naive_digests} 个摘要 / 估算 {naive_total:,} 字节")
    if naive_total:
        print(f"节点部分体积对比       : 共享 {shared_node_bytes:,}B vs 逐条 {naive_node_bytes:,}B"
              f"（减少 {1 - shared_node_bytes / naive_node_bytes:.1%}）")
    print("----------------------------------------------------")
    for c in conclusions:
        if c.present:
            print(f"  [存在] {c.query!r:28} -> 叶下标 {c.index}")
        else:
            p = f"#{c.predecessor['index']}" if c.predecessor else "∅"
            s = f"#{c.successor['index']}" if c.successor else "∅"
            print(f"  [缺失] {c.query!r:28} 前驱={p:>7} 后继={s:>7}")
    print("verify: 全部结论独立验证通过")


if __name__ == "__main__":
    main()
