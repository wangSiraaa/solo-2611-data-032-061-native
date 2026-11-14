"""单帧 DICOM 装载与像素类型校验。

本模块只负责“这一个文件能不能被纳入候选切片”的判断，
任何跨文件的几何一致性检查放在 geometry 模块。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pydicom

from .model import (
    PHOTOMETRIC_MONOCHROME2,
    TRANSFER_SYNTAX_EXPLICIT_VR_LE,
    GroupBuildError,
)

# 分组必须同时具备的标签
_REQUIRED_TAGS = (
    "SOPInstanceUID",
    "SeriesInstanceUID",
    "FrameOfReferenceUID",
    "ImagePositionPatient",
    "ImageOrientationPatient",
    "Rows",
    "Columns",
    "PixelSpacing",
    "SamplesPerPixel",
    "PhotometricInterpretation",
    "BitsAllocated",
    "BitsStored",
    "HighBit",
    "PixelRepresentation",
    "PixelData",
)


@dataclass
class LoadedSlice:
    """通过单文件校验后的候选切片（尚未做任何跨文件几何判断）。"""

    path: Path
    sop_instance_uid: str
    series_instance_uid: str
    frame_of_reference_uid: str
    rows: int
    columns: int
    # 行间距(沿 row 方向每像素物理长度)与列间距(沿 column 方向)，单位 mm，
    # 严格对应 DICOM PixelSpacing = [row spacing, column spacing]，不互换。
    row_spacing_mm: float
    column_spacing_mm: float
    image_position_patient: np.ndarray  # shape (3,)，LPS 毫米
    image_orientation_patient: np.ndarray  # shape (6,)
    pixel_array: np.ndarray  # 原始 16 位整数像素，shape (rows, columns)
    rescale_slope: float
    rescale_intercept: float
    rescale_defaulted: bool
    slice_thickness_mm: float | None
    instance_number: str | None
    file_sha256: str


def load_slice(path: str | Path) -> LoadedSlice:
    """读取并校验单个 DICOM 文件，失败时抛出 GroupBuildError。"""
    path = Path(path)
    problems: list[str] = []

    try:
        ds = pydicom.dcmread(str(path), stop_before_pixels=False)
    except Exception as exc:  # pydicom 对损坏文件可能抛多种异常
        raise GroupBuildError(f"无法作为 DICOM 读取: {exc}") from exc

    # ---- 传输语法：必须是未压缩 Explicit VR Little Endian ----
    tsuid = getattr(ds.file_meta, "TransferSyntaxUID", None)
    if tsuid is None:
        problems.append("缺少 TransferSyntaxUID")
    elif str(tsuid) != TRANSFER_SYNTAX_EXPLICIT_VR_LE:
        problems.append(
            f"不支持的传输语法 {tsuid}，仅支持未压缩 Explicit VR Little Endian"
            f"({TRANSFER_SYNTAX_EXPLICIT_VR_LE})"
        )

    # ---- 必填标签 ----
    missing = [name for name in _REQUIRED_TAGS if not hasattr(ds, name)]
    if missing:
        problems.append(f"缺少必要标签: {', '.join(missing)}")
        if missing:
            # 后续检查依赖这些标签，缺失时直接终止本文件校验
            raise GroupBuildError("文件不满足单帧切片输入要求", problems)

    # ---- 多帧明确拒绝（NumberOfFrames 存在且 >1；=1 的单帧也显式接受） ----
    number_of_frames = getattr(ds, "NumberOfFrames", None)
    if number_of_frames is not None and int(number_of_frames) > 1:
        problems.append(f"多帧对象被拒绝 (NumberOfFrames={int(number_of_frames)})")

    # ---- 彩色明确拒绝 ----
    if int(ds.SamplesPerPixel) != 1:
        problems.append(
            f"彩色/多采样像素被拒绝 (SamplesPerPixel={int(ds.SamplesPerPixel)})"
        )
    if str(ds.PhotometricInterpretation) != PHOTOMETRIC_MONOCHROME2:
        problems.append(
            f"仅支持 {PHOTOMETRIC_MONOCHROME2}，"
            f"实际为 {ds.PhotometricInterpretation}"
        )

    # ---- 16 位整数像素 ----
    if int(ds.BitsAllocated) != 16:
        problems.append(f"仅支持 16 位像素 (BitsAllocated={int(ds.BitsAllocated)})")
    if int(ds.BitsStored) != 16:
        problems.append(f"仅支持 16 位存储 (BitsStored={int(ds.BitsStored)})")
    if int(ds.HighBit) != 15:
        problems.append(f"HighBit 必须为 15，实际为 {int(ds.HighBit)}")
    if int(ds.PixelRepresentation) not in (0, 1):
        problems.append(
            f"PixelRepresentation 非法: {int(ds.PixelRepresentation)}"
        )

    rows = int(ds.Rows)
    columns = int(ds.Columns)
    if rows <= 0 or columns <= 0:
        problems.append(f"Rows/Columns 非法: Rows={rows}, Columns={columns}")

    spacing = [float(x) for x in ds.PixelSpacing]
    if (
        len(spacing) != 2
        or not all(np.isfinite(s) for s in spacing)
        or any(s <= 0 for s in spacing)
    ):
        problems.append(f"PixelSpacing 非法: {spacing}")
        spacing = [np.nan, np.nan]

    ipp = np.asarray([float(x) for x in ds.ImagePositionPatient], dtype=float)
    iop = np.asarray([float(x) for x in ds.ImageOrientationPatient], dtype=float)
    if ipp.shape != (3,):
        problems.append(f"ImagePositionPatient 必须含 3 个分量，实际 {ipp.shape}")
    if iop.shape != (6,):
        problems.append(
            f"ImageOrientationPatient 必须含 6 个分量，实际 {iop.shape}"
        )
    else:
        # IOP = [列方向(3), 行方向(3)]，与 geometry/affine 的约定保持一致
        col_dir = iop[:3]
        row_dir = iop[3:]
        if not (np.all(np.isfinite(row_dir)) and np.all(np.isfinite(col_dir))):
            problems.append("ImageOrientationPatient 含非有限数值")
        else:
            if abs(np.linalg.norm(row_dir) - 1.0) > 1e-6:
                problems.append("行方向单位向量模长不为 1")
            if abs(np.linalg.norm(col_dir) - 1.0) > 1e-6:
                problems.append("列方向单位向量模长不为 1")
            if abs(float(np.dot(row_dir, col_dir))) > 1e-6:
                problems.append("行/列方向不正交，非轴对齐切片平面")

    # ---- 像素缓冲区长度与声明尺寸一致 ----
    expected_bytes = rows * columns * 2
    if len(ds.PixelData) != expected_bytes:
        problems.append(
            f"PixelData 长度 {len(ds.PixelData)} 与 Rows*Columns*2={expected_bytes} 不一致"
        )

    if problems:
        raise GroupBuildError("文件不满足单帧切片输入要求", problems)

    pixel_array = np.asarray(ds.pixel_array)
    if pixel_array.shape != (rows, columns):
        raise GroupBuildError(
            "像素数组形状校验失败",
            [f"pixel_array.shape={pixel_array.shape}, 期望 ({rows}, {columns})"],
        )
    if pixel_array.dtype not in (np.int16, np.uint16):
        raise GroupBuildError(
            "像素数组不是 16 位整数类型",
            [f"dtype={pixel_array.dtype}"],
        )

    # Rescale 标签缺失时按 DICOM 约定退化为恒等变换，并记录该事实
    slope = float(getattr(ds, "RescaleSlope", 1.0))
    intercept = float(getattr(ds, "RescaleIntercept", 0.0))
    rescale_defaulted = not hasattr(ds, "RescaleSlope") or not hasattr(
        ds, "RescaleIntercept"
    )
    # SliceThickness 仅作记录，不作为层间距校验依据
    slice_thickness = (
        float(ds.SliceThickness) if hasattr(ds, "SliceThickness") else None
    )

    file_sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()

    return LoadedSlice(
        path=path,
        sop_instance_uid=str(ds.SOPInstanceUID),
        series_instance_uid=str(ds.SeriesInstanceUID),
        frame_of_reference_uid=str(ds.FrameOfReferenceUID),
        rows=rows,
        columns=columns,
        row_spacing_mm=spacing[0],
        column_spacing_mm=spacing[1],
        image_position_patient=ipp,
        image_orientation_patient=iop,
        pixel_array=pixel_array,
        rescale_slope=slope,
        rescale_intercept=intercept,
        rescale_defaulted=rescale_defaulted,
        slice_thickness_mm=slice_thickness,
        instance_number=str(ds.InstanceNumber) if hasattr(ds, "InstanceNumber") else None,
        file_sha256=file_sha256,
    )
