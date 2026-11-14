# 单帧 DICOM → 三维体数组构建器

把顺序打乱的单帧 DICOM 切片，按 DICOM 患者坐标系（LPS，毫米）中的
**物理平面位置与方向**组装成三维 `float32` 数组，并输出把数组索引映射到
LPS 毫米坐标的 4×4 affine。文件名、`InstanceNumber`、目录遍历顺序都不参与
空间排序；工具不做重采样、配准、NIfTI 适配、可视化或任何医学诊断。

## 1. 支持范围（超出范围一律明确拒绝）

| 项目 | 仅支持 |
| --- | --- |
| 传输语法 | 未压缩 Explicit VR Little Endian（`1.2.840.10008.1.2.1`） |
| 帧数 | 单帧（`NumberOfFrames > 1` 的多帧对象拒绝） |
| 光度解释 | `MONOCHROME2`（`MONOCHROME1`、RGB 等拒绝） |
| 采样 | `SamplesPerPixel = 1`（彩色/多采样拒绝） |
| 像素 | 16 位整数（`BitsAllocated=16, BitsStored=16, HighBit=15`，有符号/无符号均可） |
| 切片关系 | 同组切片平行、等间距、网格一致；非平行、重复平面、不均间距拒绝 |

## 2. 依赖与构建命令

需要 Python 3.11。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # numpy、pydicom
# 开发/验证测试额外需要：
.venv/bin/pip install -r requirements-dev.txt      # 额外安装 pytest

.venv/bin/python -m dicom_builder build \
    --input fixtures/dicom \
    --output .runtime/out \
    --tolerances fixtures/tolerances.json
```

退出码：全部组成功且无被拒绝文件为 `0`；存在失败组或被拒绝文件为 `1`
（成功组的产物仍会保留，失败组不产生任何体数组产物）；参数/容差配置错误为 `2`。

### 容差配置（均带物理单位）

`fixtures/tolerances.json`：

| 键 | 含义 | 单位/性质 | 默认值 |
| --- | --- | --- | --- |
| `direction_abs` | 方向余弦分量差、法向叉积模长的绝对容差 | 无量纲（单位向量） | `1e-5` |
| `spacing_mm` | 像素间距与层间距均匀性的绝对容差 | 毫米 (mm) | `1e-4` |
| `position_mm` | 平面投影位置、面内残差、重复平面判定的绝对容差 | 毫米 (mm) | `1e-4` |

容差必须为正的有限数值，启动时校验。

## 3. 分组与几何规则

1. **分组键**为 `(SeriesInstanceUID, FrameOfReferenceUID)` 二元组；任一切片
   单文件校验失败只记入 `rejected_files`，不影响其他文件。
2. 组内先校验 `Rows`、`Columns`、`PixelSpacing`（行间距/列间距分别比较）一致。
3. 方向取首片 `ImageOrientationPatient`：
   - 行方向 `r = IOP[3:6]`，列方向 `c = IOP[0:3]`；
   - 平面法向 `n = normalize(r × c)`；
   - 逐片比较行/列分量，并要求各片法向与 `n` 平行（叉积模长 ≤ `direction_abs`）。
4. **平面位置**用完整 3D 法向投影 `t = n·(IPP − IPP₀)`，同时检查面内残差
   （`Δp − n·t` 的模长）≤ `position_mm`。**不使用位置的 z 分量排序。**
5. 按 `t` 升序排序；相邻投影位置差 ≤ `position_mm` 判为重复平面并拒绝。
6. 层间距序列与其中位间距逐段比较，偏差 > `spacing_mm` 即拒绝；
   **缺层不会被补成连续体**，也不猜测缺失切片。
7. `PixelSpacing = [row spacing, column spacing]` 的行列对应关系全程保持，
   affine 的行轴、列轴方向与步长不可互换。

### 重复 SOPInstanceUID

- 像素（逐元素精确比较）、IPP/IOP（容差内）、网格与逐片 `RescaleSlope/
  RescaleIntercept`（精确相等）均一致 → 折叠为同一层，并在 metadata 与报告中
  记录保留文件与重复文件的路径及 SHA-256。
- 身份相同但像素或关键几何/缩放不同 → 该组直接判冲突失败。

## 4. 体数组与 affine

- 数组形状 `(K, Rows, Columns)`，dtype `float32`，轴次序固定：
  - 轴 0 `k`：切片（沿法向，按 `t` 升序）；
  - 轴 1 `i`：行，沿 `r`，步长 `PixelSpacing[0]`；
  - 轴 2 `j`：列，沿 `c`，步长 `PixelSpacing[1]`。
- 强度逐片独立转换：`value = pixel_int16 * RescaleSlope_k + RescaleIntercept_k`。
  几何相同但缩放不同的切片**不会复用首片参数**；rescale 标签缺失时按
  DICOM 约定退化为恒等变换并在 metadata 中标记 `rescale_defaulted=true`。
- affine（原点取空间排序后第 0 片的 IPP，因此与文件遍历顺序无关）：

  `affine @ [k, i, j, 1]ᵀ = [X_LPS, Y_LPS, Z_LPS, 1]ᵀ`（毫米）

  其中第 0/1/2 列分别是层/行/列方向向量乘以各自物理步长，第 4 列是原点。

## 5. 产物

```
<output>/
  build_report.json                # 全局报告：每组状态、失败原因、拒绝/跳过文件
  series_<清洗后的 SeriesUID>/
      volume.npy                   # float32 体数组
      metadata.json                # affine、方向、间距、像素类型、逐片 rescale 与来源
      sources.csv                  # 逐层来源表：slice_index、SOP UID、原文件、
                                   # IPP、平面投影、rescale、SHA-256 等
```

写入采用同文件系统临时目录 + 整体改名的原子发布；失败组在任何阶段都不会
留下 `volume.npy`，重跑前会清理同名旧目录，避免陈旧成功产物误导。

## 6. 验证

```sh
.venv/bin/python -m pytest      # 30 项测试
```

覆盖：附带斜向平行切片组（成功）与不均间距组（失败隔离）、用原始 IOP/IPP
独立推导角点坐标核对 affine、逐片 rescale 核对、改名/乱序后体数据与 affine
逐元素不变、多帧/彩色/RLE 压缩/Implicit VR/MONOCHROME1/8 位拒绝、非平行/
面内旋转/重复平面/不均间距/网格不一致拒绝、重复 SOP 折叠与冲突、容差松紧
配置、失败组无残留产物等。测试 DICOM 全部由 `tests/dicom_factory.py` 合成，
不含真实患者信息。
