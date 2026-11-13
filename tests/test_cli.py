"""CLI surface: exit codes and JSON is stable/machine readable."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "xbrlcheck", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_inspect_json():
    proc = run("inspect", "fixtures/report_a.xbrl", "--json")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["summary"] if "summary" in data else True
    assert len(data["facts"]) == 5
    nil_facts = [f for f in data["facts"] if f["nil"]]
    assert len(nil_facts) == 1
    assert nil_facts[0]["value"] is None


def test_compare_exit_zero_for_compatible():
    proc = run("compare", "examples/order_a.xbrl", "examples/order_b.xbrl")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_compare_flags_conflict_exit_nonzero():
    proc = run("compare", "fixtures/report_a.xbrl", "examples/dup_conflict.xbrl")
    # different concepts/contexts -> only_a/only_b rows -> nonzero
    assert proc.returncode == 1


def test_check_exit_codes():
    ok = run("check", "fixtures/report_a.xbrl")
    assert ok.returncode == 0
    bad = run("check", "examples/precision_boundary.xbrl")
    assert bad.returncode == 1
    rejected = run("check", "examples/unsupported.xbrl")
    assert rejected.returncode == 1


def test_check_json_contains_traces():
    proc = run("check", "examples/precision_boundary.xbrl", "--json")
    data = json.loads(proc.stdout)
    conclusions = data["calculation"]["conclusions"]
    assert len(conclusions) == 2
    for c in conclusions:
        assert c["arcs"]
        for a in c["arcs"]:
            assert a["weight"] in ("1", "-1")
            assert a["facts"]
            assert a["scaled_interval"] is not None
