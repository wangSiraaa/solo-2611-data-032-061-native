"""源像元块上的采样器:最近邻与双线性(对有效邻域归一化)。

有效性规则(引擎与独立参考实现严格一致):
- 源像元无效,当且仅当:内部 mask 为 0、值等于 nodata、或值非有限(NaN/Inf)。
  真实 0 与负值是普通数据,绝不因此失效。
- 最近邻:取"round half-up"最近的源像元;该像元无效或越界则采样无效。
- 双线性:取覆盖目标点的 2x2 邻域,面积权重;无效或越界邻域权重置 0、
  不进入加权;其余权重归一化。有效权重和 > 0 时采样有效,否则无效。
  因此源边缘外侧半个像元以内按边缘像元常数外推,之外无效。
"""
from __future__ import annotations

import math

import numpy as np


def needed_read_range(gmin: float, gmax: float, mode: str) -> tuple[int, int]:
    """中心相对小数坐标范围 [gmin, gmax] → 采样所需源像元索引范围 [lo, hi)(未裁剪)。

    该范围即 halo:恰好覆盖采样核可能触达的所有源像元,不多读整幅源。
    """
    if mode == "nearest":
        return math.floor(gmin + 0.5), math.floor(gmax + 0.5) + 1
    if mode == "bilinear":
        return math.floor(gmin), math.floor(gmax) + 2
    raise ValueError(f"未知采样方式: {mode}")


def sample_block(data: np.ndarray, valid: np.ndarray,
                 lc: np.ndarray, lr: np.ndarray, mode: str):
    """在已读入的源块上采样。

    data: (H, W) float64 源块;valid: (H, W) bool 源像元有效性。
    lc/lr: (N,) 块内中心相对小数坐标(块外即越界,按无效处理)。
    返回 (values float64, ok bool),无效处 values 为 NaN。
    """
    if mode == "nearest":
        return _nearest(data, valid, lc, lr)
    if mode == "bilinear":
        return _bilinear(data, valid, lc, lr)
    raise ValueError(f"未知采样方式: {mode}")


def _nearest(data, valid, lc, lr):
    h, w = data.shape
    ci = np.floor(lc + 0.5).astype(np.int64)
    ri = np.floor(lr + 0.5).astype(np.int64)
    inb = (ci >= 0) & (ci < w) & (ri >= 0) & (ri < h)
    ric = np.clip(ri, 0, h - 1)
    cic = np.clip(ci, 0, w - 1)
    ok = inb & valid[ric, cic]
    values = np.where(ok, data[ric, cic], np.nan)
    return values, ok


def _bilinear(data, valid, lc, lr):
    h, w = data.shape
    c0 = np.floor(lc).astype(np.int64)
    r0 = np.floor(lr).astype(np.int64)
    dx = lc - c0
    dy = lr - r0
    num = np.zeros(lc.shape, dtype=np.float64)
    den = np.zeros(lc.shape, dtype=np.float64)
    for dr in (0, 1):
        wr = dy if dr else 1.0 - dy
        for dc in (0, 1):
            wc = dx if dc else 1.0 - dx
            wt = wr * wc
            rr = r0 + dr
            cc = c0 + dc
            inb = (rr >= 0) & (rr < h) & (cc >= 0) & (cc < w)
            rrc = np.clip(rr, 0, h - 1)
            ccc = np.clip(cc, 0, w - 1)
            nb_ok = inb & valid[rrc, ccc]
            wv = np.where(nb_ok, wt, 0.0)
            # 无效位置的值(可能是 nodata/NaN/Inf)绝不进入加权
            num += wv * np.where(nb_ok, data[rrc, ccc], 0.0)
            den += wv
    ok = den > 0.0
    values = np.where(ok, num / np.where(ok, den, 1.0), np.nan)
    return values, ok
