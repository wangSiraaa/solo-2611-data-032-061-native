"""Input loading and hard validation.

Alignment rule: count-matrix columns and the sample table are joined **by
sample_id**, never by column position. Any mismatch (missing sample, duplicate
id, unknown condition, illegal count) raises ValidationError and blocks the run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .errors import ValidationError
from .plan import Plan

REQUIRED_SAMPLE_COLS = ("sample_id", "condition", "batch")
PAIRED_REQUIRED_COLS = REQUIRED_SAMPLE_COLS + ("subject_id",)


@dataclass
class ExclusionRecord:
    sample_id: str
    reason: str
    stage: str = "plan"


@dataclass
class Dataset:
    counts: np.ndarray                 # int64, shape (n_genes, n_samples)
    gene_ids: np.ndarray               # str, shape (n_genes,)
    sample_ids: np.ndarray             # str, aligned order
    samples: pd.DataFrame              # rows aligned with sample_ids
    exclusions: list[ExclusionRecord] = field(default_factory=list)
    collapse_records: list[dict] = field(default_factory=list)
    replicate_records: list[dict] = field(default_factory=list)
    dropped_subjects: list[dict] = field(default_factory=list)

    @property
    def n_genes(self) -> int:
        return self.counts.shape[0]

    @property
    def n_samples(self) -> int:
        return self.counts.shape[1]

    def library_sizes(self) -> np.ndarray:
        return self.counts.sum(axis=0).astype(np.float64)


def _read_counts(path: Path) -> tuple[np.ndarray, np.ndarray, list[str]]:
    df = pd.read_csv(path)
    if df.shape[1] < 2 or df.columns[0] != "gene_id":
        raise ValidationError(
            f"counts file {path} must have 'gene_id' as first column followed by "
            "one column per sample")
    gene_ids = df["gene_id"].astype(str).to_numpy()
    if len(set(gene_ids)) != len(gene_ids):
        dups = pd.Series(gene_ids).value_counts()
        raise ValidationError(
            f"duplicate gene_id values in counts: {dups[dups > 1].index.tolist()}")
    sample_cols = [str(c) for c in df.columns[1:]]
    if len(set(sample_cols)) != len(sample_cols):
        raise ValidationError("duplicate sample column names in count matrix")
    raw = df.iloc[:, 1:].to_numpy()
    # Illegal counts: NaN, negative or non-integer values block the analysis.
    if not np.isfinite(raw).all():
        raise ValidationError(f"counts file {path} contains NaN/inf values")
    if np.issubdtype(raw.dtype, np.floating):
        if np.any(raw != np.round(raw)):
            raise ValidationError(f"counts file {path} contains non-integer values")
        raw = np.round(raw).astype(np.int64)
    else:
        raw = raw.astype(np.int64)
    if (raw < 0).any():
        raise ValidationError(f"counts file {path} contains negative counts")
    return raw, gene_ids, sample_cols


def load_dataset(counts_path: str | Path,
                 samples_path: str | Path,
                 plan: Plan,
                 genes_path: str | Path | None = None) -> Dataset:
    counts_path, samples_path = Path(counts_path), Path(samples_path)
    counts, gene_ids, count_sample_ids = _read_counts(counts_path)

    if genes_path is not None:
        external = [str(x).strip() for x in Path(genes_path).read_text(
            encoding="utf-8").splitlines() if str(x).strip()]
        if external != gene_ids.tolist():
            raise ValidationError(
                "external gene identifier list does not match count-matrix gene_ids "
                f"(first mismatch around: {next((a for a, b in zip(external, gene_ids) if a != b), None)})")

    samples = pd.read_csv(samples_path, dtype=str)
    samples.columns = [str(c) for c in samples.columns]
    required = PAIRED_REQUIRED_COLS if plan.design == "paired" else REQUIRED_SAMPLE_COLS
    missing_cols = [c for c in required if c not in samples.columns]
    if missing_cols:
        raise ValidationError(f"samples table missing required columns: {missing_cols}")

    samples["sample_id"] = samples["sample_id"].astype(str)
    if samples["sample_id"].duplicated().any():
        dups = samples.loc[samples["sample_id"].duplicated(), "sample_id"].tolist()
        raise ValidationError(f"duplicate sample_id in samples table: {dups}")
    if samples["sample_id"].isna().any() or (samples["sample_id"] == "").any():
        raise ValidationError("samples table contains empty sample_id")
    if samples["condition"].isna().any() or (samples["condition"] == "").any():
        raise ValidationError("samples table contains empty condition")
    if samples["batch"].isna().any() or (samples["batch"] == "").any():
        raise ValidationError("samples table contains empty batch")

    table_ids = samples["sample_id"].tolist()
    count_set, table_set = set(count_sample_ids), set(table_ids)
    # Exact identity of sample sets; no positional matching.
    if count_set != table_set:
        only_counts = sorted(count_set - table_set)
        only_table = sorted(table_set - count_set)
        raise ValidationError(
            "count columns and samples table do not describe the same sample set; "
            f"only in counts: {only_counts}; only in samples table: {only_table}")

    # Plan exclusions must reference real samples and carry a reason.
    exclusions: list[ExclusionRecord] = []
    unknown_excl = [s for s in plan.excluded_sample_ids if s not in table_set]
    if unknown_excl:
        raise ValidationError(f"plan excludes unknown sample_ids: {unknown_excl}")
    for sid in plan.excluded_sample_ids:
        exclusions.append(ExclusionRecord(sid, plan.exclusion_reasons[sid]))

    keep = ~samples["sample_id"].isin(plan.excluded_sample_ids)
    samples = samples.loc[keep].reset_index(drop=True)

    # Conditions referenced by the plan must exist in the observed data.
    observed_conditions = set(samples["condition"])
    unknown_data_cond = sorted(observed_conditions - set(plan.conditions))
    if unknown_data_cond:
        raise ValidationError(
            f"samples have conditions not declared in plan: {unknown_data_cond}")
    empty_conds = [c for c in plan.conditions if c not in observed_conditions]
    if empty_conds:
        raise ValidationError(f"declared conditions with zero samples: {empty_conds}")

    # Explicit ID-based alignment: position in the count file is irrelevant.
    col_index = {sid: i for i, sid in enumerate(count_sample_ids)}
    aligned_pos = [col_index[s] for s in samples["sample_id"]]
    aligned_counts = counts[:, aligned_pos]
    aligned_ids = samples["sample_id"].to_numpy()

    ds = Dataset(counts=aligned_counts, gene_ids=gene_ids,
                 sample_ids=aligned_ids, samples=samples, exclusions=exclusions)

    if plan.design == "paired":
        _validate_paired(ds, plan)
    else:
        _validate_independent(ds)
    return ds


def _validate_independent(ds: Dataset) -> None:
    if "subject_id" in ds.samples.columns:
        sid = ds.samples["subject_id"].fillna("")
        repeated = sid[sid != ""].value_counts()
        repeated = repeated[repeated > 1]
        if len(repeated):
            raise ValidationError(
                "independent design but subject_id is reused across samples "
                f"(use design=paired or remove reuse): {repeated.index.tolist()}")


def _validate_paired(ds: Dataset, plan: Plan) -> None:
    s = ds.samples
    if s["subject_id"].isna().any() or (s["subject_id"] == "").any():
        raise ValidationError("paired design requires non-empty subject_id for every sample")

    # Extra replicates: more than one row for the same (subject, condition).
    grp = s.groupby(["subject_id", "condition"]).size()
    extra = grp[grp > 1]
    records = [{"subject_id": sub, "condition": cond, "n_rows": int(n)}
               for (sub, cond), n in extra.items()]
    if len(records):
        if plan.duplicate_subject_policy == "error":
            raise ValidationError(
                "paired design has repeated (subject_id, condition) rows; declare "
                f"duplicate_subject_policy 'sum' or 'retain'. Details: {records}")
        if plan.duplicate_subject_policy == "sum":
            _collapse_replicates(ds, records)
        else:  # retain: kept as documented technical repeats, not new subjects
            ds.replicate_records = records

    # Missing pairs: subjects that do not cover every declared condition.
    coverage = ds.samples.groupby("subject_id")["condition"].apply(set)
    required = set(plan.conditions)
    incomplete = coverage[coverage.apply(lambda x: x != required)]
    if len(incomplete):
        info = [{"subject_id": sub, "observed_conditions": sorted(cov)}
                for sub, cov in incomplete.items()]
        policy = getattr(plan, "missing_pair_policy", "error")
        if policy == "error":
            raise ValidationError(
                "paired design: subjects missing required condition(s); declare "
                f"missing_pair_policy 'drop_subject' to remove them. Details: {info}")
        drop_subjects = set(incomplete.index)
        keep = ~ds.samples["subject_id"].isin(drop_subjects)
        ds.samples = ds.samples.loc[keep].reset_index(drop=True)
        ds.counts = ds.counts[:, keep.to_numpy()]
        ds.sample_ids = ds.samples["sample_id"].to_numpy()
        ds.dropped_subjects = info
        if ds.samples.empty:
            raise ValidationError("all subjects dropped due to incomplete pairs")


def _collapse_replicates(ds: Dataset, records: list[dict]) -> None:
    """Sum technical replicate counts within (subject, condition); one row remains."""
    s = ds.samples.reset_index(drop=True)
    s["_orig_order"] = np.arange(len(s))
    keys = s[["subject_id", "condition"]].astype(str).agg("|".join, axis=1)
    key_to_rows: dict[str, list[int]] = {}
    for i, k in enumerate(keys):
        key_to_rows.setdefault(k, []).append(i)
    keep_rows, new_counts_cols, collapse = [], [], []
    for k, rows in key_to_rows.items():
        first = rows[0]
        keep_rows.append(first)
        new_counts_cols.append(ds.counts[:, rows].sum(axis=1))
        if len(rows) > 1:
            collapse.append({
                "subject_id": s.at[first, "subject_id"],
                "condition": s.at[first, "condition"],
                "merged_sample_ids": s.loc[rows, "sample_id"].tolist(),
                "resulting_sample_id": s.at[first, "sample_id"],
            })
    order = np.argsort(s.loc[keep_rows, "_orig_order"].to_numpy())
    keep_rows = [keep_rows[i] for i in order]
    new_counts_cols = [new_counts_cols[i] for i in order]
    ds.samples = s.loc[keep_rows].drop(columns="_orig_order").reset_index(drop=True)
    ds.counts = np.column_stack(new_counts_cols)
    ds.sample_ids = ds.samples["sample_id"].to_numpy()
    ds.collapse_records = collapse
