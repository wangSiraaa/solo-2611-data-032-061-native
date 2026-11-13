"""端到端流水线：加载 -> 建网 -> 候选 -> 联合匹配 -> JSONL 记录。"""

from __future__ import annotations

from .candidates import generate_candidates
from .config import Config
from .io import (
    SourceFiles,
    build_source_files,
    load_gps,
    load_nodes,
    load_roads,
)
from .matching import match
from .network import RoadNetwork


def run_pipeline(roads_path: str, nodes_path: str, gps_path: str,
                 config_path: str, config_overrides: dict | None = None):
    """执行完整匹配，返回 (point_records, segment_records, manifest_dict)。"""
    base_cfg = Config.from_file(config_path)
    cfg = (
        Config.from_dict({**base_cfg.to_dict(), **config_overrides})
        if config_overrides else base_cfg
    )
    sources = build_source_files(roads_path, nodes_path, gps_path, config_path)

    nodes = load_nodes(nodes_path)
    roads = load_roads(roads_path, nodes)
    network = RoadNetwork.build(roads)
    points = load_gps(gps_path)
    # 稳定排序：同时间戳保持输入顺序，负时间差在排序后不会出现。
    points.sort(key=lambda p: p.ts_ms)

    candidates_per_point = [
        generate_candidates(point, roads, cfg) for point in points
    ]
    point_records, segment_records = match(
        points, candidates_per_point, network, cfg
    )

    matched = sum(1 for r in point_records if r["status"] == "MATCHED")
    manifest = {
        "record_type": "manifest",
        "config": cfg.to_dict(),
        "cost_dimensions": {
            "obs_cost": "dimensionless: weight_obs * 0.5*(d/sigma)^2; "
                        "加权代价而非真实概率，不做归一化",
            "transition_cost":
                "dimensionless: weight_trans * max(0, path_length_m - euclidean_m)"
                " / transition_beta_m",
            "total_score": "sum of weighted costs, lower is better; 非概率、未归一化",
        },
        "counts": {
            "points_total": len(points),
            "points_matched": matched,
            "points_unknown": len(points) - matched,
            "segments": len(segment_records),
            "edges": len(roads),
            "nodes": len(nodes),
        },
        "sources": sources.to_dict(),
    }
    return point_records, segment_records, manifest
