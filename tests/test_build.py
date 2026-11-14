"""端到端与单元测试。

覆盖：附带夹具的成功/失败路径、affine 角点独立核对、逐片强度缩放、
文件顺序无关性、各类明确拒绝项、重复 SOP 折叠与冲突、容差配置、
失败隔离与陈旧产物清理。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.uid import RLELossless

from dicom_builder.builder import build
from dicom_builder.model import GroupBuildError, Tolerances
from dicom_builder.loader import load_slice

from .dicom_factory import axis_aligned_series, make_dicom

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DICOM = REPO_ROOT / "fixtures" / "dicom"
FIXTURE_TOL = REPO_ROOT / "fixtures" / "tolerances.json"


def _group_dir(output_dir: Path, series_uid: str) -> Path:
    safe = "".join(ch if ch.isalnum() else "_" for ch in series_uid)
    return output_dir / f"series_{safe}"


# ---------------------------------------------------------------------------
# 1. 附带夹具：斜向平行切片成功，非法层间距组失败，失败组隔离
# ---------------------------------------------------------------------------

def test_fixtures_g1_success_g2_failure(tmp_path: Path):
    out = tmp_path / "out"
    report = build(FIXTURE_DICOM, out, Tolerances.from_file(FIXTURE_TOL))

    assert not report.ok
    by_series = {g["series_instance_uid"]: g for g in report.to_dict()["groups"]}
    g1 = by_series["1.2.826.0.1.3680043.10.999.20.1"]
    g2 = by_series["1.2.826.0.1.3680043.10.999.20.2"]

    assert g1["status"] == "success"
    assert g2["status"] == "failed"
    assert "层间距不均" in g2["reason"]
    assert any("3.000000mm" in d for d in g2["details"])

    g1_dir = Path(g1["output_dir"])
    assert sorted(p.name for p in g1_dir.iterdir()) == [
        "metadata.json", "sources.csv", "volume.npy",
    ]
    volume = np.load(g1_dir / "volume.npy")
    assert volume.dtype == np.float32
    assert volume.shape == (3, 4, 5)

    # 失败组不得留下任何看似成功的产物
    g2_dir = _group_dir(out, "1.2.826.0.1.3680043.10.999.20.2")
    assert not g2_dir.exists()
    assert not list(out.rglob("volume.npy")) == []  # g1 的产物存在
    assert len(list(out.rglob("volume.npy"))) == 1  # 且只有 g1 一份


def test_fixture_sorting_ignores_filename_and_instance_number(tmp_path: Path):
    """空间顺序由平面位置决定：fixture 中文件名/InstanceNumber 与空间顺序相反。"""
    out = tmp_path / "out"
    build(FIXTURE_DICOM, out, Tolerances.from_file(FIXTURE_TOL))
    g1_dir = _group_dir(out, "1.2.826.0.1.3680043.10.999.20.1")
    sources = list(
        pydicom.dcmread(p) for p in sorted(FIXTURE_DICOM.glob("g1-*.dcm"))
    )
    ipp_by_sop = {str(ds.SOPInstanceUID): np.array(ds.ImagePositionPatient, float)
                  for ds in sources}

    import csv
    rows = list(csv.DictReader((g1_dir / "sources.csv").open(encoding="utf-8")))
    assert [r["source_file"].split("/")[-1] for r in rows] == [
        "g1-file1.dcm", "g1-file2.dcm", "g1-file3.dcm"
    ]
    # InstanceNumber 18/19/20 的顺序恰与文件名顺序一致，但真正依据是法向投影
    offsets = [float(r["plane_offset_mm"]) for r in rows]
    assert offsets == sorted(offsets)
    assert offsets[0] == pytest.approx(0.0, abs=1e-9)
    # 投影沿完整 3D 法向单调，而非按 z 分量（本组 z 实际递减）
    zs = [ipp_by_sop[r["sop_instance_uid"]][2] for r in rows]
    assert zs == sorted(zs, reverse=True)


# ---------------------------------------------------------------------------
# 2. affine 角点独立核对（用原始 IOP/IPP 重新推导，不复用被测代码）
# ---------------------------------------------------------------------------

def test_affine_corner_mapping_independent(tmp_path: Path):
    out = tmp_path / "out"
    build(FIXTURE_DICOM, out, Tolerances.from_file(FIXTURE_TOL))
    g1_dir = _group_dir(out, "1.2.826.0.1.3680043.10.999.20.1")
    meta = json.loads((g1_dir / "metadata.json").read_text(encoding="utf-8"))
    affine = np.array(meta["affine"]["matrix"], float)

    # 独立从原始 DICOM 标签推导期望坐标
    files = sorted(FIXTURE_DICOM.glob("g1-*.dcm"))
    datasets = {str(ds.SOPInstanceUID): ds
                for ds in (pydicom.dcmread(p) for p in files)}
    ordered_sops = [s["sop_instance_uid"] for s in meta["slices"]]
    rows, columns = 4, 5

    for k, sop in enumerate(ordered_sops):
        ds = datasets[sop]
        iop = np.array(ds.ImageOrientationPatient, float)
        col_dir, row_dir = iop[:3], iop[3:]
        row_spacing, col_spacing = (float(v) for v in ds.PixelSpacing)
        ipp = np.array(ds.ImagePositionPatient, float)
        for i, j in [(0, 0), (rows - 1, 0), (0, columns - 1),
                     (rows - 1, columns - 1), (2, 3)]:
            expected = (
                ipp
                + row_dir * (i * row_spacing)
                + col_dir * (j * col_spacing)
            )
            got = affine @ np.array([k, i, j, 1.0])
            # 夹具 IPP/IOP 本身以约 10 位小数给出，独立推导与被测实现各自
            # 浮点求积，1e-7mm 远小于配置位置容差 1e-4mm
            assert got[:3] == pytest.approx(expected, abs=1e-7), (k, i, j)
            assert got[3] == pytest.approx(1.0)

    # 行列间距方向不可互换：轴1步长=0.7mm 沿 row_dir，轴2步长=0.9mm 沿 col_dir
    # IOP 余弦以 10 位小数给出，单位向量模长本身有约 1e-10 的截断误差
    assert np.linalg.norm(affine[:3, 1]) == pytest.approx(0.7, abs=1e-9)
    assert np.linalg.norm(affine[:3, 2]) == pytest.approx(0.9, abs=1e-9)
    assert affine[:3, 1] / 0.7 == pytest.approx(
        np.array([0.0, 0.9553364891, 0.2955202067]), abs=1e-9
    )
    assert affine[:3, 2] / 0.9 == pytest.approx([1.0, 0.0, 0.0], abs=1e-12)
    # 层方向是完整 3D 法向（不是 z 轴），步长 2mm；
    # 夹具 IPP 仅 10 位小数，反推间距含约 4e-9mm 的输入截断误差
    assert np.linalg.norm(affine[:3, 0]) == pytest.approx(2.0, abs=1e-7)
    assert affine[0, 0] == pytest.approx(0.0, abs=1e-12)


# ---------------------------------------------------------------------------
# 3. 逐层强度缩放：几何相同但 slope 不同，禁止复用首片参数
# ---------------------------------------------------------------------------

def test_per_slice_rescale_applied_independently(tmp_path: Path):
    out = tmp_path / "out"
    build(FIXTURE_DICOM, out, Tolerances.from_file(FIXTURE_TOL))
    g1_dir = _group_dir(out, "1.2.826.0.1.3680043.10.999.20.1")
    volume = np.load(g1_dir / "volume.npy")
    meta = json.loads((g1_dir / "metadata.json").read_text(encoding="utf-8"))

    for entry in meta["slices"]:
        k = entry["slice_index"]
        ds = pydicom.dcmread(entry["source_file"])
        expected = (
            ds.pixel_array.astype(np.float32) * np.float32(entry["rescale_slope"])
            + np.float32(entry["rescale_intercept"])
        )
        assert volume[k] == pytest.approx(expected, abs=1e-6)

    # 三片 slope 分别为 3/2/1，若错误复用首片 slope=3 则不可能同时满足
    slopes = [s["rescale_slope"] for s in meta["slices"]]
    assert slopes == [3.0, 2.0, 1.0]
    # 抽查具体值：原始像素 0 的片，slope=1/intercept=-100 -> -100
    assert volume[2, 0, 0] == pytest.approx(-100.0)
    # 原始像素 60 的片，slope=3/intercept=-100 -> 80
    assert volume[0, 0, 0] == pytest.approx(80.0)


# ---------------------------------------------------------------------------
# 4. 文件读取/命名顺序不影响体数据
# ---------------------------------------------------------------------------

def test_build_is_independent_of_file_order(tmp_path: Path):
    dir_a = tmp_path / "input_a"
    dir_b = tmp_path / "input_b"
    axis_aligned_series(dir_a, uid_prefix="1.2.826.0.1.3680043.10.777.1.8")
    # 第二份目录：相同 SOP/几何/像素，但文件名加随机前缀使遍历顺序反转
    dir_b.mkdir(parents=True)
    for p in sorted(dir_a.glob("*.dcm")):
        ds = pydicom.dcmread(str(p))
        shutil.copy(p, dir_b / f"zzz_reversed_{p.name}")
        assert ds  # 重读确保文件可解析

    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"
    report_a = build(dir_a, out_a, Tolerances())
    report_b = build(dir_b, out_b, Tolerances())
    assert report_a.ok and report_b.ok

    va = np.load(next(out_a.rglob("volume.npy")))
    vb = np.load(next(out_b.rglob("volume.npy")))
    np.testing.assert_array_equal(va, vb)

    ma = json.loads(next(out_a.rglob("metadata.json")).read_text("utf-8"))
    mb = json.loads(next(out_b.rglob("metadata.json")).read_text("utf-8"))
    # affine 原点取空间排序后第 0 片，必须与文件遍历顺序逐元素一致
    assert np.allclose(ma["affine"]["matrix"], mb["affine"]["matrix"], atol=1e-12)
    assert ma["affine"]["origin_lps_mm"] == mb["affine"]["origin_lps_mm"]
    assert [s["sop_instance_uid"] for s in ma["slices"]] == [
        s["sop_instance_uid"] for s in mb["slices"]
    ]
    assert [s["plane_offset_mm"] for s in ma["slices"]] == pytest.approx(
        [s["plane_offset_mm"] for s in mb["slices"]]
    )
    # 规范原点就是空间第 0 片的原始 IPP
    ds0 = pydicom.dcmread(ma["slices"][0]["source_file"])
    assert ma["affine"]["origin_lps_mm"] == pytest.approx(
        [float(v) for v in ds0.ImagePositionPatient], abs=1e-12
    )


# ---------------------------------------------------------------------------
# 5. 明确拒绝：多帧、彩色、压缩、非 MONOCHROME2、非 16 位
# ---------------------------------------------------------------------------

def _build_single_input(tmp_path: Path, **kwargs) -> dict:
    in_dir = tmp_path / "in"
    make_dicom(in_dir / "only.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.1", **kwargs)
    out = tmp_path / "out"
    report = build(in_dir, out, Tolerances())
    return report.to_dict()


def test_reject_multiframe(tmp_path: Path):
    pixels = np.zeros((2, 4, 5), dtype=np.int16)
    report = _build_single_input(
        tmp_path, number_of_frames=2, pixels=pixels,
    )
    assert report["summary"]["groups_total"] == 0
    assert any("多帧" in r["reason"] or any("多帧" in d for d in r["details"])
               for r in report["rejected_files"])
    assert not list(Path(tmp_path / "out").rglob("volume.npy"))


def test_reject_color(tmp_path: Path):
    pixels = np.zeros((4, 5, 3), dtype=np.uint16)
    report = _build_single_input(
        tmp_path, samples_per_pixel=3, photometric="RGB", pixels=pixels,
        bits_allocated=16, bits_stored=16, high_bit=15, pixel_representation=0,
    )
    assert report["rejected_files"]
    joined = json.dumps(report["rejected_files"], ensure_ascii=False)
    assert "彩色" in joined


def test_reject_compressed_transfer_syntax(tmp_path: Path):
    pixels = np.arange(20, dtype=np.uint16).reshape(4, 5)
    report = _build_single_input(
        tmp_path, transfer_syntax=RLELossless, pixels=pixels,
        pixel_representation=0,
    )
    assert report["rejected_files"]
    joined = json.dumps(report["rejected_files"], ensure_ascii=False)
    assert "Explicit VR" in joined
    # 压缩流不会被解码成体数组
    assert not list(Path(tmp_path / "out").rglob("volume.npy"))


def test_reject_implicit_vr_little_endian(tmp_path: Path):
    """Implicit VR Little Endian (1.2.840.10008.1.2) 同样不在支持范围内。"""
    in_dir = tmp_path / "in"
    path = make_dicom(in_dir / "only.dcm",
                      sop_uid="1.2.826.0.1.3680043.10.777.1.20")
    ds = pydicom.dcmread(str(path))
    ds.file_meta.TransferSyntaxUID = "1.2.840.10008.1.2"  # Implicit VR LE
    ds.save_as(str(path), enforce_file_format=True)
    out = tmp_path / "out"
    report = build(in_dir, out, Tolerances()).to_dict()
    assert report["rejected_files"]
    joined = json.dumps(report["rejected_files"], ensure_ascii=False)
    assert "Explicit VR" in joined


def test_reject_monochrome1(tmp_path: Path):
    report = _build_single_input(tmp_path, photometric="MONOCHROME1")
    joined = json.dumps(report["rejected_files"], ensure_ascii=False)
    assert "MONOCHROME2" in joined


def test_reject_8bit(tmp_path: Path):
    pixels = np.arange(20, dtype=np.uint8).reshape(4, 5)
    report = _build_single_input(
        tmp_path, bits_allocated=8, bits_stored=8, high_bit=7,
        pixel_representation=0, pixels=pixels,
    )
    joined = json.dumps(report["rejected_files"], ensure_ascii=False)
    assert "16 位" in joined


# ---------------------------------------------------------------------------
# 6. 分组级几何失败：非平行、方向不一致、重复平面、间距不均、网格不一致
# ---------------------------------------------------------------------------

def _two_slice_group(tmp_path: Path, override: dict) -> dict:
    in_dir = tmp_path / "in"
    common = dict(series_uid="1.2.826.0.1.3680043.10.777.20.2", for_uid="1.2.826.0.1.3680043.10.777.30.2")
    make_dicom(in_dir / "a.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.2",
               ipp=(0, 0, 0), instance_number=2, **common)
    params = {
        "ipp": (0, 0, 2),
        "instance_number": 1,
        **common,
        **override,
    }
    make_dicom(in_dir / "b.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.3", **params)
    out = tmp_path / "out"
    return build(in_dir, out, Tolerances()).to_dict()


def test_reject_non_parallel_slices(tmp_path: Path):
    # b 片法向明显偏转 -> 非平行
    report = _two_slice_group(
        tmp_path, {"iop": (1, 0, 0, 0, 0.9, 0.43589)}
    )
    group = report["groups"][0]
    assert group["status"] == "failed"
    assert "非平行" in group["reason"] or "方向不一致" in group["reason"]


def test_reject_inplane_orientation_change(tmp_path: Path):
    # 法向仍平行（同为 z），但行/列方向在面内旋转 90°
    report = _two_slice_group(
        tmp_path, {"iop": (0, 1, 0, -1, 0, 0)}
    )
    group = report["groups"][0]
    assert group["status"] == "failed"
    assert "方向不一致" in group["reason"]


def test_reject_duplicate_plane(tmp_path: Path):
    report = _two_slice_group(tmp_path, {"ipp": (0, 0, 0)})
    group = report["groups"][0]
    assert group["status"] == "failed"
    assert "重复平面" in group["reason"]
    assert any("重复物理平面" in d for d in group["details"])


def test_reject_uneven_spacing(tmp_path: Path):
    in_dir = tmp_path / "in"
    for idx, z in enumerate((0.0, 2.0, 5.0)):  # 间距 2mm 与 3mm
        make_dicom(
            in_dir / f"s{idx}.dcm", sop_uid=f"1.2.826.0.1.3680043.10.777.1.7.{idx}",
            series_uid="1.2.826.0.1.3680043.10.777.20.2", for_uid="1.2.826.0.1.3680043.10.777.30.2",
            ipp=(0, 0, z), instance_number=idx,
        )
    out = tmp_path / "out"
    report = build(in_dir, out, Tolerances()).to_dict()
    group = report["groups"][0]
    assert group["status"] == "failed"
    assert "层间距不均" in group["reason"]
    assert not _group_dir(out, "1.2.826.0.1.3680043.10.777.20.2").exists()


def test_reject_grid_mismatch_rows_and_spacing(tmp_path: Path):
    in_dir = tmp_path / "in"
    make_dicom(in_dir / "a.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.2", ipp=(0, 0, 0),
               series_uid="1.2.826.0.1.3680043.10.777.20.2", for_uid="1.2.826.0.1.3680043.10.777.30.2", rows=4, columns=5)
    make_dicom(in_dir / "b.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.3", ipp=(0, 0, 2),
               series_uid="1.2.826.0.1.3680043.10.777.20.2", for_uid="1.2.826.0.1.3680043.10.777.30.2", rows=3, columns=5)
    out = tmp_path / "out"
    report = build(in_dir, out, Tolerances()).to_dict()
    assert report["groups"][0]["status"] == "failed"
    assert "网格几何不一致" in report["groups"][0]["reason"]

    # 间距不一致同样拒绝
    shutil.rmtree(in_dir)
    make_dicom(in_dir / "a.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.2", ipp=(0, 0, 0),
               series_uid="1.2.826.0.1.3680043.10.777.20.3", for_uid="1.2.826.0.1.3680043.10.777.30.3",
               pixel_spacing=(0.7, 0.9))
    make_dicom(in_dir / "b.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.3", ipp=(0, 0, 2),
               series_uid="1.2.826.0.1.3680043.10.777.20.3", for_uid="1.2.826.0.1.3680043.10.777.30.3",
               pixel_spacing=(0.7, 0.95))
    report2 = build(in_dir, out, Tolerances()).to_dict()
    g2 = next(g for g in report2["groups"]
              if g["series_instance_uid"] == "1.2.826.0.1.3680043.10.777.20.3")
    assert g2["status"] == "failed"
    assert "网格几何不一致" in g2["reason"]


def test_reject_single_slice_group(tmp_path: Path):
    report = _build_single_input(tmp_path)
    # 单文件通过单文件校验、成为一个切片的分组，组级判定失败
    assert report["groups"][0]["status"] == "failed"
    assert "切片数不足" in report["groups"][0]["reason"]


# ---------------------------------------------------------------------------
# 7. 重复 SOPInstanceUID：相同内容折叠；内容不同冲突失败
# ---------------------------------------------------------------------------

def test_duplicate_sop_identical_collapses(tmp_path: Path):
    in_dir = tmp_path / "in"
    pixels = np.arange(20, dtype=np.int16).reshape(4, 5)
    make_dicom(in_dir / "copy_a.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.5",
               ipp=(0, 0, 0), pixels=pixels, slope=2.0, intercept=-5.0)
    make_dicom(in_dir / "copy_b.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.5",
               ipp=(0, 0, 0), pixels=pixels, slope=2.0, intercept=-5.0)
    make_dicom(in_dir / "other.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.6", ipp=(0, 0, 2))
    out = tmp_path / "out"
    report = build(in_dir, out, Tolerances()).to_dict()
    assert report["groups"][0]["status"] == "success"

    meta = json.loads(next(out.rglob("metadata.json")).read_text("utf-8"))
    assert len(meta["slices"]) == 2  # 重复层折叠后只剩 2 层
    assert len(meta["duplicates_collapsed"]) == 1
    entry = meta["duplicates_collapsed"][0]
    assert entry["sop_instance_uid"] == "1.2.826.0.1.3680043.10.777.1.5"
    assert entry["duplicate_file"].endswith("copy_b.dcm")
    assert entry["kept_file"].endswith("copy_a.dcm")


def test_duplicate_sop_conflicting_pixels_fails(tmp_path: Path):
    in_dir = tmp_path / "in"
    make_dicom(in_dir / "a.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.5", ipp=(0, 0, 0),
               pixels=np.zeros((4, 5), np.int16))
    make_dicom(in_dir / "b.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.5", ipp=(0, 0, 0),
               pixels=np.ones((4, 5), np.int16))
    make_dicom(in_dir / "c.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.4", ipp=(0, 0, 2))
    out = tmp_path / "out"
    report = build(in_dir, out, Tolerances()).to_dict()
    assert report["groups"][0]["status"] == "failed"
    assert "内容冲突" in report["groups"][0]["reason"]
    assert not list(out.rglob("volume.npy"))


def test_duplicate_sop_conflicting_rescale_fails(tmp_path: Path):
    in_dir = tmp_path / "in"
    pixels = np.arange(20, dtype=np.int16).reshape(4, 5)
    make_dicom(in_dir / "a.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.5", ipp=(0, 0, 0),
               pixels=pixels, slope=1.0)
    make_dicom(in_dir / "b.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.5", ipp=(0, 0, 0),
               pixels=pixels, slope=9.0)
    make_dicom(in_dir / "c.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.4", ipp=(0, 0, 2))
    out = tmp_path / "out"
    report = build(in_dir, out, Tolerances()).to_dict()
    assert report["groups"][0]["status"] == "failed"
    joined = json.dumps(report["groups"], ensure_ascii=False)
    assert "RescaleSlope" in joined


# ---------------------------------------------------------------------------
# 8. 容差配置：单位与生效路径
# ---------------------------------------------------------------------------

def test_tolerance_config_tight_and_loose(tmp_path: Path):
    in_dir = tmp_path / "in"
    # 列间距相差 0.00005mm
    make_dicom(in_dir / "a.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.2", ipp=(0, 0, 0),
               pixel_spacing=(0.7, 0.9), series_uid="1.2.826.0.1.3680043.10.777.20.2", for_uid="1.2.826.0.1.3680043.10.777.30.2")
    make_dicom(in_dir / "b.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.3", ipp=(0, 0, 2),
               pixel_spacing=(0.7, 0.90005),
               series_uid="1.2.826.0.1.3680043.10.777.20.2", for_uid="1.2.826.0.1.3680043.10.777.30.2")

    tight = Tolerances(spacing_mm=1e-6)
    out_tight = tmp_path / "out_tight"
    assert build(in_dir, out_tight, tight).to_dict()["groups"][0]["status"] == "failed"

    loose = Tolerances(spacing_mm=1e-3)
    out_loose = tmp_path / "out_loose"
    assert build(in_dir, out_loose, loose).to_dict()["groups"][0]["status"] == "success"


def test_tolerance_file_units_recorded(tmp_path: Path):
    tol = Tolerances.from_file(FIXTURE_TOL)
    assert tol.spacing_mm == 0.0001
    assert tol.position_mm == 0.0001
    assert tol.direction_abs == 1e-5
    with pytest.raises(ValueError):
        Tolerances(spacing_mm=-1.0).validate()


# ---------------------------------------------------------------------------
# 9. 其他：非 DICOM 跳过、陈旧产物清理、分组键、默认 rescale
# ---------------------------------------------------------------------------

def test_non_dicom_file_skipped(tmp_path: Path):
    in_dir = tmp_path / "in"
    axis_aligned_series(in_dir)
    (in_dir / "notes.txt").write_text("not a dicom", encoding="utf-8")
    out = tmp_path / "out"
    report = build(in_dir, out, Tolerances()).to_dict()
    assert report["summary"]["groups_succeeded"] == 1
    assert any(p.endswith("notes.txt") for p in report["skipped_non_dicom"])


def test_stale_success_artifacts_removed_on_failed_rerun(tmp_path: Path):
    in_dir = tmp_path / "in"
    axis_aligned_series(in_dir, series_uid="1.2.826.0.1.3680043.10.777.20.6",
                        for_uid="1.2.826.0.1.3680043.10.777.30.6")
    out = tmp_path / "out"
    first = build(in_dir, out, Tolerances()).to_dict()
    assert first["groups"][0]["status"] == "success"
    gdir = _group_dir(out, "1.2.826.0.1.3680043.10.777.20.6")
    assert (gdir / "volume.npy").exists()

    # 用同 SeriesUID 的坏几何替换输入后重跑
    shutil.rmtree(in_dir)
    make_dicom(in_dir / "a.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.2", ipp=(0, 0, 0),
               series_uid="1.2.826.0.1.3680043.10.777.20.6", for_uid="1.2.826.0.1.3680043.10.777.30.6")
    make_dicom(in_dir / "b.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.3", ipp=(0, 0, 2),
               series_uid="1.2.826.0.1.3680043.10.777.20.6", for_uid="1.2.826.0.1.3680043.10.777.30.6")
    make_dicom(in_dir / "c.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.4", ipp=(0, 0, 9),
               series_uid="1.2.826.0.1.3680043.10.777.20.6", for_uid="1.2.826.0.1.3680043.10.777.30.6")
    second = build(in_dir, out, Tolerances()).to_dict()
    assert second["groups"][0]["status"] == "failed"
    assert not gdir.exists()  # 旧成功产物被清理，不留下误导性 volume.npy


def test_series_and_frame_of_reference_composite_grouping(tmp_path: Path):
    in_dir = tmp_path / "in"
    # 同 FoR、不同 Series -> 两个独立组
    axis_aligned_series(in_dir, series_uid="1.2.826.0.1.3680043.10.777.20.4",
                        for_uid="1.2.826.0.1.3680043.10.777.30.4",
                        uid_prefix="1.2.826.0.1.3680043.10.777.1.9")
    axis_aligned_series(in_dir, series_uid="1.2.826.0.1.3680043.10.777.20.5",
                        for_uid="1.2.826.0.1.3680043.10.777.30.4",
                        uid_prefix="1.2.826.0.1.3680043.10.777.1.10")
    out = tmp_path / "out"
    report = build(in_dir, out, Tolerances()).to_dict()
    assert report["summary"]["groups_total"] == 2
    assert report["summary"]["groups_succeeded"] == 2


def test_default_rescale_recorded(tmp_path: Path):
    in_dir = tmp_path / "in"
    make_dicom(in_dir / "a.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.2", ipp=(0, 0, 0),
               include_rescale=False,
               series_uid="1.2.826.0.1.3680043.10.777.20.2", for_uid="1.2.826.0.1.3680043.10.777.30.2")
    make_dicom(in_dir / "b.dcm", sop_uid="1.2.826.0.1.3680043.10.777.1.3", ipp=(0, 0, 2),
               include_rescale=False,
               series_uid="1.2.826.0.1.3680043.10.777.20.2", for_uid="1.2.826.0.1.3680043.10.777.30.2")
    out = tmp_path / "out"
    build(in_dir, out, Tolerances())
    meta = json.loads(next(out.rglob("metadata.json")).read_text("utf-8"))
    assert all(s["rescale_defaulted"] for s in meta["slices"])
    volume = np.load(next(out.rglob("volume.npy")))
    ds = pydicom.dcmread(in_dir / "a.dcm")
    np.testing.assert_array_equal(volume[0], ds.pixel_array.astype(np.float32))


def test_loader_rejects_corrupt_file(tmp_path: Path):
    bad = tmp_path / "bad.dcm"
    bad.write_bytes(b"\x00\x01not dicom at all")
    with pytest.raises(GroupBuildError):
        load_slice(bad)


def test_missing_input_dir_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        build(tmp_path / "does_not_exist", tmp_path / "out", Tolerances())


def test_empty_input_dir_succeeds_with_report(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    report = build(empty, tmp_path / "out", Tolerances()).to_dict()
    assert report["summary"]["groups_total"] == 0
    assert report["summary"]["groups_succeeded"] == 0


def test_affine_keeps_row_and_column_spacing_directions_distinct(tmp_path: Path):
    """非方形像素 + 正交方向：行/列间距与方向必须严格对应，不可互换。"""
    in_dir = tmp_path / "in"
    # IOP = [col_dir=(1,0,0), row_dir=(0,1,0)]，PixelSpacing=[行0.7, 列0.9]
    for k, z in enumerate((0.0, 2.0)):
        make_dicom(in_dir / f"s{k}.dcm",
                   sop_uid=f"1.2.826.0.1.3680043.10.777.5.{k}",
                   ipp=(5.0, 7.0, z), iop=(1, 0, 0, 0, 1, 0),
                   pixel_spacing=(0.7, 0.9))
    out = tmp_path / "out"
    report = build(in_dir, out, Tolerances()).to_dict()
    assert report["summary"]["groups_succeeded"] == 1
    meta = json.loads(next(out.rglob("metadata.json")).read_text("utf-8"))
    affine = np.array(meta["affine"]["matrix"])

    # 轴 1（row）：0.7mm 沿 y；轴 2（column）：0.9mm 沿 x——若互换则断言失败
    assert affine[:3, 1] == pytest.approx([0.0, 0.7, 0.0], abs=1e-12)
    assert affine[:3, 2] == pytest.approx([0.9, 0.0, 0.0], abs=1e-12)
    assert meta["spacing"]["row_spacing_mm"] == 0.7
    assert meta["spacing"]["column_spacing_mm"] == 0.9
    # 轴对齐平面的法向为 ±z，面内分量必须为 0（层方向不是 z 分量启发式）
    assert affine[0, 0] == pytest.approx(0.0, abs=1e-12)
    assert affine[1, 0] == pytest.approx(0.0, abs=1e-12)
    assert abs(affine[2, 0]) == pytest.approx(2.0, abs=1e-9)
