# 方法与参数说明

## 总览

每个网格中心独立估计局部地面，不做全局平滑、不跨无观测区插值：

1. **邻域检索**：对全部地面候选点（`classification=2`）建一棵 cKDTree，
   所有格子复用；每格取中心 `radius` 内的候选点。
2. **局部表面**：在格子中心相对坐标下拟合平面 `z = a·dx + b·dy + c`，
   估计值即截距 `c`（中心处高程）。拟合采用**低种子迭代 sigma 截断**：
   先按 z 升序取最低 `low_z_fraction` 的点拟合初始平面，再反复用
   `|残差| ≤ outlier_k · σ_inlier` 截断并重拟合（σ 为内点残差的
   MAD×1.4826，下限 `sigma_floor`）。地面被视为分类点云的**下包络**：
   孤立的误标高点、植被残留、台阶另一侧的点会被截断剔除，而不是把
   表面抬高；也绝不会把格内全部点的均值当地面。
3. **支持几何诊断**（在最终内点上计算）：
   - `max_gap > 180°` → `one_sided`：中心落在支持点凸包之外，属外推；
   - `extrapolation_ratio = √n · 杠杆值 > extrapolation_max` →
     `extrapolation`：支持重心偏离目标（一侧密集）；
   - 内点 XY 的奇异值比 `< collinear_ratio` → `near_collinear`：
     沿弱方向坡率不可定（如道路状点带）；
   - 明显高于拟合面的被剔除点（超出过 `max(outlier_k·σ,
     mixed_levels_min_step)`）数量 ≥ `max(2, mixed_levels_min_frac·n)`
     → `mixed_levels`：邻域内存在两个层面（典型为台阶两侧）。
4. **状态与掩膜**：任一诊断触发 → `low_confidence`（估计保留但明确不可
   靠）；候选数 < `min_support` → `insufficient_support`；邻域为空 →
   `empty`；截断后内点 < `min_inliers` → `insufficient_support`
   （原因 `too_many_outliers`）。`mask=True` 仅对应 `ok` 与
   `low_confidence`；`terrain` 在 mask 外为 NaN，与真实 0 m 高程区分。

## 参数表（CLI 均可覆盖）

| 参数 | 缺省 | 含义 |
|---|---|---|
| `radius` | grid.json 的 `max_support_radius`，否则 3.0 | 最大邻域半径 [m]，也是唯一允许的外推范围 |
| `min_support` | 6 | 邻域内最少地面候选点数 |
| `min_inliers` | 5 | 截断后最少内点数 |
| `outlier_k` | 3.0 | 内点阈值 = k × 稳健 σ |
| `clip_iters` | 6 | sigma 截断最大迭代次数 |
| `low_z_fraction` | 0.5 | 初始拟合所用的最低点比例 |
| `sigma_floor` | 0.02 | 稳健 σ 下限 [m]，防零噪声数据退化 |
| `extrapolation_max` | 2.0 | 杠杆外推比阈值 |
| `collinear_ratio` | 0.1 | 近共线奇异值比阈值 |
| `mixed_levels_min_frac` | 0.2 | 判混合层面所需高点比例 |
| `mixed_levels_min_step` | 0.15 | 高点计入混合层面的最小高差 [m] |

## 取舍说明

* **台阶/陡坎**：邻域横跨台阶时，低种子拟合跟随**较低一层**（保守的
  地面下包络），另一侧点被记为高点剔除并打 `mixed_levels` 标记，绝不
  把两层平均成一个看似光滑的坡。观测无法区分唯一真值时，保留估计但
  降级为 `low_confidence`，由下游按 `status`/`reasons` 取舍。
* **坡面**：平面局部假设在曲率小的坡面上无偏；网格分辨率远小于地形
  波长时误差可忽略。解析平面验证中心高程误差 < 1e-6。
* **空洞与稀疏区**：只在 `radius` 范围内估计。空洞内部无候选 →
  `empty`/`insufficient_support`（NaN）；空洞边缘只能被一侧支持 →
  `one_sided` 降级。宽半径可以把覆盖从 72.8% 提到 99.5%（提供的
  fixtures），但留一误差中位数从 0.0055 m 升到 0.0078 m、p90 从
  0.0083 m 升到 0.0158 m，且只能把空洞边缘补成低可信，空洞内部依然
  为空——覆盖与可靠性的交换由参数显式控制，见
  `scripts/validate.py` 的对比表。
* **误标高点**：低种子 + 截断使孤立高点（fixtures 中 p333、p334，
  高出地面 7–9 m）完全不进入任何格子的支持集；合成场景中 ok 格
  误差 p99 = 0.068 m。
* **边界**：网格边缘的格子天然单侧支持，会被标记 `one_sided`，这是
  有意为之的保守行为。

## 局限

* 局部平面不能表达曲率半径小于 `radius` 的地形（小沟坎），需要时用更
  小半径换分辨率。
* 低包络假设下，低于真实地面的粗差（地下点）会拉低估计；此类点通常
  稀少，必要时先按分类/回波过滤。
* 常数级参数（如 `mixed_levels_min_step=0.15 m`）针对米制地形测量
  量级设定，换场景应重新标定。
