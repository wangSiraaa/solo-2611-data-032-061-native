"""Normalization: DESeq-style median-of-ratios size factors.

Size factors correct for sequencing depth / library composition. They are
computed once on the *filtered* expression set but applied to the full matrix
metadata; the raw counts are never modified.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import WorkbenchError


@dataclass
class NormResult:
    size_factors: np.ndarray          # geometric mean 1, length n_samples
    log_size_factors: np.ndarray      # used as GLM offset
    reference_genes: np.ndarray       # bool mask over all genes used in geometric mean
    method: str
    diagnostics: dict


def median_of_ratios(counts: np.ndarray,
                     eligible_mask: np.ndarray | None = None) -> NormResult:
    """Compute size factors on eligible genes; returns mask of genes actually used.

    Genes with any zero cannot contribute to the geometric-mean reference.
    """
    n_genes, n_samples = counts.shape
    if eligible_mask is None:
        eligible_mask = np.ones(n_genes, dtype=bool)
    sub = counts[eligible_mask].astype(np.float64)
    if sub.shape[0] == 0:
        raise WorkbenchError("no eligible genes for normalization")

    with np.errstate(divide="ignore"):
        log_sub = np.log(sub)
    log_sub[~np.isfinite(log_sub)] = np.nan
    # Geometric mean across samples, per gene (requires all-positive row).
    ref_log = np.nanmean(log_sub, axis=1)
    valid = np.isfinite(ref_log)
    if valid.sum() < max(2, n_samples // 2):
        raise WorkbenchError(
            f"only {int(valid.sum())} all-positive genes available for "
            "median-of-ratios normalization; need more expressed genes")
    ratios = np.exp(log_sub[valid] - ref_log[valid][:, None])
    sf = np.nanmedian(ratios, axis=0)
    if not np.isfinite(sf).all() or np.any(sf <= 0):
        raise WorkbenchError(f"non-finite/non-positive size factor produced: {sf}")
    # Scale to geometric mean 1 (mean of logs = 0).
    sf = sf / np.exp(np.mean(np.log(sf)))

    used = np.zeros(n_genes, dtype=bool)
    eligible_idx = np.where(eligible_mask)[0]
    used[eligible_idx[valid]] = True
    lib = counts.sum(axis=0).astype(np.float64)
    diag = {
        "n_genes_input": int(n_genes),
        "n_genes_eligible": int(eligible_mask.sum()),
        "n_genes_used": int(valid.sum()),
        "library_size_min": float(lib.min()),
        "library_size_max": float(lib.max()),
        "library_size_median": float(np.median(lib)),
        "size_factor_min": float(sf.min()),
        "size_factor_max": float(sf.max()),
        "size_factor_correlation_with_library_size": float(
            np.corrcoef(np.log(sf), np.log(lib))[0, 1]),
    }
    return NormResult(size_factors=sf, log_size_factors=np.log(sf),
                      reference_genes=used, method="median_of_ratios",
                      diagnostics=diag)
