"""Capture-manifest loading and comparability checks.

The manifest carries the SQL identity (sql_sha256), parameters, the data
snapshot hash and the capture settings of each side.  Identity mismatch
makes the pair not directly comparable; a snapshot mismatch still allows
showing differences but blocks any plan-caused-regression claim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Capture:
    path: str
    raw: dict[str, Any]

    @property
    def sql_sha256(self) -> str | None:
        return self.raw.get("sql_sha256")

    @property
    def sql(self) -> str | None:
        return self.raw.get("sql")

    @property
    def parameters(self) -> Any:
        return self.raw.get("parameters")

    @property
    def data_snapshot(self) -> str | None:
        return self.raw.get("data_snapshot")

    @property
    def timing_note(self) -> str | None:
        return self.raw.get("timing_note")

    def settings_sql(self, side: str) -> str | None:
        obj = self.raw.get(side)
        return obj.get("settings_sql") if isinstance(obj, dict) else None

    def file_for(self, side: str) -> str | None:
        obj = self.raw.get(side)
        return obj.get("file") if isinstance(obj, dict) else None


def load_capture(path: str | Path) -> Capture:
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: 采集清单应为 JSON 对象")
    return Capture(path=str(path), raw=raw)


def compare_manifests(before_cap: Capture | None, after_cap: Capture | None) -> dict[str, Any]:
    """Machine-readable comparability verdict for a before/after pair."""
    if before_cap is None and after_cap is None:
        return {
            "status": "no_manifest",
            "directly_comparable": False,
            "can_attribute_to_plan": False,
            "checks": [],
            "caveats": ["未提供采集清单：无法核对 SQL 身份、参数与数据快照，仅做结构对比"],
        }

    cap = before_cap or after_cap
    shared = (
        before_cap is not None
        and after_cap is not None
        and before_cap.path == after_cap.path
    )
    checks: list[dict[str, Any]] = []
    caveats: list[str] = []

    # -- SQL identity -----------------------------------------------------
    if shared:
        checks.append({
            "check": "sql_identity",
            "result": "same_manifest",
            "detail": f"同一清单覆盖两侧，sql_sha256={cap.sql_sha256}",
        })
        identity_ok = True
    else:
        b_id = before_cap.sql_sha256 if before_cap else None
        a_id = after_cap.sql_sha256 if after_cap else None
        if b_id and a_id:
            identity_ok = b_id == a_id
        elif before_cap and after_cap:
            identity_ok = (before_cap.sql is not None
                           and before_cap.sql == after_cap.sql)
        else:
            identity_ok = False
        checks.append({
            "check": "sql_identity",
            "result": "match" if identity_ok else "mismatch",
            "before": b_id,
            "after": a_id,
        })

    # -- parameters ---------------------------------------------------------
    if shared:
        checks.append({
            "check": "parameters",
            "result": "same_manifest",
            "value": cap.parameters,
        })
        params_ok = True
    else:
        bp = before_cap.parameters if before_cap else None
        ap = after_cap.parameters if after_cap else None
        params_ok = bp == ap
        checks.append({
            "check": "parameters",
            "result": "match" if params_ok else "mismatch",
            "before": bp,
            "after": ap,
        })

    # -- data snapshot ------------------------------------------------------
    if shared:
        checks.append({
            "check": "data_snapshot",
            "result": "same_manifest",
            "value": cap.data_snapshot,
        })
        snapshot_ok = True
    else:
        bs = before_cap.data_snapshot if before_cap else None
        as_ = after_cap.data_snapshot if after_cap else None
        snapshot_ok = bs is not None and bs == as_
        checks.append({
            "check": "data_snapshot",
            "result": "match" if snapshot_ok else "mismatch",
            "before": bs,
            "after": as_,
        })
    if not snapshot_ok:
        caveats.append("数据快照不同：可展示差异，但不能断言计划本身造成回归")

    # -- capture settings -----------------------------------------------------
    b_set = before_cap.settings_sql("before") if before_cap else None
    a_set = (after_cap or before_cap).settings_sql("after") if (after_cap or before_cap) else None
    if b_set is not None and a_set is not None:
        settings_same = b_set == a_set
        checks.append({
            "check": "capture_settings",
            "result": "same" if settings_same else "differ",
            "before": b_set,
            "after": a_set,
        })
        if not settings_same:
            caveats.append(
                "两次采集的执行设置不同：计划结构变化可能由设置驱动，"
                "不能单独归因于数据或优化器"
            )
    else:
        settings_same = False
        checks.append({"check": "capture_settings", "result": "unknown"})

    # -- timing note ----------------------------------------------------------
    note = cap.timing_note
    if note:
        caveats.append(f"清单注明：{note}")

    if not identity_ok or not params_ok:
        status = "identity_mismatch"
    elif not snapshot_ok:
        status = "snapshot_mismatch"
    else:
        status = "ok"

    return {
        "status": status,
        "directly_comparable": status != "identity_mismatch",
        # 只有身份、参数、快照、设置全部一致时才允许把差异归因于计划本身。
        "can_attribute_to_plan": status == "ok" and settings_same,
        "checks": checks,
        "caveats": caveats,
    }
