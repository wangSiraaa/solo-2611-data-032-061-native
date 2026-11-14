# terrain-cli：局部地形估计

把已统一为米制、带分类的点云（CSV）转成规则网格的地面高程估计，并为每个
格子输出可回查的支持证据。稀疏区不会被平滑成"看似可靠"的地面：要么没有
估计（NaN），要么明确标注 `low_confidence` 及原因。

范围限定：只接受规范 CSV 点云，不解析 LAS、不做投影变换、不做多航带融合。

## 输入

* `fixtures/points.csv` — `point_id,x,y,z,classification`，米制、有限坐标；
  `classification=2` 为地面候选（可能混有少量孤立高点），其他分类只统计
  排除数量。非法行（ID 冲突、非有限坐标、非法分类）逐行定位并拒绝，
  不影响其他合法点。
* `fixtures/grid.json` — `xmin/ymin/xmax/ymax/resolution/max_support_radius/
  units`，定义输出网格；`max_support_radius` 作为 `--radius` 的缺省值。
* `synthetic/` — 由 `scripts/make_synthetic.py` 生成的较大合成场景
  （斜坡、台阶、密度变化、空洞、误标高点、6 行非法记录），带解析真值，
  仅供验证。不要把任何固定输入值写死进处理程序。

## 环境与构建

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt   # numpy, scipy
```

## 运行

```bash
# 缺省半径取自 grid.json 的 max_support_radius
.venv/bin/python -m terrain_cli build \
    --points fixtures/points.csv --grid fixtures/grid.json --out out

# 两组邻域参数对比（紧邻域保守 / 宽邻域激进）
.venv/bin/python -m terrain_cli build --points fixtures/points.csv \
    --grid fixtures/grid.json --out out_tight \
    --radius 1.0 --min-support 6 --min-inliers 5
.venv/bin/python -m terrain_cli build --points fixtures/points.csv \
    --grid fixtures/grid.json --out out_wide \
    --radius 1.5 --min-support 4 --min-inliers 4
```

输出目录：

| 文件 | 内容 |
|---|---|
| `terrain.npy` | float64，`(n_rows, n_cols)`，无效格为 **NaN**（真实 0 m 高程处 mask=True、值为 0.0，二者不会混淆） |
| `mask.npy` | bool，同形状；True ⟺ terrain 为有限值 |
| `cells.csv` | 每格一行：行列号、中心坐标、高程、状态、降级原因、候选/支持点数、支持点 point_id 列表、稳健 sigma、最大残差、几何诊断量 |
| `issues.csv` | 被拒绝的输入行：文件行号 + point_id + 原因 |
| `run_config.json` | 本次运行的全部参数与数组方向约定 |

## 验证

```bash
.venv/bin/python scripts/make_synthetic.py   # 首次，生成合成验证场景
.venv/bin/python scripts/validate.py         # 全部 PASS 则退出码 0
```

验证内容：解析平面中心高程精确恢复；删去一侧支持点后状态降级且诊断量
恶化；近乎共线/空邻域/点不足的报告；非法行定位；两组邻域参数在提供的
fixtures 上的覆盖率与留一误差对比、在合成场景上的覆盖率与真值 RMSE
对比；空洞与台阶行为；误标高点不被用作支持；CLI 冒烟。

## 另一进程如何定位结果

`terrain[row, col]`：row 0 = 最小 y（南），col 0 = 最小 x（西）。
格 (row, col) 的估计位于**格中心**
`x = xmin + (col+0.5)*resolution`，`y = ymin + (row+0.5)*resolution`；
网格覆盖 `[xmin, xmax] × [ymin, ymax]`。约定同时写入输出的
`run_config.json`。

估计方法、参数表与台阶/坡面取舍见 [METHOD.md](METHOD.md)。
