# 网格温度站点抽样工具（题 131）

`gridsampler` 从两份经典 NetCDF 网格文件中按维度名读取 `temperature`，在站点位置用包含它的四格点做双线性抽样，按时间合并，并输出结果 CSV、计划 JSON 与诊断 JSON。

输入材料位于 `fixtures/`；求解程序在 `gridsampler/`；验证脚本在 `scripts/`、`tests/`。
全部输入为本地合成数据，不含真实个人或业务数据。

## 环境

- Python 3.11（开发验证于 3.11.2）
- 依赖见 `requirements.txt`，可仅用 NumPy/SciPy/netCDF4（`netCDF4` 会自动带入 `cftime`）

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

固定版本（本机验证通过）：

| 包 | 版本 |
| --- | --- |
| numpy | 2.4.6 |
| scipy | 1.17.1 |
| netCDF4 | 1.7.5.1.post1 |
| cftime | 1.6.6.1 |

无需 Docker、数据库、外部账号或模型下载；不依赖数据仓库或查询服务。

## 命令

```sh
.venv/bin/python -m gridsampler sample \
  --file fixtures/part-a.nc \
  --file fixtures/part-b.nc \
  --stations fixtures/stations.csv \
  --out out/results.csv \
  --plan out/plan.json \
  --diagnostics out/diagnostics.json
```

`sample` 选项：

| 选项 | 说明 |
| --- | --- |
| `--file PATH` | 输入 NetCDF，需恰好两次，顺序无关 |
| `--stations CSV` | 站点表，列 `station_id,lat,lon` |
| `--out CSV` | 结果 CSV |
| `--plan JSON` | 计划 JSON（策略、单位、操作顺序、输入元数据） |
| `--diagnostics JSON` | 诊断 JSON（切片次数、记录计数、`ok`/错误） |
| `--variable NAME` | 温度变量名，默认 `temperature` |
| `--missing-policy` | `renormalize`（默认）或 `strict` |
| `--min-coverage` | `renormalize` 的最低有效正权重覆盖，默认 0.5 |
| `--time-block N` | 每次按时间读取的索引数，默认 24 |

出错时退出码为 2，并在 `--diagnostics` 中写入 `ok:false` 与定位信息（文件、变量、错误类别）。

## 关键规则

- **按维度名读取**：`time`/`lat`/`lon` 轴可任意排列、纬度可升可降。
- **缺测在存储值上识别**：打开文件即关闭 `netCDF4` 自动缩放/掩码；先用 `_FillValue`（及 `missing_value`，如声明）在 `int16` 存储值上做掩码，再 `scale_factor`/`add_offset`，最后 K→°C，且只转换一次。
- **拒绝**：不支持的温度单位（只接受 K/°C，如 `degF` 报错）、重复坐标、非单调或不规则轴、非标准公历、非 `hours since` 时间单位；报错均带文件与变量。
- **双线性**：含站点的四格点；正确处理降序轴、网格线与边界点。零权重邻点即使缺测也不使有效点失效。
- **正权重缺测**：`renormalize` 丢弃缺测正权重邻点后重新归一，并要求覆盖率 ≥ `--min-coverage`；`strict` 则直接拒绝。策略固定写入计划，结果保留每个邻点权重与覆盖。
- **超范围/邻域不足**：保留状态与原因，不补零、不填缺测。
- **合并**：按 `(time_utc, station_id)` 排序，输出与文件顺序无关；跨文件重复时刻、或空间坐标/meta 不兼容一律报错，不静默取最后一份。
- **按块读取**：每 N 个时间索引读取一次变量（不逐站反复加载完整变量）；诊断只记录切片次数，不做大数据压测。

输出 CSV 列含 `station_id, time_utc, station_lat, station_lon, temperature_degc, status, reason, file, time_index, lat_index, lon_index, coverage, missing_policy, min_coverage, neighbours`。`neighbours` 为 JSON，含每个邻点的 `lat_index/lon_index/lat/lon`、实际 `weight`、`original_weight`、`valid` 与 `value_degc`。

## 验证

```sh
.venv/bin/python -m pytest tests/test_sampler.py -q
```

覆盖：边界点、降序纬度（`fixtures/part-b.nc`）、打包缺测、越界站点、网格线、零权重缺测、正权重缺测的两种策略、文件顺序无关、时间块不影响结果、重复时刻与不支持单位报错。

### 单时刻手算核对

对 `fixtures/part-a.nc` 时间下标 0 的 `missing_neighbour` 站点，独立读取四个原始值并手算
（原始 `raw` → `raw*0.1+273.15` → K→°C；缺测 `-32768` 不参与）：

| (i,j) | (lat,lon) | raw | 权重 | 值(°C) |
| --- | --- | --- | --- | --- |
| (1,2) | (31,112) | -32768 | 0.42 | 缺测 |
| (1,3) | (31,113) | 129 | 0.28 | 12.9 |
| (2,2) | (32,112) | 130 | 0.18 | 13.0 |
| (2,3) | (32,113) | 132 | 0.12 | 13.2 |

覆盖率 = 0.28+0.18+0.12 = **0.58** ≥ 0.5，故按有效权重重归一：
`(0.28·12.9 + 0.18·13.0 + 0.12·13.2)/0.58 = 12.99310344… °C`，与结果 CSV 一致。

用独立脚本复核任意行：

```sh
.venv/bin/python scripts/verify_handcalc.py \
  --results out/results.csv --file fixtures/part-a.nc \
  --time-index 0 --station missing_neighbour
```

脚本不导入 `gridsampler`，直接读原始存储值、手算四邻点，并与结果 CSV 比对（打印 `MATCH`/`MISMATCH`）。

### 合成边界夹具（可选）

```sh
.venv/bin/python scripts/make_fixtures.py --outdir out/extra
```

生成更大的降序经度、`(lon,lat,time)` 轴序网格与跨时间块的时间序列，用于时间块/轴序/打包缺测验证。生成器只构造输入，不是业务能力。
