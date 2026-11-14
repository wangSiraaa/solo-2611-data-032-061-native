"""扫描输入目录、分组构建三维体并原子落盘。

产物布局（output_dir 下）：
  build_report.json                 全局构建报告（成功/失败/拒绝全记录）
  series_<清洗后的 SeriesUID>/
      volume.npy                    float32 体数组，轴次序 (slice, row, column)
      metadata.json                 几何、affine、逐片缩放与来源元数据
      sources.csv                   逐层来源表（原文件、SOP 标识、排序位置）

失败分组只在 build_report.json 中留下错误记录，不会产生 volume.npy 等
看似成功的产物；重跑前会清理同名旧目录，避免陈旧成功产物残留。
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pydicom
from pydicom.errors import InvalidDicomError

from .geometry import GroupGeometry, build_group_geometry
from .loader import LoadedSlice, load_slice
from .model import GroupBuildError, Tolerances


@dataclass
class GroupOutcome:
    key: tuple[str, str]
    status: str  # "success" | "failed"
    output_dir: str | None = None
    reason: str | None = None
    details: list[str] = field(default_factory=list)
    slice_count: int = 0


@dataclass
class BuildReport:
    input_dir: str
    output_dir: str
    tolerances: dict
    generated_at: str
    groups: list[GroupOutcome] = field(default_factory=list)
    rejected_files: list[dict] = field(default_factory=list)
    skipped_non_dicom: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(g.status == "success" for g in self.groups) and not self.rejected_files

    def to_dict(self) -> dict:
        return {
            "input_dir": self.input_dir,
            "output_dir": self.output_dir,
            "generated_at": self.generated_at,
            "tolerances": self.tolerances,
            "summary": {
                "groups_total": len(self.groups),
                "groups_succeeded": sum(g.status == "success" for g in self.groups),
                "groups_failed": sum(g.status == "failed" for g in self.groups),
                "rejected_files": len(self.rejected_files),
                "skipped_non_dicom": len(self.skipped_non_dicom),
            },
            "groups": [
                {
                    "series_instance_uid": g.key[0],
                    "frame_of_reference_uid": g.key[1],
                    "status": g.status,
                    "slice_count": g.slice_count,
                    "output_dir": g.output_dir,
                    "reason": g.reason,
                    "details": g.details,
                }
                for g in self.groups
            ],
            "rejected_files": self.rejected_files,
            "skipped_non_dicom": self.skipped_non_dicom,
        }


def _sanitize_uid(uid: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in uid)


def _is_dicom_file(path: Path) -> bool:
    """轻量探测：能否作为 DICOM 数据集打开（不解析像素）。"""
    try:
        pydicom.dcmread(str(path), stop_before_pixels=True, force=False)
    except InvalidDicomError:
        return False
    except Exception:
        # 能识别为 DICOM 但标签损坏的情况仍交给 load_slice 做正式拒绝
        return True
    return True


def discover_files(input_dir: Path) -> list[Path]:
    """按路径字符串排序收集候选文件，保证结果与目录遍历顺序无关。"""
    files = [
        p for p in input_dir.rglob("*")
        if p.is_file()
    ]
    files.sort(key=lambda p: str(p))
    return files


def build(
    input_dir: str | Path,
    output_dir: str | Path,
    tolerances: Tolerances,
) -> BuildReport:
    input_dir = Path(input_dir).resolve()
    if not input_dir.is_dir():
        raise FileNotFoundError(f"输入目录不存在或不是目录: {input_dir}")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    report = BuildReport(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        tolerances=tolerances.to_dict(),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )

    # ---- 1. 扫描与单文件校验 ----
    grouped: dict[tuple[str, str], list[LoadedSlice]] = {}
    for path in discover_files(input_dir):
        if not _is_dicom_file(path):
            report.skipped_non_dicom.append(str(path))
            continue
        try:
            loaded = load_slice(path)
        except GroupBuildError as exc:
            report.rejected_files.append(
                {
                    "file": str(path),
                    "reason": exc.reason,
                    "details": exc.details,
                }
            )
            continue
        key = (loaded.series_instance_uid, loaded.frame_of_reference_uid)
        grouped.setdefault(key, []).append(loaded)

    # 分组键排序，保证组间处理顺序确定
    for key in sorted(grouped):
        slices = grouped[key]
        outcome = GroupOutcome(
            key=key, status="failed", slice_count=len(slices)
        )
        report.groups.append(outcome)
        group_dir = output_dir / f"series_{_sanitize_uid(key[0])}"
        # 清理可能存在的陈旧成功产物
        if group_dir.exists():
            shutil.rmtree(group_dir)

        try:
            geometry = build_group_geometry(slices, tolerances)
        except GroupBuildError as exc:
            outcome.reason = exc.reason
            outcome.details = exc.details
            continue

        try:
            _write_group_atomic(group_dir, key, geometry, tolerances)
        except Exception as exc:  # 写盘失败同样视为该组失败，且不留半成品
            outcome.reason = f"产物写入失败: {exc}"
            shutil.rmtree(group_dir, ignore_errors=True)
            continue

        outcome.status = "success"
        outcome.output_dir = str(group_dir)

    _write_report_atomic(output_dir / "build_report.json", report.to_dict())
    return report


def _build_volume(geometry: GroupGeometry) -> np.ndarray:
    """逐片应用各自的 RescaleSlope/RescaleIntercept，输出 float32。

    几何相同但缩放不同的切片不得复用首片参数——这里显式逐片计算。
    """
    first = geometry.ordered[0].loaded
    volume = np.empty(
        (len(geometry.ordered), first.rows, first.columns), dtype=np.float32
    )
    for ordered in geometry.ordered:
        s = ordered.loaded
        # float32 运算与存储，保持与输出 dtype 一致
        volume[ordered.index, :, :] = (
            s.pixel_array.astype(np.float32) * np.float32(s.rescale_slope)
            + np.float32(s.rescale_intercept)
        )
    return volume


def _write_group_atomic(
    group_dir: Path,
    key: tuple[str, str],
    geometry: GroupGeometry,
    tolerances: Tolerances,
) -> None:
    """先写入同文件系统下的临时目录，成功后整体原子改名。"""
    parent = group_dir.parent
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".{group_dir.name}.", dir=parent))
    try:
        volume = _build_volume(geometry)

        # volume.npy：先写临时名再改名；用文件句柄避免 np.save 自动追加后缀
        tmp_npy = tmp_dir / "volume.npy.tmp"
        with tmp_npy.open("wb") as fh:
            np.save(fh, volume)
        os.replace(tmp_npy, tmp_dir / "volume.npy")

        metadata = _build_metadata(key, geometry, tolerances, volume)
        (tmp_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        _write_sources(tmp_dir / "sources.csv", geometry)
        # 全部产物写完后一次性发布
        os.replace(tmp_dir, group_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _build_metadata(
    key: tuple[str, str],
    geometry: GroupGeometry,
    tolerances: Tolerances,
    volume: np.ndarray,
) -> dict:
    first = geometry.ordered[0].loaded
    return {
        "series_instance_uid": key[0],
        "frame_of_reference_uid": key[1],
        "volume": {
            "file": "volume.npy",
            "dtype": str(volume.dtype),
            "shape": list(volume.shape),
            "axis_order": [
                "axis 0: slice，沿切片平面法向（层方向）",
                "axis 1: row，沿 ImageOrientationPatient 后三个分量（行方向）",
                "axis 2: column，沿 ImageOrientationPatient 前三个分量（列方向）",
            ],
            "value_mapping": "float32 值 = 原始16位整数像素 * RescaleSlope + RescaleIntercept（逐片应用）",
        },
        "coordinate_system": "DICOM 患者坐标系 LPS，单位毫米(mm)",
        "affine": {
            "matrix": geometry.affine.tolist(),
            "convention": "affine @ [k, i, j, 1]^T = 体素 (k=slice, i=row, j=column) 中心的 LPS 毫米坐标",
            "column_0_mm_per_slice": (geometry.affine[:3, 0]).tolist(),
            "column_1_mm_per_row": (geometry.affine[:3, 1]).tolist(),
            "column_2_mm_per_column": (geometry.affine[:3, 2]).tolist(),
            "origin_lps_mm": (geometry.affine[:3, 3]).tolist(),
        },
        "directions": {
            "row_direction": geometry.row_direction.tolist(),
            "column_direction": geometry.column_direction.tolist(),
            "normal_direction": geometry.normal_direction.tolist(),
        },
        "spacing": {
            "row_spacing_mm": geometry.row_spacing_mm,
            "column_spacing_mm": geometry.column_spacing_mm,
            "slice_gap_mm": geometry.slice_gaps_mm[0],
            "slice_gaps_mm": geometry.slice_gaps_mm,
            "slice_thickness_mm": first.slice_thickness_mm,
        },
        "pixel_type": {
            "photometric_interpretation": "MONOCHROME2",
            "bits_allocated": 16,
            "bits_stored": 16,
            "high_bit": 15,
            "pixel_representation": int(first.pixel_array.dtype == np.int16),
            "source_dtype": str(first.pixel_array.dtype),
        },
        "tolerances_used": tolerances.to_dict(),
        "duplicates_collapsed": geometry.duplicates_collapsed,
        "slices": [
            {
                "slice_index": o.index,
                "sop_instance_uid": o.loaded.sop_instance_uid,
                "source_file": str(o.loaded.path),
                "instance_number": o.loaded.instance_number,
                "image_position_patient_lps_mm": o.loaded.image_position_patient.tolist(),
                "image_orientation_patient": o.loaded.image_orientation_patient.tolist(),
                "plane_offset_mm": o.plane_offset_mm,
                "in_plane_residual_mm": o.in_plane_residual_mm,
                "rescale_slope": o.loaded.rescale_slope,
                "rescale_intercept": o.loaded.rescale_intercept,
                "rescale_defaulted": o.loaded.rescale_defaulted,
                "file_sha256": o.loaded.file_sha256,
            }
            for o in geometry.ordered
        ],
    }


def _write_sources(path: Path, geometry: GroupGeometry) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "slice_index",
            "sop_instance_uid",
            "source_file",
            "instance_number",
            "ipp_x_lps_mm",
            "ipp_y_lps_mm",
            "ipp_z_lps_mm",
            "plane_offset_mm",
            "in_plane_residual_mm",
            "rescale_slope",
            "rescale_intercept",
            "rescale_defaulted",
            "file_sha256",
        ])
        for o in geometry.ordered:
            s = o.loaded
            writer.writerow([
                o.index,
                s.sop_instance_uid,
                str(s.path),
                s.instance_number if s.instance_number is not None else "",
                *s.image_position_patient.tolist(),
                o.plane_offset_mm,
                o.in_plane_residual_mm,
                s.rescale_slope,
                s.rescale_intercept,
                str(s.rescale_defaulted).lower(),
                s.file_sha256,
            ])


def _write_report_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _json_default(obj):
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    raise TypeError(f"无法序列化类型 {type(obj)!r}")
