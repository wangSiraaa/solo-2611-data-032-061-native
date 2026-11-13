"""道路输入顺序不变性：打乱 GeoJSON feature 次序后结果必须完全一致。

唯一允许随输入顺序变化的是来源映射里的 feature_index，因此比较前将其
归一化为按 edge_id 的稳定下标。
"""

from __future__ import annotations

import copy
import json

from mapmatching.pipeline import run_pipeline


def _normalize(records):
    """把候选来源的 feature_index 替换为确定性的 edge_id，消除顺序敏感度。"""
    out = copy.deepcopy(records)
    for record in out:
        for cand in record.get("candidates", []):
            cand["source"] = {"file": "roads.geojson",
                              "edge_id": cand["edge_id"]}
    return out


def _write_shuffled_roads(tmp_path, fixtures_dir, seed):
    with open(f"{fixtures_dir}/roads.geojson", encoding="utf-8") as fh:
        collection = json.load(fh)
    features = collection["features"]
    # 确定性重排：循环移位 + 反转，避免依赖随机种子。
    shuffled = features[seed:] + features[:seed]
    if seed % 2:
        shuffled.reverse()
    out = copy.deepcopy(collection)
    out["features"] = shuffled
    path = tmp_path / f"roads_shuf_{seed}.geojson"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    return str(path)


def test_shuffled_road_order_gives_identical_results(tmp_path):
    fixtures = "fixtures"
    baseline_points, baseline_segments, baseline_manifest = run_pipeline(
        f"{fixtures}/roads.geojson", f"{fixtures}/nodes.csv",
        f"{fixtures}/gps.csv", f"{fixtures}/config.json",
    )
    baseline_manifest = copy.deepcopy(baseline_manifest)
    baseline_manifest.pop("sources")
    base_points = _normalize(baseline_points)

    for seed in range(1, 6):
        roads_path = _write_shuffled_roads(tmp_path, fixtures, seed)
        points, segments, manifest = run_pipeline(
            roads_path, f"{fixtures}/nodes.csv",
            f"{fixtures}/gps.csv", f"{fixtures}/config.json",
        )
        manifest.pop("sources")
        assert _normalize(points) == base_points
        assert segments == baseline_segments
        assert manifest == baseline_manifest


def test_feature_index_points_back_to_original_row(tmp_path):
    """未打乱时，来源 feature_index 必须等于 GeoJSON 中的真实行号。"""
    fixtures = "fixtures"
    with open(f"{fixtures}/roads.geojson", encoding="utf-8") as fh:
        edge_by_index = {
            i: f["properties"]["edge_id"]
            for i, f in enumerate(json.load(fh)["features"])
        }
    points, _segments, _manifest = run_pipeline(
        f"{fixtures}/roads.geojson", f"{fixtures}/nodes.csv",
        f"{fixtures}/gps.csv", f"{fixtures}/config.json",
    )
    for record in points:
        for cand in record["candidates"]:
            assert edge_by_index[cand["source"]["feature_index"]] == cand["edge_id"]


def test_candidate_ranks_are_stable_under_shuffle(tmp_path):
    fixtures = "fixtures"
    roads_path = _write_shuffled_roads(tmp_path, fixtures, 3)
    points, _, _ = run_pipeline(
        roads_path, f"{fixtures}/nodes.csv",
        f"{fixtures}/gps.csv", f"{fixtures}/config.json",
    )
    baseline_points, _, _ = run_pipeline(
        f"{fixtures}/roads.geojson", f"{fixtures}/nodes.csv",
        f"{fixtures}/gps.csv", f"{fixtures}/config.json",
    )
    for got, base in zip(points, baseline_points):
        got_ranks = [(c["edge_id"], c["rank"]) for c in got["candidates"]]
        base_ranks = [(c["edge_id"], c["rank"]) for c in base["candidates"]]
        assert got_ranks == base_ranks
