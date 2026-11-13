# deworkbench — 可复现的合成计数矩阵差异表达分析工作台

面向**项目内部生成的非负整数计数数据**的本地差异表达（DE）流水线。仅依赖普通 pip
库（NumPy / SciPy / statsmodels / pandas / matplotlib / PyYAML），不接任何外部
生物数据库，结果只用于合成研究演示，不作临床结论。

核心原则：**不是把计数除以总量后逐基因做普通 t 检验**，而是对每个基因拟合带
测序深度偏移（median-of-ratios 归一化因子）、批次/受试者效应和过度离散参数的
负二项 GLM（NB2，Var = μ + αμ²），再按冻结计划中的对比做推断与 BH 调整。

## 安装

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e .
```

## 入口

| 命令 | 作用 |
|---|---|
| `deworkbench generate --out demo` | 生成 5 个合成场景（零差异/已知倍数/配对/极端高计数/完全混淆）与计划模板 |
| `deworkbench precheck --plan ... --counts ... --samples ...` | 只做输入校验与设计可识别性检查，不拟合 |
| `deworkbench analyze --plan ... --counts ... --samples ... --run-dir runs/x` | 完整流水线，产物全部落到自包含运行目录 |
| `deworkbench verify --plan ... --counts ... --samples ... --work runs/v` | 独立核验（列乱序、编码等价、反向对比、全零、失败状态、小矩阵手算） |
| `deworkbench simulate --out simulations --replicates 20` | 多种子重复模拟，报告检出率/假阳性（含 Wilson 区间），不挑种子 |
| `deworkbench view --run-dir runs/x --what manifest\|contrast\|qc\|gene` | 读取已归档的机器可读结果 |

测试：`.venv/bin/python -m pytest tests -q`。

## 端到端流程与保障

1. **冻结计划（plan）**：设计类型（`independent`/`paired`）、条件、对比、编码
   （`treatment`/`sum`）、样本排除（必须带理由）、低表达过滤规则、离散度处理开关、
   随机种子。计划先做 SHA-256 哈希冻结，过滤阈值在看到任何 p 值之前确定。
2. **严格输入校验（阻断式）**：计数列与样本表**按 `sample_id` 连接，绝不按列位置
   碰巧匹配**。重复 ID、样本集合不一致、负值/非整数/NaN 计数、计划引用未知条件、
   空条件、缺理由的排除等一律 `ValidationError` 阻断。
3. **配对设计核对**：同一受试者必须覆盖所需条件；缺配对默认阻断，或按
   `missing_pair_policy: drop_subject` 显式剔除并归档；同一 `(subject, condition)`
   的额外重复默认阻断，或按 `duplicate_subject_policy: sum` 合并计数（明确记录，
   不会被当成多个独立受试者）。
4. **设计可识别性预检**：显式构造设计矩阵并做 QR 秩检查，**在交给数值库之前**
   指出冗余列；专门检测条件—批次完全混淆、受试者—条件混淆、空条件、每条件样本
   过少、残差自由度不足。不允许库静默丢列后仍输出同名处理效应。
5. **样本质量报告（只报告不删除）**：库大小、零比例、中位相关、log-CPM 上的 PCA
   及建议性 flag。任何样本排除只能来自冻结计划并保留理由。
6. **冻结的低表达过滤**：CPM/计数阈值 + 最小样本数（默认取参与对比的最小条件
   样本量）。全零基因独立标记 `all_zero`，低表达标记 `filtered_low_expression`，
   二者都不进入 BH 多重检验集合、永远不会得到 p 值，也不会因“不显著”被事后删除。
7. **归一化**：DESeq 型 median-of-ratios 尺寸因子（几何均值为 1），作为 GLM 的
   log 偏移；输出每个因子与诊断（用了多少基因、与库大小相关等）。
8. **NB2 拟合与离散度处理**：主拟合器是自带的 profile-ML——固定 α 时 IRLS 求 β，
   α 由标量有界似然最大化得到，含 log-μ 钳制与步长保护。statsmodels NB2 仅作独立
   交叉验证并记录似然差（其 BFGS 在小样本异均值上可能收敛到更差局部解，本工作台
   保留似然更优的 profile-ML 解）。小样本下逐基因 ML 离散度系统性下偏，因此再做
   edgeR 风格的经验贝叶斯收缩 + Pearson 偏差校正（使 Pearson χ² 与残差自由度
   匹配）；原始 ML、趋势、收缩/校正值与先验强度全部归档，可用
   `moderate_dispersion: false` 关闭。
9. **诚实的失败处理**：不收敛的基因状态为 `fit_failed`，**不填 p=0、不自动当作
   无差异**，原因写入 `fit_diagnostics.csv`，并排除在调整集合之外。
10. **对比与多重比较**：每个计划对比输出 log2FC、SE、95% Wald 区间、原始 p 值
    （残差自由度的 t 参考分布，主检验）、BH 调整 p 值，并明确写出调整的假设集合
    （仅 `tested` 且收敛的基因）。QL F 检验值作为诊断列保留。检验/过滤/全零/失败
    四类状态各自独立。
11. **编码一致性**：同一对比换参考组或换 treatment/sum 编码，生物结果在容差内
    一致；反向对比只反转效应方向、SE 与 p 不变（有自动核验）。
12. **报告与逐基因审阅**：归一化表达、样本图（PCA/库大小/零比例）、火山图、MA 图、
    离散度图，以及每个基因的 HTML 页（原始计数、分组、归一化计数、拟合值、诊断、
    对比定义与参数向量）。图上数字全部来自同一次运行归档的机器可读文件，不存在
    图内额外的未记录过滤。
13. **归档与可复现**：运行目录含 `plan_frozen.json`、`manifest.json`（输入摘要、
    依赖版本、种子、全部样本排除/受试者剔除/重复合并记录）、设计矩阵、归一化因子、
    过滤决策、拟合诊断、离散度处理、对比表、图和 HTML。重跑逐位一致，不依赖临时
    notebook 状态。

## 运行目录产物

```
plan_frozen.json  manifest.json  design_matrix.csv  design_coding.json
sample_qc_metrics.csv  sample_qc_flags.csv  sample_pca.csv
filter_decisions.csv  normalization_factors.csv  normalization_diagnostics.json
normalized_expression.csv  fit_diagnostics.csv  dispersion_moderation.csv
ql_dispersions.csv  fitted_values.csv
contrast_<name>.csv  contrast_<name>_meta.json
figures/  genes/<gene_id>.html  gene_index.html  index.html
```

## 设计矩阵（实际编码归档到 design_coding.json）

- 独立设计：`~ condition + batch`
- 配对设计：`~ condition + subject + batch`（受试者固定效应真正改变模型结构，
  在个体内比较条件，而不只是给样本贴标签）
- 对比通过显式参数向量 `c` 计算 `c'β`；treatment 与 sum 编码各自给出等价向量。

## 模拟研究如何读数

`simulations/<scenario>/` 下每个场景用**预先声明的连续种子**各跑 20 次完整流水线，
种子列表写入 `summary.json`，每次重复保留在 `per_replicate.csv`，没有任何按结果
挑种子的机制。报告平均检出率及标准差、假阳性率及合并 Wilson 95% 区间、拟合失败
总数。有限样本下检出率小于 1 是预期行为，不声称每个植入变化都必然被检出；
极小残差自由度（如配对 n=8，df≈6）下的功效损失会如实呈现。

## 已知边界

- 逐基因 ML 离散度在极小样本（配对 n≈6–8）下噪声大且下偏；Pearson 校正恢复了
  p 值校准但会降低该场景功效，应结合模拟区间解读，不应据此宣称必然检出。
- 归一化依赖足够多的全阳性基因构建几何均值参考；极度稀疏数据会显式报错而非静默继续。
- 仅处理本工作台生成的合成数据，不含任何外部生物数据库注释或临床推断。
