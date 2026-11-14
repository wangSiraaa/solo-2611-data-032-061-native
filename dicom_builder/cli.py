"""命令行入口：python -m dicom_builder build ..."""

from __future__ import annotations

import argparse
import json
import sys

from .builder import build
from .model import Tolerances


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dicom_builder",
        description=(
            "把单帧 DICOM 切片按物理坐标组装为三维 float32 体数组及 LPS "
            "affine。仅支持未压缩 Explicit VR Little Endian、MONOCHROME2、"
            "16 位整数像素的平行切片；不做重采样、配准或医学诊断。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser(
        "build", help="扫描输入目录并按 Series/FrameOfReference 分组构建"
    )
    p_build.add_argument(
        "--input", required=True, help="存放单帧 DICOM 文件的目录（递归扫描）"
    )
    p_build.add_argument(
        "--output", required=True, help="产物输出目录（不存在会自动创建）"
    )
    p_build.add_argument(
        "--tolerances",
        required=False,
        help=(
            "容差 JSON 文件，键: direction_abs(无量纲)、spacing_mm(mm)、"
            "position_mm(mm)；缺省使用内置默认值"
        ),
    )
    p_build.add_argument(
        "--report-only",
        action="store_true",
        help="只在标准输出打印报告 JSON，不改变退出码语义（默认即打印）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command != "build":  # argparse 已保证不会发生
        return 2

    try:
        tolerances = Tolerances.from_file(args.tolerances)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"容差配置读取失败: {exc}", file=sys.stderr)
        return 2

    try:
        report = build(args.input, args.output, tolerances)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"输入错误: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))

    # 存在失败分组或被拒绝文件时以非零退出码结束，成功分组产物仍然保留
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
