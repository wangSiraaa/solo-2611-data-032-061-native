# 多重插补分析使用说明

对含缺失协变量的连续结果关系做多重插补:迭代条件插补(FCS,贝叶斯线性
后验抽取 + 预测均值匹配)产生 m 份完成数据,在每份上拟合同一冻结线性模型,
再用 Rubin 规则合并系数与不确定性(插补内 U 与插补间 B 分列)。

范围:仅连续变量;响应须完整观测;不做二元/逻辑模型,不做缺失机制识别。

## 环境

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # numpy / scipy / statsmodels
```

## 命令

```sh
# 1) 运行:诊断 -> 插补 -> 拟合 -> 合并 -> 写出全部产物
.venv/bin/python -m mi run \
    --plan fixtures/plan.json \
    --survey fixtures/survey.csv \
    --applicability fixtures/applicability.csv \
    --out output

# 2) 查看:读取产物目录,打印诊断、链、分布与合并结果摘要
.venv/bin/python -m mi inspect --out output

# 3) 复核:独立重算(逐单元不变性、资格记录、由每份估计重新合并)
.venv/bin/python -m mi verify \
    --plan fixtures/plan.json \
    --survey fixtures/survey.csv \
    --applicability fixtures/applicability.csv \
    --out output
```

退出码:`0` 正常;`2` 运行前诊断失败(不生产物);`verify` 任一检查失败为 `1`。

## 产物(output/)

| 文件 | 内容 |
|---|---|
| `completed/completed_<k>.csv` | 各份完成数据;观测原样保留,不适用单元留空 |
| `cell_states.csv` | 逐单元状态:observed / missing / not_applicable |
| `analysis_rows.csv` | 逐行分析资格与排除原因 |
| `estimates/per_dataset_estimates.csv` | 每份数据的系数与标准误 |
| `estimates/per_dataset_vcov.json` | 每份的系数向量与完整方差矩阵(供另一程序重新合并) |
| `estimates/pooled.json` / `pooled.csv` | Rubin 合并结果(U、B、T、df、FMI、CI) |
| `estimates/complete_case.csv` | 完整病例对照(非正式结果) |
| `chains.json` | 每链状态、事件与逐轮轨迹 |
| `diagnostics.json` | 诊断与分布汇总、限制 |
| `design.json` | 冻结设计编码(计划、方法、顺序、随机流、环境版本) |
| `report.md` | 静态诊断报告 |

## 方法要点

- **随机性**:每变量每轮三重——(1) 初始化从观测值随机抽取;(2) 平坦先验下
  后验抽取 σ²、β;(3) PMM 在最近 5 名供体中均匀抽签,取供体观测值。
  三条链(及更多链)相互独立,绝不复制同一份确定填补。
- **更新顺序**:变量按缺失数升序(同数按声明序),逐轮以最新条件值更新。
- **范围处理**:PMM 只返回通过范围校验的观测供体值;另有防御性截断并计数。
- **随机流**:`SeedSequence([seed, chain, sweep, variable, purpose])`;行按
  `row_id` 排序的规范顺序处理,输入重排不改变规范结果;固定输入与种子可复现。
- **状态**:供体不足、模型退化、边际回退、截断、漂移等记入链事件;
  链状态为 ok / ok_with_flags / failed,失败即中止。
- **合并**:不平均完成数据;每份分别拟合后按 Rubin 规则合并,
  自由度取 Barnard–Rubin 调整。完整病例仅作对照,不按显著性取舍。

## 小例与测试

`examples/` 下五个小例(由 `examples/make_examples.py` 确定性生成):

- `no_missing/` 无缺失:插补为恒等,合并结果须等于直接拟合;
- `range_bound/` x1 限制在 [0,10]:插补值不得越界;
- `all_missing_column/` x2 全缺:运行前诊断拒绝;
- `correlated_missing/` x1、x2 联合缺失:可运行,插补间变异显著;
- `contradiction/` 不适用单元带观测值:运行前诊断拒绝。

运行测试(标准库 unittest,无需额外依赖):

```sh
.venv/bin/python -m unittest discover -s tests -v
```

测试覆盖:逐单元不变性、无缺失直接拟合核对、范围约束、全缺列与矛盾诊断、
相关缺失小例、同种子复现、输入重排不变、异种子相异、多份数据互不相同、
由每份系数与方差独立复算合并不确定性。
