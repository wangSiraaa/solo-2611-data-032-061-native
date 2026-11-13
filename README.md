# 输入素材

两张 GeoTIFF 是真实单波段 float32 影像，含内部有效 mask、不同 CRS/分辨率/原点、nodata 和有效零值。plan.json 声明来源优先级及目标网格。只有输入，未提供统一网格、输出数值、来源栅格或求解器。素材通过 rasterio 独立打开确认可读。
