"""数据模型与容差配置。

所有几何容差均带明确物理单位，只允许在配置文件中覆盖，
不在处理器内写死任何夹具数值。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

# 仅支持的传输语法：未压缩 Explicit VR Little Endian
TRANSFER_SYNTAX_EXPLICIT_VR_LE = "1.2.840.10008.1.2.1"

# 仅支持的光度解释
PHOTOMETRIC_MONOCHROME2 = "MONOCHROME2"


class GroupBuildError(Exception):
    """单个分组在装载或几何校验阶段失败。

    一个分组抛出该错误不应影响其他分组继续构建。
    """

    def __init__(self, reason: str, details: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details: list[str] = details or []


@dataclass(frozen=True)
class Tolerances:
    """几何校验容差，全部为绝对容差。

    direction_abs: 方向余弦分量的绝对差（无量纲单位向量分量）。
    spacing_mm:    层间距与间距像素值的绝对容差，单位毫米(mm)。
    position_mm:   平面位置/投影坐标的绝对容差，单位毫米(mm)。
    """

    direction_abs: float = 1e-5
    spacing_mm: float = 1e-4
    position_mm: float = 1e-4

    @classmethod
    def from_file(cls, path: str | Path | None) -> "Tolerances":
        if path is None:
            return cls()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        tol = cls(
            direction_abs=float(data.get("direction_abs", cls.direction_abs)),
            spacing_mm=float(data.get("spacing_mm", cls.spacing_mm)),
            position_mm=float(data.get("position_mm", cls.position_mm)),
        )
        tol.validate()
        return tol

    def validate(self) -> None:
        for name in ("direction_abs", "spacing_mm", "position_mm"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"容差 {name} 必须为正的有限数值，实际为 {value!r}")

    def to_dict(self) -> dict[str, float]:
        return asdict(self)
