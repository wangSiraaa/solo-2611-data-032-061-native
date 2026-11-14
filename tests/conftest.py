import sys
from pathlib import Path

import pytest

# 让测试无需安装即可 import 仓库根下的包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dicom_builder.model import Tolerances  # noqa: E402


@pytest.fixture()
def tolerances() -> Tolerances:
    return Tolerances()


@pytest.fixture()
def tol_file(tmp_path: Path) -> Path:
    p = tmp_path / "tol.json"
    p.write_text(
        '{"direction_abs": 1e-05, "spacing_mm": 0.0001, "position_mm": 0.0001}',
        encoding="utf-8",
    )
    return p
