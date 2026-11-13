"""Independent verification of a run's artifacts.

Three independent recomputations, deliberately not reusing the fitting
code paths:

1. cell-by-cell invariance: every observed cell and every not_applicable
   mark in the inputs is unchanged in every completed dataset;
2. pooled uncertainty recomputed from the saved per-dataset coefficients
   and covariance matrices (an independent Rubin implementation);
3. when the input has no ordinary-missing cells, the pooled estimates are
   compared against a direct numpy least-squares fit on the raw input.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np

from .dataset import CellState, compute_eligibility, load_dataset
from .plan import load_plan


def _read_completed(path):
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    header, body = rows[0], rows[1:]
    out = {}
    for r in body:
        if len(r) != len(header):
            continue  # malformed row: counted as a violation by missing cells
        rid = r[0]
        for j, name in enumerate(header[1:]):
            out[(rid, name)] = r[j + 1]
    return out


def verify_run(plan_path, survey_path, applicability_path, out_dir,
               tol=1e-9):
    out = Path(out_dir)
    checks = []  # (name, ok, detail)

    plan = load_plan(plan_path)
    ds = load_dataset(plan, survey_path, applicability_path)
    elig = compute_eligibility(ds, plan)

    # ---------- 1. cell-by-cell invariance ----------
    completed_files = sorted(
        out.glob("completed/completed_*.csv"),
        key=lambda p: int(p.stem.split("_")[1]),
    )
    if len(completed_files) != plan.n_datasets:
        checks.append((
            "completed dataset count",
            False,
            f"expected {plan.n_datasets}, found {len(completed_files)}",
        ))
    n_bad = 0
    n_imputed_checked = 0
    for path in completed_files:
        cells = _read_completed(path)
        for i, rid in enumerate(ds.row_ids):
            for v in ds.variables:
                st = ds.states[v][i]
                got = cells.get((rid, v))
                if got is None:
                    n_bad += 1
                    continue
                if st == CellState.OBSERVED:
                    # float parse of a round-trip formatted value must be exact
                    if got == "" or float(got) != ds.values[v][i]:
                        n_bad += 1
                elif st == CellState.NOT_APPLICABLE:
                    if got != "":
                        n_bad += 1
                else:  # MISSING must be imputed, inside the declared range
                    spec = plan.spec(v)
                    if got == "":
                        n_bad += 1
                    else:
                        x = float(got)
                        if not (spec.lo <= x <= spec.hi):
                            n_bad += 1
                        n_imputed_checked += 1
    checks.append((
        "cell-by-cell invariance (observed unchanged, not_applicable empty, "
        "imputed in range)",
        n_bad == 0,
        f"{len(completed_files)} datasets, {n_imputed_checked} imputed cells "
        f"checked per dataset, {n_bad} violations",
    ))

    # ---------- analysis rows cross-check ----------
    ar_path = out / "analysis_rows.csv"
    ok_ar = True
    detail = ""
    if not ar_path.exists():
        ok_ar, detail = False, "analysis_rows.csv missing"
    else:
        with open(ar_path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        got = {r["row_id"]: (r["eligible"] == "true", r["reason"]) for r in rows}
        for i, rid in enumerate(ds.row_ids):
            want = (bool(elig.eligible[i]), elig.reasons[i])
            if got.get(rid) != want:
                ok_ar = False
                detail = f"mismatch at row {rid!r}: file {got.get(rid)} vs recomputed {want}"
                break
        else:
            detail = f"{len(rows)} rows match recomputed eligibility"
    checks.append(("analysis eligibility record", ok_ar, detail))

    # ---------- 2. independent Rubin recomputation ----------
    est_path = out / "estimates" / "per_dataset_estimates.csv"
    vcov_path = out / "estimates" / "per_dataset_vcov.json"
    pooled_path = out / "estimates" / "pooled.json"
    ok_pool, detail_pool = True, ""
    if not (est_path.exists() and vcov_path.exists() and pooled_path.exists()):
        checks.append(("independent re-pooling", False, "estimate artifacts missing"))
    else:
        with open(est_path, newline="", encoding="utf-8") as fh:
            est_rows = list(csv.DictReader(fh))
        vcov_doc = json.loads(vcov_path.read_text(encoding="utf-8"))
        pooled_saved = json.loads(pooled_path.read_text(encoding="utf-8"))
        terms = vcov_doc["terms"]
        labels = sorted(vcov_doc["datasets"].keys(),
                        key=lambda s: int(s.split("_")[1]))
        m = len(labels)
        # coefficients from the CSV (independent of the JSON params)
        Q = np.empty((m, len(terms)))
        for k, lab in enumerate(labels):
            for j, t in enumerate(terms):
                hit = [r for r in est_rows
                       if r["dataset"] == lab and r["term"] == t]
                Q[k, j] = float(hit[0]["estimate"])
        U = np.array([vcov_doc["datasets"][lab]["vcov"] for lab in labels])
        df_resid = vcov_doc["datasets"][labels[0]]["df_resid"]

        qbar = Q.mean(axis=0)
        Ubar = U.mean(axis=0)
        B = np.cov(Q, rowvar=False, ddof=1).reshape(Ubar.shape)
        T = Ubar + (1.0 + 1.0 / m) * B
        max_dev = 0.0
        for j, t in enumerate(terms):
            saved = pooled_saved["results"][j]
            if saved["term"] != t:
                ok_pool, detail_pool = False, f"term order mismatch at {j}"
                break
            u, b, tt = Ubar[j, j], B[j, j], T[j, j]
            se = math.sqrt(tt)
            if b <= 0.0:
                df = None
            else:
                lam = (1.0 + 1.0 / m) * b / tt
                df_old = (m - 1.0) / lam**2
                dfc = float(df_resid)
                df_obs = (1.0 - lam) * dfc * (dfc + 1.0) / (dfc + 3.0)
                df = df_old * df_obs / (df_old + df_obs) if df_obs > 0 else df_old
            dev = max(
                abs(qbar[j] - saved["estimate"]),
                abs(se - saved["std_error"]),
                abs(u - saved["within_var"]),
                abs(b - saved["between_var"]),
                abs(tt - saved["total_var"]),
            )
            if df is None:
                dev = max(dev, 0.0 if saved["df_infinite"] else 1.0)
            else:
                dev = max(dev, abs(df - saved["df"]) if not saved["df_infinite"] else 1.0)
            max_dev = max(max_dev, dev)
        else:
            if max_dev > tol:
                ok_pool = False
            detail_pool = f"max |recomputed - saved| = {max_dev:.3e} over {m} datasets"
        checks.append(("independent re-pooling (Rubin U/B/T, df)", ok_pool, detail_pool))

    # ---------- 3. no-missing direct fit ----------
    n_missing = sum(
        int(np.sum(ds.states[v] == CellState.MISSING)) for v in ds.variables
    )
    if n_missing == 0:
        row_idx = np.flatnonzero(elig.eligible)
        cols = [np.ones(len(row_idx))] if plan.intercept else []
        cols += [ds.values[c][row_idx] for c in plan.covariates]
        X = np.column_stack(cols)
        y = ds.values[plan.response][row_idx]
        beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        pooled_saved = json.loads(pooled_path.read_text(encoding="utf-8"))
        got = np.array([r["estimate"] for r in pooled_saved["results"]])
        dev = float(np.max(np.abs(beta - got)))
        checks.append((
            "no-missing input: pooled estimates vs direct numpy lstsq",
            dev < 1e-8,
            f"max |difference| = {dev:.3e}",
        ))
    else:
        checks.append((
            "no-missing direct fit",
            True,
            f"skipped ({n_missing} ordinary-missing cells present)",
        ))

    return checks
