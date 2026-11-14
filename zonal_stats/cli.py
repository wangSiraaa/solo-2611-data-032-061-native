"""Command line interface: ``python -m zonal_stats query ...``."""

from __future__ import annotations

import argparse

from .core import run_queries


def build_parser():
    parser = argparse.ArgumentParser(
        prog="zonal_stats",
        description="Zonal statistics for single-band rasters (rotation-safe).")
    sub = parser.add_subparsers(dest="command", required=True)
    q = sub.add_parser(
        "query",
        help="run zonal-statistics queries for all regions in a GeoJSON file")
    q.add_argument("--raster", default="fixtures/surface.tif",
                   help="single-band raster (default: fixtures/surface.tif)")
    q.add_argument("--regions", default="fixtures/regions.geojson",
                   help="GeoJSON FeatureCollection of Polygon query regions")
    q.add_argument("--config", default="fixtures/query-config.json",
                   help="JSON config with max_read_pixels and crs")
    q.add_argument("--out", default="outputs",
                   help="output directory (default: outputs)")
    q.add_argument("--max-read-pixels", type=int, default=None,
                   help="override config max_read_pixels (read block cap)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "query":
        report = run_queries(args.raster, args.regions, args.config,
                             args.out, args.max_read_pixels)
        print(f"raster: {report['raster']} "
              f"({report['width']}x{report['height']}, crs {report['crs']})")
        print(f"read block cap: {report['max_read_pixels']} pixels")
        for q in report["queries"]:
            line = f"  {q['region_id']:<12} {q['status']}"
            stats = q["stats"]
            if stats:
                line += (f"  count={stats['count']} min={stats['min']:.4f}"
                         f" max={stats['max']:.4f} mean={stats['mean']:.4f}")
            line += f"  (pixels read: {q['pixels_read']})"
            print(line)
        print(f"wrote {args.out}/report.json, metadata.json, "
              f"boundaries.geojson and clipped arrays")
    return 0
