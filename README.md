# geomosaic:连续指标 GeoTIFF 镶嵌工具

把几张覆盖重叠区域的单波段连续指标影像合成到统一网格:接缝不错位、
无效的高优先级影像不会挡住有效来源、每个有效输出像元记录真正贡献的来源。
纯本地栅格处理,不做地图服务。

## 输入素材

`fixtures/source_a.tif`、`fixtures/source_b.tif` 是真实单波段 float32 影像,
含内部有效 mask、不同 CRS/分辨率/原点、nodata 和有效零值。`fixtures/plan.json`
声明来源优先级及目标网格。素材通过 rasterio 独立打开确认可读。

## 支持范围(超出即在开始前拒绝)

- 单波段、浮点(float32/float64)连续量;整型/多波段按分类量风险拒绝。
- 北向上、无旋转/剪切的仿射网格;来源分辨率、网格原点、nodata 可不同。
- CRS 仅限计划 `supported_crs` 列出的两种(EPSG:3857、EPSG:4326);
  来源缺失 CRS 或不在列表内、来源与目标单位不一致,都在读任何像元前拒绝。
- 不做单位推断、不处理跨日期变更线;真实零与负值是普通数据,必须保留。

## 环境

Python 3.11,依赖见 `requirements.txt`(numpy + rasterio):

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 命令

```sh
# 1) 校验计划:网格推算、来源诊断、逐窗口读取计划;失败退出码 2
.venv/bin/python -m geomosaic plan --plan fixtures/plan.json

# 2) 执行镶嵌:写出产品并生成检查报告(默认做独立逐像元验证)
.venv/bin/python -m geomosaic mosaic --plan fixtures/plan.json --out out
#    可选:--window-shape H W 覆盖窗口尺寸;--no-verify 跳过验证

# 3) 按地理坐标查询数值与来源,并与已产出的栅格交叉核对
.venv/bin/python -m geomosaic query-source --plan fixtures/plan.json \
    --x 275 --y 675 --out out
#    可用 --crs EPSG:4326 以其他支持 CRS 传坐标
```

退出码:0 成功;2 计划被拒绝;3 镶嵌验证未通过;4 查询重算与产物不一致。

## 网格与窗口

- 目标 CRS、分辨率、范围、网格锚点由计划给出;程序把范围向外取整到锚点
  网格线,得到实际输出边界(见报告 `grid.actual_bounds`)。
- 输出按 `window_shape`([高, 宽],像元)切窗处理。每个输出像元的取值只
  依赖其自身坐标与源数据,与窗口划分、来源枚举顺序无关——改窗口大小或
  重排 sources 列表,产物逐位一致(有测试保证)。
- 每个窗口内只从源读取采样核触达的最小索引范围(halo):最近邻为
  `floor(g±0.5)`、双线性为 `floor(g)` 起 2 像元,裁剪到源栅格;不逐窗
  重读整幅源。每次实际读取(源、窗口、halo、有效样本数)记入报告
  `windows` 段,可据此说明执行方式。

## 有效性、采样与裁决语义

- 源像元无效,当且仅当:内部 mask 为 0、值等于 nodata、或值非有限
  (NaN/Inf)。真实 0 与负值不受影响。
- 最近邻:round half-up 取最近源像元;该像元无效或越界则采样无效。
- 双线性:2x2 邻域面积加权;无效或越界邻域权重置 0、**不进入加权**,
  其余权重归一化;有效权重和为 0 时采样无效。源边缘外侧半像元以内按
  边缘像元常数外推,之外无效。
- 裁决:数值优先级大者优先,并列按来源 id 升序;逐输出像元进行,
  高优先级源在该像元无有效采样时才退到下一源。数值与来源栅格出自同一
  采样与裁决过程,绝不按覆盖框整片填来源号。
- 来源栅格取值:0=无来源,1..N=按来源 id 字典序(与枚举顺序无关),
  映射见 `sources_index.json`、报告及栅格 tag。

## 产品(out/)

- `mosaic.tif`:float32 指标影像,nodata=NaN,带 CRS 与 transform。
- `mask.tif`:uint8 有效 mask(255/0),同一网格。
- `sources.tif`:uint8 来源栅格(nodata=0),同一网格。
- `sources_index.json`:来源栅格取值 → 来源 id。
- `report.json`:检查报告——网格推算、来源诊断、逐窗口实际读取记录、
  像元统计、各产品 SHA-256、独立验证结果。

## 验证方式(不只凭预览图或覆盖数量)

1. **独立逐像元参考**(`geomosaic/reference.py`,无窗口纯标量实现)逐像元
   核对数值、mask、来源;`mosaic` 默认自动执行并写入报告。双线性误差界:
   float32 存储量化(相对 ≤2^-24)加 float64 累加次序,容差
   `atol=1e-6, rtol=1e-5`;最近邻严格相等。
2. **窗口尺寸不变性**:`tests` 以 (7,9)/(20,20)/(3,4) 三种窗口跑同一计划,
   产品逐位一致。
3. **新进程地理坐标核对**:子进程独立打开产品,按 EPSG:4326 坐标采样、
   按地理范围开窗读取,核对 CRS/transform/边界/数值。
4. **合成小例**:半像元错位(双线性=四邻域等权平均、最近邻平局 half-up)、
   重叠无效洞(高优先级洞由低优先级补、双洞处无效、nodata 不渗入)、
   边界邻域(边缘常数外推、界外无效)、合法零与负值、仅 mask 失效。

运行全部测试(14 项):

```sh
.venv/bin/python -m tests.test_mosaic
```

## 目录结构

```
geomosaic/            # 工具包
  plan.py             # 计划解析与开始前校验
  grid.py             # 目标网格推算(锚点取整)
  sampling.py         # 最近邻/双线性采样器(有效性规则)
  engine.py           # 窗口化引擎:halo 读取、逐像元裁决、报告
  reference.py        # 独立逐像元参考实现与比对
  query.py            # query-source:现场重算 + 读取产物交叉核对
  __main__.py         # CLI:plan / mosaic / query-source
tests/test_mosaic.py  # 14 项测试(可直接运行或 pytest)
fixtures/             # 输入素材与计划
```
