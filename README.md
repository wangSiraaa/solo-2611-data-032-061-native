# 地块能耗 → 服务区空间分配工具

本地命令行工具，用 Python 3.11 + Shapely 2 把**地块年度能耗（整数 Wh）**按优先级裁决后的
**归属碎片面积**分配到服务区。交叠区域只归属一个服务区，未被任何服务区覆盖的部分标为
`UNASSIGNED`，两者都保留在碎片集合中，不会消失，也不会被重复承接。

不做地图界面，不做跨 CRS 重投影，不做几何修复，不并行执行。

## 安装与运行

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 分配（写出全部产物）
.venv/bin/python -m energyalloc allocate \
    --plan fixtures/plan.json \
    --parcels fixtures/parcels.geojson \
    --zones fixtures/zones.geojson \
    --out out/

# 单地块证据（JSON 打到 stdout；--out 可省略）
.venv/bin/python -m energyalloc explain-parcel P1 \
    --plan fixtures/plan.json \
    --parcels fixtures/parcels.geojson \
    --zones fixtures/zones.geojson

# 独立复核（不 import 分配引擎，只读 GeoJSON/CSV/JSON）
.venv/bin/python verify_outputs.py --out out/ --parcels fixtures/parcels.geojson

# 测试
.venv/bin/python -m pytest tests/ -q
```

退出码：`0` 全部检查通过；`1` 产物已写但某项闭合检查失败；`2` 输入被拒绝（结构性错误或
CRS 不符），错误以 JSON 打到 stderr。

## 输入契约

`plan.json` 必填：`crs`、`coordinate_unit`（必须为 `metre`）、`priority_rule`、
`precision_grid`、`area_tolerance_m2`、`unassigned_id`、`wh_remainder_tie_break`。

- 地块：唯一 `parcel_id`、非负整数 `energy_wh`、`Polygon`/`MultiPolygon`（保留孔洞）。
- 服务区：唯一 `zone_id`、整数 `priority`、`Polygon`/`MultiPolygon`。
- 仅接受 2D；Z 坐标、LineString/Point、空面均拒绝。
- GeoJSON 本身不带 CRS。若文件里出现 `crs` 成员（旧格式），必须与 plan 声明一致
  （支持 `EPSG:3857`、`urn:ogc:def:crs:EPSG::3857` 等写法）；不一致直接拒绝，绝不重投影。

### 拒绝策略（不自动修复）

结构性错误使整次运行失败：重复 `parcel_id`/`zone_id`、CRS 不符、plan 字段缺失或非法。

逐地块错误只拒绝该地块，并**单独报告**，其能耗保留在该记录中、绝不转给邻居：

| code | 含义 |
| --- | --- |
| `invalid_geometry` | 自交/未闭合等无效面；拒绝而不是 `make_valid` |
| `unsupported_geometry` | 非 Polygon/MultiPolygon，或含 Z |
| `empty_geometry` / `zero_area` | 空面或面积为 0 |
| `missing_energy` / `invalid_energy` / `negative_energy` | 能耗缺失、非整数、为负 |
| `missing_parcel_id` | 无 `parcel_id` |

服务区无效即使无法裁决，直接报错拒绝本次运行。

## 算法

### 1. 裁决覆盖（不按比例归一）

对每个地块：

1. 用 `STRtree` 做**空间候选筛选**，只保留包络相交的服务区。
2. 按 `(-priority, zone_id)` 排序：优先级数值大者先，同级按 `zone_id` 升序。
3. 依次对候选区做精确求交 `raw = parcel ∩ zone`；再从 `raw` 中减去此前**已归属**
   的并集，得到互不重叠的碎片 `net = raw − taken`，然后并入 `taken`。
4. 全部候选处理完后，`UNASSIGNED = parcel − taken`。

这样重叠区只由先裁决者获得，后裁决者得到 `raw` 与 `preempted` 面积证据（即使净碎片为
0，也保留“被高优先级排除”的记录）。绝不“逐区求交再按面积归一”，因此不会把交叠面积
计两次。线/点接触 `raw` 面积为 0，不产生碎片，仅记为 `line_or_point_contact_only`。

碎片按连通分量拆开（MultiPolygon 的每个 Polygon 单独成片），孔洞留在所在碎片中。

### 2. 整数 Wh 分配（严格守恒）

对每个地块的碎片（含 `UNASSIGNED`）：

- 份额 = `energy_wh × area_i / Σarea`，用 `Fraction` 精确计算高精度份额；
- 先取整数下界（floor），剩余 `energy_wh − Σfloor` 个单位按**小数余数降序**逐片加 1；
- 余数相同按 `(owner_id, canonical_geometry_key)` 升序裁决，即 `zone_id` 升序再按
  规范化碎片几何，与 `plan.wh_remainder_tie_break` 一致；
- 逐地块严格守恒：`Σ 碎片 Wh == parcel.energy_wh`，用断言双重校验。

因为所有输入顺序都会先规范化排序，**输入顺序变化不改变规范归属与整数分配**。

### 3. 两类闭合检查（彼此独立）

- **几何闭合**：碎片两两不相交（两两求交面积 ≤ 容差）；碎片并集与原地块的对称差
  ≤ 容差；碎片面积和 ≈ 原地块面积。任一失败 → `geometry_check_failed`。
- **能耗闭合**：碎片 Wh 和 == 地块 Wh（精确整数比较）。失败 → `energy_check_failed`。

两者分别记录，**能耗闭合不能掩盖漏面**：漏面会同时破坏几何闭合，几何检查独立报错。

## 产物（out/）

- `fragments.geojson` — 碎片 FeatureCollection。属性含 `fragment_id`、`parcel_id`、
  `owner_id`、`zone_id`、`kind`（`zone`/`unassigned`）、`priority`、`adjudication_rank`、
  `area_m2`、`wh`、`parcel_energy_wh`、`tiny_fragment`、`geometry_key`。几何为原始
  GeoJSON 坐标（未吸附、未修复）。
- `allocation.csv` — 整数分配表，一行一碎片，含 `area_m2`、`wh`、`parcel_energy_wh`，
  以及 `exact_share`、`fractional_remainder`、`remainder_rank` 供审计。
- `evidence/parcel_<id>.json` — 逐地块证据：原地块几何与原面积、覆盖候选、裁决顺序与
  每项 `raw/preempted/net` 面积、裁决后碎片、每片 Wh 来源（floor、余数、排名、精确份额）、
  几何/能耗检查与说明。被拒绝地块写成 `evidence/rejected_<id>.json`。
- `summary.json` — plan、统计、全局检查、被拒绝/零面积地块、精度边界说明。

`fragments.geojson`、`allocation.csv`、`summary.json` 都是普通 GeoJSON/CSV/JSON，
独立读取器可直接消费。`verify_outputs.py` 正是这样一个独立读取器：它重建几何、检查
碎片无重叠、并集复原、能耗守恒和全局账目（含被拒绝地块的能耗）。

## 精度边界

- `precision_grid` **不移动任何坐标**。它只用于：规范化几何键（WKT 按 `grid` 对应小数位
  四舍五入后比较）、极小碎片阈值、以及 `summary.json` 中“顶点偏离网格”的诊断统计。
  这样碎片仍可直接与原地块比较，闭合检查才诚实。
- 极小碎片阈值为 `max(area_tolerance_m2, precision_grid²)`（本 fixture 为 `1e-4 m²`）。
  面积不高于阈值的碎片会标 `tiny_fragment=true`，但**仍然保留**并按其精确面积份额参与
  分配，绝不因处理困难而丢弃。
- 面积容差（本 fixture `1e-5 m²`）用于浮点闭合比较。快照/求交后的面积误差通常在
  `1e-12` 量级，远小于容差；容差不用于删除几何。
- 数据中若出现网格无法分辨的亚网格细节，工具不会吸附而是照原样计算，并在
  `precision_input_report` 报告最大顶点残差。
- 性能：候选对数量为 `Σ 每地块候选区数`，实际精确求交数与之相同；`pairs_avoided_by_spatial_filter`
  报告被 STRtree 直接跳过的全组合数。未做十万地块压测。

## 小例

`examples/cases.py` 提供可直接手算的矩形案例，运行 `examples/build_examples.py` 会写出到
`examples/data/<name>/`：

| 名称 | 要点 |
| --- | --- |
| `01_overlap` | 两区重叠，重叠带只计一次：A=60、B=40 m² |
| `02_equal_priority` | 同级按 `zone_id` 裁决；低优先级区被完全抢占仍保留证据 |
| `03_hole` | 带 10×10 孔洞的地块，孔洞保留在归属碎片中 |
| `04_unassigned` | 未覆盖带形成 `UNASSIGNED` 碎片并被分配能耗 |
| `05_remainder_tie` | 三片各 1 m²、总 1 Wh，余数并列按 `zone_id` 给 A |
| `06_geometry_tie` | 同一区两片同面积，余数并列按规范化几何裁决 |
| `07_tiny_fragment` | 0.0001 m² 单格碎片保留、标记并按比例得 1 Wh |
| `08_candidate_filter` | 3 个区只有 1 个候选，统计候选对与实际求交数 |
| `09_rejections` | 自交面 / 负能耗 / 小数能耗 / 线要素分别拒绝并单独报告 |

示例的期望值均由人工矩形面积核对，测试同时用独立读取器复核碎片无重叠、并集复原与能耗守恒。
