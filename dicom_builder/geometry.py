"""切片分组的几何校验、平面排序与 LPS affine 构造。

坐标约定：DICOM 患者坐标系为 LPS（X+ 指向患者左侧、Y+ 指向后侧、
Z+ 指向头侧），单位毫米。ImageOrientationPatient 的前三个分量是沿
数组列方向（column，体素第 2 轴）移动一行像素的方向，后三个分量是
沿数组行方向（row，体素第 1 轴）移动一列像素的方向——本模块严格
保持该对应关系，不互换行列间距。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .loader import LoadedSlice
from .model import GroupBuildError, Tolerances


@dataclass
class OrderedSlice:
    """通过全部几何校验、并赋予空间序号后的切片。"""

    loaded: LoadedSlice
    index: int                 # 体数组第 0 轴下标（沿层方向自底向上）
    plane_offset_mm: float     # 沿公共法向 n 的投影位置 t = n·IPP，单位 mm
    in_plane_residual_mm: float  # IPP 相对首片平面原点的面内残差，单位 mm


@dataclass
class GroupGeometry:
    """一个分组的完整几何结论。"""

    ordered: list[OrderedSlice]
    row_direction: np.ndarray   # shape (3,)，单位向量，体素轴 1 的物理步进方向
    column_direction: np.ndarray  # shape (3,)，单位向量，体素轴 2 的物理步进方向
    normal_direction: np.ndarray  # shape (3,)，单位向量，体素轴 0 的物理步进方向
    row_spacing_mm: float
    column_spacing_mm: float
    slice_gaps_mm: list[float]
    affine: np.ndarray          # shape (4,4)，数组索引 -> LPS 毫米
    duplicates_collapsed: list[dict]


def _check_common_scalars(slices: list[LoadedSlice], tol: Tolerances) -> None:
    """Rows/Columns/PixelSpacing 必须全组一致。"""
    problems: list[str] = []
    base = slices[0]
    for s in slices[1:]:
        if s.rows != base.rows or s.columns != base.columns:
            problems.append(
                f"{s.path.name}: Rows/Columns 为 {s.rows}x{s.columns}，"
                f"与首片 {base.rows}x{base.columns} 不一致"
            )
        if abs(s.row_spacing_mm - base.row_spacing_mm) > tol.spacing_mm:
            problems.append(
                f"{s.path.name}: 行间距 {s.row_spacing_mm}mm 与首片 "
                f"{base.row_spacing_mm}mm 差异超过容差 {tol.spacing_mm}mm"
            )
        if abs(s.column_spacing_mm - base.column_spacing_mm) > tol.spacing_mm:
            problems.append(
                f"{s.path.name}: 列间距 {s.column_spacing_mm}mm 与首片 "
                f"{base.column_spacing_mm}mm 差异超过容差 {tol.spacing_mm}mm"
            )
    if problems:
        raise GroupBuildError("分组内像素网格几何不一致", problems)


def _collapse_duplicate_sop(
    slices: list[LoadedSlice], tol: Tolerances
) -> tuple[list[LoadedSlice], list[dict]]:
    """折叠重复 SOPInstanceUID。

    身份相同且像素与关键几何完全一致（像素按字节值精确比较，
    几何在配置容差内、缩放参数精确相等）时折叠并记录；
    身份相同而内容不同则直接判冲突，该组失败。
    """
    seen: dict[str, LoadedSlice] = {}
    kept: list[LoadedSlice] = []
    collapsed: list[dict] = []
    problems: list[str] = []

    for s in slices:
        if s.sop_instance_uid not in seen:
            seen[s.sop_instance_uid] = s
            kept.append(s)
            continue
        ref = seen[s.sop_instance_uid]
        diffs: list[str] = []
        if not np.array_equal(s.pixel_array, ref.pixel_array):
            diffs.append("像素数据不同")
        if not np.allclose(
            s.image_position_patient, ref.image_position_patient,
            atol=tol.position_mm, rtol=0.0,
        ):
            diffs.append(
                f"ImagePositionPatient 差异超过位置容差 {tol.position_mm}mm"
            )
        if not np.allclose(
            s.image_orientation_patient, ref.image_orientation_patient,
            atol=tol.direction_abs, rtol=0.0,
        ):
            diffs.append(
                f"ImageOrientationPatient 差异超过方向容差 {tol.direction_abs}"
            )
        if abs(s.row_spacing_mm - ref.row_spacing_mm) > tol.spacing_mm:
            diffs.append("行间距不同")
        if abs(s.column_spacing_mm - ref.column_spacing_mm) > tol.spacing_mm:
            diffs.append("列间距不同")
        if (s.rows, s.columns) != (ref.rows, ref.columns):
            diffs.append("Rows/Columns 不同")
        # 缩放参数是强度转换的关键参数，要求精确一致，不复用首片参数
        if (s.rescale_slope, s.rescale_intercept) != (
            ref.rescale_slope,
            ref.rescale_intercept,
        ):
            diffs.append("RescaleSlope/RescaleIntercept 不同")
        if diffs:
            problems.append(
                f"SOPInstanceUID {s.sop_instance_uid} 重复但内容冲突 "
                f"({s.path.name} vs {ref.path.name}): " + "; ".join(diffs)
            )
            continue
        collapsed.append(
            {
                "sop_instance_uid": s.sop_instance_uid,
                "kept_file": str(ref.path),
                "duplicate_file": str(s.path),
                "file_sha256_kept": ref.file_sha256,
                "file_sha256_duplicate": s.file_sha256,
                "reason": "SOPInstanceUID 相同且像素与关键几何一致，折叠为同一层",
            }
        )

    if problems:
        raise GroupBuildError("重复 SOPInstanceUID 存在内容冲突", problems)
    return kept, collapsed


def build_group_geometry(
    slices: list[LoadedSlice], tol: Tolerances
) -> GroupGeometry:
    """对同一 (SeriesInstanceUID, FrameOfReferenceUID) 分组做完整校验。"""
    if len(slices) < 2:
        raise GroupBuildError(
            "分组切片数不足，无法构成三维体（至少需要 2 个平行切片）",
            [f"当前切片数: {len(slices)}"],
        )

    slices, duplicates = _collapse_duplicate_sop(slices, tol)
    _check_common_scalars(slices, tol)

    base = slices[0]
    row_dir = base.image_orientation_patient[3:].copy()
    col_dir = base.image_orientation_patient[:3].copy()
    normal = np.cross(row_dir, col_dir)
    norm = np.linalg.norm(normal)
    if not np.isfinite(norm) or norm < 1e-9:
        raise GroupBuildError(
            "行/列方向叉积退化，无法确定切片平面法向",
            [f"ImageOrientationPatient={base.image_orientation_patient.tolist()}"],
        )
    normal /= norm

    # ---- 方向一致性：逐片比较行/列分量，并要求各片法向平行 ----
    problems: list[str] = []
    for s in slices[1:]:
        r = s.image_orientation_patient[3:]
        c = s.image_orientation_patient[:3]
        if np.any(np.abs(r - row_dir) > tol.direction_abs):
            problems.append(
                f"{s.path.name}: 行方向 {r.tolist()} 与首片 {row_dir.tolist()} "
                f"分量差超过方向容差 {tol.direction_abs}"
            )
        if np.any(np.abs(c - col_dir) > tol.direction_abs):
            problems.append(
                f"{s.path.name}: 列方向 {c.tolist()} 与首片 {col_dir.tolist()} "
                f"分量差超过方向容差 {tol.direction_abs}"
            )
        n_i = np.cross(r, c)
        n_i_norm = np.linalg.norm(n_i)
        if n_i_norm < 1e-9:
            problems.append(f"{s.path.name}: 方向叉积退化，切片平面无法确定")
            continue
        n_i = n_i / n_i_norm
        # 平行（同向或反向）判定，容差作用于叉积模长（无量纲）
        if np.linalg.norm(np.cross(n_i, normal)) > tol.direction_abs:
            problems.append(
                f"{s.path.name}: 切片平面与首片不平行，法向夹角超出方向容差 "
                f"{tol.direction_abs}（非平行切片明确拒绝，不做配准）"
            )
    if problems:
        raise GroupBuildError("分组内切片方向不一致或存在非平行切片", problems)

    # ---- 平面位置：完整 3D 法向投影，禁止只用位置的 z 分量 ----
    origin = base.image_position_patient
    records: list[tuple[LoadedSlice, float, float]] = []
    for s in slices:
        delta = s.image_position_patient - origin
        t = float(np.dot(normal, delta))
        # 面内残差：delta 去掉法向分量后的长度，理想平行切片应为 0
        residual_vec = delta - normal * t
        residual = float(np.linalg.norm(residual_vec))
        records.append((s, t, residual))

    for s, _t, residual in records:
        if residual > tol.position_mm:
            problems.append(
                f"{s.path.name}: 平面原点存在 {residual:.6f}mm 的面内偏移，"
                f"超过位置容差 {tol.position_mm}mm"
            )
    if problems:
        raise GroupBuildError("切片平面原点未落在同一法向轴上", problems)

    # ---- 沿法向排序（与文件名、InstanceNumber、读取顺序无关）----
    records.sort(key=lambda item: item[1])

    # ---- 重复平面检测：投影位置相同即同一物理平面 ----
    for (s_a, t_a, _), (s_b, t_b, _) in zip(records, records[1:]):
        if abs(t_b - t_a) <= tol.position_mm:
            problems.append(
                f"检测到重复物理平面: {s_a.path.name} 与 {s_b.path.name} "
                f"法向投影位置差 {abs(t_b - t_a):.6f}mm <= 位置容差 "
                f"{tol.position_mm}mm"
            )
    if problems:
        raise GroupBuildError("分组内存在重复平面", problems)

    # ---- 层间距均匀性 ----
    gaps = [t_b - t_a for (_a, t_a, _), (_b, t_b, _) in zip(records, records[1:])]
    median_gap = float(np.median(gaps))
    for gap, (rec_a, rec_b) in zip(gaps, zip(records, records[1:])):
        sa, _ta, _ = rec_a
        sb, t_b, _ = rec_b
        if abs(gap - median_gap) > tol.spacing_mm:
            problems.append(
                f"层间距不均: {sa.path.name} -> {sb.path.name} 间距 "
                f"{gap:.6f}mm，与中位间距 {median_gap:.6f}mm 之差 "
                f"{abs(gap - median_gap):.6f}mm 超过间距容差 {tol.spacing_mm}mm"
            )
    if problems:
        raise GroupBuildError("分组内层间距不均匀，拒绝猜测缺失层", problems)

    ordered = [
        OrderedSlice(loaded=s, index=i, plane_offset_mm=t, in_plane_residual_mm=r)
        for i, (s, t, r) in enumerate(records)
    ]

    # 规范原点取空间排序后第 0 片的 IPP，而不是读取到的首片，
    # 保证 affine 与文件遍历/命名顺序完全无关
    canonical_origin = ordered[0].loaded.image_position_patient
    affine = _build_affine(
        origin=canonical_origin,
        row_dir=row_dir,
        col_dir=col_dir,
        normal=normal,
        row_spacing=base.row_spacing_mm,
        col_spacing=base.column_spacing_mm,
        slice_gap=median_gap,
    )

    return GroupGeometry(
        ordered=ordered,
        row_direction=row_dir,
        column_direction=col_dir,
        normal_direction=normal,
        row_spacing_mm=base.row_spacing_mm,
        column_spacing_mm=base.column_spacing_mm,
        slice_gaps_mm=gaps,
        affine=affine,
        duplicates_collapsed=duplicates,
    )


def _build_affine(
    origin: np.ndarray,
    row_dir: np.ndarray,
    col_dir: np.ndarray,
    normal: np.ndarray,
    row_spacing: float,
    col_spacing: float,
    slice_gap: float,
) -> np.ndarray:
    """构造 4x4 affine。

    体数组轴次序固定为 (slice, row, column) = (K, Rows, Columns)：
      轴 0 (k)：沿切片法向 normal，每步 slice_gap mm；
      轴 1 (i)：沿 IOP 后三个分量 row_dir，每步 PixelSpacing[0] mm；
      轴 2 (j)：沿 IOP 前三个分量 col_dir，每步 PixelSpacing[1] mm。
    affine @ [k, i, j, 1]^T = 体素中心在 DICOM LPS 患者坐标系中的
    毫米坐标 [X, Y, Z, 1]^T。行列间距方向严格不互换。
    """
    affine = np.eye(4, dtype=float)
    affine[:3, 0] = normal * slice_gap
    affine[:3, 1] = row_dir * row_spacing
    affine[:3, 2] = col_dir * col_spacing
    affine[:3, 3] = origin
    return affine
