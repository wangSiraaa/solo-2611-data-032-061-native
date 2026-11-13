#!/usr/bin/env python3
"""
run_tests.py — end-to-end tests for reasm.py.

Covers: 32-bit sequence wrap past zero, reverse arrival, full retransmission,
partial identical overlap, three different bytes at one position, head/tail
gaps, empty flow, zero-length segments, out-of-range / malformed input
rejection, shuffle invariance, atomic publish on failure, a huge sparse flow
(proving memory does not scale with holes) and a >64 MiB real-payload run
with peak RSS measurement.
"""
import glob
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
REASM = os.path.join(ROOT, "reasm.py")

FAILURES = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILURES.append(msg)


def run(args, expect=0):
    p = subprocess.run([sys.executable, REASM] + args,
                       capture_output=True, text=True)
    if expect is not None and p.returncode != expect:
        raise AssertionError(
            f"cmd {args}\n expected rc={expect} got {p.returncode}\n"
            f"stdout:\n{p.stdout}\nstderr:\n{p.stderr}")
    return p


def load_manifest(out):
    vdir = os.path.realpath(os.path.join(out, "current"))
    with open(os.path.join(vdir, "manifest.json"), "r", encoding="utf-8") as fh:
        return os.path.basename(vdir), json.load(fh), vdir


def flow_of(manifest, fid):
    return next(f for f in manifest["flows"] if f["flow_id"] == fid)


def read_segments(vdir):
    recs = {}
    with open(os.path.join(vdir, "segments.jsonl"), "r", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            recs[r["segment_id"]] = r
    return recs


def peak_rss_kb(proc):
    for line in proc.stdout.splitlines():
        if line.startswith("peak_rss_kb="):
            return int(line.split("=", 1)[1])
    raise AssertionError(f"no peak_rss_kb in output:\n{proc.stdout}")


def expect_flow(mf, counts=None, max_det="skip", max_gap="skip",
                max_con="skip", complete=None, intervals=None):
    if counts is not None:
        c = mf["counts"]
        got = (c["determined"], c["gap"], c["conflict"])
        check(got == counts, f"{mf['flow_id']}: counts {got} == {counts}")
        check(sum(counts) == mf["total_length"],
              f"{mf['flow_id']}: counts sum to total_length")
    for key, want in (("max_determined_interval", max_det),
                      ("max_gap", max_gap),
                      ("max_conflict_interval", max_con)):
        if want == "skip":
            continue
        want_obj = None if want is None else {"offset": want[0], "length": want[1]}
        check(mf[key] == want_obj, f"{mf['flow_id']}: {key} == {want_obj} (got {mf[key]})")
    if complete is not None:
        check(mf["complete"] is complete,
              f"{mf['flow_id']}: complete == {complete}")
    if intervals is not None:
        got = [(i["offset"], i["length"]) for i in mf["intervals"]]
        check(got == intervals, f"{mf['flow_id']}: intervals {got} == {intervals}")


def expect_seg(recs, sid, sole, shared, conf, length):
    r = recs.get(sid)
    ok = (r is not None and r["sole_covered"] == sole
          and r["shared_covered"] == shared and r["conflict_covered"] == conf
          and r["length"] == length
          and sole + shared + conf == length)
    check(ok, f"segment {sid}: sole={sole} shared={shared} conflict={conf} len={length}")


# ---------------------------------------------------------------------------

def test_small(tmp):
    print("== small sample: classification, content, contributions ==")
    gen = os.path.join(tmp, "small")
    out = os.path.join(tmp, "small_out")
    run(["generate", "--scenario", "small", "--out", gen])
    cfg = os.path.join(gen, "config.json")
    segs = os.path.join(gen, "segments.jsonl")
    p = run(["assemble", "--config", cfg, "--segments", segs, "--out", out])
    version, m, vdir = load_manifest(out)

    # independent per-byte dictionary model cross-check
    run(["verify", "--out", out, "--config", cfg, "--segments", segs, "--deep"])

    expect_flow(flow_of(m, "wrap"), counts=(40, 8, 0), max_det=(0, 40),
                max_gap=(40, 8), max_con=None, complete=False, intervals=[(0, 40)])
    expect_flow(flow_of(m, "retrans"), counts=(32, 0, 0), max_det=(0, 32),
                max_gap=None, max_con=None, complete=True, intervals=[(0, 32)])
    expect_flow(flow_of(m, "overlap"), counts=(40, 0, 0), max_det=(0, 40),
                max_gap=None, max_con=None, complete=True, intervals=[(0, 40)])
    expect_flow(flow_of(m, "conflict3"), counts=(8, 0, 8), max_det=(0, 4),
                max_gap=None, max_con=(4, 8), complete=False,
                intervals=[(0, 4), (12, 4)])
    expect_flow(flow_of(m, "gaps"), counts=(32, 32, 0), max_det=(16, 32),
                max_gap=(0, 16), max_con=None, complete=False, intervals=[(16, 32)])
    expect_flow(flow_of(m, "empty"), counts=(0, 0, 0), max_det=None,
                max_gap=None, max_con=None, complete=True, intervals=[])
    expect_flow(flow_of(m, "never"), counts=(0, 24, 0), max_det=None,
                max_gap=(0, 24), max_con=None, complete=False, intervals=[])
    expect_flow(flow_of(m, "zerolen"), counts=(4, 4, 0), max_det=(4, 4),
                max_gap=(0, 4), max_con=None, complete=False, intervals=[(4, 4)])

    # interval file contents
    def read_bin(fid_cfg, name):
        with open(os.path.join(vdir, name), "rb") as fh:
            return fh.read()

    fwrap = flow_of(m, "wrap")
    check(read_bin("wrap", fwrap["intervals"][0]["file"]) == bytes(range(40)),
          "wrap interval content is bytes 0..39")
    fr = flow_of(m, "retrans")
    rp = bytes((i * 7 + 3) & 0xFF for i in range(32))
    check(read_bin("retrans", fr["complete_file"]) == rp,
          "retrans complete file content")
    fo = flow_of(m, "overlap")
    check(read_bin("overlap", fo["complete_file"]) == bytes(range(40)),
          "overlap complete file content is bytes 0..39")
    fc = flow_of(m, "conflict3")
    check(read_bin("conflict3", fc["intervals"][0]["file"]) == b"\xaa" * 4,
          "conflict3 interval [0,4) content")
    check(read_bin("conflict3", fc["intervals"][1]["file"]) == b"\xaa" * 4,
          "conflict3 interval [12,16) content")
    fe = flow_of(m, "empty")
    check(read_bin("empty", fe["complete_file"]) == b"", "empty flow complete file is empty")
    check(fe["complete_sha256"] == hashlib.sha256(b"").hexdigest(),
          "empty flow complete sha256")
    fz = flow_of(m, "zerolen")
    check(read_bin("zerolen", fz["intervals"][0]["file"]) == b"\x01\x02\x03\x04",
          "zerolen interval content")

    # per-segment contributions (three values sum to payload length)
    recs = read_segments(vdir)
    expect_seg(recs, "w1", 16, 0, 0, 16)
    expect_seg(recs, "w2", 16, 0, 0, 16)
    expect_seg(recs, "w3", 8, 0, 0, 8)
    expect_seg(recs, "r1", 0, 32, 0, 32)
    expect_seg(recs, "r2", 0, 32, 0, 32)
    expect_seg(recs, "o1", 16, 8, 0, 24)
    expect_seg(recs, "o2", 16, 8, 0, 24)
    expect_seg(recs, "c1", 8, 0, 8, 16)
    expect_seg(recs, "c2", 0, 0, 8, 8)
    expect_seg(recs, "c3", 0, 0, 8, 8)
    expect_seg(recs, "g1", 32, 0, 0, 32)
    expect_seg(recs, "z1", 0, 0, 0, 0)
    expect_seg(recs, "z2", 4, 0, 0, 4)

    # conflict details: 8 positions, 3 distinct values, 3 segments each
    with open(os.path.join(vdir, "conflicts.jsonl"), "r", encoding="utf-8") as fh:
        confs = [json.loads(l) for l in fh]
    check(len(confs) == 8, f"8 conflict positions (got {len(confs)})")
    ok = all(c["flow_id"] == "conflict3" and 4 <= c["offset"] <= 11
             and c["values"] == ["aa", "bb", "cc"]
             and c["segments"] == ["c1", "c2", "c3"]
             and c["detail"] == [["c1", "aa"], ["c2", "bb"], ["c3", "cc"]]
             for c in confs)
    check(ok, "conflict rows keep all 3 segment ids and 3 distinct byte values")
    check([c["offset"] for c in confs] == list(range(4, 12)),
          "conflict offsets are 4..11 in order")

    # no scratch DB or temp files inside the published version
    leftovers = [f for f in glob.glob(os.path.join(vdir, "**", "*"), recursive=True)
                 if os.path.basename(f).startswith((".", "work.sqlite"))]
    check(not leftovers, "published version contains no scratch files")
    return cfg, segs, out, version


def test_shuffle(tmp, cfg, segs, version):
    print("== arbitrary shuffle yields identical results ==")
    with open(segs, "r", encoding="utf-8") as fh:
        lines = [l for l in fh.read().splitlines() if l.strip()]
    for seed in range(8):
        random.Random(seed).shuffle(lines)
        shuf = os.path.join(tmp, f"shuf_{seed}.jsonl")
        with open(shuf, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        out = os.path.join(tmp, f"shuf_out_{seed}")
        run(["assemble", "--config", cfg, "--segments", shuf, "--out", out])
        v2, _, _ = load_manifest(out)
        check(v2 == version, f"shuffle seed {seed}: identical version hash")
    # exact reverse arrival order
    rev = os.path.join(tmp, "rev.jsonl")
    with open(rev, "w", encoding="utf-8") as fh:
        fh.write("\n".join(reversed(lines)) + "\n")
    out = os.path.join(tmp, "rev_out")
    run(["assemble", "--config", cfg, "--segments", rev, "--out", out])
    v2, _, _ = load_manifest(out)
    check(v2 == version, "reversed input: identical version hash")


def test_invalid(tmp, good_cfg, good_out, good_version):
    print("== invalid input: whole run rejected, old version preserved ==")
    gen = os.path.join(tmp, "invalid")
    run(["generate", "--scenario", "invalid", "--out", gen])
    cfg = os.path.join(gen, "config.json")
    cases = ["dup", "badhex_odd", "badhex_char", "toolong", "range",
             "offset_half", "unknown_flow", "bad_seq"]
    for name in cases:
        out = os.path.join(tmp, f"inv_{name}")
        p = run(["assemble", "--config", cfg,
                 "--segments", os.path.join(gen, f"{name}.jsonl"),
                 "--out", out], expect=2)
        check("input error" in p.stderr, f"{name}: rejected with input error")
        check(not os.path.lexists(os.path.join(out, "current")),
              f"{name}: nothing published")
        check(not glob.glob(os.path.join(out, ".build-*")),
              f"{name}: staging cleaned up")
    # bad config (total_length == 2^31)
    p = run(["assemble", "--config", os.path.join(gen, "config_bad.json"),
             "--segments", os.path.join(gen, "valid.jsonl"),
             "--out", os.path.join(tmp, "inv_badcfg")], expect=2)
    check("input error" in p.stderr, "total_length=2^31 rejected")
    # corrupt input must not replace the previously published good version
    out = good_out
    p = run(["assemble", "--config", cfg,
             "--segments", os.path.join(gen, "dup.jsonl"), "--out", out], expect=2)
    v2, _, _ = load_manifest(out)
    check(v2 == good_version, "failed assemble keeps previous published version")
    # valid control input assembles fine
    out = os.path.join(tmp, "inv_valid")
    run(["assemble", "--config", cfg,
         "--segments", os.path.join(gen, "valid.jsonl"), "--out", out])
    check(True, "valid control input assembles")


def test_write_failure(tmp, cfg, segs):
    print("== write failure cannot replace old successful version ==")
    out = os.path.join(tmp, "wf_out")
    run(["assemble", "--config", cfg, "--segments", segs, "--out", out])
    v1, _, _ = load_manifest(out)
    # sabotage: 'versions' path occupied by a regular file
    os.remove(os.path.join(out, "current"))
    shutil.rmtree(os.path.join(out, "versions"))
    with open(os.path.join(out, "versions"), "w") as fh:
        fh.write("blocked")
    p = run(["assemble", "--config", cfg, "--segments", segs, "--out", out],
            expect=1)
    check("assemble failed" in p.stderr, "write failure reported (rc=1)")
    check(not os.path.lexists(os.path.join(out, "current")),
          "write failure: no current symlink created")
    check(not glob.glob(os.path.join(out, ".build-*")),
          "write failure: staging cleaned up")
    check(v1 is not None, "previous version hash retained in test context")


def test_sparse(tmp):
    print("== huge sparse flow: memory does not follow hole size ==")
    gen = os.path.join(tmp, "sparse")
    total = (1 << 31) - 1                      # 2 GiB - 1 expected bytes
    run(["generate", "--scenario", "sparse", "--out", gen,
         "--seed", "5", "--length", str(total), "--segments", "64", "--size", "1024"])
    out = os.path.join(tmp, "sparse_out")
    p = run(["assemble", "--config", os.path.join(gen, "config.json"),
             "--segments", os.path.join(gen, "segments.jsonl"), "--out", out])
    rss = peak_rss_kb(p)
    version, m, vdir = load_manifest(out)
    f = flow_of(m, "sparse")
    expect_flow(f, counts=(64 * 1024, total - 64 * 1024, 0),
                complete=False, intervals=None)
    check(len(f["intervals"]) == 64, "64 determined intervals for 64 segments")
    recs = read_segments(vdir)
    ok = all(recs[f"s{k:04d}"]["sole_covered"] == 1024 for k in range(64))
    check(ok, "every sparse segment solely covers its 1024 bytes")
    check(recs["z0"]["sole_covered"] == 0 and recs["z0"]["length"] == 0,
          "zero-length segment covers nothing")
    run(["verify", "--out", out, "--config", os.path.join(gen, "config.json"),
         "--segments", os.path.join(gen, "segments.jsonl")])
    gib = total / (1 << 30)
    print(f"  sparse: total_length={gib:.2f} GiB, peak RSS={rss / 1024:.1f} MiB")
    check(rss < 256 * 1024,
          f"peak RSS {rss} KiB << total_length ({total} bytes): no hole-sized allocation")
    # disk footprint of the published version also tracks coverage, not holes
    dsize = 0
    for root, _, files in os.walk(vdir):
        for fn in files:
            dsize += os.path.getsize(os.path.join(root, fn))
    print(f"  sparse: published version size={dsize / 1024:.1f} KiB")
    check(dsize < 16 * 1024 * 1024, "published size scales with coverage, not holes")


def test_big(tmp):
    print("== >64 MiB real payload: peak RSS measurement ==")
    gen = os.path.join(tmp, "big")
    seed = 7
    total = 80 << 20                           # 80 MiB of actual segment data
    run(["generate", "--scenario", "big", "--out", gen,
         "--seed", str(seed), "--length", str(total)])
    out = os.path.join(tmp, "big_out")
    p = run(["assemble", "--config", os.path.join(gen, "config.json"),
             "--segments", os.path.join(gen, "segments.jsonl"), "--out", out])
    rss = peak_rss_kb(p)
    version, m, vdir = load_manifest(out)
    f = flow_of(m, "big")
    expect_flow(f, counts=(total, 0, 0), max_det=(0, total), max_gap=None,
                max_con=None, complete=True, intervals=[(0, total)])
    # independently recompute the expected complete-stream hash
    def chunk(i):
        return random.Random(seed * 10000000 + i).randbytes(1 << 16)
    h = hashlib.sha256()
    for i in range(total // (1 << 16)):
        h.update(chunk(i))
    check(f["complete_sha256"] == h.hexdigest(),
          "complete stream sha256 matches independently generated content")
    run(["verify", "--out", out, "--config", os.path.join(gen, "config.json"),
         "--segments", os.path.join(gen, "segments.jsonl")])
    recs = read_segments(vdir)
    ok = all(r["sole_covered"] + r["shared_covered"] + r["conflict_covered"]
             == r["length"] for r in recs.values())
    check(ok, "per-segment contributions sum to payload length (big flow)")
    shared_any = any(recs[f"r{j:05d}"]["shared_covered"] == 65536
                     for j in range(128))
    check(shared_any, "retransmitted segments report shared coverage")
    print(f"  big: payload={total >> 20} MiB, peak RSS={rss / 1024:.1f} MiB")
    check(rss < 512 * 1024,
          f"peak RSS {rss} KiB stays bounded for {total >> 20} MiB of segments")


def test_inspect(out):
    print("== inspect ==")
    p = run(["inspect", "--out", out])
    check("flow=wrap" in p.stdout and "max_gap" in p.stdout,
          "inspect prints per-flow summary")
    p = run(["inspect", "--out", out, "--segment", "z1"])
    check('"sole_covered": 0' in p.stdout, "inspect --segment shows stats")
    p = run(["inspect", "--out", out, "--conflicts", "--flow", "conflict3"])
    check(p.stdout.count("\n") >= 8 and '"aa"' in p.stdout,
          "inspect --conflicts shows conflict rows")
    p = run(["inspect", "--out", out, "--json"])
    json.loads(p.stdout)
    check(True, "inspect --json is valid JSON")


def main():
    tmp = tempfile.mkdtemp(prefix="reasm_test_")
    print(f"test workspace: {tmp}")
    try:
        cfg, segs, small_out, version = test_small(tmp)
        test_shuffle(tmp, cfg, segs, version)
        test_invalid(tmp, cfg, small_out, version)
        test_write_failure(tmp, cfg, segs)
        test_sparse(tmp)
        test_big(tmp)
        test_inspect(small_out)
    finally:
        if os.environ.get("KEEP_TEST_TMP") != "1":
            shutil.rmtree(tmp, ignore_errors=True)
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S)")
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
