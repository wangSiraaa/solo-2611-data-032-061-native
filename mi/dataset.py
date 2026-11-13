"""Survey + applicability loading, cell-state construction, diagnostics.

Every cell of the analysis grid (row_id x variable) is classified into
exactly one state:

* OBSERVED       -- applicable and a value is present; never overwritten
* MISSING        -- applicable but empty: an ordinary unknown, imputed
* NOT_APPLICABLE -- structurally inapplicable: never imputed, and the
                    affected row is excluded from the analysis sample per
                    the plan's policy

All findings are collected and raised together as DiagnosticError; no
column or row is ever silently dropped.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from enum import IntEnum

import numpy as np

from .errors import DiagnosticError
from .plan import ROW_ID, Plan


class CellState(IntEnum):
    OBSERVED = 0
    MISSING = 1
    NOT_APPLICABLE = 2


STATE_LABELS = {
    CellState.OBSERVED: "observed",
    CellState.MISSING: "missing",
    CellState.NOT_APPLICABLE: "not_applicable",
}

APPLICABLE = "applicable"
NOT_APPLICABLE = "not_applicable"


@dataclass
class Dataset:
    """Canonical, row_id-sorted view of the survey grid."""

    row_ids: list  # sorted ascending; canonical order for all computation
    variables: list  # plan declaration order
    values: dict  # var -> float64 array, NaN where not observed
    states: dict  # var -> int8 array of CellState

    @property
    def n_rows(self):
        return len(self.row_ids)

    def state_counts(self, var):
        s = self.states[var]
        return {
            "observed": int(np.sum(s == CellState.OBSERVED)),
            "missing": int(np.sum(s == CellState.MISSING)),
            "not_applicable": int(np.sum(s == CellState.NOT_APPLICABLE)),
        }


def _read_csv(path, what):
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
    except OSError as exc:
        raise DiagnosticError([f"{what}: cannot read {path}: {exc}"])
    if not rows or not rows[0]:
        raise DiagnosticError([f"{what}: {path} is empty or has no header"])
    return rows[0], rows[1:]


def load_dataset(plan: Plan, survey_path, applicability_path) -> Dataset:
    findings = []
    var_names = plan.variable_names

    # ---------------- survey: structure ----------------
    header, raw_rows = _read_csv(survey_path, "survey")
    header = [h.strip() for h in header]
    if len(set(header)) != len(header):
        findings.append(f"survey: duplicate header names {sorted(header)}")
    if ROW_ID not in header:
        findings.append(f"survey: missing required {ROW_ID!r} column")
    declared = set(var_names)
    unknown_cols = [h for h in header if h not in declared and h != ROW_ID]
    if unknown_cols:
        findings.append(
            f"survey: columns not declared in plan: {unknown_cols}; "
            "refusing to ignore them silently"
        )
    missing_cols = [v for v in var_names if v not in header]
    if missing_cols:
        findings.append(f"survey: declared variables absent from file: {missing_cols}")
    if findings:
        raise DiagnosticError(findings)

    col = {name: header.index(name) for name in [ROW_ID] + var_names}
    n_declared = len(header)

    # ---------------- survey: row identities ----------------
    row_ids = []
    seen = set()
    for i, r in enumerate(raw_rows):
        if len(r) != n_declared:
            findings.append(
                f"survey: data row {i + 2} has {len(r)} fields, expected {n_declared}"
            )
            continue
        rid = r[col[ROW_ID]].strip()
        if not rid:
            findings.append(f"survey: data row {i + 2} has an empty {ROW_ID}")
        elif rid in seen:
            findings.append(f"survey: duplicate {ROW_ID} {rid!r}")
        else:
            seen.add(rid)
            row_ids.append(rid)
    if findings:
        raise DiagnosticError(findings)
    if not row_ids:
        raise DiagnosticError(["survey: no data rows"])

    # canonical order: sort by stable row identity so that input row
    # permutation cannot change any downstream result.
    order = sorted(range(len(row_ids)), key=lambda i: row_ids[i])
    row_ids = [row_ids[i] for i in order]
    raw_rows = [raw_rows[i] for i in order]

    # ---------------- survey: values ----------------
    values = {v: np.full(len(row_ids), np.nan) for v in var_names}
    for v in var_names:
        spec = plan.spec(v)
        arr = values[v]
        for i, r in enumerate(raw_rows):
            text = r[col[v]].strip()
            if text == "":
                continue
            try:
                x = float(text)
            except ValueError:
                findings.append(f"survey: {v} row {row_ids[i]!r}: non-numeric value {text!r}")
                continue
            if not np.isfinite(x):
                findings.append(f"survey: {v} row {row_ids[i]!r}: non-finite value {text!r}")
                continue
            if not (spec.lo <= x <= spec.hi):
                findings.append(
                    f"survey: {v} row {row_ids[i]!r}: observed value {x} outside "
                    f"declared range [{spec.lo}, {spec.hi}]"
                )
                continue
            arr[i] = x
    if findings:
        raise DiagnosticError(findings)

    # ---------------- applicability ----------------
    a_header, a_rows = _read_csv(applicability_path, "applicability")
    a_header = [h.strip() for h in a_header]
    if a_header != [ROW_ID, "column", "status"]:
        findings.append(
            f"applicability: header must be [{ROW_ID}, column, status], got {a_header}"
        )
        raise DiagnosticError(findings)

    status_grid = {}
    known_rows = set(row_ids)
    for i, r in enumerate(a_rows):
        if len(r) != 3:
            findings.append(f"applicability: row {i + 2} has {len(r)} fields, expected 3")
            continue
        rid, cname, status = (x.strip() for x in r)
        if rid not in known_rows:
            findings.append(f"applicability: row {i + 2}: unknown {ROW_ID} {rid!r}")
            continue
        if cname not in declared:
            findings.append(f"applicability: row {i + 2}: undeclared column {cname!r}")
            continue
        if status not in (APPLICABLE, NOT_APPLICABLE):
            findings.append(f"applicability: row {i + 2}: unknown status {status!r}")
            continue
        if (rid, cname) in status_grid:
            findings.append(f"applicability: duplicate entry for ({rid!r}, {cname!r})")
            continue
        status_grid[(rid, cname)] = status
    if findings:
        raise DiagnosticError(findings)

    # ---------------- cell states + contradictions ----------------
    states = {v: np.zeros(len(row_ids), dtype=np.int8) for v in var_names}
    for i, rid in enumerate(row_ids):
        for v in var_names:
            st = status_grid.get((rid, v))
            if st is None:
                findings.append(f"applicability: no status for cell ({rid!r}, {v!r})")
                continue
            observed = np.isfinite(values[v][i])
            if st == NOT_APPLICABLE and observed:
                findings.append(
                    f"applicability contradiction: cell ({rid!r}, {v!r}) is "
                    "not_applicable but carries an observed value"
                )
                continue
            if st == NOT_APPLICABLE:
                states[v][i] = CellState.NOT_APPLICABLE
            elif observed:
                states[v][i] = CellState.OBSERVED
            else:
                states[v][i] = CellState.MISSING
    if findings:
        raise DiagnosticError(findings)

    ds = Dataset(row_ids=row_ids, variables=list(var_names), values=values, states=states)

    # ---------------- scope + structural diagnostics ----------------
    # The response must be fully observed wherever applicable (task scope).
    resp = plan.response
    n_resp_missing = int(np.sum(ds.states[resp] == CellState.MISSING))
    if n_resp_missing:
        bad = [ds.row_ids[i] for i in np.flatnonzero(ds.states[resp] == CellState.MISSING)]
        raise DiagnosticError(
            [
                f"response {resp!r} has {n_resp_missing} ordinary-missing cells "
                f"(rows {bad}); a fully observed response is required"
            ]
        )

    # All-missing columns: no observed donor exists, so no library could
    # impute them -- diagnose here instead of letting a column be dropped.
    for v in var_names:
        c = ds.state_counts(v)
        if c["observed"] == 0:
            if c["not_applicable"] == ds.n_rows:
                findings.append(
                    f"variable {v!r}: every cell is not_applicable; no observed value "
                    "exists to anchor an imputation model"
                )
            else:
                findings.append(
                    f"variable {v!r}: all {c['missing']} applicable cells are empty; "
                    "an all-missing column cannot be imputed (no donors)"
                )
    if findings:
        raise DiagnosticError(findings)

    return ds


@dataclass
class Eligibility:
    eligible: np.ndarray  # bool per row (canonical order)
    reasons: list  # per-row reason string, "" when eligible

    @property
    def n_eligible(self):
        return int(np.sum(self.eligible))


def compute_eligibility(ds: Dataset, plan: Plan) -> Eligibility:
    """Analysis eligibility per row, with an explicit recorded reason.

    Policy (from the plan): rows affected by structural inapplicability in
    any analysis variable are excluded from this analysis.  Ordinary
    missing covariates do NOT exclude a row -- those cells are imputed.
    """
    n = ds.n_rows
    eligible = np.ones(n, dtype=bool)
    reasons = [""] * n
    for i in range(n):
        row_reasons = []
        if ds.states[plan.response][i] == CellState.NOT_APPLICABLE:
            row_reasons.append(f"response {plan.response!r} not_applicable")
        na_covs = [
            c for c in plan.covariates if ds.states[c][i] == CellState.NOT_APPLICABLE
        ]
        if na_covs:
            row_reasons.append("structural not_applicable: " + ", ".join(na_covs))
        if row_reasons:
            eligible[i] = False
            reasons[i] = "; ".join(row_reasons)

    # The analysis sample must be able to support the frozen model.
    findings = []
    n_params = len(plan.covariates) + (1 if plan.intercept else 0)
    n_eligible = int(np.sum(eligible))
    if n_eligible == 0:
        findings.append(
            "analysis sample: no eligible rows remain after applying the "
            "structural-inapplicability policy; cannot form an analysis sample"
        )
    elif n_eligible < n_params + 2:
        findings.append(
            f"analysis sample: {n_eligible} eligible rows cannot support "
            f"{n_params} parameters with residual degrees of freedom; "
            "cannot form an analysis sample"
        )
    if n_eligible:
        for c in plan.covariates:
            n_obs = int(
                np.sum((ds.states[c] == CellState.OBSERVED) & eligible)
            )
            if n_obs < 2:
                findings.append(
                    f"analysis sample: covariate {c!r} has {n_obs} observed values "
                    "among eligible rows; its coefficient would rest entirely on "
                    "imputation -- refusing to form the sample"
                )
    if findings:
        raise DiagnosticError(findings)
    return Eligibility(eligible=eligible, reasons=reasons)
