"""Sample-level quality control — reporting only.

No sample is ever removed because of a plot. Exclusions happen exclusively via
the frozen plan's excluded_sample_ids with a documented reason; this module only
measures library size, zero fraction and global structure and flags extremes.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class QCResult:
    metrics: pd.DataFrame
    pca: pd.DataFrame
    pca_variance_explained: np.ndarray
    flags: pd.DataFrame
    note: str


def sample_qc(counts: np.ndarray, samples: pd.DataFrame,
              size_factors: np.ndarray) -> QCResult:
    n_genes, n_samples = counts.shape
    library = counts.sum(axis=0).astype(float)
    zero_frac = (counts == 0).mean(axis=0)
    nonzero = counts[counts.sum(axis=1) > 0]
    log_cpm = np.log2(nonzero / library[None, :] * 1e6 + 1.0)

    # PCA on centered log-CPM (global structure; no sample removal).
    centered = log_cpm - log_cpm.mean(axis=1, keepdims=True)
    # SVD on samples x genes for memory economy.
    u, s, vt = np.linalg.svd(centered.T, full_matrices=False)
    var = (s ** 2) / np.sum(s ** 2)
    coords = u * s
    n_pc = min(4, coords.shape[1])
    pca = pd.DataFrame({
        "sample_id": samples["sample_id"].to_numpy(),
        **{f"PC{k+1}": coords[:, k] for k in range(n_pc)},
    })
    for col in ("condition", "batch"):
        pca[col] = samples[col].to_numpy()
    if "subject_id" in samples.columns:
        pca["subject_id"] = samples["subject_id"].to_numpy()

    # Median-correlation distance to other samples (another global-structure view).
    corr = np.corrcoef(log_cpm.T)
    np.fill_diagonal(corr, np.nan)
    median_corr = np.nanmedian(corr, axis=1)

    metrics = pd.DataFrame({
        "sample_id": samples["sample_id"].to_numpy(),
        "condition": samples["condition"].to_numpy(),
        "batch": samples["batch"].to_numpy(),
        "library_size": library,
        "size_factor": size_factors,
        "zero_fraction": zero_frac,
        "median_correlation_to_others": median_corr,
    })
    if "subject_id" in samples.columns:
        metrics.insert(3, "subject_id", samples["subject_id"].to_numpy())

    # Flags are advisory only; nothing is dropped here.
    lib_med = np.median(library)
    flags = pd.DataFrame({
        "sample_id": metrics["sample_id"],
        "library_size_below_half_median": library < 0.5 * lib_med,
        "library_size_above_double_median": library > 2.0 * lib_med,
        "zero_fraction_above_0.5": zero_frac > 0.5,
    })
    note = (
        "QC flags are advisory. No sample is removed automatically; exclusions "
        "must be declared in the frozen analysis plan with a reason and are "
        "archived under exclusions.json.")
    return QCResult(metrics=metrics, pca=pca,
                    pca_variance_explained=var[:n_pc], flags=flags, note=note)
