"""Loading and validation of the analysis plan (plan.json).

The plan freezes the downstream analysis model, the legal value ranges
and the columns that imputation models may use as predictors.  Anything
outside the declared schema is rejected here, before any computation.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from .errors import DiagnosticError

SUPPORTED_VARIABLE_TYPE = "continuous"
SUPPORTED_INAPPLICABLE_POLICY = "exclude affected rows from this analysis"
ROW_ID = "row_id"


@dataclass(frozen=True)
class VariableSpec:
    name: str
    lo: float
    hi: float


@dataclass(frozen=True)
class Plan:
    variables: tuple  # tuple[VariableSpec], declaration order
    allowed_predictors: tuple  # tuple[str]
    response: str
    covariates: tuple  # tuple[str]
    intercept: bool
    inapplicable_policy: str
    n_datasets: int
    n_iterations: int
    seed: int
    raw: dict = field(compare=False, default=None, repr=False)

    @property
    def variable_names(self):
        return [v.name for v in self.variables]

    def spec(self, name):
        for v in self.variables:
            if v.name == name:
                return v
        raise KeyError(name)


def load_plan(path):
    """Parse and validate plan.json; raise DiagnosticError on any problem."""
    findings = []
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiagnosticError([f"plan: cannot read/parse {path}: {exc}"])
    if not isinstance(raw, dict):
        raise DiagnosticError(["plan: top level must be a JSON object"])

    # ---- variables -----------------------------------------------------
    variables = []
    raw_vars = raw.get("variables")
    if not isinstance(raw_vars, dict) or not raw_vars:
        findings.append("plan.variables: must be a non-empty object")
    else:
        for name, spec in raw_vars.items():
            if not isinstance(name, str) or not name:
                findings.append("plan.variables: variable names must be non-empty strings")
                continue
            if name == ROW_ID:
                findings.append(f"plan.variables: {ROW_ID!r} is reserved for the row identifier")
                continue
            if not isinstance(spec, dict):
                findings.append(f"plan.variables[{name!r}]: must be an object")
                continue
            vtype = spec.get("type")
            if vtype != SUPPORTED_VARIABLE_TYPE:
                findings.append(
                    f"plan.variables[{name!r}].type: only {SUPPORTED_VARIABLE_TYPE!r} is "
                    f"supported, got {vtype!r}"
                )
                continue
            rng = spec.get("range")
            ok = (
                isinstance(rng, (list, tuple))
                and len(rng) == 2
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in rng)
                and all(math.isfinite(float(x)) for x in rng)
            )
            if not ok:
                findings.append(
                    f"plan.variables[{name!r}].range: must be [lo, hi] of finite numbers"
                )
                continue
            lo, hi = float(rng[0]), float(rng[1])
            if not lo < hi:
                findings.append(f"plan.variables[{name!r}].range: lo must be < hi, got {rng}")
                continue
            variables.append(VariableSpec(name, lo, hi))
    names = [v.name for v in variables]

    # ---- allowed predictors ---------------------------------------------
    allowed = raw.get("allowed_predictors")
    if not isinstance(allowed, list) or not allowed or not all(isinstance(a, str) for a in allowed):
        findings.append("plan.allowed_predictors: must be a non-empty list of variable names")
        allowed = []
    else:
        if len(set(allowed)) != len(allowed):
            findings.append("plan.allowed_predictors: duplicate entries")
        for a in allowed:
            if a not in names:
                findings.append(f"plan.allowed_predictors: {a!r} is not a declared variable")

    # ---- analysis block ---------------------------------------------------
    analysis = raw.get("analysis")
    response, covariates, intercept, policy = None, [], True, None
    if not isinstance(analysis, dict):
        findings.append("plan.analysis: must be an object")
    else:
        response = analysis.get("response")
        if response not in names:
            findings.append(f"plan.analysis.response: {response!r} is not a declared variable")
        covariates = analysis.get("covariates")
        if (
            not isinstance(covariates, list)
            or not covariates
            or not all(isinstance(c, str) for c in covariates)
        ):
            findings.append("plan.analysis.covariates: must be a non-empty list of variable names")
            covariates = []
        else:
            if len(set(covariates)) != len(covariates):
                findings.append("plan.analysis.covariates: duplicate entries")
            for c in covariates:
                if c not in names:
                    findings.append(f"plan.analysis.covariates: {c!r} is not a declared variable")
                if c == response:
                    findings.append("plan.analysis.covariates: must not contain the response")
        intercept = analysis.get("intercept", True)
        if not isinstance(intercept, bool):
            findings.append("plan.analysis.intercept: must be a boolean")
            intercept = True
        policy = analysis.get("structural_inapplicable_policy")
        if policy != SUPPORTED_INAPPLICABLE_POLICY:
            findings.append(
                f"plan.analysis.structural_inapplicable_policy: unsupported value {policy!r}; "
                f"only {SUPPORTED_INAPPLICABLE_POLICY!r} is implemented"
            )

    # analysis variables must be legal predictors, otherwise the imputation
    # models could not condition on them.
    for a in [response] + list(covariates):
        if a is not None and a in names and a not in allowed:
            findings.append(
                f"plan.allowed_predictors: analysis variable {a!r} is not allowed as a predictor"
            )

    # ---- run sizes --------------------------------------------------------
    def _pos_int(key, lo):
        val = raw.get(key)
        if isinstance(val, bool) or not isinstance(val, int) or val < lo:
            findings.append(f"plan.{key}: must be an integer >= {lo}, got {val!r}")
            return None
        return val

    n_datasets = _pos_int("completed_datasets", 2)  # Rubin pooling needs m >= 2
    n_iterations = _pos_int("iterations", 1)
    seed = raw.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or not (0 <= seed < 2**32):
        findings.append(f"plan.seed: must be an integer in [0, 2**32), got {seed!r}")
        seed = None

    if findings:
        raise DiagnosticError(findings)

    return Plan(
        variables=tuple(variables),
        allowed_predictors=tuple(allowed),
        response=response,
        covariates=tuple(covariates),
        intercept=intercept,
        inapplicable_policy=policy,
        n_datasets=n_datasets,
        n_iterations=n_iterations,
        seed=seed,
        raw=raw,
    )
