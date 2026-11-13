"""Analysis plan: parsing, validation and content-addressed freezing.

The plan is the single source of truth for design choice, contrasts, sample
exclusions and filtering thresholds. It is frozen (hashed) *before* any data
dependent decision, so filtering rules can never be chosen after looking at
p-values.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

from .errors import PlanError, ValidationError

VALID_DESIGNS = ("independent", "paired")
VALID_ENCODINGS = ("treatment", "sum")
VALID_DUP_POLICIES = ("error", "sum", "retain")
VALID_FILTER_TYPES = ("cpm", "counts", "none")


@dataclass
class Contrast:
    name: str
    numerator: str
    denominator: str

    def label(self) -> str:
        return f"{self.numerator}_vs_{self.denominator}"


@dataclass
class FilterRule:
    type: str = "cpm"               # cpm | counts | none
    min_value: float = 1.0          # min CPM (type=cpm) or min count
    min_samples: int | None = None  # default: size of smallest involved condition
    frozen_before_fit: bool = True


@dataclass
class Plan:
    design: str
    conditions: list[str]
    contrasts: list[Contrast]
    reference_condition: str | None = None
    encoding: str = "treatment"          # treatment | sum
    include_batch: bool = True
    duplicate_subject_policy: str = "error"  # error | sum | retain
    missing_pair_policy: str = "error"       # error | drop_subject
    excluded_sample_ids: list[str] = field(default_factory=list)
    exclusion_reasons: dict[str, str] = field(default_factory=dict)
    filter_rule: FilterRule = field(default_factory=FilterRule)
    moderate_dispersion: bool = True
    random_seed: int = 20261113
    project: str = "synthetic_demo"
    notes: str = ""

    @property
    def ref_level(self) -> str:
        return self.reference_condition or sorted(self.conditions)[0]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["contrasts"] = [asdict(c) for c in self.contrasts]
        return d

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))

    def freeze_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()[:16]


def load_plan(path: str | Path) -> Plan:
    p = Path(path)
    if not p.exists():
        raise PlanError(f"plan file not found: {p}")
    text = p.read_text(encoding="utf-8")
    raw = yaml.safe_load(text) if p.suffix in (".yaml", ".yml") else json.loads(text)
    return parse_plan(raw)


def parse_plan(raw: dict[str, Any]) -> Plan:
    if not isinstance(raw, dict):
        raise PlanError("plan must be a mapping")

    design = raw.get("design")
    if design not in VALID_DESIGNS:
        raise PlanError(f"design must be one of {VALID_DESIGNS}, got {design!r}")

    conditions = raw.get("conditions")
    if not isinstance(conditions, list) or len(conditions) < 2 or \
            not all(isinstance(c, str) and c for c in conditions) or \
            len(set(conditions)) != len(conditions):
        raise PlanError("conditions must be a list of >=2 unique non-empty strings")
    cond_set = set(conditions)

    contrast_raw = raw.get("contrasts")
    if not isinstance(contrast_raw, list) or not contrast_raw:
        raise PlanError("contrasts must be a non-empty list")
    contrasts: list[Contrast] = []
    seen: set[str] = set()
    for i, c in enumerate(contrast_raw):
        if not isinstance(c, dict):
            raise PlanError(f"contrast #{i} must be a mapping")
        name = c.get("name")
        num = c.get("numerator")
        den = c.get("denominator")
        if not name or not num or not den:
            raise PlanError(f"contrast #{i} needs name/numerator/denominator")
        if name in seen:
            raise PlanError(f"duplicate contrast name: {name}")
        seen.add(name)
        # Referencing an unknown condition blocks the analysis.
        if num not in cond_set:
            raise ValidationError(
                f"contrast {name!r} references unknown numerator condition {num!r}; "
                f"known conditions: {sorted(cond_set)}")
        if den not in cond_set:
            raise ValidationError(
                f"contrast {name!r} references unknown denominator condition {den!r}; "
                f"known conditions: {sorted(cond_set)}")
        if num == den:
            raise PlanError(f"contrast {name!r}: numerator and denominator are identical")
        contrasts.append(Contrast(name=name, numerator=num, denominator=den))

    ref = raw.get("reference_condition")
    if ref is not None and ref not in cond_set:
        raise ValidationError(
            f"reference_condition {ref!r} not in conditions {sorted(cond_set)}")

    encoding = raw.get("encoding", "treatment")
    if encoding not in VALID_ENCODINGS:
        raise PlanError(f"encoding must be one of {VALID_ENCODINGS}")

    dup_policy = raw.get("duplicate_subject_policy", "error")
    if dup_policy not in VALID_DUP_POLICIES:
        raise PlanError(f"duplicate_subject_policy must be one of {VALID_DUP_POLICIES}")
    missing_policy = raw.get("missing_pair_policy", "error")
    if missing_policy not in ("error", "drop_subject"):
        raise PlanError("missing_pair_policy must be 'error' or 'drop_subject'")

    excluded = list(raw.get("excluded_sample_ids", []) or [])
    reasons = dict(raw.get("exclusion_reasons", {}) or {})
    missing_reasons = [s for s in excluded if not reasons.get(s)]
    if missing_reasons:
        raise PlanError(
            f"every excluded sample needs a documented reason; missing: {missing_reasons}")
    unknown_reasons = [s for s in reasons if s not in excluded]
    if unknown_reasons:
        raise PlanError(f"exclusion_reasons given for non-excluded samples: {unknown_reasons}")

    fr_raw = raw.get("filter_rule", {}) or {}
    ftype = fr_raw.get("type", "cpm")
    if ftype not in VALID_FILTER_TYPES:
        raise PlanError(f"filter_rule.type must be one of {VALID_FILTER_TYPES}")
    min_value = float(fr_raw.get("min_value", 1.0))
    if min_value < 0:
        raise PlanError("filter_rule.min_value must be >= 0")
    min_samples = fr_raw.get("min_samples")
    if min_samples is not None and (not isinstance(min_samples, int) or min_samples < 1):
        raise PlanError("filter_rule.min_samples must be a positive integer or null")
    filter_rule = FilterRule(type=ftype, min_value=min_value,
                             min_samples=min_samples, frozen_before_fit=True)

    seed = int(raw.get("random_seed", 20261113))
    include_batch = bool(raw.get("include_batch", True))
    moderate = bool(raw.get("moderate_dispersion", True))

    return Plan(
        design=design,
        conditions=conditions,
        contrasts=contrasts,
        reference_condition=ref,
        encoding=encoding,
        include_batch=include_batch,
        duplicate_subject_policy=dup_policy,
        missing_pair_policy=missing_policy,
        excluded_sample_ids=excluded,
        exclusion_reasons=reasons,
        filter_rule=filter_rule,
        moderate_dispersion=moderate,
        random_seed=seed,
        project=str(raw.get("project", "synthetic_demo")),
        notes=str(raw.get("notes", "")),
    )
