"""配置加载与校验。

配置项（均为可选覆盖，未给出时使用默认值）：

- candidate_radius_m : 候选半径（米），投影距离超过该值的边不产生候选；
- max_candidates     : 每个采样点保留的候选数上限；
- speed_slack_factor : 速度上限松弛系数，允许平均速度为 max_speed * slack；
- obs_sigma_m        : 观测噪声尺度（米），缺失 accuracy_m 的点使用该值；
- obs_weight         : 观测代价权重 w_obs（无量纲乘子）；
- transition_beta_m  : 转移绕路的指数尺度（米）；
- transition_weight  : 转移代价权重 w_trans（无量纲乘子）；
- ambiguity_cost_gap : 分数差距不超过该值的候选视为近乎等价（歧义保留依据）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .errors import InputValidationError

_DEFAULTS = {
    "candidate_radius_m": 25.0,
    "max_candidates": 4,
    "speed_slack_factor": 1.3,
    "obs_sigma_m": 10.0,
    "obs_weight": 1.0,
    "transition_beta_m": 10.0,
    "transition_weight": 1.0,
    "ambiguity_cost_gap": 0.5,
}


@dataclass(frozen=True)
class Config:
    candidate_radius_m: float
    max_candidates: int
    speed_slack_factor: float
    obs_sigma_m: float
    obs_weight: float
    transition_beta_m: float
    transition_weight: float
    ambiguity_cost_gap: float

    @classmethod
    def from_file(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise InputValidationError("config.json 顶层必须是 JSON 对象")
        unknown = set(raw) - set(_DEFAULTS)
        if unknown:
            raise InputValidationError(f"config.json 含未知配置项: {sorted(unknown)}")
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        values = dict(_DEFAULTS)
        values.update(raw)
        for key in (
            "candidate_radius_m",
            "speed_slack_factor",
            "obs_sigma_m",
            "obs_weight",
            "transition_beta_m",
            "transition_weight",
            "ambiguity_cost_gap",
        ):
            values[key] = float(values[key])
            if not values[key] > 0:
                raise InputValidationError(f"配置项 {key} 必须为正数")
        values["max_candidates"] = int(values["max_candidates"])
        if values["max_candidates"] < 1:
            raise InputValidationError("max_candidates 必须 >= 1")
        return cls(**values)

    def to_dict(self) -> dict:
        return {
            "candidate_radius_m": self.candidate_radius_m,
            "max_candidates": self.max_candidates,
            "speed_slack_factor": self.speed_slack_factor,
            "obs_sigma_m": self.obs_sigma_m,
            "obs_weight": self.obs_weight,
            "transition_beta_m": self.transition_beta_m,
            "transition_weight": self.transition_weight,
            "ambiguity_cost_gap": self.ambiguity_cost_gap,
        }
