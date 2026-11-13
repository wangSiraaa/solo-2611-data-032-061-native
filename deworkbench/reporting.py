"""Static HTML reporting. Numbers rendered here are read back from the same
machine-readable CSV/JSON outputs produced by the pipeline — the pages never
compute statistics independently."""
from __future__ import annotations

import html
import json
from pathlib import Path

import numpy as np
import pandas as pd

CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
margin:24px;color:#1d2733;background:#f7f8fa}
h1,h2{color:#16324f}table{border-collapse:collapse;background:#fff;margin:12px 0}
th,td{border:1px solid #d6dbe1;padding:5px 9px;font-size:13px}
th{background:#eef2f7}code{background:#eef2f7;padding:1px 4px;border-radius:3px}
.fail{color:#b03a2e;font-weight:600}.zero{color:#7d6608}.filt{color:#6c7a89}
.small{font-size:12px;color:#566573}.card{background:#fff;border:1px solid #d6dbe1;
border-radius:8px;padding:14px 18px;margin:14px 0}
img{max-width:760px;border:1px solid #d6dbe1;border-radius:6px;background:#fff}
a{color:#1f618d}
"""


def _esc(x) -> str:
    return html.escape(str(x))


def _df_to_html(df: pd.DataFrame, float_cols=(), max_rows=None) -> str:
    if max_rows is not None:
        df = df.head(max_rows)
    head = "".join(f"<th>{_esc(c)}</th>" for c in df.columns)
    rows = []
    for _, r in df.iterrows():
        cells = []
        for c in df.columns:
            v = r[c]
            if isinstance(v, (float, np.floating)):
                cells.append(f"<td>{'' if pd.isna(v) else f'{v:.6g}'}</td>")
            else:
                cls = ""
                if c == "status":
                    cls = {"fit_failed": "fail", "all_zero": "zero",
                           "filtered_low_expression": "filt"}.get(str(v), "")
                cells.append(f'<td class="{cls}">{_esc(v)}</td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def write_run_index(run_dir: Path, manifest: dict, qc, filter_result,
                    contrast_tables: dict[str, pd.DataFrame],
                    design_columns: list[str]) -> None:
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>"
             f"DE workbench run {_esc(manifest['run_id'])}</title><style>{CSS}</style></head><body>"]
    parts.append(f"<h1>Run {_esc(manifest['run_id'])}</h1>")
    parts.append("<div class='card'><h2>Frozen plan</h2>")
    parts.append(f"<p>plan hash: <code>{_esc(manifest['plan_hash'])}</code>; "
                 f"design: <code>{_esc(manifest['design'])}</code>; "
                 f"encoding: <code>{_esc(manifest['encoding'])}</code>; "
                 f"seed: <code>{_esc(manifest['random_seed'])}</code></p>")
    parts.append(f"<p class='small'>{_esc(filter_result.rule_description)}</p>")
    parts.append(f"<p class='small'>{_esc(filter_result.multiple_testing_family_note)}</p></div>")

    parts.append("<div class='card'><h2>Input summary</h2>")
    parts.append(_df_to_html(pd.DataFrame([manifest["input_summary"]])))
    if manifest.get("exclusions"):
        parts.append("<h3>Sample exclusions (plan-declared only)</h3>")
        parts.append(_df_to_html(pd.DataFrame(manifest["exclusions"])))
    else:
        parts.append("<p class='small'>No samples excluded.</p>")
    parts.append("</div>")

    parts.append("<div class='card'><h2>Sample QC (advisory; no auto-removal)</h2>")
    parts.append(_df_to_html(qc.metrics))
    parts.append("<h3>Advisory flags</h3>")
    parts.append(_df_to_html(qc.flags))
    parts.append("<p class='small'>" + _esc(qc.note) + "</p>")
    parts.append("<img src='figures/pca.png'><img src='figures/library_sizes.png'>"
                 "<img src='figures/zero_fraction.png'></div>")

    parts.append("<div class='card'><h2>Design matrix coding</h2><p>Columns: "
                 + ", ".join(f"<code>{_esc(c)}</code>" for c in design_columns) + "</p>")
    parts.append("<p class='small'>Full design matrix: <code>design_matrix.csv</code></p></div>")

    parts.append("<div class='card'><h2>Contrasts</h2><ul>")
    for name, tab in contrast_tables.items():
        n_sig = int((tab["p_value_bh"] < 0.05).sum())
        n_fail = int((tab["status"] == "fit_failed").sum())
        parts.append(f"<li><a href='contrast_{_esc(name)}.html'>{_esc(name)}</a> — "
                     f"family n={int((tab['status']=='tested').sum())}, "
                     f"BH&lt;0.05: {n_sig}, fit_failed: {n_fail}</li>")
    parts.append("</ul></div>")
    parts.append("<div class='card'><h2>Per-gene review</h2>"
                 "<p>Open <code>genes/&lt;gene_id&gt;.html</code> (index: "
                 "<a href='gene_index.html'>gene_index.html</a>) for raw counts, "
                 "grouping, fitted values, diagnostics and contrast definitions.</p></div>")
    parts.append("</body></html>")
    (run_dir / "index.html").write_text("".join(parts), encoding="utf-8")


def write_contrast_page(run_dir: Path, contrast_name: str, table: pd.DataFrame,
                        contrast_meta: dict) -> None:
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>{_esc(contrast_name)}</title>"
             f"<style>{CSS}</style></head><body>"]
    parts.append(f"<h1>Contrast {_esc(contrast_name)}</h1>")
    parts.append("<div class='card'><h2>Definition</h2>")
    parts.append(f"<p>numerator <code>{_esc(contrast_meta['numerator'])}</code> "
                 f"minus denominator <code>{_esc(contrast_meta['denominator'])}</code> "
                 "on the log scale (reported / log(2)).</p>")
    vec = {k: v for k, v in contrast_meta["vector"].items() if v != 0}
    parts.append("<p>Parameter vector c: <code>" + _esc(json.dumps(vec)) + "</code></p></div>")
    n_tested = int((table["status"] == "tested").sum())
    n_sig = int((table["p_value_bh"] < 0.05).sum())
    parts.append(f"<div class='card'><p>BH family size: {n_tested}; BH&lt;0.05: {n_sig}; "
                 f"fit_failed: {int((table['status']=='fit_failed').sum())}; "
                 f"filtered: {int((table['status']=='filtered_low_expression').sum())}; "
                 f"all-zero: {int((table['status']=='all_zero').sum())}</p>")
    parts.append(f"<img src='figures/volcano_{_esc(contrast_name)}.png'>")
    parts.append(f"<img src='figures/ma_{_esc(contrast_name)}.png'></div>")
    cols = ["gene_id", "status", "log2_fold_change", "se_log2fc",
            "ci95_low_log2fc", "ci95_high_log2fc", "p_value", "p_value_bh",
            "dispersion", "fit_method", "failure_reason"]
    show = table[cols].sort_values(["status", "p_value_bh"],
                                   na_position="last")
    parts.append("<div class='card'><h2>All genes (statuses kept separate)</h2>")
    parts.append(_df_to_html(show))
    parts.append("</div></body></html>")
    (run_dir / f"contrast_{contrast_name}.html").write_text(
        "".join(parts), encoding="utf-8")


def write_gene_index(run_dir: Path, gene_ids: np.ndarray,
                     status_by_contrast: dict[str, pd.DataFrame]) -> None:
    base = pd.DataFrame({"gene_id": gene_ids})
    for name, tab in status_by_contrast.items():
        base[f"status:{name}"] = tab["status"].to_numpy()
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>Gene index</title>"
             f"<style>{CSS}</style></head><body><h1>Gene index</h1><ul>"]
    for g in gene_ids:
        parts.append(f"<li><a href='genes/{_esc(g)}.html'>{_esc(g)}</a></li>")
    parts.append("</ul></body></html>")
    (run_dir / "gene_index.html").write_text("".join(parts), encoding="utf-8")


def write_gene_page(run_dir: Path, gene_id: str, info: dict) -> None:
    gdir = run_dir / "genes"
    gdir.mkdir(exist_ok=True)
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>{_esc(gene_id)}</title>"
             f"<style>{CSS}</style></head><body>"]
    parts.append(f"<h1>Gene {_esc(gene_id)}</h1>")
    parts.append("<p><a href='../index.html'>back to run index</a></p>")
    parts.append("<div class='card'><h2>Status &amp; fit diagnostics</h2>")
    diag = info["diagnostics"]
    parts.append(_df_to_html(pd.DataFrame([diag])))
    if diag.get("failure_reason"):
        parts.append(f"<p class='fail'>Failure: {_esc(diag['failure_reason'])}</p>")
    parts.append("</div>")
    parts.append("<div class='card'><h2>Raw counts, grouping and fitted values</h2>")
    parts.append(_df_to_html(info["sample_table"]))
    if info.get("figure"):
        parts.append(f"<img src='../figures/gene_{_esc(gene_id)}.png'>")
    parts.append("</div>")
    parts.append("<div class='card'><h2>Contrast results (this gene)</h2>")
    parts.append(_df_to_html(info["contrast_table"]))
    parts.append("<h3>Contrast definitions</h3>")
    for m in info["contrast_meta"]:
        vec = {k: v for k, v in m["vector"].items() if v != 0}
        parts.append(f"<p><b>{_esc(m['name'])}</b>: {_esc(m['numerator'])} vs "
                     f"{_esc(m['denominator'])}; c=<code>{_esc(json.dumps(vec))}</code></p>")
    parts.append("</div></body></html>")
    (gdir / f"{gene_id}.html").write_text("".join(parts), encoding="utf-8")
