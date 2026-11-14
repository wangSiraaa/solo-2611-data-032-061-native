"""测试用合成 DICOM 生成器（仅测试代码使用，不属于业务处理器）。

所有数据均为程序合成，不含真实患者信息。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import RLELossless

TRANSFER_EXPLICIT_VR_LE = "1.2.840.10008.1.2.1"


def _rle_encode_segment(data: bytes) -> bytes:
    """最小 RLE 分段编码器（仅测试夹具使用）。

    采用“字面值”模式：每 128 字节以内用 1 字节计数头 + 原始字节，
    产生的是合法但未做游程压缩的 RLE 数据流。
    """
    out = bytearray()
    for start in range(0, len(data), 128):
        chunk = data[start:start + 128]
        out.append(len(chunk) - 1)  # 字面值计数 n-1
        out.extend(chunk)
    return bytes(out)


def _rle_codestream(pixels: np.ndarray, rows: int, columns: int,
                    samples_per_pixel: int, number_of_frames: int) -> bytes:
    """组装带 RLE 头（16 个 uint32 段偏移）的完整封装流。"""
    frames = (
        pixels.reshape(number_of_frames, rows, columns, samples_per_pixel)
        if number_of_frames > 1
        else pixels.reshape(1, rows, columns, samples_per_pixel)
    )
    segments: list[bytes] = []
    for frame in frames:
        # 16 位像素按高字节、低字节两个平面分段
        raw = np.ascontiguousarray(frame, dtype="<u2").tobytes()
        high = bytes(raw[i] for i in range(1, len(raw), 2))
        low = bytes(raw[i] for i in range(0, len(raw), 2))
        segments.append(_rle_encode_segment(high))
        segments.append(_rle_encode_segment(low))
    header = bytearray(64)
    offsets = []
    cursor = 64
    stream = bytearray()
    for seg in segments:
        offsets.append(cursor)
        stream.extend(seg)
        cursor += len(seg)
    for i, offset in enumerate(offsets):
        header[i * 4:i * 4 + 4] = int(offset).to_bytes(4, "little")
    return bytes(header) + bytes(stream)



def make_dicom(
    path: str | Path,
    *,
    sop_uid: str,
    series_uid: str = "1.2.826.0.1.3680043.10.777.20",
    for_uid: str = "1.2.826.0.1.3680043.10.777.30",
    ipp=(0.0, 0.0, 0.0),
    iop=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0),
    rows: int = 4,
    columns: int = 5,
    pixel_spacing=(0.7, 0.9),
    pixels: np.ndarray | None = None,
    slope: float = 1.0,
    intercept: float = 0.0,
    photometric: str = "MONOCHROME2",
    bits_allocated: int = 16,
    bits_stored: int = 16,
    high_bit: int = 15,
    pixel_representation: int = 1,
    samples_per_pixel: int = 1,
    number_of_frames: int | None = None,
    transfer_syntax: str = TRANSFER_EXPLICIT_VR_LE,
    instance_number: int | str = 1,
    include_rescale: bool = True,
    slice_thickness: float | None = 2.0,
) -> Path:
    """生成一个单帧（或指定多帧）合成 DICOM 文件并返回路径。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if pixels is None:
        pixels = (np.arange(rows * columns, dtype=np.int16) % 100).reshape(
            rows, columns
        )

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = transfer_syntax
    file_meta.ImplementationClassUID = "1.2.826.0.1.3680043.10.999.99"

    ds = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.SOPInstanceUID = sop_uid
    ds.Modality = "CT"
    ds.PatientName = "SYNTHETIC"
    ds.PatientID = "TEST"
    ds.StudyInstanceUID = "1.2.826.0.1.3680043.10.999.10"
    ds.SeriesInstanceUID = series_uid
    ds.FrameOfReferenceUID = for_uid
    ds.InstanceNumber = str(instance_number)
    if slice_thickness is not None:
        ds.SliceThickness = str(slice_thickness)

    ds.ImagePositionPatient = [float(v) for v in ipp]
    ds.ImageOrientationPatient = [float(v) for v in iop]

    ds.SamplesPerPixel = samples_per_pixel
    ds.PhotometricInterpretation = photometric
    ds.Rows = rows
    ds.Columns = columns
    ds.PixelSpacing = [float(v) for v in pixel_spacing]
    ds.BitsAllocated = bits_allocated
    ds.BitsStored = bits_stored
    ds.HighBit = high_bit
    ds.PixelRepresentation = pixel_representation
    if number_of_frames is not None:
        ds.NumberOfFrames = str(number_of_frames)
    if include_rescale:
        ds.RescaleSlope = str(slope)
        ds.RescaleIntercept = str(intercept)

    if transfer_syntax == RLELossless:
        # 用内置最小 RLE 编码器生成合法封装流，避免引入额外编码插件
        n_frames = int(number_of_frames) if number_of_frames else 1
        codestream = _rle_codestream(
            np.ascontiguousarray(pixels), rows, columns,
            samples_per_pixel, n_frames,
        )
        ds.PixelData = pydicom.encaps.encapsulate([codestream])
    else:
        ds.PixelData = np.ascontiguousarray(pixels).tobytes()

    ds.save_as(str(path), enforce_file_format=True)
    return path


def axis_aligned_series(
    directory: Path,
    *,
    series_uid: str = "1.2.826.0.1.3680043.10.777.20.1",
    for_uid: str = "1.2.826.0.1.3680043.10.777.30.1",
    n: int = 3,
    gap_mm: float = 2.0,
    slopes=(1.0, 2.0, 3.0),
    intercepts=(0.0, 0.0, 0.0),
    start_z: float = 10.0,
    iop=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0),
    rows: int = 4,
    columns: int = 5,
    pixel_spacing=(0.7, 0.9),
    uid_prefix: str = "1.2.826.0.1.3680043.10.777.1",
) -> list[Path]:
    """生成 n 个轴对齐平行切片，文件名顺序与空间顺序刻意打乱。"""
    paths = []
    # 逆序写文件名，模拟文件名/读取顺序与空间顺序不一致
    for k in range(n):
        idx = n - 1 - k
        pixels = np.full((rows, columns), idx * 10, dtype=np.int16)
        # 文件名带 UID 前缀，避免同一目录中多个 series 的 file0.dcm 互相覆盖
        tag = uid_prefix.replace(".", "_")
        paths.append(
            make_dicom(
                directory / f"{tag}_file{idx}.dcm",
                sop_uid=f"{uid_prefix}.{idx}",
                series_uid=series_uid,
                for_uid=for_uid,
                ipp=(0.0, 0.0, start_z + idx * gap_mm),
                iop=iop,
                rows=rows,
                columns=columns,
                pixel_spacing=pixel_spacing,
                pixels=pixels,
                slope=slopes[idx],
                intercept=intercepts[idx],
                instance_number=100 - idx,  # 同样与空间顺序相反
            )
        )
    return paths
