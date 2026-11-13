"""Synthetic data generation. All inputs used by the workbench are produced by
this module — no external biological databases.

A negative-binomial generative model with:
  * condition-specific log fold changes (implanted effects),
  * subject-level random intercepts (paired designs),
  * batch offsets,
  * per-gene dispersion and base abundance,
  * an optional extreme-count gene and all-zero genes.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats


@dataclass
class GeneratedData:
    counts: np.ndarray
    gene_ids: list[str]
    samples: pd.DataFrame
    truth: dict


def _nb_rvs(mu: np.ndarray, alpha: float, rng: np.random.Generator) -> np.ndarray:
    """NB with Var = mu + alpha*mu^2 (shape n = 1/alpha)."""
    n = 1.0 / alpha
    p = n / (n + mu)
    return stats.nbinom.rvs(n=n, p=p, random_state=rng)


def generate_dataset(
    out_dir: str | Path,
    *,
    design: str = "independent",
    n_genes: int = 400,
    n_per_condition: int = 8,
    n_batches: int = 2,
    n_subjects: int | None = None,
    effect_genes: dict[str, float] | None = None,
    n_effect_genes: int = 30,
    log2fc_range: tuple[float, float] = (-2.0, 2.0),
    subject_sd: float = 0.35,
    batch_sd: float = 0.25,
    dispersion_range: tuple[float, float] = (0.05, 0.6),
    base_logmean_range: tuple[float, float] = (1.5, 8.0),
    extreme_gene: bool = False,
    n_all_zero: int = 5,
    n_low_expression: int = 20,
    seed: int = 20261113,
    balanced_batches: bool = True,
    prefix: str = "synth",
) -> GeneratedData:
    rng = np.random.default_rng(seed)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conditions = ["control", "treated"]

    # ---- Sample table -------------------------------------------------------
    if design == "paired":
        n_subjects = n_subjects or n_per_condition
        rows = []
        for s in range(n_subjects):
            sid = f"{prefix}_s{s:03d}"
            for ci, cond in enumerate(conditions):
                if balanced_batches:
                    # Balance batches *within* subject so batch is estimable
                    # alongside subject and condition effects.
                    batch = f"batch{((s + ci) % n_batches) + 1}"
                else:
                    batch = f"batch{rng.integers(1, n_batches + 1)}"
                rows.append({"sample_id": f"{sid}_{cond}",
                             "condition": cond, "batch": batch,
                             "subject_id": sid})
        samples = pd.DataFrame(rows)
    else:
        rows = []
        total = n_per_condition * len(conditions)
        for ci, cond in enumerate(conditions):
            for k in range(n_per_condition):
                idx = ci * n_per_condition + k
                sid = f"{prefix}_i{idx:03d}"
                if balanced_batches:
                    batch = f"batch{(idx % n_batches) + 1}"
                else:
                    batch = f"batch{rng.integers(1, n_batches + 1)}"
                rows.append({"sample_id": sid, "condition": cond,
                             "batch": batch, "subject_id": sid})
        samples = pd.DataFrame(rows)

    # ---- Gene-level parameters ---------------------------------------------
    gene_ids = [f"GENE{g:04d}" for g in range(n_genes)]
    base_mu = np.exp(rng.uniform(*base_logmean_range, size=n_genes))
    alphas = rng.uniform(*dispersion_range, size=n_genes)

    log2fc = np.zeros(n_genes)
    if effect_genes:
        for g, fc in effect_genes.items():
            if g in gene_ids:
                log2fc[gene_ids.index(g)] = fc
        chosen = [gene_ids.index(g) for g in effect_genes if g in gene_ids]
    else:
        chosen = list(rng.choice(
            [i for i in range(n_genes)], size=min(n_effect_genes, n_genes),
            replace=False))
        log2fc[chosen] = rng.uniform(*log2fc_range, size=len(chosen))
    effect_map = {gene_ids[i]: float(log2fc[i]) for i in chosen}

    # All-zero and low-expression genes (kept distinct from effect genes).
    pool = [i for i in range(n_genes) if i not in chosen]
    zero_idx = sorted(pool[:n_all_zero])
    low_idx = sorted(pool[n_all_zero:n_all_zero + n_low_expression])
    for i in low_idx:
        base_mu[i] = np.exp(rng.uniform(-3.0, -0.5))

    extreme_idx = None
    if extreme_gene:
        extreme_idx = pool[n_all_zero + n_low_expression]
        base_mu[extreme_idx] = 5e4
        alphas[extreme_idx] = 0.02

    # ---- Subject and batch offsets -----------------------------------------
    subject_levels = sorted(samples["subject_id"].unique())
    subj_effect = {s: rng.normal(0, subject_sd) for s in subject_levels}
    batch_levels = sorted(samples["batch"].unique())
    # constrain batch offsets to sum ~0 for interpretability
    boff = rng.normal(0, batch_sd, size=len(batch_levels))
    boff -= boff.mean()
    batch_effect = dict(zip(batch_levels, boff))

    # ---- Counts -------------------------------------------------------------
    n_samples = len(samples)
    counts = np.zeros((n_genes, n_samples), dtype=np.int64)
    cond_index = {c: j for j, c in enumerate(conditions)}
    for j, row in samples.iterrows():
        log_mu = (np.log(base_mu)
                  + np.log(2.0) * log2fc * cond_index[row["condition"]]
                  + subj_effect[row["subject_id"]]
                  + batch_effect[row["batch"]])
        mu = np.exp(log_mu)
        col = _nb_rvs(mu, alphas, rng)
        for i in zero_idx:
            col[i] = 0
        counts[:, j] = col

    truth = {
        "seed": seed,
        "design": design,
        "effect_genes_log2fc": effect_map,
        "all_zero_genes": [gene_ids[i] for i in zero_idx],
        "low_expression_genes": [gene_ids[i] for i in low_idx],
        "extreme_gene": gene_ids[extreme_idx] if extreme_idx is not None else None,
        "dispersions": dict(zip(gene_ids, alphas.tolist())),
        "subject_effects": subj_effect,
        "batch_effects": batch_effect,
        "generative_model": "NB: Var=mu+alpha*mu^2; log-mu = base + log2(2)*log2fc*I(treated) "
                            "+ subject intercept + batch offset",
    }

    counts_df = pd.DataFrame(counts, columns=samples["sample_id"])
    counts_df.insert(0, "gene_id", gene_ids)
    counts_df.to_csv(out_dir / "counts.csv", index=False)
    samples.to_csv(out_dir / "samples.csv", index=False)
    (out_dir / "gene_ids.txt").write_text(
        "\n".join(gene_ids) + "\n", encoding="utf-8")
    import json
    (out_dir / "truth.json").write_text(
        json.dumps(truth, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8")
    return GeneratedData(counts=counts, gene_ids=gene_ids,
                         samples=samples, truth=truth)


def make_confounded_dataset(out_dir: str | Path, *, seed: int = 77,
                            n_genes: int = 200) -> GeneratedData:
    """Condition and batch perfectly aliased: control only in batch1, treated
    only in batch2. The precheck must reject this design."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    conditions, batches = ["control", "treated"], ["batch1", "batch2"]
    rows = []
    for ci, (cond, batch) in enumerate(zip(conditions, batches)):
        for k in range(6):
            rows.append({"sample_id": f"conf_i{ci*6+k:03d}",
                         "condition": cond, "batch": batch,
                         "subject_id": f"conf_i{ci*6+k:03d}"})
    samples = pd.DataFrame(rows)
    gene_ids = [f"GENE{g:04d}" for g in range(n_genes)]
    base = np.exp(rng.uniform(1.5, 7.0, n_genes))
    alphas = rng.uniform(0.05, 0.5, n_genes)
    counts = np.zeros((n_genes, len(samples)), dtype=np.int64)
    for j, row in samples.iterrows():
        mu = base * (2.0 if row["condition"] == "treated" else 1.0)
        counts[:, j] = _nb_rvs(mu, alphas, rng)
    counts_df = pd.DataFrame(counts, columns=samples["sample_id"])
    counts_df.insert(0, "gene_id", gene_ids)
    counts_df.to_csv(out_dir / "counts.csv", index=False)
    samples.to_csv(out_dir / "samples.csv", index=False)
    (out_dir / "gene_ids.txt").write_text("\n".join(gene_ids) + "\n")
    return GeneratedData(counts, gene_ids, samples, {"seed": seed,
                                                      "confounded": True})
