"""对现成 fixtures 的端到端验证与输出契约检查。"""

from __future__ import annotations

import json

import pytest

from mapmatching.config import Config
from mapmatching.errors import InputValidationError
from mapmatching.pipeline import run_pipeline

FIX = "fixtures"


@pytest.fixture(scope="module")
def result():
    return run_pipeline(
        f"{FIX}/roads.geojson", f"{FIX}/nodes.csv",
        f"{FIX}/gps.csv", f"{FIX}/config.json",
    )


def test_fixture_match_statuses(result):
    points, segments, manifest = result
    by_id = {r["point_id"]: r for r in points}
    assert [by_id[f"p{i}"]["status"] for i in range(1, 9)] == (
        ["MATCHED"] * 6 + ["UNKNOWN", "MATCHED"]
    )
    assert by_id["p7"]["unknown_reason"] == "NO_CANDIDATE"
    assert by_id["p7"]["candidates"] == []
    assert manifest["counts"] == {
        "points_total": 8, "points_matched": 7, "points_unknown": 1,
        "segments": 2, "edges": 9, "nodes": 9,
    }


def test_parallel_roads_do_not_oscillate(result):
    _points, segments, _ = result
    main = segments[0]
    assert main["directed_edge_sequence"] == ["ab", "bc"]
    # 每个点的有向边都属于下方主路链，未跳到 de/ef。
    assert {p["edge_id"] for p in main["points"]} <= {"ab", "bc"}


def test_geometric_intersection_is_not_a_junction(result):
    points, _segments, _ = result
    p3 = next(r for r in points if r["point_id"] == "p3")
    gh = next(c for c in p3["candidates"] if c["edge_id"] == "gh")
    assert gh["dp_state"]["reachable"] is False
    assert gh["dp_state"]["reasons"] == ["no_directed_path"]
    assert p3["selection"]["edge_id"] == "bc"


def test_zero_time_duplicate_is_stationary(result):
    points, _segments, _ = result
    by_id = {r["point_id"]: r for r in points}
    trans = by_id["p6"]["selection"]["incoming_transition"]
    assert trans["dt_ms"] == 0
    assert trans["path_length_m"] == 0.0
    assert trans["feasible"] is True
    assert by_id["p5"]["selection"]["edge_id"] == "bc"
    assert by_id["p6"]["selection"]["edge_id"] == "bc"


def test_segment_break_positions_and_edges(result):
    _points, segments, _ = result
    assert len(segments) == 2
    first, second = segments
    assert first["break_after"] == {"point_id": "p6",
                                    "reason": "NEXT_POINT_UNKNOWN"}
    assert second["break_before"] == {"point_id": "p8",
                                      "reason": "PREVIOUS_POINT_UNKNOWN"}
    assert first["boundary_positions"]["start"] == {"edge_id": "ab", "along_m": 8.0}
    assert first["boundary_positions"]["end"] == {"edge_id": "bc", "along_m": 95.0}
    # 边界位置必须落在对应边的里程范围内。
    for seg in segments:
        for end in ("start", "end"):
            pos = seg["boundary_positions"][end]
            assert 0.0 <= pos["along_m"]


def test_candidate_contract(result):
    points, _segments, _ = result
    required = {
        "cand_id", "edge_id", "rank", "projection_distance_m", "along_m",
        "projection_xy", "bearing_deg", "accuracy_m", "obs_cost",
        "weighted_obs_cost", "source", "dp_state",
    }
    for record in points:
        for cand in record["candidates"]:
            assert required.issubset(cand)
            assert 0.0 <= cand["along_m"]
            assert 0.0 <= cand["bearing_deg"] < 360.0
            assert cand["projection_distance_m"] <= 25.0
            assert cand["source"]["file"] == "roads.geojson"
        # 候选按投影距离非降序排列。
        dists = [c["projection_distance_m"] for c in record["candidates"]]
        assert dists == sorted(dists)
        assert len(record["candidates"]) <= 4


def test_source_mapping_uses_row_and_feature_indices(result):
    points, segments, manifest = result
    for i, record in enumerate(points):
        assert record["source"] == {"file": "gps.csv", "row_index": i}
        for cand in record["candidates"]:
            assert isinstance(cand["source"]["feature_index"], int)
    assert set(manifest["sources"]) == {"roads", "nodes", "gps", "config"}
    for entry in manifest["sources"].values():
        assert len(entry["sha256"]) == 64


def test_scores_are_not_probabilities(result):
    points, _segments, manifest = result
    raw = json.dumps({"points": points, "manifest": manifest})
    for banned in ("probability", "posterior", "logprob", "likelihood"):
        assert banned not in raw
    assert "真实概率" in manifest["cost_dimensions"]["obs_cost"]
    assert "非概率" in manifest["cost_dimensions"]["total_score"]


def test_unknown_points_never_get_straight_line_repair(result):
    points, segments, _ = result
    # 所有 segment 的边序列只包含真实有向 edge_id。
    valid_edges = {"ab", "bc", "de", "ef", "be", "fc", "ca", "gh", "fi"}
    for seg in segments:
        assert set(seg["directed_edge_sequence"]) <= valid_edges
    for record in points:
        if record["status"] == "UNKNOWN":
            assert record["selection"] is None


def test_config_overrides_change_weights(tmp_path):
    base = run_pipeline(
        f"{FIX}/roads.geojson", f"{FIX}/nodes.csv",
        f"{FIX}/gps.csv", f"{FIX}/config.json",
    )
    weighted = run_pipeline(
        f"{FIX}/roads.geojson", f"{FIX}/nodes.csv",
        f"{FIX}/gps.csv", f"{FIX}/config.json",
        config_overrides={"obs_weight": 10.0},
    )
    assert base[2]["config"]["obs_weight"] == 1.0
    assert weighted[2]["config"]["obs_weight"] == 10.0


def test_invalid_config_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"candidate_radius_m": -1}))
    with pytest.raises(InputValidationError):
        Config.from_file(str(bad))
    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps({"bogus": 1}))
    with pytest.raises(InputValidationError):
        Config.from_file(str(unknown))


def test_missing_node_reference_rejected(tmp_path):
    with open(f"{FIX}/roads.geojson", encoding="utf-8") as fh:
        roads = json.load(fh)
    roads["features"][0]["properties"]["to"] = "GHOST"
    path = tmp_path / "roads_bad.geojson"
    path.write_text(json.dumps(roads))
    with pytest.raises(InputValidationError):
        run_pipeline(str(path), f"{FIX}/nodes.csv",
                     f"{FIX}/gps.csv", f"{FIX}/config.json")
