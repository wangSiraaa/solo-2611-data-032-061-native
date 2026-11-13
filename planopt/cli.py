"""planopt 本地命令行入口。

子命令：
  generate  输出合成配置（预设或固定种子随机）
  optimize  全局穷举求最优左深计划；可附带 --user-order 评估用户顺序
  explain   输出人类可读的逐步解释
  verify    独立穷举/DP 交叉验证、洗牌不变性、结果文件复核
"""

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, archive, presets
from .errors import PlanoptError
from .model import parse_problem, validate_order
from .optimizer import exhaustive_optimize
from .render import build_result, canonical_json, explain_text
from .verifier import shuffle_invariance, verify_result

DEFAULT_DB = "var/planopt_archive.sqlite3"


def _read_input(path: str | None) -> dict:
    if path is None or path == "-":
        try:
            payload = json.loads(sys.stdin.read())
        except json.JSONDecodeError as exc:
            raise PlanoptError(f"标准输入不是合法 JSON: {exc}") from exc
    else:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError as exc:
            raise PlanoptError(f"输入文件不存在: {path}") from exc
        except json.JSONDecodeError as exc:
            raise PlanoptError(f"输入文件不是合法 JSON: {exc}") from exc
    # 兼容 generate 子命令的包裹格式 {"generated": ..., "input": {...}}
    if isinstance(payload, dict) and "tables" not in payload and isinstance(payload.get("input"), dict):
        return payload["input"]
    return payload


def _write_text(text: str, path: str | None) -> None:
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)


def _open_db(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return archive.connect(db_path)


def cmd_generate(args: argparse.Namespace) -> int:
    if args.random is not None:
        payload = presets.random_problem(
            args.random, seed=args.seed, edge_probability=args.edge_probability
        )
        label = f"random_n{args.random}_seed{args.seed}"
    else:
        payload = presets.build_preset(args.preset)
        label = args.preset
    text = canonical_json({"generated": label, "input": payload})
    _write_text(text, args.output)
    if args.output:
        print(f"已生成合成配置 {label} -> {args.output}", file=sys.stderr)
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    payload = _read_input(args.input)
    problem = parse_problem(payload)
    user_order = validate_order(problem, args.user_order) if args.user_order else None
    plan = exhaustive_optimize(problem)
    result = build_result(problem, plan, user_order=user_order, raw_input=payload)
    _write_text(canonical_json(result), args.output)

    run_id = None
    if args.db:
        with _open_db(args.db) as conn:
            run_id = archive.save_run(
                conn, input_payload=payload, result=result,
                preset=args.preset_tag,
            )
    print(
        f"[optimize] 穷举 {plan.evaluated_permutations} 个排列，"
        f"最优顺序 {' -> '.join(plan.order)}，总成本 {plan.cost}，"
        f"耗时 {plan.elapsed_seconds:.6f}s"
        + (f"，已归档 run_id={run_id}" if run_id is not None else ""),
        file=sys.stderr,
    )
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    payload = _read_input(args.input)
    problem = parse_problem(payload)
    if args.result:
        with open(args.result, "r", encoding="utf-8") as fh:
            result = json.load(fh)
    else:
        plan = exhaustive_optimize(problem)
        result = build_result(problem, plan)
    _write_text(explain_text(problem, result), args.output)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    if args.run_id is not None:
        with _open_db(args.db) as conn:
            try:
                stored = archive.load_run(conn, args.run_id)
            except KeyError:
                raise PlanoptError(f"归档中不存在 run_id={args.run_id}")
        payload, result = stored["input"], stored["result"]
        problem = parse_problem(payload)
    else:
        payload = _read_input(args.input)
        problem = parse_problem(payload)
        if args.result:
            with open(args.result, "r", encoding="utf-8") as fh:
                result = json.load(fh)
        else:
            plan = exhaustive_optimize(problem)
            result = build_result(problem, plan)

    checks = verify_result(problem, result)
    if args.shuffle:
        checks.append(shuffle_invariance(problem, result, trials=args.shuffle_trials))

    width = max(len(c.name) for c in checks)
    all_ok = True
    for check in checks:
        all_ok &= check.ok
        print(f"[{'PASS' if check.ok else 'FAIL'}] {check.name.ljust(width)}  {check.detail}")
    print("-" * 72)
    print(f"验证结论: {'全部通过' if all_ok else '存在失败项'}")
    return 0 if all_ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="planopt",
        description="教学用关系查询左深连接顺序全局优化器（精确分数、全排列穷举）",
    )
    parser.add_argument("--version", action="version", version=f"planopt {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="生成合成输入配置")
    p_gen.add_argument("--preset", choices=presets.preset_names(),
                       help="预设场景名（与 --random 二选一）")
    p_gen.add_argument("--random", type=int, metavar="N", help="随机生成 N 张表 (1..8)")
    p_gen.add_argument("--seed", type=int, default=20261113)
    p_gen.add_argument("--edge-probability", type=float, default=0.4)
    p_gen.add_argument("--output", "-o")
    p_gen.set_defaults(func=cmd_generate)

    p_opt = sub.add_parser("optimize", help="全局穷举求最优左深计划")
    p_opt.add_argument("--input", "-i", help="输入 JSON 文件，缺省读标准输入")
    p_opt.add_argument("--output", "-o")
    p_opt.add_argument("--user-order", nargs="+", metavar="TABLE_ID",
                       help="用户给定的合法表排列，报告其逐步代价与最优差值")
    p_opt.add_argument("--db", nargs="?", const=DEFAULT_DB,
                       help=f"归档到 SQLite（可给路径，缺省 {DEFAULT_DB}）")
    p_opt.add_argument("--preset-tag", help="归档时记录的场景标签")
    p_opt.set_defaults(func=cmd_optimize)

    p_exp = sub.add_parser("explain", help="人类可读的逐步解释")
    p_exp.add_argument("--input", "-i", required=True)
    p_exp.add_argument("--result", help="已保存的 optimize 结果 JSON；缺省现场重算")
    p_exp.add_argument("--output", "-o")
    p_exp.set_defaults(func=cmd_explain)

    p_ver = sub.add_parser("verify", help="独立验证结果与洗牌不变性")
    p_ver.add_argument("--input", "-i")
    p_ver.add_argument("--result")
    p_ver.add_argument("--run-id", type=int, help="从 SQLite 归档读取指定运行")
    p_ver.add_argument("--db", default=DEFAULT_DB)
    p_ver.add_argument("--shuffle", action="store_true", help="额外执行洗牌不变性检查")
    p_ver.add_argument("--shuffle-trials", type=int, default=5)
    p_ver.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate" and args.random is None and not args.preset:
            parser.error("generate 需要 --preset 或 --random N")
        return args.func(args)
    except PlanoptError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        # 管道关闭（如 head）时安静退出
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
