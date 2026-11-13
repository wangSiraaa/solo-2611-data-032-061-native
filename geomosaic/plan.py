"""计划加载与开始前校验。

任何不满足约束的情况(缺失 CRS、不兼容单位、旋转网格、非单波段、
非连续量、CRS 不在支持列表等)都在读任何像元数据之前抛出 PlanError。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import rasterio
from rasterio.crs import CRS

from .errors import PlanError

#: 支持的采样方式
RESAMPLING_MODES = ("nearest", "bilinear")
#: 唯一支持的裁决规则(与 plan.json 的 priority_rule 文本对应)
PRIORITY_RULE = "larger numeric priority wins; ties ascending source id"
#: 连续量只接受浮点存储;整型/复数按分类量风险拒绝
CONTINUOUS_DTYPES = ("float32", "float64")


@dataclass
class SourceSpec:
    id: str
    path: str            # 解析后的实际路径
    priority: float
    unit: str
    crs: str = ""
    width: int = 0
    height: int = 0
    transform: object = None  # affine.Affine,已校验北向上无旋转
    nodata: float | None = None
    dtype: str = ""
    mask_flags: str = ""

    @property
    def res(self):
        return (self.transform.a, -self.transform.e)


@dataclass
class Plan:
    sources: list[SourceSpec]
    target_crs: str
    resolution: tuple[float, float]
    bounds: tuple[float, float, float, float]
    grid_anchor: tuple[float, float]
    unit: str
    supported_crs: list[str]
    resampling: str
    window_shape: tuple[int, int]  # (高, 宽),输出像元
    path: str = ""

    @property
    def priority_order(self) -> list[SourceSpec]:
        """裁决顺序:数值优先级大者优先,并列按来源 id 升序。与枚举顺序无关。"""
        return sorted(self.sources, key=lambda s: (-s.priority, s.id))

    @property
    def source_index(self) -> dict[str, int]:
        """来源栅格取值映射:id 字典序 → 1..N(0 保留为"无来源")。

        按 id 排序而非计划枚举顺序,保证重排 sources 列表后产物一致。
        """
        return {s.id: i + 1 for i, s in enumerate(sorted(self.sources, key=lambda s: s.id))}


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _require(cond, msg: str):
    if not cond:
        raise PlanError(msg)


def _resolve_path(path_str: str, plan_dir: Path) -> Path:
    """相对路径先相对当前工作目录、再相对计划文件目录解析。"""
    p = Path(path_str)
    if p.is_absolute():
        _require(p.exists(), f"来源文件不存在: {p}")
        return p
    if p.exists():
        return p.resolve()
    q = plan_dir / p
    if q.exists():
        return q.resolve()
    raise PlanError(f"来源文件不存在(已尝试当前目录与计划目录): {path_str}")


def _validate_source(spec: SourceSpec, plan_dir: Path, supported: list[CRS]) -> None:
    path = _resolve_path(spec.path, plan_dir)
    spec.path = str(path)
    try:
        ds = rasterio.open(path)
    except Exception as e:
        raise PlanError(f"来源 {spec.id} 无法打开: {e}") from e
    with ds:
        _require(ds.count == 1, f"来源 {spec.id} 为 {ds.count} 波段,仅支持单波段连续量")
        _require(ds.dtypes[0] in CONTINUOUS_DTYPES,
                 f"来源 {spec.id} 数据类型 {ds.dtypes[0]} 非连续浮点量,拒绝(不支持分类量)")
        if ds.crs is None:
            raise PlanError(f"来源 {spec.id} 缺失 CRS,开始前拒绝")
        if not any(ds.crs == c for c in supported):
            raise PlanError(f"来源 {spec.id} 的 CRS {ds.crs.to_string()} 不在 supported_crs 内,拒绝")
        t = ds.transform
        _require(t.b == 0 and t.d == 0, f"来源 {spec.id} 含旋转/剪切项,不支持旋转源")
        _require(t.a > 0 and t.e < 0, f"来源 {spec.id} 非北向上仿射网格,拒绝")
        spec.crs = ds.crs.to_string()
        spec.width, spec.height = ds.width, ds.height
        spec.transform = t
        spec.nodata = ds.nodata
        spec.dtype = ds.dtypes[0]
        spec.mask_flags = ",".join(f.name for f in ds.mask_flag_enums[0])


def load_plan(path) -> Plan:
    """解析并完整校验计划;失败抛 PlanError(在读任何像元之前)。"""
    p = Path(path)
    _require(p.exists(), f"计划文件不存在: {path}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PlanError(f"计划文件不是合法 JSON: {e}") from e
    _require(isinstance(raw, dict), "计划顶层必须是 JSON 对象")

    rule = raw.get("priority_rule", PRIORITY_RULE)
    _require(rule == PRIORITY_RULE,
             f"不支持的裁决规则 {rule!r},仅支持 {PRIORITY_RULE!r}")

    raw_sources = raw.get("sources")
    _require(isinstance(raw_sources, list) and raw_sources, "计划必须包含非空 sources 列表")
    sources: list[SourceSpec] = []
    seen: set[str] = set()
    for i, s in enumerate(raw_sources):
        _require(isinstance(s, dict), f"sources[{i}] 必须是对象")
        sid = s.get("id")
        _require(isinstance(sid, str) and sid.strip(), f"sources[{i}] 缺失有效 id")
        _require(sid not in seen, f"来源 id 重复: {sid}")
        seen.add(sid)
        spath = s.get("path")
        _require(isinstance(spath, str) and spath.strip(), f"来源 {sid} 缺失 path")
        pri = s.get("priority")
        _require(_is_num(pri), f"来源 {sid} 的 priority 必须是有限数值")
        unit = s.get("unit")
        _require(isinstance(unit, str) and unit.strip(),
                 f"来源 {sid} 缺失 unit,无法核对单位,开始前拒绝")
        sources.append(SourceSpec(id=sid, path=spath, priority=float(pri), unit=unit))

    tgt = raw.get("target")
    _require(isinstance(tgt, dict), "计划缺失 target 对象")
    tcrs = tgt.get("crs")
    _require(isinstance(tcrs, str) and tcrs.strip(), "target 缺失 crs")
    try:
        target_crs = CRS.from_string(tcrs)
    except Exception as e:
        raise PlanError(f"目标 CRS 无法识别: {tcrs}") from e
    res = tgt.get("resolution")
    _require(isinstance(res, list) and len(res) == 2 and all(_is_num(v) and v > 0 for v in res),
             "target.resolution 必须是两个正数 [rx, ry]")
    bounds = tgt.get("bounds")
    _require(isinstance(bounds, list) and len(bounds) == 4 and all(_is_num(v) for v in bounds),
             "target.bounds 必须是 [xmin, ymin, xmax, ymax]")
    xmin, ymin, xmax, ymax = (float(v) for v in bounds)
    _require(xmin < xmax and ymin < ymax, "target.bounds 必须满足 xmin<xmax 且 ymin<ymax")
    anchor = tgt.get("grid_anchor")
    _require(isinstance(anchor, list) and len(anchor) == 2 and all(_is_num(v) for v in anchor),
             "target.grid_anchor 必须是 [ax, ay]")
    unit = tgt.get("unit")
    _require(isinstance(unit, str) and unit.strip(), "target 缺失 unit,开始前拒绝")

    sup = raw.get("supported_crs")
    _require(isinstance(sup, list) and sup and all(isinstance(c, str) for c in sup),
             "supported_crs 必须是非空字符串列表")
    supported: list[CRS] = []
    for c in sup:
        try:
            supported.append(CRS.from_string(c))
        except Exception as e:
            raise PlanError(f"supported_crs 含无法识别的 CRS: {c}") from e
    _require(any(target_crs == c for c in supported),
             f"目标 CRS {tcrs} 不在 supported_crs 内,拒绝")

    rs = raw.get("resampling")
    _require(rs in RESAMPLING_MODES, f"不支持的 resampling {rs!r},仅 nearest/bilinear")
    ws = raw.get("window_shape")
    _require(isinstance(ws, list) and len(ws) == 2
             and all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in ws),
             "window_shape 必须是两个正整数 [高, 宽]")

    for s in sources:
        _require(s.unit == unit,
                 f"来源 {s.id} 的单位 {s.unit!r} 与目标单位 {unit!r} 不兼容,开始前拒绝")

    for s in sources:
        _validate_source(s, p.parent, supported)

    return Plan(
        sources=sources,
        target_crs=target_crs.to_string(),
        resolution=(float(res[0]), float(res[1])),
        bounds=(xmin, ymin, xmax, ymax),
        grid_anchor=(float(anchor[0]), float(anchor[1])),
        unit=unit,
        supported_crs=[c.to_string() for c in supported],
        resampling=rs,
        window_shape=(int(ws[0]), int(ws[1])),
        path=str(p),
    )
