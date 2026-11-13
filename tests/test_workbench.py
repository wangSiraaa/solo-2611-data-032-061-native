"""Unit/integration tests. Run with: .venv/bin/python -m pytest tests -q"""
import json

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from deworkbench.design import build_design, contrast_vector
from deworkbench.errors import DesignError, ValidationError
from deworkbench.filtering import apply_filter
from deworkbench.fitting import custom_fit, nb_loglik
from deworkbench.generate import generate_dataset, make_confounded_dataset
from deworkbench.inference import _bh
from deworkbench.io import load_dataset
from deworkbench.plan import parse_plan


@pytest.fixture(scope="session")
def independent_data(tmp_path_factory):
    d = tmp_path_factory.mktemp("ind")
    generate_dataset(d, design="independent", n_genes=120, n_per_condition=8,
                     n_effect_genes=12, seed=11, prefix="t")
    plan = parse_plan({
        "design": "independent", "conditions": ["control", "treated"],
        "contrasts": [{"name": "c", "numerator": "treated",
                       "denominator": "control"}],
        "filter_rule": {"type": "cpm", "min_value": 1.0},
    })
    return d, plan


@pytest.fixture(scope="session")
def paired_data(tmp_path_factory):
    d = tmp_path_factory.mktemp("pair")
    generate_dataset(d, design="paired", n_genes=100, n_subjects=8,
                     n_per_condition=8, n_effect_genes=10, seed=12, prefix="p")
    plan = parse_plan({
        "design": "paired", "conditions": ["control", "treated"],
        "contrasts": [{"name": "c", "numerator": "treated",
                       "denominator": "control"}],
        "filter_rule": {"type": "cpm", "min_value": 1.0},
    })
    return d, plan


def test_plan_rejects_unknown_contrast_condition():
    with pytest.raises(ValidationError, match="unknown numerator condition"):
        parse_plan({
            "design": "independent", "conditions": ["a", "b"],
            "contrasts": [{"name": "x", "numerator": "z", "denominator": "b"}]})


def test_plan_requires_exclusion_reason(tmp_path):
    with pytest.raises(ValidationError):
        parse_plan({
            "design": "independent", "conditions": ["a", "b"],
            "contrasts": [{"name": "x", "numerator": "a", "denominator": "b"}],
            "excluded_sample_ids": ["s1"]})


def test_id_alignment_not_positional(independent_data):
    d, plan = independent_data
    ds = load_dataset(d / "counts.csv", d / "samples.csv", plan)
    counts_df = pd.read_csv(d / "counts.csv")
    rng = np.random.default_rng(7)
    perm = rng.permutation(ds.n_samples)
    shuffled = counts_df[["gene_id"] +
                         [counts_df.columns[1:][i] for i in perm]]
    shuffled_path = d / "shuffled.csv"
    shuffled.to_csv(shuffled_path, index=False)
    ds2 = load_dataset(shuffled_path, d / "samples.csv", plan)
    assert np.array_equal(ds.counts, ds2.counts)
    assert list(ds.sample_ids) == list(ds2.sample_ids)


def test_missing_sample_blocks(independent_data):
    d, plan = independent_data
    c = pd.read_csv(d / "counts.csv")
    c = c.drop(columns=c.columns[1])
    c.to_csv(d / "missing.csv", index=False)
    with pytest.raises(ValidationError, match="same sample set"):
        load_dataset(d / "missing.csv", d / "samples.csv", plan)


def test_illegal_counts_block(independent_data):
    d, plan = independent_data
    c = pd.read_csv(d / "counts.csv")
    c[c.columns[1]] = c[c.columns[1]].astype(float)
    c.iloc[0, 1] = -1
    c.to_csv(d / "neg.csv", index=False)
    with pytest.raises(ValidationError, match="negative counts"):
        load_dataset(d / "neg.csv", d / "samples.csv", plan)


def test_confounded_design_blocked(tmp_path):
    d = tmp_path / "conf"
    make_confounded_dataset(d, seed=3, n_genes=40)
    plan = parse_plan({
        "design": "independent", "conditions": ["control", "treated"],
        "contrasts": [{"name": "c", "numerator": "treated",
                       "denominator": "control"}]})
    ds = load_dataset(d / "counts.csv", d / "samples.csv", plan)
    with pytest.raises(DesignError, match="complete confounding"):
        build_design(ds.samples, plan)


def test_paired_design_has_subject_columns(paired_data):
    d, plan = paired_data
    ds = load_dataset(d / "counts.csv", d / "samples.csv", plan)
    design = build_design(ds.samples, plan)
    assert any("subject" in c for c in design.columns)
    assert design.rank == len(design.columns)


def test_paired_and_independent_structures_differ(independent_data, paired_data):
    di, pi = independent_data
    dp, pp = paired_data
    dsi = load_dataset(di / "counts.csv", di / "samples.csv", pi)
    dsp = load_dataset(dp / "counts.csv", dp / "samples.csv", pp)
    cols_i = build_design(dsi.samples, pi).columns
    cols_p = build_design(dsp.samples, pp).columns
    assert not any("subject" in c for c in cols_i)
    assert any("subject" in c for c in cols_p)


def test_missing_pair_blocked_then_dropped(paired_data):
    d, plan = paired_data
    s = pd.read_csv(d / "samples.csv")
    c = pd.read_csv(d / "counts.csv")
    drop = s.iloc[0]["sample_id"]  # one half of a subject's pair
    s2 = s[s["sample_id"] != drop]
    c2 = c.drop(columns=[drop])
    s2.to_csv(d / "s_missing.csv", index=False)
    c2.to_csv(d / "c_missing.csv", index=False)
    with pytest.raises(ValidationError, match="missing required condition"):
        load_dataset(d / "c_missing.csv", d / "s_missing.csv", plan)
    drop_plan = parse_plan({**plan.to_dict(), "missing_pair_policy": "drop_subject"})
    ds = load_dataset(d / "c_missing.csv", d / "s_missing.csv", drop_plan)
    assert ds.dropped_subjects and ds.n_samples == len(s2) - 1


def test_replicate_sum_collapse(paired_data):
    d, plan = paired_data
    s = pd.read_csv(d / "samples.csv")
    c = pd.read_csv(d / "counts.csv")
    row = s.iloc[0].copy()
    original_id = row["sample_id"]
    row["sample_id"] = "extra_rep"
    s2 = pd.concat([s, pd.DataFrame([row])], ignore_index=True)
    c2 = c.copy()
    c2["extra_rep"] = c2[original_id]
    s2.to_csv(d / "s_rep.csv", index=False)
    c2.to_csv(d / "c_rep.csv", index=False)
    with pytest.raises(ValidationError, match="repeated"):
        load_dataset(d / "c_rep.csv", d / "s_rep.csv", plan)
    sum_plan = parse_plan({**plan.to_dict(),
                           "duplicate_subject_policy": "sum"})
    ds = load_dataset(d / "c_rep.csv", d / "s_rep.csv", sum_plan)
    assert ds.n_samples == len(s)  # one row per subject-condition again
    assert ds.collapse_records and "extra_rep" in ds.collapse_records[0][
        "merged_sample_ids"]


def test_filter_statuses_and_family(independent_data):
    d, plan = independent_data
    ds = load_dataset(d / "counts.csv", d / "samples.csv", plan)
    fr = apply_filter(ds.counts, ds.samples, plan)
    assert set(fr.status) <= {"tested", "filtered_low_expression", "all_zero"}
    assert fr.n_kept + fr.n_filtered + fr.n_all_zero == ds.n_genes
    assert "before" in fr.multiple_testing_family_note or "family" in fr.multiple_testing_family_note


def test_all_zero_genes_never_tested(independent_data):
    d, plan = independent_data
    ds = load_dataset(d / "counts.csv", d / "samples.csv", plan)
    fr = apply_filter(ds.counts, ds.samples, plan)
    zero_rows = ds.counts.sum(axis=1) == 0
    assert (fr.status[zero_rows] == "all_zero").all()


def test_bh_adjustment_known_values():
    p = np.array([0.01, 0.02, 0.03, 0.5, 0.9])
    adj = _bh(p)
    # manual BH for m=5 sorted: .05,.05,.05,.625,.9 then monotone -> .05,.05,.05,.625,.9
    assert np.allclose(adj, [0.05, 0.05, 0.05, 0.625, 0.9])
    assert np.allclose(_bh(np.full(4, np.nan)), np.full(4, np.nan),
                       equal_nan=True)


def test_contrast_vectors_treatment_and_sum():
    samples = pd.DataFrame({
        "sample_id": [f"s{i}" for i in range(8)],
        "condition": ["a"] * 4 + ["b"] * 4,
        "batch": ["b1", "b2"] * 4,
        "subject_id": [f"s{i}" for i in range(8)]})
    ct = {"name": "bva", "numerator": "b", "denominator": "a"}
    pt = parse_plan({"design": "independent", "conditions": ["a", "b"],
                     "contrasts": [ct]})
    ps = parse_plan({"design": "independent", "conditions": ["a", "b"],
                     "contrasts": [ct], "encoding": "sum"})
    dt = build_design(samples, pt)
    ds = build_design(samples, ps)
    vt = contrast_vector(pt.contrasts[0], pt, dt.columns)
    vs = contrast_vector(ps.contrasts[0], ps, ds.columns)
    # Both encode b - a on the log scale.
    assert abs(vt[dt.columns.index("condition[b-vs-a]")] - 1.0) < 1e-12
    # Sum coding omits 'b': mu_a=int+coef_a, mu_b=int-coef_a, so
    # (b - a) maps to -2 on the single sum:a dummy.
    assert abs(vs[ds.columns.index("condition[sum:a]")] - (-2.0)) < 1e-12
    beta_t = np.array([1.0, 0.5, 0.1])
    beta_s = np.array([1.0, -0.25, 0.1])  # implies mu_a=.75, mu_b=1.25
    assert np.isclose(vt @ beta_t, 0.5)
    assert np.isclose(vs @ beta_s, 0.5)


def test_reverse_contrast_flips_vector():
    samples = pd.DataFrame({
        "sample_id": [f"s{i}" for i in range(8)],
        "condition": ["a"] * 4 + ["b"] * 4,
        "batch": ["b1", "b2"] * 4,
        "subject_id": [f"s{i}" for i in range(8)]})
    p1 = parse_plan({"design": "independent", "conditions": ["a", "b"],
                     "contrasts": [{"name": "f", "numerator": "b",
                                    "denominator": "a"}]})
    p2 = parse_plan({"design": "independent", "conditions": ["a", "b"],
                     "contrasts": [{"name": "r", "numerator": "a",
                                    "denominator": "b"}]})
    d = build_design(samples, p1)
    v1 = contrast_vector(p1.contrasts[0], p1, d.columns)
    v2 = contrast_vector(p2.contrasts[0], p2, d.columns)
    assert np.allclose(v1, -v2)


def test_custom_fit_recovers_known_coefficients():
    rng = np.random.default_rng(99)
    n = 120
    X = np.column_stack([np.ones(n), rng.normal(size=n),
                         rng.integers(0, 2, size=n).astype(float)])
    beta_true = np.array([2.5, 0.6, -0.4])
    alpha_true = 0.25
    mu = np.exp(X @ beta_true)
    y = stats.nbinom.rvs(n=1 / alpha_true,
                         p=(1 / alpha_true) / (1 / alpha_true + mu),
                         random_state=rng).astype(float)
    out, err = custom_fit(y, X, np.zeros(n))
    assert err == "" and out is not None
    beta_hat, vcov, mu_hat, alpha, _, _ = out
    # Monte-Carlo recovery with n=120; SEs are ~0.05-0.12.
    assert np.abs(beta_hat - beta_true).max() < 0.3
    assert abs(alpha - alpha_true) < 0.15
    se = np.sqrt(np.diag(vcov))
    assert np.all(np.abs(beta_hat - beta_true) < 2.5 * se)
    # normal equations satisfied at the IRLS optimum
    w = mu_hat / (1 + alpha * mu_hat)
    z = np.log(mu_hat) + (y - mu_hat) / mu_hat
    assert np.max(np.abs((X * w[:, None]).T @ X @ beta_hat
                         - (X * w[:, None]).T @ z)) < 1e-6


def test_null_pvalue_calibration():
    rng = np.random.default_rng(2024)
    n = 40
    samples = pd.DataFrame({
        "sample_id": [f"s{i}" for i in range(n)],
        "condition": ["ctrl"] * 20 + ["trt"] * 20,
        "batch": ["b1", "b2"] * 20,
        "subject_id": [f"s{i}" for i in range(n)]})
    plan = parse_plan({
        "design": "independent", "conditions": ["ctrl", "trt"],
        "contrasts": [{"name": "c", "numerator": "trt", "denominator": "ctrl"}],
        "filter_rule": {"type": "none"}})
    design = build_design(samples, plan)
    mu = np.exp(design.X @ np.array([3.0, 0.0, 0.2]))
    pvals = []
    for _ in range(200):
        y = stats.nbinom.rvs(n=1 / 0.3, p=(1 / 0.3) / (1 / 0.3 + mu),
                             random_state=rng).astype(float)
        out, _ = custom_fit(y, design.X, np.zeros(n))
        beta, vcov, *_ = out
        j = 1
        t = beta[j] / np.sqrt(vcov[j, j])
        pvals.append(2 * stats.t.sf(abs(t), df=design.residual_df))
    rate = float(np.mean(np.array(pvals) < 0.05))
    assert rate < 0.12  # small-sample Wald/t anti-conservatism bounded


def test_nb_loglik_matches_scipy():
    rng = np.random.default_rng(0)
    y = rng.integers(0, 10, size=50).astype(float)
    mu = np.full(50, 4.0)
    alpha = 0.4
    direct = nb_loglik(y, mu, alpha)
    sp = float(np.sum(stats.nbinom.logpmf(y, n=1 / alpha,
                                          p=(1 / alpha) / (1 / alpha + mu))))
    assert np.isclose(direct, sp)


def test_pearson_correction_moves_toward_unit_chi2():
    from deworkbench.dispersion import (pearson_bias_correct_alpha,
                                         pearson_damping)
    rng = np.random.default_rng(5)
    n = 16
    X = np.column_stack([np.ones(n), rng.integers(0, 2, n).astype(float)])
    alpha_true, beta_true = 0.4, np.array([3.0, 0.0])
    mu = np.exp(X @ beta_true)
    y = stats.nbinom.rvs(n=1 / alpha_true,
                         p=(1 / alpha_true) / (1 / alpha_true + mu),
                         random_state=rng).astype(float)
    df = n - X.shape[1]
    # deliberately start from a downward-biased dispersion
    alpha0 = 0.05
    alpha_c, beta, vcov, _ = pearson_bias_correct_alpha(
        y, X, np.zeros(n), alpha0, df)
    mu_c = np.exp(X @ beta)
    chi2_df = np.sum((y - mu_c) ** 2 / (mu_c + alpha_c * mu_c ** 2)) / df
    assert 0.5 < chi2_df < 1.8
    assert pearson_damping(6) == 1.0 and pearson_damping(30) == 0.3
    assert np.isfinite(vcov).all()


def test_squeeze_var_finite_and_ordered():
    from deworkbench.ql import squeeze_var
    rng = np.random.default_rng(3)
    s2 = rng.gamma(shape=3, scale=0.3, size=100)
    post, d0, prior_scale, df_total = squeeze_var(s2, df=6)
    assert np.isfinite(post).all() and np.isfinite(d0)
    # posterior values are shrunk toward the prior -> lower variance than raw
    assert np.var(np.log(post)) <= np.var(np.log(s2)) + 1e-12
    assert df_total > 6


def test_plan_dispersion_flag_parses():
    p = parse_plan({
        "design": "independent", "conditions": ["a", "b"],
        "contrasts": [{"name": "x", "numerator": "a", "denominator": "b"}],
        "moderate_dispersion": False})
    assert p.moderate_dispersion is False
    assert parse_plan({
        "design": "independent", "conditions": ["a", "b"],
        "contrasts": [{"name": "x", "numerator": "a", "denominator": "b"}]
    }).moderate_dispersion is True


def test_retain_policy_keeps_replicates_documented(paired_data):
    d, plan = paired_data
    s = pd.read_csv(d / "samples.csv")
    c = pd.read_csv(d / "counts.csv")
    row = s.iloc[0].copy()
    original_id = row["sample_id"]
    row["sample_id"] = "extra_rep"
    s2 = pd.concat([s, pd.DataFrame([row])], ignore_index=True)
    c2 = c.copy()
    c2["extra_rep"] = c2[original_id]
    s2.to_csv(d / "s_retain.csv", index=False)
    c2.to_csv(d / "c_retain.csv", index=False)
    retain_plan = parse_plan({**plan.to_dict(),
                              "duplicate_subject_policy": "retain"})
    ds = load_dataset(d / "c_retain.csv", d / "s_retain.csv", retain_plan)
    # rows are retained (not treated as new independent subjects) and recorded
    assert ds.n_samples == len(s2)
    assert ds.replicate_records
    assert ds.samples["subject_id"].nunique() == s["subject_id"].nunique()
