"""Tests for the independent verifier and the CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from cases import CASES, DEFAULT_PLAN  # noqa: E402
from verify_outputs import verify  # noqa: E402

PY = sys.executable


def _materialise(tmp_path: Path, case: dict) -> tuple[Path, Path, Path, Path]:
    d = tmp_path / case["name"]
    d.mkdir(parents=True)
    plan = d / "plan.json"
    parcels = d / "parcels.geojson"
    zones = d / "zones.geojson"
    plan.write_text(json.dumps(case.get("plan", DEFAULT_PLAN)), encoding="utf-8")
    parcels.write_text(
        json.dumps({"type": "FeatureCollection", "features": case["parcels"]}),
        encoding="utf-8",
    )
    zones.write_text(
        json.dumps({"type": "FeatureCollection", "features": case["zones"]}),
        encoding="utf-8",
    )
    return plan, parcels, zones, d / "out"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-m", "energyalloc", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_independent_verifier_passes(case, tmp_path):
    plan, parcels, zones, out = _materialise(tmp_path, case)
    proc = _run(
        [
            "allocate",
            "--plan",
            str(plan),
            "--parcels",
            str(parcels),
            "--zones",
            str(zones),
            "--out",
            str(out),
        ]
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout

    report = verify(out, parcels)
    assert report["passed"], json.dumps(report, indent=2)
    # Independent verifier must reconstruct each accepted parcel's union from
    # fragments within tolerance; symdiff is computed against the source file.
    for parcel_report in report["parcel_reports"]:
        assert parcel_report["overlap_area_m2"] <= 1e-5
        assert parcel_report["checks"]["energy_conserved"]


def test_verifier_detects_overlap(tmp_path):
    plan, parcels, zones, out = _materialise(tmp_path, CASES[0])
    _run(
        [
            "allocate",
            "--plan",
            str(plan),
            "--parcels",
            str(parcels),
            "--zones",
            str(zones),
            "--out",
            str(out),
        ]
    )
    doc = json.loads((out / "fragments.geojson").read_text())
    # Duplicate one feature to create an artificial overlap.
    doc["features"].append(json.loads(json.dumps(doc["features"][0])))
    (out / "fragments.geojson").write_text(json.dumps(doc), encoding="utf-8")
    report = verify(out, parcels)
    assert not report["passed"]
    assert report["global_overlap_area_m2"] > 0


def test_cli_allocate_exit_zero(tmp_path):
    plan, parcels, zones, out = _materialise(tmp_path, CASES[0])
    proc = _run(
        [
            "allocate",
            "--plan",
            str(plan),
            "--parcels",
            str(parcels),
            "--zones",
            str(zones),
            "--out",
            str(out),
        ]
    )
    assert proc.returncode == 0
    assert "candidate zone-parcel pairs" in proc.stdout
    assert (out / "fragments.geojson").exists()


def test_cli_explain_parcel(tmp_path):
    plan, parcels, zones, out = _materialise(tmp_path, CASES[0])
    proc = _run(
        [
            "explain-parcel",
            "P",
            "--plan",
            str(plan),
            "--parcels",
            str(parcels),
            "--zones",
            str(zones),
            "--out",
            str(out),
        ]
    )
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["parcel_id"] == "P"
    assert doc["candidates"]
    assert doc["fragments"]


def test_cli_rejection_exit_code(tmp_path):
    case = next(c for c in CASES if c["name"] == "09_rejections")
    plan, parcels, zones, out = _materialise(tmp_path, case)
    proc = _run(
        [
            "explain-parcel",
            "BAD_INVALID",
            "--plan",
            str(plan),
            "--parcels",
            str(parcels),
            "--zones",
            str(zones),
            "--out",
            str(out),
        ]
    )
    assert proc.returncode == 2
    doc = json.loads(proc.stdout)
    assert doc["code"] == "invalid_geometry"


def test_cli_crs_mismatch_exit_code(tmp_path):
    plan, parcels, zones, out = _materialise(tmp_path, CASES[0])
    doc = json.loads(parcels.read_text())
    doc["crs"] = "EPSG:4326"
    parcels.write_text(json.dumps(doc), encoding="utf-8")
    proc = _run(
        [
            "allocate",
            "--plan",
            str(plan),
            "--parcels",
            str(parcels),
            "--zones",
            str(zones),
            "--out",
            str(out),
        ]
    )
    assert proc.returncode == 2
    assert "crs_mismatch" in proc.stderr


def test_fixture_outputs_verify(tmp_path):
    out = tmp_path / "fixture_out"
    proc = _run(
        [
            "allocate",
            "--plan",
            str(ROOT / "fixtures" / "plan.json"),
            "--parcels",
            str(ROOT / "fixtures" / "parcels.geojson"),
            "--zones",
            str(ROOT / "fixtures" / "zones.geojson"),
            "--out",
            str(out),
        ]
    )
    assert proc.returncode == 0, proc.stderr
    report = verify(out, ROOT / "fixtures" / "parcels.geojson")
    assert report["passed"], json.dumps(report, indent=2)
    assert report["total_wh_in_output"] == 2807
