"""Self-contained HTML report: reconstruction timeline, model diagnostics,
held-out comparison, occupancy charts, verification summary.

No external assets; matplotlib is used to render PNG charts embedded as
base64 data URIs when available, and the page degrades to tables otherwise.
"""
from __future__ import annotations

import base64
import io
import json
import os
from html import escape
from typing import Any

from .common import load_json


def _load_optional(path: str) -> Any:
    if os.path.exists(path):
        try:
            return load_json(path)
        except Exception:
            return None
    return None


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    h = "".join(f"<th>{escape(str(x))}</th>" for x in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in row) + "</tr>"
        for row in rows)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>"


def _img_png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    import matplotlib.pyplot as plt
    plt.close(fig)
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    return f'<img src="data:image/png;base64,{data}" />'


def _occupancy_chart(sim_run: dict, station_order: list[str], title: str):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    stations = sim_run.get("stations", {})
    intervals = sim_run.get("occupancy_intervals") or sim_run.get("stations")
    if not intervals:
        return None
    fig, axes = plt.subplots(len(station_order), 1, figsize=(9, 2.2 * len(station_order)),
                             squeeze=False)
    for ax, sid in zip(axes[:, 0], station_order):
        for item in intervals.get(sid, []):
            start, end, kind, tid, si = item
            color = "#4c78a8" if kind == "service" else "#e45756"
            ax.barh(0.5, max(0.001, end - start), left=start, height=0.6,
                    color=color, alpha=0.8)
        ax.set_title(f"{sid} occupancy (blue=service, red=blocked)")
        ax.set_yticks([])
        ax.set_xlabel("time (ms)")
    fig.suptitle(title)
    fig.tight_layout()
    return _img_png(fig)


def render_html_report(data_dir: str, out_path: str) -> str:
    preflight = _load_optional(os.path.join(data_dir, "preflight.json"))
    calibration = _load_optional(os.path.join(data_dir, "calibration.json"))
    validation = _load_optional(os.path.join(data_dir, "validation.json"))
    comparison = _load_optional(os.path.join(data_dir, "comparison.json"))
    verification = _load_optional(os.path.join(data_dir, "verification.json"))
    timeline = _load_optional(os.path.join(data_dir, "timeline.json"))
    generation = _load_optional(os.path.join(data_dir, "generation.json"))

    parts: list[str] = []
    parts.append("""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>queueops report</title>
<style>
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:2rem;color:#222;line-height:1.45}
h1,h2,h3{margin-top:1.6rem}
table{border-collapse:collapse;margin:.6rem 0;font-size:13px}
th,td{border:1px solid #ccc;padding:4px 8px;text-align:left}
th{background:#f2f4f7}
code,pre{background:#f6f8fa;padding:2px 4px;border-radius:3px}
.bad{color:#b00020;font-weight:600}.ok{color:#1a7f37;font-weight:600}
.banner{padding:.6rem 1rem;border-radius:6px;margin:1rem 0}
.note{background:#eef4ff;border-left:4px solid #4c78a8;padding:.6rem 1rem}
img{max-width:100%;border:1px solid #eee;margin:.4rem 0}
</style></head><body><h1>queueops 观测重建 / 校准 / 留出验证 / 容量仿真报告</h1>""")

    parts.append('<div class="note">本报告只读取运营日志与配置构建的产物。'
                 '生成器真值文件仅由 <code>verifier</code> 用于恢复误差与偏差校验，'
                 '模型拟合路径不触碰它。</div>')

    # --- generation overview
    if generation:
        parts.append("<h2>1. 数据生成</h2>")
        parts.append(f"<p>事件总数：{generation.get('n_events', 'NA')}；"
                     f"随机种子：{generation.get('seed', 'NA')}</p>")

    # --- preflight / diagnostics
    if preflight:
        d = preflight.get("diagnostics", {})
        parts.append("<h2>2. 预检与事件重建诊断</h2>")
        parts.append(_table(
            ["项", "值"],
            [["保留事件数", preflight.get("n_events_kept")],
             ["错误", len(d.get("errors", []))],
             ["警告", len(d.get("warnings", []))],
             ["折叠重复", d.get("duplicates_folded", 0)],
             ["身份冲突", len(d.get("conflicts", []))],
             ["逆序", len(d.get("order_violations", []))],
             ["不可能占位", len(d.get("impossible", []))],
             ["未知任务", len(d.get("unknown_tasks", []))],
             ["无法解析事件", len(d.get("bad_events", []))]]))
        if d.get("order_violations"):
            parts.append("<h3>逆序 / 不可能占位样例</h3>")
            rows = [[json.dumps(x, ensure_ascii=False)][0] for x in
                    (d.get("order_violations", []) + d.get("impossible", []))[:8]]
            parts.append(_table(["定位"], rows))
        est = preflight.get("estimability")
        if est:
            parts.append("<h3>可估计性（服务/阻塞可分离程度）</h3>")
            bs = est.get("by_status", {})
            parts.append(_table(["状态桶", "阶段数"],
                                 [[k, v] for k, v in sorted(bs.items())]))
            n = est.get("service_blocking_unidentifiable_count", 0)
            parts.append(f'<p class="note">缺少 service_complete 但有离开事件时，'
                         f'服务与阻塞无法分离，仅有区间上界；此类阶段数：{n}。'
                         f'这些片段不会被当作完整样本，也绝不填零。</p>')

    # --- timeline
    if timeline:
        parts.append("<h2>3. 观测重建时间线（逐任务阶段）</h2>")
        tasks = timeline.get("tasks", [])
        # compact timeline: only tasks with events
        rows = []
        for t in tasks:
            for s in t.get("stages", []):
                if not s.get("start_ts") and not s.get("enqueue_ts"):
                    continue
                rows.append([
                    t["task_id"], s["stage_index"], s["station_id"], s["category"],
                    s.get("enqueue_ts"), s.get("start_ts"), s.get("complete_ts"),
                    s.get("leave_ts"),
                    s.get("queue_wait_ms"), s.get("service_ms"), s.get("blocking_ms"),
                    s.get("service_status"),
                ])
        parts.append(_table(
            ["task", "stage", "station", "category", "enqueue", "start", "complete",
             "leave", "queue_wait", "service", "blocking", "service_status"],
            rows[:400]))
        if len(rows) > 400:
            parts.append(f"<p><em>仅显示前 400 行，共 {len(rows)} 行；完整时间线见 "
                         f"<code>timeline.json</code>。</em></p>")
        parts.append('<p class="note">service_status 取值：exact=直接观测；'
                     'right_censored=观察期结束时仍在服务（右删失，仅下界）；'
                     'interval_upper=缺少 service_complete 但存在离开/最终离开，'
                     '只有上界 (0,U]；unknown=无法界定。缺失值绝不写成 0。</p>')

    # --- calibration
    if calibration:
        parts.append("<h2>4. 服务模型校准与诊断</h2>")
        parts.append(f"<p>训练窗口：{calibration.get('train_windows')}；"
                     f"有效样本总量：{calibration.get('total_effective_samples')}</p>")
        rows = []
        for c in calibration.get("choices", []):
            rows.append([c["group"], c["selected"], c["n_exact"], c["n_right"],
                         c["n_upper"], c["n_unknown"], c["n_effective"],
                         c["decision"], c.get("fallback_reason") or ""])
        parts.append(_table(
            ["组", "选中模型", "exact", "right", "interval", "unknown",
             "有效样本", "决策", "回退原因"], rows))
        # method + drift
        parts.append("<h3>候选比较 / 选择方法</h3>")
        for c in calibration.get("choices", []):
            cand = _table(
                ["候选", "loglik", "AIC", "收敛", "exact", "right"],
                [[r["name"], r.get("loglik"), r.get("aic"), r.get("converged"),
                  r.get("n_exact"), r.get("n_right")] for r in c.get("candidates", [])])
            drift = c.get("drift")
            drift_txt = ""
            if drift:
                drift_txt = (f"<p>漂移检验：{drift['shifts_compared']} KS p="
                             f"{drift['p_value']:.3g}，检测到漂移={drift['drift_detected']}</p>")
            parts.append(f"<h4>{escape(c['group'])} — {escape(c['method'])}</h4>"
                         f"{cand}{drift_txt}")
            if c.get("diagnostics"):
                parts.append(_table(["级别", "代码", "说明"],
                                     [[g.get("severity"), g.get("code"),
                                       g.get("message")]
                                      for g in c["diagnostics"]]))
        parts.append("<h3>片段来源（完整/删失/不可估计）</h3>")
        rows = [[p["station_id"], p["category"], p["exact"],
                 p["right_censored"], p["interval_upper"], p["unknown"],
                 p["total"]] for p in calibration.get("provenance", [])]
        parts.append(_table(["station", "category", "exact", "right_censored",
                              "interval_upper", "unknown", "total"], rows))

    # --- validation
    if validation:
        parts.append("<h2>5. 留出期验证</h2>")
        parts.append(f"<p>留出窗口：{validation.get('holdout_windows')}；"
                     f"校准摘要 {validation.get('calibration_digest', '')[:12]}…；"
                     f"冻结={validation.get('frozen')}</p>")
        for r in validation.get("results", []):
            parts.append(f"<h3>{escape(r['shift_id'])} "
                         f"[{r['window'][0]}, {r['window'][1]})</h3>")
            parts.append(_table(
                ["指标", "观测", "仿真", "差异"],
                [["吞吐", r["observed"]["throughput"], r["simulated"]["throughput"],
                  r["throughput_delta"]],
                 ["到达数", r["arrivals"], "", ""],
                 ["未完成任务(censored)", "", r["simulated"].get("censored"), ""]]))
            rows = []
            for s, d in r["per_station"].items():
                rows.append([s,
                             d["queue_wait_obs"].get("mean"),
                             d["queue_wait_sim"].get("mean"),
                             _fmt(d["queue_mean_delta"]),
                             d["blocking_obs"].get("mean"),
                             d["blocking_sim"].get("mean"),
                             _fmt(d["blocking_mean_delta"]),
                             _fmt(d.get("service_occupancy_obs")),
                             _fmt(d.get("service_occupancy_sim")),
                             _fmt(d.get("blocked_occupancy_obs")),
                             _fmt(d.get("blocked_occupancy_sim"))])
            parts.append(_table(["station", "queue obs", "queue sim", "Δqueue",
                                  "block obs", "block sim", "Δblock",
                                  "svc占用 obs", "svc占用 sim",
                                  "blk占用 obs", "blk占用 sim"], rows))
            if r.get("unexplained_differences"):
                parts.append('<div class="banner note"><strong>模型无法解释的差异：</strong>'
                             + "<br>".join(escape(u) for u in
                                           r["unexplained_differences"]) + "</div>")
            if r.get("deadlock"):
                parts.append('<div class="banner bad">阻塞链：'
                             + escape(json.dumps(r["deadlock"], ensure_ascii=False))
                             + "</div>")

    # --- comparison
    if comparison:
        parts.append("<h2>6. 容量方案情景比较</h2>")
        parts.append(f"<p>基准班次：{comparison.get('base_shift')}；"
                     f"种子：{comparison.get('seeds')}；"
                     f"到达数：{comparison.get('arrivals')}</p>")
        rows = []
        for row in comparison.get("summary", []):
            qw = row["queue_wait_mean_by_station"]
            bl = row["blocking_mean_by_station"]
            rows.append([row["scenario"]["name"],
                         f"{row['throughput_mean']:.2f} ± {row['throughput_std']:.2f}",
                         row["censored_mean"],
                         " | ".join(f"{s}:{_fmt(qw.get(s))}" for s in sorted(qw)),
                         " | ".join(f"{s}:{_fmt(bl.get(s))}" for s in sorted(bl))])
        parts.append(_table(["方案", "吞吐(均值±std)", "censored",
                              "排队均值", "阻塞均值"], rows))
        inv = comparison.get("invariance", {})
        parts.append("<p>基础随机抽样跨方案一致性："
                     + escape(json.dumps({k: v["identical_base_draws"]
                                          for k, v in inv.items()})) + "</p>")
        gate = comparison.get("gate")
        if gate and gate.get("refused"):
            conf = comparison.get("confirmation") or {}
            parts.append('<div class="banner note">该模型版本存在不确定组 '
                         + escape(str([b["group"] for b in gate.get("blocked_groups", [])]))
                         + "，比较已要求操作者确认后才能运行。确认原因："
                         + escape(str(conf.get("reason", "(未确认 - 比较被拒绝)")))
                         + "</div>")
        if comparison.get("tradeoffs"):
            parts.append("<h3>等待改善与代价转移</h3>")
            tr = []
            for t in comparison["tradeoffs"]:
                tr.append([t["scenario"], _fmt(t["throughput_delta"]),
                           ", ".join(t["wait_improved_at"]) or "-",
                           ", ".join(t["wait_transferred_to"]) or "-",
                           t["explanation"]])
            parts.append(_table(["方案", "Δ吞吐", "等待改善于", "代价转移至",
                                  "解释"], tr))
        parts.append('<p class="note">多个 seed 展示预测变异；不以单次模拟保证收益。'
                     '同一任务的阶段基础随机抽样跨方案保持一致，'
                     '因此差异来自容量而非随机数顺序。</p>')

        sim = _load_optional(os.path.join(data_dir, "simulation.json"))
        occ_sim = (sim or {}).get("occupancy_intervals") or (sim or {}).get("stations")
        if occ_sim:
            parts.append("<h3>工位占用图（单独 simulate 运行）</h3>")
            occ = occ_sim
            station_order = sorted(occ.keys())
            img = _occupancy_chart(
                {"stations": sim.get("stations", {}), "occupancy_intervals": occ},
                station_order, "seat occupancy (service vs blocked)")
            if img:
                parts.append(img)
        # per-scenario occupancy charts from comparison runs
        runs = comparison.get("runs", [])
        if runs:
            parts.append("<h3>各容量方案工位占用（seed 取第一个）</h3>")
            shown = set()
            for run in runs:
                name = run["scenario"]["name"]
                seed = run["seed"]
                if name in shown or seed != comparison["seeds"][0]:
                    continue
                occ = run.get("stations")
                if not occ:
                    continue
                shown.add(name)
                img = _occupancy_chart({"occupancy_intervals": occ},
                                       sorted(occ.keys()),
                                       f"{name} (seed {seed})")
                if img:
                    parts.append(f"<h4>{escape(name)}</h4>" + img)

    # --- verification
    if verification:
        parts.append("<h2>7. 核验（真值仅用于误差报告）</h2>")
        rows = []
        for k, v in verification.items():
            if isinstance(v, dict) and "checks" in v:
                fails = [c for c, ok in v["checks"].items() if ok is False]
                rows.append([k, "PASS" if not fails else "FAIL " + ", ".join(fails)])
        parts.append(_table(["核验项", "结果"], rows))
        if verification.get("recovery"):
            rr = []
            for g in verification["recovery"]["groups"]:
                rr.append([f"{g['station_id']}/{g['category']}",
                           g.get("selected", ""), _fmt(g.get("fitted_mean")),
                           _fmt(g.get("true_mean")), _fmt(g.get("relative_error")),
                           g.get("n_effective"), g.get("status")])
            parts.append("<h3>参数恢复误差（统计层面，不要求逐样本完全一致）</h3>")
            parts.append(_table(["组", "模型", "拟合均值", "真值均值", "相对误差",
                                  "有效样本", "状态"], rr))
        if verification.get("prediction_bias"):
            pb = verification["prediction_bias"]
            parts.append(f"<p>留出预测偏差：最大 |Δqueue| = "
                         f"{_fmt(pb.get('max_abs_queue_delta'))} ms，"
                         f"比较数={pb.get('n_comparisons')}</p>")
        iso = verification.get("model_truth_isolation")
        if iso:
            parts.append(f"<p>模型代码未引用 truth.json："
                         f"{'是' if iso.get('clean') else '否 — ' + str(iso.get('offenders'))}"
                         f"</p>")

    parts.append("</body></html>")
    html = "\n".join(parts)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out_path


def _fmt(x):
    if x is None:
        return "NA"
    try:
        return f"{float(x):.2f}"
    except (TypeError, ValueError):
        return str(x)
