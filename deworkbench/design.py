"""Design matrix construction and identifiability diagnostics.

The design is built explicitly (treatment or sum coding), with full rank
information returned to the caller. We never hand a rank-deficient matrix to a
library and let it silently drop columns: redundant columns are identified here
and either reported as an error or mapped explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .errors import DesignError, ValidationError
from .plan import Plan, Contrast


@dataclass
class DesignResult:
    X: np.ndarray                  # (n_samples, p)
    columns: list[str]             # parameter names, length p
    frame: pd.DataFrame            # human-readable design matrix
    coding: dict                   # term -> reference/omitted level info
    rank: int
    n_samples: int
    residual_df: int
    term_of_column: list[str]


def _treatment_indicator(values: pd.Series, levels: list[str],
                         ref: str, prefix: str) -> tuple[np.ndarray, list[str], dict]:
    cols, names = [], []
    for lev in levels:
        if lev == ref:
            continue
        cols.append((values == lev).astype(float).to_numpy())
        names.append(f"{prefix}[{lev}-vs-{ref}]")
    mat = np.column_stack(cols) if cols else np.empty((len(values), 0))
    return mat, names, {"reference": ref, "levels": levels}


def _sum_indicator(values: pd.Series, levels: list[str],
                   prefix: str) -> tuple[np.ndarray, list[str], dict]:
    # Classic sum-to-zero coding: k-1 dummies, omitted level maps to -1.
    ref = sorted(levels)[-1]
    cols, names = [], []
    for lev in levels:
        if lev == ref:
            continue
        col = np.where(values == lev, 1.0, np.where(values == ref, -1.0, 0.0))
        cols.append(col)
        names.append(f"{prefix}[sum:{lev}]")
    mat = np.column_stack(cols) if cols else np.empty((len(values), 0))
    return mat, names, {"reference": ref, "levels": levels}


def build_design(samples: pd.DataFrame, plan: Plan) -> DesignResult:
    n = len(samples)
    pieces: list[np.ndarray] = [np.ones((n, 1))]
    columns: list[str] = ["Intercept"]
    terms: list[str] = ["Intercept"]
    coding: dict[str, dict] = {}

    cond_levels = sorted(plan.conditions)
    ref = plan.ref_level
    cond = samples["condition"]
    if plan.encoding == "treatment":
        mat, names, info = _treatment_indicator(cond, cond_levels, ref, "condition")
    else:
        mat, names, info = _sum_indicator(cond, cond_levels, "condition")
    if mat.shape[1]:
        pieces.append(mat)
        columns += names
        terms += ["condition"] * len(names)
    coding["condition"] = {**info, "encoding": plan.encoding}

    # Paired design changes the *model structure*: subject fixed effects block
    # on the individual, they are not just a label on the samples.
    if plan.design == "paired":
        subj_levels = sorted(samples["subject_id"].unique())
        subj = samples["subject_id"]
        subj_ref = subj_levels[0]
        mat, names, info = _treatment_indicator(subj, subj_levels, subj_ref, "subject")
        if mat.shape[1]:
            pieces.append(mat)
            columns += names
            terms += ["subject"] * len(names)
        coding["subject"] = {**info, "encoding": "treatment"}

    if plan.include_batch:
        batch_levels = sorted(samples["batch"].unique())
        if len(batch_levels) > 1:
            batch = samples["batch"]
            if plan.encoding == "treatment":
                mat, names, info = _treatment_indicator(
                    batch, batch_levels, batch_levels[0], "batch")
            else:
                mat, names, info = _sum_indicator(batch, batch_levels, "batch")
            if mat.shape[1]:
                pieces.append(mat)
                columns += names
                terms += ["batch"] * len(names)
            coding["batch"] = {**info, "encoding": plan.encoding}
        else:
            coding["batch"] = {"encoding": "none", "reason": "single batch observed"}

    X = np.column_stack(pieces)
    frame = pd.DataFrame(X, columns=columns)
    frame.insert(0, "sample_id", samples["sample_id"].to_numpy())

    rank = int(np.linalg.matrix_rank(X))
    res = DesignResult(X=X, columns=columns, frame=frame, coding=coding,
                       rank=rank, n_samples=n, residual_df=n - rank,
                       term_of_column=terms)
    _check_identifiability(res, samples, plan)
    return res


def _check_identifiability(design: DesignResult, samples: pd.DataFrame,
                           plan: Plan) -> None:
    X, p = design.X, len(design.columns)

    # Targeted confounding diagnostics first (clearer than a generic rank error).
    _check_confounding(samples, "condition", "batch", plan)
    if plan.design == "paired":
        _check_subject_confounding(samples)

    if design.rank < p:
        # Name the offending columns with an explicit pivoted QR, instead of
        # relying on the GLM library to drop them.
        R = np.linalg.qr(X, mode="r")
        tol = X.shape[0] * np.finfo(float).eps * np.abs(np.diag(R)).max()
        diag = np.abs(np.diag(R))
        redundant = [design.columns[j] for j, d in enumerate(diag) if d <= tol]
        raise DesignError(
            "design matrix is rank deficient (rank "
            f"{design.rank} < {p} columns); redundant columns: {redundant}. "
            "Likely complete confounding between condition and batch/subject.")

    # Empty conditions / too few samples per condition.
    counts_per_cond = samples.groupby("condition").size()
    for cond in plan.conditions:
        n_c = int(counts_per_cond.get(cond, 0))
        if n_c == 0:
            raise DesignError(f"condition {cond!r} has zero samples")
        if n_c < 2:
            raise DesignError(
                f"condition {cond!r} has only {n_c} sample; cannot estimate "
                "biological variability with a single observation per condition")

    if design.residual_df < 1:
        raise DesignError(
            f"no residual degrees of freedom (n={design.n_samples}, rank={design.rank}); "
            "add samples or simplify the model")
    if design.residual_df < 2:
        # Not fatal, but dispersion estimation is nearly impossible.
        design.coding["_warning"] = (
            "residual df = 1; dispersion estimates will be unstable")


def _check_confounding(samples: pd.DataFrame, a: str, b: str, plan: Plan) -> None:
    if b not in samples.columns:
        return
    tab = samples.groupby(a)[b].nunique()
    fully = tab[tab == 1]
    if len(fully) == len(tab):
        mapping = samples.groupby(a)[b].first().to_dict()
        # Every condition lives in exactly one batch. Check whether batches are
        # shared across conditions — if not, condition and batch are aliased.
        if len(set(mapping.values())) == len(mapping):
            raise DesignError(
                f"complete confounding: each condition appears in exactly one batch "
                f"({mapping}); condition and batch effects cannot be separated. "
                "Redesign the experiment or remove batch from the plan and document it.")


def _check_subject_confounding(samples: pd.DataFrame) -> None:
    subj_conds = samples.groupby("subject_id")["condition"].nunique()
    if (subj_conds == 1).all():
        mapping = samples.groupby("subject_id")["condition"].first().to_dict()
        raise DesignError(
            "paired design but every subject appears in only one condition "
            f"({mapping}); subject effects are then aliased with condition. "
            "Use design=independent or supply true paired observations.")


def contrast_vector(contrast: Contrast, plan: Plan,
                    design_columns: list[str]) -> np.ndarray:
    """Return a full-length parameter vector c such that c'beta is the contrast."""
    c = np.zeros(len(design_columns))
    for j, name in enumerate(design_columns):
        if plan.encoding == "treatment":
            ref = plan.ref_level
            num_col = f"condition[{contrast.numerator}-vs-{ref}]"
            den_col = f"condition[{contrast.denominator}-vs-{ref}]"
            if name == num_col:
                c[j] += 1.0
            if name == den_col:
                c[j] -= 1.0
        else:
            levels = sorted(plan.conditions)
            omitted = levels[-1]
            for lev in levels[:-1]:
                col = f"condition[sum:{lev}]"
                if name == col:
                    # group mean = mu + coef (lev), = mu - sum(coefs) (omitted)
                    num_k = 1.0 if contrast.numerator == lev else (
                        -1.0 if contrast.numerator == omitted else 0.0)
                    den_k = 1.0 if contrast.denominator == lev else (
                        -1.0 if contrast.denominator == omitted else 0.0)
                    c[j] += num_k - den_k
    if not np.any(c):
        raise DesignError(
            f"contrast {contrast.label()} produced a zero vector — not estimable "
            f"from columns {design_columns}")
    return c
