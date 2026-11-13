"""Plots. Every figure consumes the archived run outputs only — no ad-hoc
re-filtering or recomputation happens at plotting time."""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .qc import QCResult


def _save(fig, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_library_sizes(qc: QCResult, out: Path) -> None:
    m = qc.metrics.sort_values("library_size")
    colors = m["condition"].astype("category").cat.codes
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.bar(range(len(m)), m["library_size"], color=plt.cm.tab10(colors % 10))
    ax.set_xticks(range(len(m)))
    ax.set_xticklabels(m["sample_id"], rotation=60, ha="right", fontsize=7)
    ax.set_ylabel("library size (total raw counts)")
    ax.set_title("Library sizes (from archived QC metrics; no samples removed)")
    _save(fig, out)


def plot_zero_fraction(qc: QCResult, out: Path) -> None:
    m = qc.metrics.sort_values("zero_fraction")
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.bar(range(len(m)), m["zero_fraction"],
           color=plt.cm.viridis(m["zero_fraction"]))
    ax.set_xticks(range(len(m)))
    ax.set_xticklabels(m["sample_id"], rotation=60, ha="right", fontsize=7)
    ax.set_ylabel("zero-count fraction")
    ax.set_title("Per-sample zero fraction (advisory QC only)")
    _save(fig, out)


def plot_pca(qc: QCResult, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(6.5, 5.5))
    conds = sorted(qc.pca["condition"].unique())
    batches = sorted(qc.pca["batch"].unique())
    markers = ["o", "s", "^", "D", "v"]
    for bi, b in enumerate(batches):
        for ci, c in enumerate(conds):
            sub = qc.pca[(qc.pca["condition"] == c) & (qc.pca["batch"] == b)]
            if len(sub) == 0:
                continue
            ax.scatter(sub["PC1"], sub["PC2"],
                       marker=markers[bi % len(markers)],
                       color=plt.cm.tab10(ci % 10), s=70,
                       label=f"{c} / batch {b}", edgecolor="black", linewidth=0.4)
    for _, r in qc.pca.iterrows():
        ax.annotate(r["sample_id"], (r["PC1"], r["PC2"]),
                    fontsize=6, xytext=(3, 3), textcoords="offset points")
    ve = qc.pca_variance_explained
    ax.set_xlabel(f"PC1 ({ve[0]*100:.1f}% var)")
    ax.set_ylabel(f"PC2 ({ve[1]*100:.1f}% var)")
    ax.set_title("PCA on log2(CPM+1) — color=condition, shape=batch")
    ax.legend(fontsize=7, framealpha=0.9)
    _save(fig, out)


def plot_volcano(table: pd.DataFrame, contrast_name: str, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(7, 6))
    t = table[table["status"] == "tested"]
    sig = t["p_value_bh"] < 0.05
    ax.scatter(t.loc[~sig, "log2_fold_change"],
               -np.log10(t.loc[~sig, "p_value"]),
               s=14, color="grey", alpha=0.6, label="BH >= 0.05")
    ax.scatter(t.loc[sig, "log2_fold_change"],
               -np.log10(t.loc[sig, "p_value"]),
               s=22, color="#c0392b", label="BH < 0.05")
    ax.axhline(-np.log10(0.05), color="blue", lw=0.8, ls="--")
    ax.set_xlabel("log2 fold change (fitted, normalized)")
    ax.set_ylabel("-log10 raw p-value")
    ax.set_title(f"Volcano — {contrast_name}")
    ax.legend(fontsize=8)
    _save(fig, out)


def plot_ma(table: pd.DataFrame, contrast_name: str, out: Path,
            base_mean: pd.Series) -> None:
    fig, ax = plt.subplots(figsize=(7, 6))
    t = table[table["status"] == "tested"].copy()
    t["base_mean"] = base_mean.loc[t["gene_id"]].to_numpy()
    sig = t["p_value_bh"] < 0.05
    ax.scatter(np.log2(t.loc[~sig, "base_mean"] + 1),
               t.loc[~sig, "log2_fold_change"],
               s=14, color="grey", alpha=0.6, label="BH >= 0.05")
    ax.scatter(np.log2(t.loc[sig, "base_mean"] + 1),
               t.loc[sig, "log2_fold_change"],
               s=22, color="#c0392b", label="BH < 0.05")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_xlabel("log2 mean normalized count + 1")
    ax.set_ylabel("log2 fold change")
    ax.set_title(f"MA plot — {contrast_name}")
    ax.legend(fontsize=8)
    _save(fig, out)


def plot_dispersion(base_mean: pd.Series, dispersions: np.ndarray,
                    gene_ids: np.ndarray, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(7, 5.5))
    ok = np.isfinite(dispersions)
    ax.scatter(np.log2(base_mean.loc[gene_ids[ok]].to_numpy() + 1),
               np.log10(dispersions[ok]), s=12, alpha=0.6)
    ax.set_xlabel("log2 mean normalized count + 1")
    ax.set_ylabel("log10 NB dispersion (alpha, Var=mu+alpha*mu^2)")
    ax.set_title("Per-gene dispersion estimates")
    _save(fig, out)


def plot_gene_counts(gene_id: str, counts_row: np.ndarray, mu_row: np.ndarray,
                     samples: pd.DataFrame, size_factors: np.ndarray,
                     out: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    conds = sorted(samples["condition"].unique())
    colors = {c: plt.cm.tab10(i % 10) for i, c in enumerate(conds)}
    x = np.arange(len(samples))
    axes[0].bar(x, counts_row, color=[colors[c] for c in samples["condition"]])
    axes[0].set_xticks(x)
    axes[0].set_xticklabels(samples["sample_id"], rotation=60, ha="right", fontsize=7)
    axes[0].set_title(f"{gene_id}: raw counts")
    axes[0].set_ylabel("raw count")

    norm_counts = counts_row / size_factors
    axes[1].scatter(x, norm_counts,
                    color=[colors[c] for c in samples["condition"]],
                    s=55, label="normalized count", zorder=3)
    axes[1].plot(x, mu_row / size_factors, "k--", lw=1, label="fitted mean / SF")
    axes[1].set_xticks(x)
    axes[1].set_xticklabels(samples["sample_id"], rotation=60, ha="right", fontsize=7)
    axes[1].set_title(f"{gene_id}: normalized counts vs fitted values")
    axes[1].set_ylabel("count / size factor")
    handles = [plt.Line2D([0], [0], marker="o", color="w", markerfacecolor=v,
                          label=k, markersize=8) for k, v in colors.items()]
    axes[1].legend(handles=handles, fontsize=8)
    _save(fig, out)
