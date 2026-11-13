"""Shared helpers for the test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xbrlcheck.calc import check_calculations  # noqa: E402
from xbrlcheck.cli import build_taxonomy  # noqa: E402
from xbrlcheck.compare import compare_reports  # noqa: E402

FIXTURES = ROOT / "fixtures"
EXAMPLES = ROOT / "examples"


def load(report_path: Path):
    taxonomy, reports = build_taxonomy([report_path])
    return taxonomy, reports[0]


def load_pair(a: Path, b: Path):
    taxonomy, reports = build_taxonomy([a, b])
    return taxonomy, reports[0], reports[1]


def check(report_path: Path):
    taxonomy, report = load(report_path)
    return report, check_calculations(report, taxonomy)


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def examples() -> Path:
    return EXAMPLES
