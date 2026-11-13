"""命令行入口：generate / build / prove / verify / inspect。

所有路径都是本机文件，不接入任何网络服务。
退出码：0 成功；1 业务/IO 错误（含验证拒绝）；2 参数错误。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from typing import Optional

from .errors import MerkleError
from .hashing import tree_height
from .manifest import Manifest, read_manifest, write_manifest
from .proving import (
    generate_proof,
    naive_path_digest_count,
    per_query_bytes,
    proof_byte_size,
    read_proof,
    shared_node_count,
    write_proof,
)
from .storage import build_tree, load_tree
from .verifier import verify_proof


def _read_queries(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        queries = json.load(f)
    if not isinstance(queries, list):
        raise MerkleError("查询文件必须是 JSON 字符串数组")
    return queries


def _write_queries(queries: list[str], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(queries, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---- generate -----------------------------------------------------------

def cmd_generate(args: argparse.Namespace) -> int:
    if args.count < 0:
        raise MerkleError("记录数不能为负")
    rng = random.Random(args.seed)
    cjk = "数据集验证叶子摘要排序证明中文键值条目"
    with open(args.output, "w", encoding="utf-8") as f:
        for i in range(args.count):
            key = f"key-{i:010d}"
            if i % 7 == 0:  # 混入中文键，演示 UTF-8 字节序、不做归一化
                key = f"中文-{cjk[rng.randrange(len(cjk))]}-{i:08d}"
            value = f"value#{i:010d}-{rng.randrange(1 << 32):08x}"
            f.write(json.dumps({"key": key, "value": value}, ensure_ascii=False))
            f.write("\n")
    print(f"generate: 已写入 {args.count} 条合成记录 -> {args.output}")
    return 0


# ---- build --------------------------------------------------------------

def cmd_build(args: argparse.Namespace) -> int:
    manifest = build_tree(args.input, args.db)
    write_manifest(manifest, args.manifest)
    print(f"build: 构建成功 -> {args.db}")
    print(f"  record_count = {manifest.record_count}")
    print(f"  height       = {tree_height(manifest.record_count)}")
    print(f"  root         = {manifest.root.hex()}")
    print(f"  manifest     -> {args.manifest}（请经带外渠道交给验证者）")
    return 0


# ---- prove --------------------------------------------------------------

def cmd_prove(args: argparse.Namespace) -> int:
    manifest = read_manifest(args.manifest)
    queries = _read_queries(args.queries)
    with load_tree(args.db, expected_manifest=manifest) as tree:
        proof = generate_proof(tree, manifest, queries)
    write_proof(proof, args.output)

    size = proof_byte_size(proof)
    naive = naive_path_digest_count(proof)
    unique = shared_node_count(proof)
    print(f"prove: {len(queries)} 个查询 -> {args.output}")
    print(f"  证明体积                 = {size} 字节")
    print(f"  共享节点数（去重后）     = {unique}")
    print(f"  逐条路径摘要数（未共享） = {naive}")
    if naive:
        saved = 1.0 - unique / naive
        print(f"  节点复用减少比例         = {saved:.1%}")
    for answer, qb in zip(proof["answers"], per_query_bytes(proof)):
        if answer["present"]:
            print(f"  [存在] {answer['query']!r} index={answer['index']} 证明≈{qb}B")
        else:
            pred = answer["predecessor"]
            succ = answer["successor"]
            p = f"#{pred['index']}:{pred['key']!r}" if pred else "∅"
            s = f"#{succ['index']}:{succ['key']!r}" if succ else "∅"
            print(f"  [缺失] {answer['query']!r} 前驱={p} 后继={s} 证明≈{qb}B")
    return 0


# ---- verify -------------------------------------------------------------

def cmd_verify(args: argparse.Namespace) -> int:
    manifest = read_manifest(args.manifest)
    queries = _read_queries(args.queries)
    proof = read_proof(args.proof)
    conclusions = verify_proof(manifest, queries, proof)

    size = proof_byte_size(proof)
    print(f"verify: 可信根 {manifest.root.hex()}")
    print(f"  证明体积 = {size} 字节，结论 {len(conclusions)} 项：")
    for c in conclusions:
        if c.present:
            print(f"  [存在] {c.query!r} -> 叶下标 {c.index}")
        else:
            p = f"#{c.predecessor['index']}:{c.predecessor['key']!r}" if c.predecessor else "∅"
            s = f"#{c.successor['index']}:{c.successor['key']!r}" if c.successor else "∅"
            print(f"  [缺失] {c.query!r} 前驱={p} 后继={s}")
    print("verify: 全部核对通过（排序/邻接/叶范围/根重建/节点集合）")
    return 0


# ---- inspect ------------------------------------------------------------

def _inspect_tree(args: argparse.Namespace) -> int:
    manifest: Optional[Manifest] = None
    if args.manifest:
        manifest = read_manifest(args.manifest)
    with load_tree(args.db, expected_manifest=manifest) as tree:
        info = {
            "format_version": int(tree.meta["format_version"]),
            "record_count": tree.record_count,
            "leaf_count": tree.leaf_count,
            "height": tree.height,
            "root_hex": tree.root.hex(),
            "padding_leaves": tree.leaf_count - tree.record_count,
            "integrity": "recomputed-and-matched",
        }
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("inspect tree: 加载时重算核验通过")
        for k, v in info.items():
            print(f"  {k} = {v}")
    return 0


def _inspect_manifest(args: argparse.Namespace) -> int:
    m = read_manifest(args.manifest)
    info = {
        "type": "ordered-merkle-manifest",
        "format_version": m.version,
        "record_count": m.record_count,
        "height": tree_height(m.record_count),
        "root_hex": m.root.hex(),
    }
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("inspect manifest:")
        for k, v in info.items():
            print(f"  {k} = {v}")
    return 0


def _inspect_proof(args: argparse.Namespace) -> int:
    proof = read_proof(args.proof)
    present = sum(1 for a in proof["answers"] if a["present"])
    info = {
        "type": proof["type"],
        "format_version": proof["format_version"],
        "record_count": proof["record_count"],
        "height": proof["height"],
        "answers": len(proof["answers"]),
        "present": present,
        "missing": len(proof["answers"]) - present,
        "shared_nodes": shared_node_count(proof),
        "naive_path_digests": naive_path_digest_count(proof),
        "byte_size": proof_byte_size(proof),
    }
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("inspect proof:")
        for k, v in info.items():
            print(f"  {k} = {v}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    if args.db:
        return _inspect_tree(args)
    if args.proof:
        return _inspect_proof(args)
    if args.manifest:
        return _inspect_manifest(args)
    raise MerkleError("inspect 需要指定 --manifest、--db 或 --proof 之一")


# ---- argparse 装配 ------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merkle-dataset",
        description="本地有序 Merkle 数据集 CLI（SQLite 外排，SHA256，离线）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate", help="生成合成 JSONL 数据")
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--seed", type=int, default=20261113)
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("build", help="从 JSONL 构建 SQLite Merkle 树与根清单")
    p.add_argument("--input", required=True, help="JSONL 输入路径")
    p.add_argument("--db", required=True, help="输出 SQLite 树路径")
    p.add_argument("--manifest", required=True, help="输出根清单路径（可信）")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("prove", help="对一组互不重复的查询键生成共享证明")
    p.add_argument("--db", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--queries", required=True, help="查询键 JSON 数组文件")
    p.add_argument("--output", required=True, help="证明输出路径")
    p.set_defaults(func=cmd_prove)

    p = sub.add_parser("verify", help="仅凭可信清单、查询集合与证明做独立验证")
    p.add_argument("--manifest", required=True)
    p.add_argument("--queries", required=True)
    p.add_argument("--proof", required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("inspect", help="查看清单/树/证明的元信息并触发完整性核验")
    p.add_argument("--manifest", help="根清单路径（inspect 树时可选交叉核验）")
    p.add_argument("--db", help="SQLite 树路径")
    p.add_argument("--proof", help="证明文件路径")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_inspect)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except MerkleError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"错误: 文件不存在: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
