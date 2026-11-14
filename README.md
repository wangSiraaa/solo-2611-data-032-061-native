# 题 138 输入材料

此目录内容复制到题目仓库根。fixtures/ 下是小型合成输入及必要的输入生成材料，不含求解程序、标准输出或评分答案。题面所列相对路径以仓库根为起点。全部输入仅用于本地开发，不含真实个人或业务数据。

依赖可按 requirements.txt 安装；无需 Docker、管理员权限、外部账号或模型下载。生成材料只负责构造输入，不是需要实现的业务能力。不要把固定输入值写死在处理器中。

---

# 区域统计 query 命令

`zonal_stats` 包实现 `query` 命令：对单波段 float32 栅格（仿射可含旋转项）按
GeoJSON 多边形（含孔洞）做区域统计。像元中心位于多边形内部或外边界即被选中；
孔洞内部与孔洞边界排除。nodata、NaN 与文件 mask 共同决定有效性，真实 0 有效。

## 查询示例

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# 默认读取 fixtures/ 下三个输入，结果写入 outputs/
.venv/bin/python -m zonal_stats query
# 显式指定输入与输出目录
.venv/bin/python -m zonal_stats query \
    --raster fixtures/surface.tif \
    --regions fixtures/regions.geojson \
    --config fixtures/query-config.json \
    --out outputs
# 覆盖配置的最大读取块像元数（分块变化不改变选中像元）
.venv/bin/python -m zonal_stats query --max-read-pixels 7 --out outputs-mrp7
# 测试
.venv/bin/python -m pytest tests/ -q
```

控制台输出示例：

```
raster: fixtures/surface.tif (64x48, crs EPSG:3857)
read block cap: 128 pixels
  with-hole    ok  count=691 min=0.0000 max=17.2400 mean=10.2738  (pixels read: 754)
  partial      partial  count=80 min=0.0000 max=5.2415 mean=2.6521  (pixels read: 81)
  outside      out_of_bounds  (pixels read: 0)
```

## 输出（数值结果，不用图片代替）

- `report.json`：每个查询的 `status`（`ok` / `partial` / `out_of_bounds` /
  `empty` / `unsupported`）、`count/min/max/mean`、有效覆盖
  （`selected_cells`、`valid_cells`、`fraction`）、实际读取窗口及像元数
  （`read_windows`、`pixels_read`），并声明均值浮点容差
  `mean_relative_tolerance: 1e-6`。
- `<region_id>_data.npy` / `<region_id>_mask.npy`：裁切数组与参与统计的
  布尔掩膜（多边形外或无效像元为 False，不参与统计）。
- `metadata.json`：每个裁切数组的原网格窗口（`row_off/col_off/height/width`）
  与裁切 affine；`array[i, j]` 对应原图像元 `(row_off+i, col_off+j)`，
  可准确放回原图。
- `boundaries.geojson`：栅格外框、各查询区域及裁切窗口的世界坐标边界。

## 口径与约束

- 旋转网格：查询几何整体逆变换到像元空间取保守窗口（不只由左上/右下两个
  地理角推断），再按配置 `max_read_pixels` 分块读取，逐块做精确中心测试；
  中心测试在世界坐标系进行，避免逆变换往返误差影响边界判定。
- 完全越界不读取任何像元；部分相交裁到栅格范围；选中但全部无效给 `empty`。
- 不做重投影（配置/GeoJSON 与栅格 CRS 不一致即报错）、不做表达式、多源拼接、
  瓦片服务或预览；仅支持 Polygon（MultiPolygon 标记 `unsupported`）。
