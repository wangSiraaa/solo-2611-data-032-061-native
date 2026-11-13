"""命令行入口：match / selftest 两个子命令。"""

from __future__ import annotations

import argparse
import json
import sys

from .config import Config
from .errors import InputValidationError
from .pipeline import run_pipeline
from .selftest import run_selftest


def _write_jsonl(path: str, records: list) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            fh.write("\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapmatching",
        description="本地有向路网轨迹匹配 CLI（无地图服务、无缓存平台）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    m = sub.add_parser("match", help="对单车辆 GPS 轨迹执行地图匹配")
    m.add_argument("--roads", default="fixtures/roads.geojson",
                   help="有向道路 GeoJSON 路径（默认 fixtures/roads.geojson）")
    m.add_argument("--nodes", default="fixtures/nodes.csv",
                   help="节点 CSV 路径（默认 fixtures/nodes.csv）")
    m.add_argument("--gps", default="fixtures/gps.csv",
                   help="GPS 采样点 CSV 路径（默认 fixtures/gps.csv）")
    m.add_argument("--config", default="fixtures/config.json",
                   help="配置 JSON 路径（默认 fixtures/config.json）")
    m.add_argument("--out-dir", default="out",
                   help="输出目录（默认 out）")
    m.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="覆盖配置项，可重复，如 --set obs_weight=2.0")
    m.add_argument("--points-out", default="points.jsonl",
                   help="逐点输出文件名（默认 points.jsonl）")
    m.add_argument("--segments-out", default="segments.jsonl",
                   help="路段序列输出文件名（默认 segments.jsonl）")
    m.add_argument("--manifest-out", default="manifest.json",
                   help="来源与配置清单文件名（默认 manifest.json）")

    s = sub.add_parser("selftest", help="不依赖 pytest 的内置冒烟自检")
    s.add_argument("--fixtures-dir", default="fixtures")
    return parser


def _parse_overrides(items: list) -> dict:
    overrides: dict = {}
    for item in items:
        if "=" not in item:
            raise InputValidationError(f"--set 参数格式应为 KEY=VALUE: {item}")
        key, raw = item.split("=", 1)
        key = key.strip()
        if key not in Config.from_dict({}).to_dict():
            raise InputValidationError(f"未知配置项: {key}")
        try:
            if key == "max_candidates":
                overrides[key] = int(raw)
            else:
                overrides[key] = float(raw)
        except ValueError as exc:
            raise InputValidationError(f"配置项 {key} 的值不是数值: {raw}") from exc
    return overrides


def _cmd_match(args) -> int:
    import os

    overrides = _parse_overrides(args.set)
    point_records, segment_records, manifest = run_pipeline(
        args.roads, args.nodes, args.gps, args.config, overrides
    )
    os.makedirs(args.out_dir, exist_ok=True)
    points_path = os.path.join(args.out_dir, args.points_out)
    segments_path = os.path.join(args.out_dir, args.segments_out)
    manifest_path = os.path.join(args.out_dir, args.manifest_out)
    _write_jsonl(points_path, point_records)
    _write_jsonl(segments_path, segment_records)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")

    unknown = [r["point_id"] for r in point_records if r["status"] == "UNKNOWN"]
    print(f"points: {manifest['counts']['points_total']} "
          f"matched={manifest['counts']['points_matched']} "
          f"unknown={manifest['counts']['points_unknown']} "
          f"segments={manifest['counts']['segments']}")
    if unknown:
        print("unknown points:", ", ".join(unknown))
    print(f"wrote {points_path}\nwrote {segments_path}\nwrote {manifest_path}")
    return 0


def main(argv: list | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "match":
            return _cmd_match(args)
        if args.command == "selftest":
            return run_selftest(args.fixtures_dir)
    except InputValidationError as exc:
        print(f"输入校验失败: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"文件读取失败: {exc}", file=sys.stderr)
        return 2
    parser.error(f"未知子命令: {args.command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
