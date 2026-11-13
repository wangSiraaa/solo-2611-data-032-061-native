"""Static diagnostic report (report.md) rendering."""
from __future__ import annotations

from .dataset import CellState
from .impute import PMM_DONORS


def _f(x, digits=4):
    if x is None:
        return "—"
    return f"{x:.{digits}g}"


def render_report(*, plan, ds, elig, chains, pooled, cc_fit, cc_note,
                  distributions, limitations, paths):
    L = []
    add = L.append

    add("# 多重插补诊断报告")
    add("")
    add("## 1. 输入与计划")
    add("")
    add(f"- 调查数据:`{paths['survey']}`({ds.n_rows} 行)")
    add(f"- 适用性表:`{paths['applicability']}`")
    add(f"- 分析计划:`{paths['plan']}`")
    add(f"- 下游模型(冻结):`{plan.response} ~ "
        + ("1 + " if plan.intercept else "")
        + " + ".join(plan.covariates) + "`(线性回归)")
    add(f"- 完成数据集 m = {plan.n_datasets},每链扫描 iterations = "
        f"{plan.n_iterations},种子 seed = {plan.seed}")
    add(f"- 结构性不适用策略:{plan.inapplicable_policy}")
    add("")

    add("## 2. 运行前诊断")
    add("")
    add("以下检查全部在插补之前完成;任何一项失败都会中止运行,不会交给库静默处理:")
    add("")
    add("- 计划模式:变量均为连续型且带合法范围;响应/协变量均属于允许预测列。")
    add("- 调查数据:`row_id` 唯一;无未声明列;观测值可解析且落在声明范围内。")
    add("- 适用性表:每个 (row_id, 变量) 单元都有合法状态;不存在 "
        "`not_applicable` 却带观测值的矛盾。")
    add("- 响应在适用单元上完整观测(任务前提)。")
    add("- 无全缺列(任何变量都至少有一个观测值可作为供体)。")
    add("- 分析样本可形成:资格行数足以支撑冻结模型的参数与剩余自由度。")
    add("")
    add("各变量单元状态:")
    add("")
    add("| 变量 | 观测 | 普通缺失(待插补) | 结构性不适用 | 声明范围 |")
    add("|---|---|---|---|---|")
    for v in ds.variables:
        c = ds.state_counts(v)
        spec = plan.spec(v)
        add(f"| {v} | {c['observed']} | {c['missing']} | "
            f"{c['not_applicable']} | [{_f(spec.lo)}, {_f(spec.hi)}] |")
    add("")

    add("## 3. 不适用行的分析资格")
    add("")
    n_excl = ds.n_rows - elig.n_eligible
    add(f"分析样本:{elig.n_eligible} / {ds.n_rows} 行合格,"
        f"{n_excl} 行被排除。排除逐行记录于 `analysis_rows.csv`。")
    if n_excl:
        add("")
        add("| row_id | 排除原因 |")
        add("|---|---|")
        for i, rid in enumerate(ds.row_ids):
            if not elig.eligible[i]:
                add(f"| {rid} | {elig.reasons[i]} |")
    add("")
    add("说明:被排除行中的普通缺失单元仍在完成数据集中按记录的方式插补"
        "(预测列不适用时退化为边际随机抽取),但这些行不进入任何一份拟合。")
    add("")

    add("## 4. 插补方法")
    add("")
    add("迭代条件插补(全条件设定,FCS)+ 贝叶斯线性回归后验抽取 + "
        "预测均值匹配(PMM)。")
    add("")
    add("**随机性来源(每变量每轮三重)**:")
    add("")
    add("1. 初始化:每个普通缺失单元从该变量观测值中有放回随机抽取(非均值填补)。")
    add("2. 后验抽取:平坦先验下 σ² ~ df·s²/χ²(df),β|σ² ~ N(β̂, σ²(XᵀX)⁻¹)。")
    add(f"3. 供体抽签:对每名受补者按预测均值取最近 {PMM_DONORS} 名供体,"
        "均匀随机选其一,取其**观测值**。")
    add("")
    add("**更新顺序**:变量按缺失数升序(同数按声明序)依次更新;每轮用最新"
        "条件值,共扫描 plan 声明的轮数;每条链独立构成一份完成数据集。")
    add("")
    add("**范围处理**:PMM 只返回通过范围校验的观测供体值,因此插补天然落在"
        "声明范围内;另设防御性截断并计数(预期为 0,若发生会记入链事件)。")
    add("")
    add("**随机流**:`SeedSequence([seed, chain, sweep, variable, purpose])`,"
        "行按 `row_id` 排序的规范顺序处理,输入文件重排不改变规范结果。")
    add("")

    add("## 5. 链状态与轨迹")
    add("")
    add("| 链 | 状态 | 事件 |")
    add("|---|---|---|")
    for ch in chains:
        ev = "; ".join(
            f"{e['kind']}({e['variable'] or '-'})" for e in ch.events
        ) or "—"
        add(f"| {ch.chain + 1} | {ch.status} | {ev} |")
    add("")
    impute_vars = [v for v in ds.variables
                   if any(v in ch.traces for ch in chains)]
    impute_vars = [v for v in impute_vars
                   if any(ds.states[v] == CellState.MISSING)]
    if impute_vars:
        add("插补单元均值轨迹(首 2 轮与末 2 轮;完整轨迹见 `chains.json`):")
        add("")
        add("| 变量 | 链 | 轮次:均值(SD) |")
        add("|---|---|---|")
        for v in impute_vars:
            for ch in chains:
                tr = ch.traces[v]
                pick = tr[:2] + tr[-2:] if len(tr) > 4 else tr
                txt = ", ".join(
                    f"t{p['iteration']}: {_f(p['mean'])}({_f(p['sd'])})"
                    for p in pick
                )
                add(f"| {v} | {ch.chain + 1} | {txt} |")
        add("")
    add("注意:轨迹仅供检查;达到计划轮数**不**构成收敛声明。")
    add("")

    add("## 6. 观测 / 插补分布")
    add("")
    add("| 变量 | 来源 | n | 均值 | SD | 最小 | 最大 |")
    add("|---|---|---|---|---|---|---|")
    for v, d in distributions.items():
        o = d["observed"]
        add(f"| {v} | 观测 | {o['n']} | {_f(o['mean'])} | {_f(o['sd'])} | "
            f"{_f(o['min'])} | {_f(o['max'])} |")
        if "imputed" in d:
            im = d["imputed"]
            add(f"| {v} | 插补(全部 {len(chains)} 份合并) | {im['n']} | "
                f"{_f(im['mean'])} | {_f(im['sd'])} | {_f(im['min'])} | "
                f"{_f(im['max'])} |")
    add("")

    add("## 7. 合并结果(Rubin 规则,正式结果)")
    add("")
    add(f"对 {pooled['m']} 份完成数据分别拟合同一冻结模型后合并;"
        "插补内(U)与插补间(B)变异分别保留,总方差 T = U + (1+1/m)B,"
        "自由度为 Barnard–Rubin 调整。")
    add("")
    add("| 项 | 估计 | SE | U(内) | B(间) | T(总) | df | FMI | 95% CI | p |")
    add("|---|---|---|---|---|---|---|---|---|---|")
    for r in pooled["results"]:
        df_txt = "Inf" if r["df_infinite"] else _f(r["df"])
        add(f"| {r['term']} | {_f(r['estimate'])} | {_f(r['std_error'])} | "
            f"{_f(r['within_var'])} | {_f(r['between_var'])} | "
            f"{_f(r['total_var'])} | {df_txt} | {_f(r['fmi'])} | "
            f"[{_f(r['ci_lo'])}, {_f(r['ci_hi'])}] | {_f(r['p_value'])} |")
    add("")

    add("## 8. 完整病例对照(非正式结果)")
    add("")
    if cc_fit is not None:
        add(f"完整病例 n = {cc_fit.n}(状态:{cc_note})。仅供对照,"
            "不按显著性取舍;正式结果以第 7 节为准。")
        add("")
        add("| 项 | 估计 | SE |")
        add("|---|---|---|")
        for j, term in enumerate(cc_fit.terms):
            add(f"| {term} | {_f(cc_fit.params[j])} | "
                f"{_f(cc_fit.std_errors[j])} |")
    else:
        add(f"完整病例拟合不可用:{cc_note}。")
    add("")

    add("## 9. 限制")
    add("")
    for lim in limitations:
        add(f"- {lim}")
    add("")

    add("## 10. 产物清单")
    add("")
    add("- `completed/completed_<k>.csv`:各份完成数据(不适用单元留空,未填补)")
    add("- `cell_states.csv`:逐单元状态(observed / missing / not_applicable)")
    add("- `analysis_rows.csv`:逐行分析资格与排除原因")
    add("- `estimates/per_dataset_estimates.csv`、`per_dataset_vcov.json`:"
        "每份估计与方差(供另一程序重新合并)")
    add("- `estimates/pooled.json` / `pooled.csv`:合并结果(U/B/T 分列)")
    add("- `estimates/complete_case.csv`:完整病例对照")
    add("- `chains.json`:链轨迹与事件;`diagnostics.json`:诊断汇总;"
        "`design.json`:冻结设计编码")
    add("")
    return "\n".join(L)
