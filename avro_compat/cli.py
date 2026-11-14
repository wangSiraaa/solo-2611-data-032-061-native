"""命令行入口：check（兼容性预检）与 project（预检通过后投影）。

用法：
  python -m avro_compat check   WRITER.avsc READER.avsc [--plan plan.json]
  python -m avro_compat project WRITER.avsc READER.avsc MESSAGES.avro \\
                                --out out.jsonl [--plan plan.json]

退出码：0 成功；1 模式不兼容；2 schema 非法/参数错误；3 解码或投影失败。
"""

from __future__ import annotations

import argparse
import copy
import sys
from typing import Sequence

from .io_utils import (build_report, decode_avro, normalize_schema, write_outputs,
                       write_plan)
from .projection import ProjectionError, project_records
from .resolver import CompatibilityError
from .schema import SchemaError, load_json, validate_schema

EXIT_OK = 0
EXIT_INCOMPATIBLE = 1
EXIT_SCHEMA = 2
EXIT_RUNTIME = 3


def _print_report(report, stream) -> None:
    if report.compatible:
        stream.write("兼容性预检通过：writer 的全部类型与 union 分支均可被 reader 接收。\n")
        return
    stream.write(f"兼容性预检失败，共 {len(report.errors)} 项不兼容：\n")
    for i, err in enumerate(report.errors, 1):
        stream.write(
            f"  {i}. writer {err['writer_path']} -> reader {err['reader_path']}\n"
            f"     原因：{err['reason']}\n")


def _cmd_check(args: argparse.Namespace) -> int:
    report = build_report(args.writer_avsc, args.reader_avsc)
    _print_report(report, sys.stdout)
    if args.plan:
        write_plan(args.plan, report)
        sys.stdout.write(f"解析计划已写入 {args.plan}\n")
    return EXIT_OK if report.compatible else EXIT_INCOMPATIBLE


def _cmd_project(args: argparse.Namespace) -> int:
    # 1) 先做纯 schema 级预检；不通过绝不触碰数据、不写任何输出
    report = build_report(args.writer_avsc, args.reader_avsc)
    if not report.compatible:
        _print_report(report, sys.stdout)
        return EXIT_INCOMPATIBLE

    # 2) 完整解码（任一条解码失败都在此抛出，此时尚未创建输出文件）
    try:
        records, embedded_writer = decode_avro(args.messages_avro)
    except Exception as exc:  # fastavro 的各类解码错误统一转退出码 3
        raise ProjectionError(f"Avro 文件解码失败：{exc}") from exc

    # 交叉核对：文件内嵌 writer schema 必须与给定 writer.avsc 归一化后一致
    given = load_json(args.writer_avsc)
    given_copy = copy.deepcopy(given)
    validate_schema(given_copy)
    if normalize_schema(given_copy) != normalize_schema(embedded_writer):
        raise ProjectionError(
            "messages.avro 内嵌 writer schema 与给定 writer.avsc 不一致，拒绝投影")

    # 3) 全部记录先在内存投影成功；4) 渲染并原子写出计划与 JSONL
    projected = project_records(report, records)
    rows = [{"index": idx, "record": rec} for idx, rec in enumerate(projected)]
    write_outputs(out_path=args.out, rows=rows, plan_path=args.plan, report=report)
    sys.stdout.write(
        f"已投影 {len(rows)} 条记录 -> {args.out}"
        + (f"，计划 -> {args.plan}" if args.plan else "") + "\n")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="avro_compat",
        description="受限 Avro 子集的 writer->reader 兼容性预检与投影")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="只做 schema 级兼容性预检")
    p_check.add_argument("writer_avsc")
    p_check.add_argument("reader_avsc")
    p_check.add_argument("--plan", help="把解析计划写入该 JSON 文件")
    p_check.set_defaults(func=_cmd_check)

    p_proj = sub.add_parser("project", help="预检通过后把 Avro 记录投影为 reader JSONL")
    p_proj.add_argument("writer_avsc")
    p_proj.add_argument("reader_avsc")
    p_proj.add_argument("messages_avro")
    p_proj.add_argument("--out", required=True, help="输出 JSONL 路径")
    p_proj.add_argument("--plan", help="把解析计划写入该 JSON 文件")
    p_proj.set_defaults(func=_cmd_project)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CompatibilityError as exc:
        for err in exc.errors:
            sys.stderr.write(
                f"[writer {err['writer_path']} -> reader {err['reader_path']}] "
                f"{err['reason']}\n")
        return EXIT_INCOMPATIBLE
    except SchemaError as exc:
        sys.stderr.write(f"schema 非法：{exc}\n")
        return EXIT_SCHEMA
    except (ProjectionError, OSError) as exc:
        sys.stderr.write(f"执行失败：{exc}\n")
        return EXIT_RUNTIME


if __name__ == "__main__":
    raise SystemExit(main())
