"""输入输出：节点 CSV、GPS CSV、道路 GeoJSON 的加载与来源记录。"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Optional

from shapely.geometry import LineString

from .errors import InputValidationError


@dataclass(frozen=True)
class Road:
    """一条有向道路边。"""

    edge_id: str
    from_node: str
    to_node: str
    max_speed_mps: float
    geometry: LineString
    length_m: float
    source_index: int  # 在 GeoJSON features 中的原始下标，用于来源映射


@dataclass(frozen=True)
class GpsPoint:
    point_id: str
    ts_ms: int
    x: float
    y: float
    accuracy_m: Optional[float]
    source_index: int  # 在 CSV 数据行中的原始下标（不含表头）


@dataclass
class SourceFiles:
    """输入文件路径与内容哈希，构成输出的来源映射。"""

    roads_path: str
    nodes_path: str
    gps_path: str
    config_path: str
    sha256: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "roads": {"path": self.roads_path, "sha256": self.sha256["roads"]},
            "nodes": {"path": self.nodes_path, "sha256": self.sha256["nodes"]},
            "gps": {"path": self.gps_path, "sha256": self.sha256["gps"]},
            "config": {"path": self.config_path, "sha256": self.sha256["config"]},
        }


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_nodes(path: str) -> dict:
    """返回 {node_id: (x, y)}，并校验节点唯一、坐标为数值。"""
    nodes: dict = {}
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        required = {"node_id", "x", "y"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise InputValidationError(
                f"nodes.csv 必须包含列 {sorted(required)}，实际为 {reader.fieldnames}"
            )
        for idx, row in enumerate(reader):
            node_id = row["node_id"].strip()
            if not node_id:
                raise InputValidationError(f"nodes.csv 第 {idx + 2} 行 node_id 为空")
            if node_id in nodes:
                raise InputValidationError(f"nodes.csv 节点 {node_id} 重复定义")
            try:
                xy = (float(row["x"]), float(row["y"]))
            except (TypeError, ValueError) as exc:
                raise InputValidationError(
                    f"nodes.csv 节点 {node_id} 坐标不是数值"
                ) from exc
            nodes[node_id] = xy
    if not nodes:
        raise InputValidationError("nodes.csv 为空")
    return nodes


def load_roads(path: str, nodes: dict) -> list:
    """加载有向 LineString 道路，校验拓扑引用与几何。"""
    with open(path, "r", encoding="utf-8") as fh:
        collection = json.load(fh)
    if collection.get("type") != "FeatureCollection":
        raise InputValidationError("roads.geojson 必须是 FeatureCollection")
    features = collection.get("features")
    if not isinstance(features, list) or not features:
        raise InputValidationError("roads.geojson 不包含任何 feature")

    roads: list = []
    seen_ids: set = set()
    for idx, feat in enumerate(features):
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        try:
            edge_id = str(props["edge_id"])
            from_node = str(props["from"])
            to_node = str(props["to"])
            max_speed = float(props["max_speed_mps"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InputValidationError(
                f"roads.geojson 第 {idx} 个 feature 属性缺失或类型错误"
            ) from exc
        if geom.get("type") != "LineString":
            raise InputValidationError(f"道路 {edge_id} 几何必须是 LineString")
        coords = geom.get("coordinates")
        if not isinstance(coords, list) or len(coords) < 2:
            raise InputValidationError(f"道路 {edge_id} 坐标点数不足")
        try:
            line = LineString([(float(x), float(y)) for x, y in coords])
        except Exception as exc:  # shapely 抛出的几何异常统一转换
            raise InputValidationError(f"道路 {edge_id} 几何无效: {exc}") from exc
        if not line.is_valid or line.length <= 0:
            raise InputValidationError(f"道路 {edge_id} 几何无效或长度为零")
        if edge_id in seen_ids:
            raise InputValidationError(f"edge_id {edge_id} 重复")
        for node_id in (from_node, to_node):
            if node_id not in nodes:
                raise InputValidationError(
                    f"道路 {edge_id} 引用了 nodes.csv 中不存在的节点 {node_id}"
                )
        if max_speed <= 0:
            raise InputValidationError(f"道路 {edge_id} max_speed_mps 必须为正数")
        seen_ids.add(edge_id)
        roads.append(
            Road(
                edge_id=edge_id,
                from_node=from_node,
                to_node=to_node,
                max_speed_mps=max_speed,
                geometry=line,
                length_m=line.length,
                source_index=idx,
            )
        )
    return roads


def load_gps(path: str) -> list:
    """加载 GPS 采样点。accuracy_m 可空；时间戳必须是整数毫秒。"""
    points: list = []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        required = {"point_id", "ts_ms", "x", "y"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise InputValidationError(
                f"gps.csv 必须包含列 {sorted(required)}，实际为 {reader.fieldnames}"
            )
        for idx, row in enumerate(reader):
            point_id = row["point_id"].strip()
            if not point_id:
                raise InputValidationError(f"gps.csv 第 {idx + 2} 行 point_id 为空")
            raw_ts = row["ts_ms"].strip()
            try:
                ts_ms = int(raw_ts)
            except (TypeError, ValueError) as exc:
                raise InputValidationError(
                    f"gps.csv 点 {point_id} 的 ts_ms 不是整数毫秒"
                ) from exc
            try:
                x = float(row["x"])
                y = float(row["y"])
            except (TypeError, ValueError) as exc:
                raise InputValidationError(
                    f"gps.csv 点 {point_id} 坐标不是数值"
                ) from exc
            accuracy_raw = (row.get("accuracy_m") or "").strip()
            accuracy: Optional[float]
            if accuracy_raw:
                try:
                    accuracy = float(accuracy_raw)
                except ValueError as exc:
                    raise InputValidationError(
                        f"gps.csv 点 {point_id} accuracy_m 不是数值"
                    ) from exc
                if accuracy <= 0:
                    raise InputValidationError(
                        f"gps.csv 点 {point_id} accuracy_m 必须为正数"
                    )
            else:
                accuracy = None
            points.append(
                GpsPoint(
                    point_id=point_id,
                    ts_ms=ts_ms,
                    x=x,
                    y=y,
                    accuracy_m=accuracy,
                    source_index=idx,
                )
            )
    if not points:
        raise InputValidationError("gps.csv 为空")
    return points


def build_source_files(roads_path: str, nodes_path: str, gps_path: str,
                       config_path: str) -> SourceFiles:
    paths = {
        "roads": roads_path,
        "nodes": nodes_path,
        "gps": gps_path,
        "config": config_path,
    }
    hashes = {key: _sha256(path) for key, path in paths.items()}
    return SourceFiles(
        roads_path=os.path.abspath(roads_path),
        nodes_path=os.path.abspath(nodes_path),
        gps_path=os.path.abspath(gps_path),
        config_path=os.path.abspath(config_path),
        sha256=hashes,
    )
