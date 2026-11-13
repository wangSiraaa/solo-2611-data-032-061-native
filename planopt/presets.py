"""合成测试配置。

所有数据均为手工构造的合成数据，不接触任何外部数据库或真实负载。
预设覆盖：链、星形、环形、断开图（无条件）、空表、大表之间的强选择率
条件、零选择率、大量同成本方案，以及“最小表优先明显更差”的可手算小样例。
"""

import random

from .errors import InputValidationError

PRESETS: dict[str, dict] = {
    # 1. 链状条件 A-B-C-D
    "chain_4": {
        "tables": [
            {"table_id": "A", "rows": 1000},
            {"table_id": "B", "rows": 100},
            {"table_id": "C", "rows": 100},
            {"table_id": "D", "rows": 1000},
        ],
        "conditions": [
            {"left": "A", "right": "B", "selectivity": "0.1"},
            {"left": "B", "right": "C", "selectivity": "0.2"},
            {"left": "C", "right": "D", "selectivity": "0.05"},
        ],
    },
    # 2. 星形：中心大表 O，四片叶子
    "star_5": {
        "tables": [
            {"table_id": "O", "rows": 5000},
            {"table_id": "L1", "rows": 200},
            {"table_id": "L2", "rows": 200},
            {"table_id": "L3", "rows": 200},
            {"table_id": "L4", "rows": 200},
        ],
        "conditions": [
            {"left": "O", "right": "L1", "selectivity": "0.02"},
            {"left": "O", "right": "L2", "selectivity": "0.05"},
            {"left": "O", "right": "L3", "selectivity": "0.1"},
            {"left": "O", "right": "L4", "selectivity": "0.1"},
        ],
    },
    # 3. 环形 A-B-C-D-A
    "ring_4": {
        "tables": [
            {"table_id": "A", "rows": 200},
            {"table_id": "B", "rows": 400},
            {"table_id": "C", "rows": 800},
            {"table_id": "D", "rows": 1600},
        ],
        "conditions": [
            {"left": "A", "right": "B", "selectivity": "0.1"},
            {"left": "B", "right": "C", "selectivity": "0.2"},
            {"left": "C", "right": "D", "selectivity": "0.1"},
            {"left": "D", "right": "A", "selectivity": "0.05"},
        ],
    },
    # 4. 断开图：没有任何条件，全部为笛卡尔积
    "disjoint_4": {
        "tables": [
            {"table_id": "P", "rows": 10},
            {"table_id": "Q", "rows": 30},
            {"table_id": "R", "rows": 7},
            {"table_id": "S", "rows": 50},
        ],
        "conditions": [],
    },
    # 5. 含零行空表；只要 E 在首位，之后所有基数都严格为 0
    "empty_3": {
        "tables": [
            {"table_id": "E", "rows": 0},
            {"table_id": "F", "rows": 50},
            {"table_id": "G", "rows": 80},
        ],
        "conditions": [
            {"left": "F", "right": "G", "selectivity": "0.25"},
        ],
    },
    # 6. 两张万行大表之间存在强过滤条件（选择率 0.001）
    #    约定：本项目“高选择性/强选择率”指选择率数值小、过滤性强
    "high_sel_large_4": {
        "tables": [
            {"table_id": "BIG1", "rows": 10000},
            {"table_id": "BIG2", "rows": 10000},
            {"table_id": "SMALL", "rows": 10},
            {"table_id": "MID", "rows": 100},
        ],
        "conditions": [
            {"left": "BIG1", "right": "BIG2", "selectivity": "0.001"},
            {"left": "SMALL", "right": "BIG1", "selectivity": "0.5"},
            {"left": "MID", "right": "BIG2", "selectivity": "0.5"},
        ],
    },
    # 7. 零选择率：X-Y 一旦连接，基数严格归零
    "zero_sel_3": {
        "tables": [
            {"table_id": "X", "rows": 100},
            {"table_id": "Y", "rows": 200},
            {"table_id": "Z", "rows": 50},
        ],
        "conditions": [
            {"left": "X", "right": "Y", "selectivity": "0"},
            {"left": "Y", "right": "Z", "selectivity": "0.5"},
        ],
    },
    # 8. 同成本方案：三表等行数且无条件，3! = 6 个排列成本完全相同
    "ties_no_cond_3": {
        "tables": [
            {"table_id": "P", "rows": 10},
            {"table_id": "Q", "rows": 10},
            {"table_id": "R", "rows": 10},
        ],
        "conditions": [],
    },
    # 9. 贪心反例一（可手算）：首表只看最小表会选错
    #    S=20(最小), M=100, L=10000；S-L 选择率 0.5（弱过滤），
    #    M-L 选择率 0.001（强过滤）。从最小表 S 起步的两个计划成本
    #    24140 / 220140 都远差于全局最优 M,L,S = 22220：大表 L 经 M 的
    #    强过滤首步即收缩到 10，小表反而应最后接入。
    "greedy_trap_3": {
        "tables": [
            {"table_id": "S", "rows": 20},
            {"table_id": "M", "rows": 100},
            {"table_id": "L", "rows": 10000},
        ],
        "conditions": [
            {"left": "S", "right": "L", "selectivity": "0.5"},
            {"left": "M", "right": "L", "selectivity": "0.001"},
        ],
    },
    # 9b. 贪心反例二（可手算）：最小表 S 开头后，每步只挑“最小表”会先接
    #     万行 L（弱过滤边），把中间结果放大；全局最优先经 M-B 强过滤收缩。
    #     最优 S,M,B,L = 100033120；贪心式 S,L,B,M = 300211120（约 3 倍）。
    "greedy_trap_first_4": {
        "tables": [
            {"table_id": "S", "rows": 10},
            {"table_id": "M", "rows": 100},
            {"table_id": "L", "rows": 10000},
            {"table_id": "B", "rows": 1000},
        ],
        "conditions": [
            {"left": "S", "right": "L", "selectivity": "1"},
            {"left": "M", "right": "B", "selectivity": "0.01"},
            {"left": "B", "right": "L", "selectivity": "1"},
        ],
    },
    # 9c. 三角形（可手算）：三对表都有边，任意顺序最终基数完全相同
    #     （1000*100*100*0.1*0.1*0.5 = 50000），但中间代价不同；
    #     B,C,A 与 C,B,A 并列最优 61300，码点序裁决 B,C,A 胜出。
    "triangle_tie_3": {
        "tables": [
            {"table_id": "A", "rows": 1000},
            {"table_id": "B", "rows": 100},
            {"table_id": "C", "rows": 100},
        ],
        "conditions": [
            {"left": "A", "right": "B", "selectivity": "0.1"},
            {"left": "A", "right": "C", "selectivity": "0.1"},
            {"left": "B", "right": "C", "selectivity": "0.5"},
        ],
    },
    # 10. 六表等行数无条件：6! = 720 个排列全部同成本，压测并列裁决
    "ties_6": {
        "tables": [{"table_id": f"T{i}", "rows": 3} for i in range(1, 7)],
        "conditions": [],
    },
    # 11. 八表上限计时场景：链 + 两条跨链边
    "eight_8": {
        "tables": [
            {"table_id": "T1", "rows": 1000},
            {"table_id": "T2", "rows": 50},
            {"table_id": "T3", "rows": 200},
            {"table_id": "T4", "rows": 80},
            {"table_id": "T5", "rows": 500},
            {"table_id": "T6", "rows": 20},
            {"table_id": "T7", "rows": 300},
            {"table_id": "T8", "rows": 60},
        ],
        "conditions": [
            {"left": "T1", "right": "T2", "selectivity": "0.1"},
            {"left": "T2", "right": "T3", "selectivity": "0.2"},
            {"left": "T3", "right": "T4", "selectivity": "0.05"},
            {"left": "T4", "right": "T5", "selectivity": "0.25"},
            {"left": "T5", "right": "T6", "selectivity": "0.1"},
            {"left": "T6", "right": "T7", "selectivity": "0.4"},
            {"left": "T7", "right": "T8", "selectivity": "0.2"},
            {"left": "T1", "right": "T4", "selectivity": "0.01"},
            {"left": "T5", "right": "T8", "selectivity": "0.02"},
        ],
    },
}


def preset_names() -> list[str]:
    return sorted(PRESETS)


def build_preset(name: str) -> dict:
    if name not in PRESETS:
        raise InputValidationError(
            f"未知预设 {name!r}，可选: {', '.join(preset_names())}"
        )
    # 返回深拷贝，避免调用方改写
    import json
    return json.loads(json.dumps(PRESETS[name]))


def random_problem(n: int, *, seed: int, edge_probability: float = 0.4) -> dict:
    """生成可复现的随机合成配置（固定种子）。"""
    if not 1 <= n <= 8:
        raise InputValidationError("随机配置表数必须在 1 到 8 之间")
    rng = random.Random(seed)
    ids = [f"R{i}" for i in range(1, n + 1)]
    tables = [{"table_id": t, "rows": rng.randint(1, 10000)} for t in ids]
    conditions = []
    decimals = ["0.001", "0.01", "0.05", "0.1", "0.2", "0.25", "0.5", "0.8", "1"]
    for i in range(n):
        for j in range(i + 1, n):
            if rng.random() < edge_probability:
                conditions.append(
                    {"left": ids[i], "right": ids[j],
                     "selectivity": rng.choice(decimals)}
                )
    return {"tables": tables, "conditions": conditions}
