"""Generate the small example datasets under examples/.

Each example is a self-contained (survey.csv, applicability.csv,
plan.json) triple exercising one diagnostic or methodological aspect.
All generators are deterministic.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent


def _write_example(name, rows, plan, not_applicable_cells=()):
    """rows: list of dicts variable -> value or "" ; plan: dict."""
    d = HERE / name
    d.mkdir(parents=True, exist_ok=True)
    variables = list(plan["variables"].keys())

    with open(d / "survey.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["row_id"] + variables)
        for rid, vals in rows:
            w.writerow([rid] + [
                "" if vals[v] == "" else f"{vals[v]:.5f}" for v in variables
            ])

    na = set(not_applicable_cells)
    with open(d / "applicability.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["row_id", "column", "status"])
        for rid, vals in rows:
            for v in variables:
                status = "not_applicable" if (rid, v) in na else "applicable"
                w.writerow([rid, v, status])

    with open(d / "plan.json", "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2)
        fh.write("\n")
    return d


def _base_plan(m, iters, seed, ranges=None):
    ranges = ranges or {}
    return {
        "variables": {
            "x1": {"type": "continuous", "range": ranges.get("x1", [0, 100])},
            "x2": {"type": "continuous", "range": ranges.get("x2", [0, 100])},
            "outcome": {"type": "continuous", "range": [-100, 200]},
        },
        "allowed_predictors": ["x1", "x2", "outcome"],
        "analysis": {
            "response": "outcome",
            "covariates": ["x1", "x2"],
            "intercept": True,
            "structural_inapplicable_policy":
                "exclude affected rows from this analysis",
        },
        "completed_datasets": m,
        "iterations": iters,
        "seed": seed,
    }


def make_no_missing():
    """Complete data: imputation must be a no-op; pooled == direct fit."""
    rng = np.random.default_rng(20261113)
    n = 30
    x1 = rng.uniform(0, 100, n)
    x2 = rng.uniform(0, 100, n)
    y = 5 + 0.4 * x1 - 0.3 * x2 + rng.normal(0, 5, n)
    rows = [(f"N{i:03d}", {"x1": x1[i], "x2": x2[i], "outcome": y[i]})
            for i in range(n)]
    return _write_example("no_missing", rows, _base_plan(4, 8, 7))


def make_range_bound():
    """x1 restricted to [0, 10]: every imputed x1 must stay inside."""
    rng = np.random.default_rng(20261114)
    n = 40
    x1 = rng.uniform(0, 10, n)
    x2 = rng.uniform(0, 100, n)
    y = 1 + 2.0 * x1 + 0.1 * x2 + rng.normal(0, 3, n)
    rows = []
    miss = set(rng.choice(n, size=8, replace=False))
    for i in range(n):
        rows.append((f"R{i:03d}", {
            "x1": "" if i in miss else x1[i],
            "x2": x2[i],
            "outcome": y[i],
        }))
    plan = _base_plan(4, 10, 11, ranges={"x1": [0, 10]})
    return _write_example("range_bound", rows, plan)


def make_all_missing_column():
    """x2 entirely empty: pre-run diagnostics must refuse to run."""
    rng = np.random.default_rng(20261115)
    n = 25
    x1 = rng.uniform(0, 100, n)
    y = 2 + 0.5 * x1 + rng.normal(0, 4, n)
    rows = [(f"A{i:03d}", {"x1": x1[i], "x2": "", "outcome": y[i]})
            for i in range(n)]
    return _write_example("all_missing_column", rows, _base_plan(4, 8, 3))


def make_correlated_missing():
    """x1 and x2 jointly missing for a block of rows (correlated missingness)."""
    rng = np.random.default_rng(20261116)
    n = 60
    x1 = rng.uniform(0, 100, n)
    x2 = 0.6 * x1 + rng.uniform(0, 20, n)
    y = 2 + 0.5 * x1 + 0.3 * x2 + rng.normal(0, 4, n)
    score = x1 + x2
    cut = np.quantile(score, 0.75)
    rows = []
    for i in range(n):
        both_missing = score[i] > cut
        rows.append((f"C{i:03d}", {
            "x1": "" if both_missing else x1[i],
            "x2": "" if both_missing else x2[i],
            "outcome": y[i],
        }))
    return _write_example("correlated_missing", rows, _base_plan(5, 12, 21))


def make_contradiction():
    """A not_applicable cell carrying an observed value: must be diagnosed."""
    rng = np.random.default_rng(20261117)
    n = 15
    x1 = rng.uniform(0, 100, n)
    x2 = rng.uniform(0, 100, n)
    y = 1 + 0.3 * x1 + 0.2 * x2 + rng.normal(0, 3, n)
    rows = [(f"K{i:03d}", {"x1": x1[i], "x2": x2[i], "outcome": y[i]})
            for i in range(n)]
    # K004 x2 is marked not_applicable yet has an observed value.
    return _write_example(
        "contradiction", rows, _base_plan(4, 8, 5),
        not_applicable_cells={("K004", "x2")},
    )


def main():
    for fn in (make_no_missing, make_range_bound, make_all_missing_column,
               make_correlated_missing, make_contradiction):
        d = fn()
        print(f"wrote {d}")


if __name__ == "__main__":
    main()
