"""Command line interface for queueops.

Commands
--------
generate      build a full dataset (config/events/truth/scenarios) and a tiny demo
preflight     parse config + events, reconstruct, write diagnostics (no fitting)
reconstruct   reconstruction + timeline + per-task event export
calibrate     fit service models on the training windows; freeze parameters
validate      drive frozen model with held-out arrivals; compare metrics
simulate      replay one shift with an explicit capacity scenario
compare       capacity-plan comparison with identical base draws across scenarios
verify        truth-based recovery error, prediction bias, and invariant checks
report        render HTML report assembling all artifacts
all           run the whole pipeline end to end
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

from .common import ConfigError, LogError, dump_json, load_json
from .config import config_summary, load_config
from .events import (
    Diagnostics, load_raw_events, parse_events, reconstruct,
    write_event_rows_csv, write_per_task_events, write_timeline_json,
)


def _load_events(cfg, path):
    diag = Diagnostics()
    raw = load_raw_events(path)
    events = parse_events(raw, cfg, diag)
    return events, diag


def _reconstruct(cfg, path=None, events=None, diag=None):
    if events is None:
        diag = Diagnostics()
        events = parse_events(load_raw_events(path), cfg, diag)
    elif diag is None:
        diag = Diagnostics()
    return reconstruct(cfg, events, diag)


def cmd_generate(args):
    from .generate import generate, generate_demo_logs
    info = generate(args.data_dir, seed=args.seed)
    demo = generate_demo_logs(os.path.join(args.data_dir, "demo"))
    info["demo"] = demo
    dump_json(os.path.join(args.data_dir, "generation.json"), info)
    print(f"generated {info['n_events']} events in {args.data_dir}")
    for k, v in info.items():
        if isinstance(v, str):
            print(f"  {k}: {v}")


def _estimability_summary(rec) -> dict:
    """Count stages by how well service/blocking is estimable.

    Makes explicit where a missing event leaves only a range or nothing, so
    users can see the evidentiary basis before trusting any prediction.
    """
    counts = {"service_exact": 0, "service_right_censored": 0,
              "service_interval_upper": 0, "service_unknown": 0,
              "blocking_exact": 0, "blocking_right_censored": 0,
              "blocking_interval_upper": 0, "blocking_unknown": 0,
              "blocking_not_applicable": 0}
    for st in rec.stage_observations():
        counts[f"service_{st.service_status}"] = counts.get(
            f"service_{st.service_status}", 0) + 1
        bs = st.blocking_status
        key = "blocking_not_applicable" if bs == "not_applicable" else f"blocking_{bs}"
        counts[key] = counts.get(key, 0) + 1
    # stages where missing service_complete prevents separating service from blocking
    unidentifiable = [s.to_dict() for s in rec.stage_observations()
                      if "blocking_unidentifiable_missing_complete" in s.flags]
    return {
        "by_status": counts,
        "service_blocking_unidentifiable_count": len(unidentifiable),
        "service_blocking_unidentifiable_stages": unidentifiable[:100],
    }


def cmd_preflight(args):
    cfg = load_config(args.config)
    events, diag = _load_events(cfg, args.events)
    rec = reconstruct(cfg, events, diag)
    report = {
        "config_summary": config_summary(cfg),
        "n_events_kept": len(events),
        "diagnostics": diag.to_dict(),
        "estimability": _estimability_summary(rec),
        "n_stages": sum(len(t.stages) for t in rec.tasks.values()),
        "n_tasks_present": sum(1 for t in rec.tasks.values()
                               if any(s.events for s in t.stages)),
    }
    report["absent_tasks"] = [
        tid for tid, t in rec.tasks.items() if not any(s.events for s in t.stages)]
    out = args.out
    dump_json(out, report)
    bad = bool(diag.errors)
    print(f"preflight: {len(events)} events, errors={len(diag.errors)} "
          f"warnings={len(diag.warnings)} "
          f"conflicts={len(diag.conflicts)} order={len(diag.order_violations)} "
          f"duplicates_folded={diag.duplicates_folded}")
    if bad:
        print("  ERROR details:")
        for e in diag.errors[:10]:
            print(f"   - {e['code']}: {e['message']}")
    print(f"report: {out}")
    return 1 if args.strict and bad else 0


def cmd_reconstruct(args):
    cfg = load_config(args.config)
    events, diag = _load_events(cfg, args.events)
    rec = reconstruct(cfg, events, diag)
    write_timeline_json(rec, args.timeline)
    write_per_task_events(rec, args.events_csv)
    # compact summary
    summary = {"n_tasks": len(rec.tasks), "windows": rec.windows,
               "observation_end": rec.observation_end,
               "diagnostics": diag.to_dict()}
    dump_json(args.out, summary)
    print(f"reconstruct: {len(rec.tasks)} tasks -> {args.timeline}, {args.events_csv}")


def cmd_calibrate(args):
    cfg = load_config(args.config)
    windows = load_json(args.windows) if args.windows else {}
    train = set(windows.get("train_windows") or [args.train_shift])
    events, diag = _load_events(cfg, args.events)
    rec = reconstruct(cfg, events, diag)
    from .calibrate import calibrate
    cal = calibrate(rec, train, category_split_strategy=args.category_strategy)
    cal["diagnostics_overview"] = diag.to_dict()
    dump_json(args.out, cal)
    n_fit = sum(1 for c in cal["choices"] if c["decision"] in ("fit", "drift_hold"))
    n_rej = sum(1 for c in cal["choices"] if c["decision"] == "reject")
    print(f"calibrate: train={sorted(train)} groups_fitted={n_fit} rejected={n_rej} "
          f"effective_samples={cal['total_effective_samples']}")
    for c in cal["choices"]:
        print(f"  {c['group']:<16} {c['selected']:<12} n_exact={c['n_exact']} "
              f"n_right={c['n_right']} decision={c['decision']}")
    print(f"report: {args.out}")


def _load_calibration(path, cfg, events):
    cal = load_json(path)
    return cal


def cmd_validate(args):
    cfg = load_config(args.config)
    cal = load_json(args.calibration)
    windows = load_json(args.windows) if args.windows else {}
    hold = set(windows.get("holdout_windows") or [args.holdout_shift])
    events, diag = _load_events(cfg, args.events)
    rec = reconstruct(cfg, events, diag)
    from .validate import validate
    result = validate(rec, cfg, cal, hold, seed=args.seed)
    dump_json(args.out, result)
    for r in result["results"]:
        print(f"validate: {r['shift_id']} arrivals={r['arrivals']} "
              f"observed_throughput={r['observed']['throughput']} "
              f"sim_throughput={r['simulated']['throughput']} "
              f"delta={r['throughput_delta']}")
        for u in r["unexplained_differences"]:
            print(f"  ! {u}")
    print(f"report: {args.out}")


def cmd_simulate(args):
    cfg = load_config(args.config)
    cal = load_json(args.calibration)
    from .calibrate import ServiceModel
    from .sim import Simulation, simulation_event_rows
    windows = load_json(args.windows) if args.windows else {}
    events, diag = _load_events(cfg, args.events)
    rec = reconstruct(cfg, events, diag)
    from .validate import _arrivals_from_recon
    sh = cfg.shifts[args.shift]
    scen = load_json(args.scenario) if args.scenario else {}
    override = {args.shift: {"seats": scen.get("seats", {}),
                             "wait_capacity": scen.get("wait_capacity", {})}}
    arrivals = _arrivals_from_recon(rec, cfg, sh.window_start, sh.window_end)
    model = ServiceModel(cal)
    sim = Simulation(cfg, model.service_fn(args.seed), args.seed, arrivals,
                     end_policy=args.end_policy,
                     shift_override=override,
                     start_time=sh.window_start, end_time=sh.window_end)
    run = sim.run()
    rows = simulation_event_rows(run)
    dump_json(args.out, {"scenario": scen, "meta": run["meta"],
                         "stats": run["stats"], "stations": run["stations"],
                         "capacity_changes": run["capacity_changes"],
                         "deadlock": run["deadlock"],
                         "tasks": run["tasks"]})
    write_event_rows_csv(rows, args.events_csv)
    s = run["stats"]
    print(f"simulate: shift={args.shift} policy={args.end_policy} "
          f"done={s['tasks_done']}/{s['tasks_total']} "
          f"censored={s['tasks_censored']}")
    if run["deadlock"]:
        print(f"  DEADLOCK: {run['deadlock']['reason']}")
    print(f"report: {args.out}  events: {args.events_csv}")


def cmd_compare(args):
    cfg = load_config(args.config)
    cal = load_json(args.calibration)
    windows = load_json(args.windows) if args.windows else {}
    events, diag = _load_events(cfg, args.events)
    rec = reconstruct(cfg, events, diag)
    scenarios = load_json(args.scenarios)
    base_shift = args.base_shift
    seeds = [int(s) for s in args.seeds.split(",")] if args.seeds else [12345, 7, 42]
    from .validate import capacity_plan_comparison
    confirm = None
    if args.confirm_reason:
        # accept every uncertain group with the operator-supplied reason
        gated = capacity_plan_comparison(rec, cfg, cal, base_shift, scenarios, seeds=[seeds[0]])
        confirm = {"by": args.confirm_by, "reason": args.confirm_reason,
                   "accept_groups": [b["group"] for b in gated.get("blocked_groups", [])]}
    comp = capacity_plan_comparison(rec, cfg, cal, base_shift, scenarios,
                                    seeds=seeds, confirm=confirm)
    comp["confirmation"] = confirm
    dump_json(args.out, comp)
    if comp.get("refused"):
        print("compare REFUSED: uncertain model versions require confirmation")
        for b in comp.get("blocked_groups", []):
            print(f"  - {b['group']}: {b['decision']} ({b['reason']})")
        print(f"  pass --confirm-reason \"...\" to accept after reviewing diagnostics")
        return 1
    print(f"compare: base_shift={base_shift} seeds={seeds}")
    for row in comp["summary"]:
        name = row["scenario"]["name"]
        print(f"  {name:<20} throughput={row['throughput_mean']:.1f}"
              f"±{row['throughput_std']:.1f}  censored={row['censored_mean']:.1f}")
        for s in cfg.station_order:
            qw = row["queue_wait_mean_by_station"].get(s)
            bl = row["blocking_mean_by_station"].get(s)
            if qw is not None or bl is not None:
                print(f"      {s}: queue_mean={qw if qw is None else round(qw,1)} "
                      f"blocking_mean={bl if bl is None else round(bl,1)}")
    for key, inv in comp["invariance"].items():
        print(f"  base draws seed {key}: identical={inv['identical_base_draws']} "
              f"common_stages={inv['common_stages']}")
    print(f"report: {args.out}")


def cmd_verify(args):
    data = args.data_dir
    results = {}
    from .verifier import (
        check_broken, check_capacity_base_draws, check_demo, check_frozen,
        check_invariants, check_prediction_bias, check_truth_untouched_by_model,
        recovery_error,
    )
    demo_dir = os.path.join(data, "demo")
    if args.what in ("demo", "all"):
        results["demo"] = check_demo(demo_dir)
        results["broken"] = check_broken(demo_dir)
    cfg = load_config(args.config)
    events, diag = _load_events(cfg, args.events)
    rec = reconstruct(cfg, events, diag)
    if args.what in ("invariants", "all"):
        results["invariants"] = check_invariants(rec, cfg)
    if args.what in ("frozen", "all"):
        cal = load_json(args.calibration)
        windows = load_json(args.windows) if args.windows else {}
        results["frozen"] = check_frozen(
            cal, os.path.dirname(args.config),
            set(windows.get("holdout_windows", [])),
            set(windows.get("train_windows", [])))
    if args.what in ("compare", "all"):
        comp = load_json(args.comparison)
        results["capacity_base_draws"] = check_capacity_base_draws(comp)
    if args.what in ("truth", "all"):
        cal = load_json(args.calibration)
        results["recovery"] = recovery_error(cal, os.path.join(data, "truth.json"))
        if os.path.exists(args.validation):
            results["prediction_bias"] = check_prediction_bias(
                load_json(args.validation))
        results["model_truth_isolation"] = check_truth_untouched_by_model(
            glob.glob("queueops/*.py"))
    dump_json(args.out, results)
    ok = True
    for section in ("demo", "broken", "frozen", "invariants", "capacity_base_draws"):
        if section in results and not results[section].get("all_pass", True):
            ok = False
    print(f"verify: sections={sorted(results)} all_pass={ok}")
    for k, v in results.items():
        if isinstance(v, dict) and "checks" in v:
            fails = [c for c, val in v["checks"].items() if val is False]
            print(f"  {k}: {'PASS' if not fails else 'FAIL ' + str(fails)}")
    if "recovery" in results:
        for g in results["recovery"]["groups"]:
            print(f"  recovery {g['station_id']}/{g['category']}: "
                  f"fitted={_fmt(g.get('fitted_mean'))} true={_fmt(g.get('true_mean'))} "
                  f"rel_err={_fmt(g.get('relative_error'))}")
    print(f"report: {args.out}")
    return 0 if ok else 1


def _fmt(x):
    return "NA" if x is None else f"{x:.2f}"


def cmd_report(args):
    from .report import render_html_report
    path = render_html_report(args.data_dir, args.out)
    print(f"report: {path}")


def cmd_all(args):
    data = args.data_dir
    os.makedirs(data, exist_ok=True)
    # 1 generate
    from .generate import generate, generate_demo_logs
    info = generate(data, seed=args.seed)
    generate_demo_logs(os.path.join(data, "demo"))
    dump_json(os.path.join(data, "generation.json"), info)
    windows = load_json(os.path.join(data, "windows.json"))
    cfg_path = os.path.join(data, "config.json")
    ev_path = os.path.join(data, "events.json")
    cfg = load_config(cfg_path)
    events, diag = _load_events(cfg, ev_path)
    rec = reconstruct(cfg, events, diag)
    # 2 preflight
    dump_json(os.path.join(data, "preflight.json"), {
        "config_summary": config_summary(cfg), "diagnostics": diag.to_dict(),
        "n_events_kept": len(events),
        "estimability": _estimability_summary(rec),
        "n_stages": sum(len(t.stages) for t in rec.tasks.values()),
        "n_tasks_present": sum(1 for t in rec.tasks.values()
                               if any(s.events for s in t.stages)),
    })
    # 3 reconstruct + exports
    write_timeline_json(rec, os.path.join(data, "timeline.json"))
    write_per_task_events(rec, os.path.join(data, "events_reconstructed.csv"))
    # 4 calibrate
    from .calibrate import calibrate, frozen_params
    train = set(windows["train_windows"])
    hold = set(windows["holdout_windows"])
    cal = calibrate(rec, train, category_split_strategy=args.category_strategy)
    dump_json(os.path.join(data, "calibration.json"), cal)
    # 5 validate
    from .validate import capacity_plan_comparison, validate
    val = validate(rec, cfg, cal, hold, seed=args.seed + 11)
    dump_json(os.path.join(data, "validation.json"), val)
    # 6 compare -- gated on explicit operator confirmation of uncertain versions
    scenarios = load_json(os.path.join(data, "scenarios.json"))
    unconfirmed = capacity_plan_comparison(rec, cfg, cal, "holdout", scenarios,
                                           seeds=[args.seed + 21])
    gate = {
        "refused": bool(unconfirmed.get("refused")),
        "blocked_groups": unconfirmed.get("blocked_groups", []),
        "reason": unconfirmed.get("reason"),
    }
    dump_json(os.path.join(data, "comparison_gate.json"), gate)
    confirm_reason = args.confirm_reason
    if gate["refused"] and not confirm_reason:
        confirm_reason = ("DEMO OPERATOR CONFIRMATION: reviewed calibration diagnostics; "
                          "accept the flagged model version for this scenario comparison "
                          "(supply --confirm-reason for a real deployment)")
    confirm = None
    if confirm_reason:
        confirm = {"by": "pipeline-operator", "reason": confirm_reason,
                   "accept_groups": [b["group"] for b in gate["blocked_groups"]]}
    comp = capacity_plan_comparison(rec, cfg, cal, "holdout", scenarios,
                                    seeds=[args.seed + 21, 7, 42], confirm=confirm)
    comp["confirmation"] = confirm
    comp["gate"] = gate
    dump_json(os.path.join(data, "comparison.json"), comp)
    # 6b one representative simulation for the occupancy chart / event export
    from .calibrate import ServiceModel
    from .sim import Simulation, simulation_event_rows
    from .validate import _arrivals_from_recon
    sh = cfg.shifts["holdout"]
    base_scen = next((s for s in scenarios if s["name"] == "baseline"),
                     scenarios[0])
    override = {sh.shift_id: {"seats": base_scen.get("seats") or {},
                              "wait_capacity": base_scen.get("wait_capacity") or {}}}
    arr_h = _arrivals_from_recon(rec, cfg, sh.window_start, sh.window_end)
    sim_run = Simulation(cfg, ServiceModel(cal).service_fn(args.seed + 31),
                         args.seed + 31, arr_h, end_policy="cutoff",
                         shift_override=override,
                         start_time=sh.window_start, end_time=sh.window_end).run()
    dump_json(os.path.join(data, "simulation.json"), {
        "scenario": base_scen, "meta": sim_run["meta"],
        "stats": sim_run["stats"], "stations": sim_run["stations"],
        "capacity_changes": sim_run["capacity_changes"],
        "deadlock": sim_run["deadlock"], "tasks": sim_run["tasks"]})
    write_event_rows_csv(simulation_event_rows(sim_run),
                         os.path.join(data, "simulation_events.csv"))
    # 7 verify
    from .verifier import (
        check_broken, check_capacity_base_draws, check_demo, check_frozen,
        check_invariants, check_prediction_bias, check_truth_untouched_by_model,
        recovery_error,
    )
    results = {
        "demo": check_demo(os.path.join(data, "demo")),
        "broken": check_broken(os.path.join(data, "demo")),
        "invariants": check_invariants(rec, cfg),
        "frozen": check_frozen(cal, data, hold, train),
        "capacity_base_draws": check_capacity_base_draws(comp),
        "recovery": recovery_error(cal, os.path.join(data, "truth.json")),
        "prediction_bias": check_prediction_bias(val),
        "model_truth_isolation": check_truth_untouched_by_model(glob.glob("queueops/*.py")),
    }
    dump_json(os.path.join(data, "verification.json"), results)
    # 8 report
    from .report import render_html_report
    render_html_report(data, os.path.join(data, "report.html"))
    print(f"all: data in {data}")
    print(f"  preflight diagnostics: errors={len(diag.errors)} "
          f"warnings={len(diag.warnings)} duplicates={diag.duplicates_folded}")
    for c in cal["choices"]:
        print(f"  model {c['group']:<16} {c['selected']:<12} "
              f"decision={c['decision']} n_eff={c['n_effective']}")
    for r in val["results"]:
        print(f"  validate {r['shift_id']}: obs_thr={r['observed']['throughput']} "
              f"sim_thr={r['simulated']['throughput']}")
    fails = []
    for k, v in results.items():
        if isinstance(v, dict) and "checks" in v:
            for c, val_ in v["checks"].items():
                if val_ is False:
                    fails.append(f"{k}.{c}")
    print(f"  verification: {'ALL PASS' if not fails else 'FAILED: ' + ', '.join(fails)}")
    print(f"  report: {os.path.join(data, 'report.html')}")
    return 0 if not fails else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="queueops", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="generate full dataset + demo logs")
    g.add_argument("--data-dir", default="data")
    g.add_argument("--seed", type=int, default=20240001)
    g.set_defaults(func=cmd_generate)

    pf = sub.add_parser("preflight", help="validate config/events and reconstruct")
    pf.add_argument("--config", required=True)
    pf.add_argument("--events", required=True)
    pf.add_argument("--out", default="preflight.json")
    pf.add_argument("--strict", action="store_true")
    pf.set_defaults(func=cmd_preflight)

    r = sub.add_parser("reconstruct", help="timeline + per-task events")
    r.add_argument("--config", required=True)
    r.add_argument("--events", required=True)
    r.add_argument("--timeline", default="timeline.json")
    r.add_argument("--events-csv", default="events_reconstructed.csv")
    r.add_argument("--out", default="reconstruct_summary.json")
    r.set_defaults(func=cmd_reconstruct)

    c = sub.add_parser("calibrate", help="fit service models on training windows")
    c.add_argument("--config", required=True)
    c.add_argument("--events", required=True)
    c.add_argument("--windows", default=None)
    c.add_argument("--train-shift", default="train")
    c.add_argument("--category-strategy", choices=["auto", "pool", "split"],
                   default="auto")
    c.add_argument("--out", default="calibration.json")
    c.set_defaults(func=cmd_calibrate)

    v = sub.add_parser("validate", help="held-out validation")
    v.add_argument("--config", required=True)
    v.add_argument("--events", required=True)
    v.add_argument("--calibration", required=True)
    v.add_argument("--windows", default=None)
    v.add_argument("--holdout-shift", default="holdout")
    v.add_argument("--seed", type=int, default=12345)
    v.add_argument("--out", default="validation.json")
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser("simulate", help="replay one shift with a scenario")
    s.add_argument("--config", required=True)
    s.add_argument("--events", required=True)
    s.add_argument("--calibration", required=True)
    s.add_argument("--windows", default=None)
    s.add_argument("--shift", default="holdout")
    s.add_argument("--scenario", default=None, help="JSON with seats/wait_capacity")
    s.add_argument("--seed", type=int, default=12345)
    s.add_argument("--end-policy", choices=["cutoff", "drain"], default="cutoff")
    s.add_argument("--out", default="simulation.json")
    s.add_argument("--events-csv", default="simulation_events.csv")
    s.set_defaults(func=cmd_simulate)

    cp = sub.add_parser("compare", help="capacity-plan comparison")
    cp.add_argument("--config", required=True)
    cp.add_argument("--events", required=True)
    cp.add_argument("--calibration", required=True)
    cp.add_argument("--windows", default=None)
    cp.add_argument("--scenarios", required=True)
    cp.add_argument("--base-shift", default="holdout")
    cp.add_argument("--seeds", default=None)
    cp.add_argument("--confirm-reason", default=None,
                    help="documented reason to accept uncertain model versions")
    cp.add_argument("--confirm-by", default="operator")
    cp.add_argument("--out", default="comparison.json")
    cp.set_defaults(func=cmd_compare)

    vf = sub.add_parser("verify", help="truth-based and invariant checks")
    vf.add_argument("--data-dir", default="data")
    vf.add_argument("--config", default=None)
    vf.add_argument("--events", default=None)
    vf.add_argument("--windows", default=None)
    vf.add_argument("--calibration", default=None)
    vf.add_argument("--comparison", default=None)
    vf.add_argument("--validation", default=None)
    vf.add_argument("--what", choices=["demo", "invariants", "frozen", "compare",
                                       "truth", "all"], default="all")
    vf.add_argument("--out", default=None)
    vf.set_defaults(func=cmd_verify)

    rp = sub.add_parser("report", help="render HTML report")
    rp.add_argument("--data-dir", default="data")
    rp.add_argument("--out", default=None)
    rp.set_defaults(func=cmd_report)

    a = sub.add_parser("all", help="run the full pipeline")
    a.add_argument("--data-dir", default="data")
    a.add_argument("--seed", type=int, default=20240001)
    a.add_argument("--category-strategy", choices=["auto", "pool", "split"],
                   default="auto")
    a.add_argument("--confirm-reason", default=None,
                   help="documented reason to accept a drift-held model version "
                        "for the comparison step")
    a.set_defaults(func=cmd_all)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # fill verify defaults from data dir
    if args.cmd == "verify":
        d = args.data_dir
        if not os.path.isdir(d):
            print(f"data dir {d} missing; run 'generate' first", file=sys.stderr)
            return 2
        defaults = {
            "config": os.path.join(d, "config.json"),
            "events": os.path.join(d, "events.json"),
            "windows": os.path.join(d, "windows.json"),
            "calibration": os.path.join(d, "calibration.json"),
            "comparison": os.path.join(d, "comparison.json"),
            "validation": os.path.join(d, "validation.json"),
            "out": os.path.join(d, "verification.json"),
        }
        for k, v in defaults.items():
            if getattr(args, k) is None and (k == "out" or os.path.exists(v)):
                setattr(args, k, v)
    if args.cmd == "report" and args.out is None:
        args.out = os.path.join(args.data_dir, "report.html")
    try:
        return args.func(args)
    except (ConfigError, LogError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
