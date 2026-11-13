"""Low-expression filtering — thresholds come from the frozen plan only.

The rule is applied before model fitting and its decision set is archived, so
genes can never be removed *because* they were non-significant. Filtered genes
receive an explicit status and are excluded from the multiple-testing family.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .plan import Plan


@dataclass
class FilterResult:
    keep: np.ndarray                 # bool over all genes
    status: np.ndarray               # 'tested' | 'filtered_low_expression' | 'all_zero'
    rule_description: str
    min_samples_effective: int
    per_gene_metric: np.ndarray      # the metric (CPM or count) the rule used
    n_kept: int
    n_filtered: int
    n_all_zero: int
    multiple_testing_family_note: str


def apply_filter(counts: np.ndarray, samples, plan: Plan) -> FilterResult:
    n_genes, n_samples = counts.shape
    rule = plan.filter_rule
    library = counts.sum(axis=0).astype(np.float64)
    # CPM with the actual library sizes (pseudocount only guards zero libraries).
    cpm = counts / np.where(library[None, :] == 0, np.nan, library[None, :]) * 1e6

    # Default min_samples: smallest number of samples in any condition involved
    # in a declared contrast (the standard "replicate count" convention).
    involved = set()
    for ct in plan.contrasts:
        involved.add(ct.numerator)
        involved.add(ct.denominator)
    per_cond = samples.groupby("condition").size()
    smallest = int(min(int(per_cond.get(c, 0)) for c in involved))
    min_samples = rule.min_samples if rule.min_samples is not None else smallest

    rowsum = counts.sum(axis=1)
    all_zero = rowsum == 0

    if rule.type == "cpm":
        metric = cpm
        passes = (cpm >= rule.min_value).sum(axis=1) >= min_samples
        desc = (f"CPM >= {rule.min_value} in at least {min_samples} samples "
                f"(smallest involved condition has {smallest} samples)")
    elif rule.type == "counts":
        metric = counts.astype(np.float64)
        passes = (counts >= int(rule.min_value)).sum(axis=1) >= min_samples
        desc = (f"raw count >= {int(rule.min_value)} in at least {min_samples} samples")
    else:
        metric = rowsum.astype(np.float64)
        passes = np.ones(n_genes, dtype=bool)
        desc = "no expression filtering (filter_rule.type=none)"

    keep = passes & (~all_zero)
    status = np.full(n_genes, "filtered_low_expression", dtype=object)
    status[all_zero] = "all_zero"
    status[keep] = "tested"

    note = (
        f"The multiple-testing family contains {int(keep.sum())} tested genes only. "
        f"{int((status == 'filtered_low_expression').sum())} low-expression genes and "
        f"{int(all_zero.sum())} all-zero genes are reported with independent statuses, "
        "kept out of the BH adjustment family, and never receive p-values."
    )
    return FilterResult(
        keep=keep, status=status, rule_description=desc,
        min_samples_effective=min_samples, per_gene_metric=np.nan_to_num(metric),
        n_kept=int(keep.sum()),
        n_filtered=int((status == "filtered_low_expression").sum()),
        n_all_zero=int(all_zero.sum()),
        multiple_testing_family_note=note)
