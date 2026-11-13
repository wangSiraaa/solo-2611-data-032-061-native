#!/usr/bin/env python3
"""
reasm.py — offline stream reassembler (no network access).

Rebuilds contiguous byte intervals from captured, possibly out-of-order /
duplicated / overlapping segments, and reports exactly which positions are
unrecoverable.

Subcommands:
  generate   create sample / stress inputs (config.json + segments.jsonl)
  assemble   validate input, classify every byte position, publish a version
  inspect    read a published version (summary / segment / conflicts)
  verify     re-check a published version against the original input,
             including an independent per-byte dictionary reference model

Storage model: all working state lives in a temporary SQLite database.
Covered positions are kept in 64 KiB pages (state byte + value byte per
position); pages only exist where something was actually covered, so memory
and disk scale with real coverage, never with total_length.  Segments are
streamed from the JSONL input and are never all held in memory.

Publishing is atomic: everything is built inside <out>/.build-* and only on
complete success renamed to <out>/versions/<content-hash>, with <out>/current
repointed via an atomic symlink swap.  A failed run never disturbs the
previously published version.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import random
import re
import resource
import shutil
import sqlite3
import sys
import tempfile
from collections import OrderedDict

PAGE = 1 << 16          # classification page granularity (64 KiB)
MAX_PAYLOAD = 1 << 16   # max payload per segment (64 KiB)
U32 = 1 << 32           # sequence number space
HALF = 1 << 31          # offsets and total_length must stay below 2^31

ST_UNCOV, ST_SOLE, ST_SHARED, ST_CONFLICT = 0, 1, 2, 3
CL_GAP, CL_DET, CL_CON = 0, 1, 2

# translate tables over state bytes (states only ever take values 0..3)
T_NEQ = bytes([0] + [1] * 255)                              # 0 -> 0, else 1
T_BASE = bytes([1, 2, 2, 3]) + bytes(range(4, 256))         # state after seeing a byte, ignoring equality
T_M12 = bytes([0, 1, 1, 0]) + bytes(252)                    # 1 for determined states (sole/shared)
T_ZERO = bytes([255]) + bytes(255)                          # 255 only for uncovered
T_CLASS = bytes([CL_GAP, CL_DET, CL_DET, CL_CON]) + bytes(252)

HEX_RE = re.compile(r"^[0-9a-fA-F]*$")
NONZERO_RE = re.compile(rb"[^\x00]")

SCHEMA = """
CREATE TABLE segments(
  segment_id TEXT PRIMARY KEY,
  flow_id    TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  off        INTEGER NOT NULL,
  len        INTEGER NOT NULL,
  payload    BLOB NOT NULL
);
CREATE TABLE pages(
  flow_id  TEXT NOT NULL,
  page_idx INTEGER NOT NULL,
  data     BLOB NOT NULL,          -- PAGE state bytes followed by PAGE value bytes
  PRIMARY KEY(flow_id, page_idx)
) WITHOUT ROWID;
CREATE TABLE seg_stats(
  segment_id TEXT PRIMARY KEY,
  sole     INTEGER NOT NULL,
  shared   INTEGER NOT NULL,
  conflict INTEGER NOT NULL
);
CREATE TABLE conflicts(
  flow_id    TEXT NOT NULL,
  pos        INTEGER NOT NULL,
  segment_id TEXT NOT NULL,
  byte       INTEGER NOT NULL
);
CREATE INDEX conflicts_idx ON conflicts(flow_id, pos, segment_id);
"""


class InputError(Exception):
    """Any invalid configuration or segment input: the whole run is rejected."""


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def load_config(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except OSError as e:
        raise InputError(f"cannot read config {path}: {e}")
    except json.JSONDecodeError as e:
        raise InputError(f"config {path} is not valid JSON: {e}")
    if not isinstance(obj, dict) or set(obj) != {"flows"} or not isinstance(obj["flows"], list):
        raise InputError("config must be an object with a single 'flows' array")
    flows = {}
    for i, f in enumerate(obj["flows"]):
        if not isinstance(f, dict) or set(f) != {"flow_id", "base_seq", "total_length"}:
            raise InputError(f"config flows[{i}] must have exactly flow_id, base_seq, total_length")
        fid, base, total = f["flow_id"], f["base_seq"], f["total_length"]
        if not isinstance(fid, str) or not fid:
            raise InputError(f"config flows[{i}]: flow_id must be a non-empty string")
        if fid in flows:
            raise InputError(f"config: duplicate flow_id {fid!r}")
        if not _is_int(base) or not (0 <= base < U32):
            raise InputError(f"config flows[{i}]: base_seq must be a 32-bit unsigned integer")
        if not _is_int(total) or not (0 <= total < HALF):
            raise InputError(f"config flows[{i}]: total_length must be an integer in [0, 2^31)")
        flows[fid] = {"flow_id": fid, "base_seq": base, "total_length": total, "index": i}
    return flows


def parse_segment_line(line, lineno):
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        raise InputError(f"line {lineno}: invalid JSON: {e}")
    if not isinstance(obj, dict):
        raise InputError(f"line {lineno}: expected a JSON object")
    allowed = {"segment_id", "flow_id", "seq", "payload"}
    extra = set(obj) - allowed
    missing = allowed - set(obj)
    if extra:
        raise InputError(f"line {lineno}: unknown fields {sorted(extra)}")
    if missing:
        raise InputError(f"line {lineno}: missing fields {sorted(missing)}")
    sid, fid, seq, pay = obj["segment_id"], obj["flow_id"], obj["seq"], obj["payload"]
    if not isinstance(sid, str) or not sid:
        raise InputError(f"line {lineno}: segment_id must be a non-empty string")
    if not isinstance(fid, str) or not fid:
        raise InputError(f"line {lineno}: flow_id must be a non-empty string")
    if not _is_int(seq) or not (0 <= seq < U32):
        raise InputError(f"line {lineno}: seq must be a 32-bit unsigned integer")
    if not isinstance(pay, str):
        raise InputError(f"line {lineno}: payload must be a hex string")
    if len(pay) % 2:
        raise InputError(f"line {lineno}: payload hex length must be even")
    if len(pay) > 2 * MAX_PAYLOAD:
        raise InputError(f"line {lineno}: payload exceeds 64 KiB")
    if not HEX_RE.match(pay):
        raise InputError(f"line {lineno}: payload is not strict hexadecimal")
    return sid, fid, seq, bytes.fromhex(pay)


def segment_offset(flow, seq, ln, lineno):
    """Relative offset; rejects offsets >= 2^31 and ranges past total_length.

    The offset is defined purely as (seq - base_seq) mod 2^32; no wrap count
    is ever inferred from arrival order.
    """
    off = (seq - flow["base_seq"]) % U32
    if off >= HALF:
        raise InputError(f"line {lineno}: relative offset {off} is not below 2^31")
    if off + ln > flow["total_length"]:
        raise InputError(
            f"line {lineno}: segment [{off}, {off + ln}) exceeds total_length {flow['total_length']}")
    return off


# ---------------------------------------------------------------------------
# vectorized per-slice state machine (big-int ops, no per-byte Python loops)
# ---------------------------------------------------------------------------

def update_slice(st, vl, dt):
    """Merge incoming bytes dt into a page slice (states st, values vl).

    All three are bytes of equal length; returns (new_states, new_values).
    Per position:
      uncovered(0)                  -> sole(1), value = dt
      sole(1)/shared(2), same byte  -> shared(2)
      sole(1)/shared(2), other byte -> conflict(3)
      conflict(3)                   -> conflict(3)
    """
    n = len(dt)
    if st == bytes(n):                      # whole slice uncovered: fast path
        return b"\x01" * n, dt
    allf = (1 << (8 * n)) - 1
    vi = int.from_bytes(vl, "big")
    di = int.from_bytes(dt, "big")
    neq = int.from_bytes(((vi ^ di).to_bytes(n, "big")).translate(T_NEQ), "big")
    base = int.from_bytes(st.translate(T_BASE), "big")
    mask = int.from_bytes(st.translate(T_M12), "big")
    # neq&mask lanes are 0/1, so *3 cannot carry across byte lanes; OR with
    # base lanes (1|3=3, 2|3=3, 3|3=3) turns exactly those positions conflict.
    nst = (base | ((neq & mask) * 3)).to_bytes(n, "big")
    zm = int.from_bytes(st.translate(T_ZERO), "big")
    nvl = ((di & zm) | (vi & (allf ^ zm))).to_bytes(n, "big")
    return nst, nvl


class PageStore:
    """Bounded LRU cache of 2*PAGE-byte page images backed by SQLite."""

    def __init__(self, conn, cap=32):
        self.conn = conn
        self.cap = cap
        self.cache = OrderedDict()          # (flow_id, page_idx) -> bytearray
        self.dirty = set()
        self.pending = 0

    def get(self, fid, pidx):
        key = (fid, pidx)
        ba = self.cache.get(key)
        if ba is not None:
            self.cache.move_to_end(key)
            return ba
        row = self.conn.execute(
            "SELECT data FROM pages WHERE flow_id=? AND page_idx=?", (fid, pidx)).fetchone()
        ba = bytearray(row[0]) if row else bytearray(2 * PAGE)
        self.cache[key] = ba
        if len(self.cache) > self.cap:
            k, v = self.cache.popitem(last=False)
            if k in self.dirty:
                self._write(k, v)
                self.dirty.discard(k)
        return ba

    def mark_dirty(self, fid, pidx):
        self.dirty.add((fid, pidx))

    def _write(self, key, ba):
        self.conn.execute(
            "INSERT OR REPLACE INTO pages(flow_id, page_idx, data) VALUES(?,?,?)",
            (key[0], key[1], sqlite3.Binary(bytes(ba))))
        self.pending += 1
        if self.pending >= 512:
            self.conn.commit()
            self.pending = 0

    def flush(self):
        for k in list(self.dirty):
            self._write(k, self.cache[k])
        self.dirty.clear()
        self.conn.commit()
        self.pending = 0


# ---------------------------------------------------------------------------
# assemble pipeline
# ---------------------------------------------------------------------------

def init_db(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=OFF")     # scratch DB, deleted after build
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(SCHEMA)
    return conn


def pass1_ingest(conn, flows, segments_path, pages):
    """Validate every line, store segments, fold payloads into pages."""
    nseg = 0
    try:
        fh = open(segments_path, "r", encoding="utf-8")
    except OSError as e:
        raise InputError(f"cannot read segments file {segments_path}: {e}")
    with fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            sid, fid, seq, payload = parse_segment_line(line, lineno)
            flow = flows.get(fid)
            if flow is None:
                raise InputError(f"line {lineno}: unknown flow_id {fid!r}")
            off = segment_offset(flow, seq, len(payload), lineno)
            try:
                conn.execute(
                    "INSERT INTO segments(segment_id, flow_id, seq, off, len, payload)"
                    " VALUES(?,?,?,?,?,?)",
                    (sid, fid, seq, off, len(payload), sqlite3.Binary(payload)))
            except sqlite3.IntegrityError:
                raise InputError(f"line {lineno}: duplicate segment_id {sid!r}")
            pos, idx = off, 0
            ln = len(payload)
            while idx < ln:
                pidx = pos >> 16
                ip = pos & (PAGE - 1)
                n = min(PAGE - ip, ln - idx)
                ba = pages.get(fid, pidx)
                st = bytes(ba[ip:ip + n])
                vl = bytes(ba[PAGE + ip:PAGE + ip + n])
                nst, nvl = update_slice(st, vl, payload[idx:idx + n])
                ba[ip:ip + n] = nst
                ba[PAGE + ip:PAGE + ip + n] = nvl
                pages.mark_dirty(fid, pidx)
                pos += n
                idx += n
            nseg += 1
            if nseg % 256 == 0:
                conn.commit()
    pages.flush()
    conn.commit()
    return nseg


def pass2_segment_stats(conn, pages):
    """Per-segment contributions + conflict details, from the FINAL page states.

    Everything here is derived from the complete input set, so processing
    order cannot influence the metrics.
    """
    rowids = [r[0] for r in conn.execute("SELECT rowid FROM segments ORDER BY rowid")]
    batch_stat, batch_conf = [], []

    def flush():
        if batch_stat:
            conn.executemany("INSERT INTO seg_stats VALUES(?,?,?,?)", batch_stat)
            batch_stat.clear()
        if batch_conf:
            conn.executemany("INSERT INTO conflicts VALUES(?,?,?,?)", batch_conf)
            batch_conf.clear()
        conn.commit()

    done = 0
    for rid in rowids:
        sid, fid, off, ln, payload = conn.execute(
            "SELECT segment_id, flow_id, off, len, payload FROM segments WHERE rowid=?",
            (rid,)).fetchone()
        sole = shared = conf = 0
        pos, idx = off, 0
        while idx < ln:
            pidx = pos >> 16
            ip = pos & (PAGE - 1)
            n = min(PAGE - ip, ln - idx)
            ba = pages.get(fid, pidx)
            st = bytes(ba[ip:ip + n])
            sole += st.count(ST_SOLE)
            shared += st.count(ST_SHARED)
            c3 = st.count(ST_CONFLICT)
            if c3:
                conf += c3
                base = (pidx << 16) + ip
                j = st.find(ST_CONFLICT)
                while j != -1:
                    batch_conf.append((fid, base + j, sid, payload[idx + j]))
                    j = st.find(ST_CONFLICT, j + 1)
            pos += n
            idx += n
        batch_stat.append((sid, sole, shared, conf))
        done += 1
        if done % 32 == 0 or len(batch_conf) >= 65536:
            flush()
    flush()


class RunMerger:
    """Streams one flow's pages in order and merges same-class runs."""

    def __init__(self, findex, total, files_dir):
        self.findex = findex
        self.total = total
        self.files_dir = files_dir
        self.counts = [0, 0, 0]             # gap, determined, conflict
        self.best = [None, None, None]      # (start, length) per class
        self.intervals = []                 # determined interval records
        self.open_run = None                # [class, start, length]
        self.det_fh = None
        self.det_hash = None
        self.complete_file = None
        self.complete_sha256 = None

    def feed(self, base, st, vl):
        plen = len(st)
        cls = st.translate(T_CLASS)
        self.counts[0] += cls.count(CL_GAP)
        self.counts[1] += cls.count(CL_DET)
        self.counts[2] += cls.count(CL_CON)
        ci = int.from_bytes(cls, "big")
        allf = (1 << (8 * plen)) - 1
        # byte j of xb is cls[j]^cls[j+1] (and cls[last] at the end)
        xb = ((ci ^ (ci << 8)) & allf).to_bytes(plen, "big")
        prev = 0
        for mch in NONZERO_RE.finditer(xb):
            e = mch.start()
            self._emit(base, prev, e, cls[prev], vl)
            prev = e + 1
        if prev < plen:
            self._emit(base, prev, plen - 1, cls[prev], vl)

    def _emit(self, base, s, e, c, vl):
        gstart = base + s
        glen = e - s + 1
        if self.open_run is not None and self.open_run[0] == c:
            self.open_run[2] += glen
        else:
            self._close()
            self.open_run = [c, gstart, glen]
            if c == CL_DET:
                self.det_fh = open(os.path.join(self.files_dir, ".det_tmp"), "wb")
                self.det_hash = hashlib.sha256()
        if c == CL_DET:
            chunk = vl[s:e + 1]
            self.det_fh.write(chunk)
            self.det_hash.update(chunk)

    def _close(self):
        if self.open_run is None:
            return
        c, start, ln = self.open_run
        b = self.best[c]
        if b is None or ln > b[1]:
            self.best[c] = (start, ln)
        if c == CL_DET:
            _fsync_file(self.det_fh)
            self.det_fh.close()
            name = f"f{self.findex}_off{start}_len{ln}.bin"
            os.replace(os.path.join(self.files_dir, ".det_tmp"),
                       os.path.join(self.files_dir, name))
            self.intervals.append({
                "offset": start,
                "length": ln,
                "sha256": self.det_hash.hexdigest(),
                "file": f"files/{name}",
            })
            self.det_fh = None
            self.det_hash = None
        self.open_run = None

    def finish(self):
        self._close()
        if self.counts[CL_DET] == self.total:   # every position determined
            name = f"f{self.findex}_complete.bin"
            dst = os.path.join(self.files_dir, name)
            if self.total == 0:
                self.complete_sha256 = hashlib.sha256(b"").hexdigest()
                with open(dst, "wb") as fh:
                    _fsync_file(fh)
            else:
                src = os.path.join(self.files_dir,
                                   os.path.basename(self.intervals[0]["file"]))
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copyfile(src, dst)
                self.complete_sha256 = self.intervals[0]["sha256"]
            self.complete_file = f"files/{name}"

    def result(self, fid, fl):
        def fmt(b):
            return None if b is None else {"offset": b[0], "length": b[1]}
        return {
            "flow_id": fid,
            "base_seq": fl["base_seq"],
            "total_length": self.total,
            "complete": self.complete_file is not None,
            "counts": {
                "determined": self.counts[CL_DET],
                "gap": self.counts[CL_GAP],
                "conflict": self.counts[CL_CON],
            },
            "max_determined_interval": fmt(self.best[CL_DET]),
            "max_gap": fmt(self.best[CL_GAP]),
            "max_conflict_interval": fmt(self.best[CL_CON]),
            "intervals": self.intervals,
            "complete_file": self.complete_file,
            "complete_sha256": self.complete_sha256,
        }


def pass3_runs(conn, flows, files_dir):
    results = {}
    for fid in sorted(flows):
        fl = flows[fid]
        total = fl["total_length"]
        rm = RunMerger(fl["index"], total, files_dir)
        npages = (total + PAGE - 1) // PAGE
        cur = conn.execute(
            "SELECT page_idx, data FROM pages WHERE flow_id=? ORDER BY page_idx", (fid,))
        row = cur.fetchone()
        for pidx in range(npages):
            base = pidx * PAGE
            plen = min(PAGE, total - base)
            if row is not None and row[0] == pidx:
                data = row[1]
                row = cur.fetchone()
                st = bytes(data[:plen])
                vl = bytes(data[PAGE:PAGE + plen])
            else:
                st = bytes(plen)                # never covered
                vl = None
            rm.feed(base, st, vl)
        cur.close()
        rm.finish()
        results[fid] = rm.result(fid, fl)
    return results


# ---------------------------------------------------------------------------
# output writers (deterministic, canonical JSON)
# ---------------------------------------------------------------------------

def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fsync_file(fh):
    fh.flush()
    os.fsync(fh.fileno())


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_segments_jsonl(conn, path):
    h = hashlib.sha256()
    with open(path, "wb") as fh:
        cur = conn.execute(
            "SELECT s.segment_id, s.flow_id, s.seq, s.off, s.len,"
            "       t.sole, t.shared, t.conflict"
            " FROM segments s JOIN seg_stats t ON t.segment_id = s.segment_id"
            " ORDER BY s.segment_id")
        for sid, fid, seq, off, ln, sole, shared, conf in cur:
            rec = {
                "segment_id": sid, "flow_id": fid, "seq": seq,
                "offset": off, "length": ln,
                "sole_covered": sole, "shared_covered": shared,
                "conflict_covered": conf,
            }
            b = _canonical(rec) + b"\n"
            fh.write(b)
            h.update(b)
        _fsync_file(fh)
    return h.hexdigest()


def write_conflicts_jsonl(conn, path):
    h = hashlib.sha256()
    with open(path, "wb") as fh:
        cur = conn.execute(
            "SELECT flow_id, pos, segment_id, byte FROM conflicts"
            " ORDER BY flow_id, pos, segment_id")
        for (fid, pos), grp in itertools.groupby(cur, key=lambda r: (r[0], r[1])):
            pairs = sorted({(r[2], r[3]) for r in grp})
            rec = {
                "flow_id": fid,
                "offset": pos,
                "values": [f"{b:02x}" for b in sorted({b for _, b in pairs})],
                "segments": sorted({s for s, _ in pairs}),
                "detail": [[s, f"{b:02x}"] for s, b in pairs],
            }
            b = _canonical(rec) + b"\n"
            fh.write(b)
            h.update(b)
        _fsync_file(fh)
    return h.hexdigest()


def publish(staging, out_dir, manifest):
    """Atomically publish the staging directory as a new version."""
    body = _canonical(manifest)
    version = hashlib.sha256(body).hexdigest()
    with open(os.path.join(staging, "manifest.json"), "wb") as fh:
        fh.write(body + b"\n")
        _fsync_file(fh)
    _fsync_dir(os.path.join(staging, "files"))
    _fsync_dir(staging)
    versions_dir = os.path.join(out_dir, "versions")
    os.makedirs(versions_dir, exist_ok=True)
    final = os.path.join(versions_dir, version)
    if os.path.isdir(final):
        shutil.rmtree(staging)                  # identical content already published
    else:
        os.rename(staging, final)               # atomic on the same filesystem
        _fsync_dir(versions_dir)
    tmp_link = os.path.join(out_dir, f".current.{os.getpid()}")
    if os.path.lexists(tmp_link):
        os.remove(tmp_link)
    os.symlink(os.path.join("versions", version), tmp_link)
    os.replace(tmp_link, os.path.join(out_dir, "current"))   # atomic repoint
    _fsync_dir(out_dir)
    return version


def cmd_assemble(args):
    try:
        flows = load_config(args.config)
    except InputError as e:
        print(f"input error: {e}", file=sys.stderr)
        return 2
    try:
        os.makedirs(args.out, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".build-", dir=args.out)
    except OSError as e:
        print(f"assemble failed: cannot create staging area: {e}", file=sys.stderr)
        return 1
    try:
        with open(args.config, "rb") as fh:
            config_sha = hashlib.sha256(fh.read()).hexdigest()
        db_path = os.path.join(staging, "work.sqlite")
        conn = init_db(db_path)
        pages = PageStore(conn)
        nseg = pass1_ingest(conn, flows, args.segments, pages)
        pass2_segment_stats(conn, pages)
        files_dir = os.path.join(staging, "files")
        os.mkdir(files_dir)
        results = pass3_runs(conn, flows, files_dir)
        seg_hash = write_segments_jsonl(conn, os.path.join(staging, "segments.jsonl"))
        con_hash = write_conflicts_jsonl(conn, os.path.join(staging, "conflicts.jsonl"))
        conn.close()
        os.remove(db_path)                      # scratch DB is not published
        files_map = {"segments.jsonl": seg_hash, "conflicts.jsonl": con_hash}
        for r in results.values():
            for it in r["intervals"]:
                files_map[it["file"]] = it["sha256"]
            if r["complete_file"]:
                files_map[r["complete_file"]] = r["complete_sha256"]
        manifest = {
            "format": "reasm/1",
            "config_sha256": config_sha,
            "flows": [results[fid] for fid in sorted(flows)],
            "files": files_map,
        }
        version = publish(staging, args.out, manifest)
    except InputError as e:
        shutil.rmtree(staging, ignore_errors=True)
        print(f"input error: {e}", file=sys.stderr)
        return 2
    except Exception as e:                      # I/O, sqlite, ...: keep old version
        shutil.rmtree(staging, ignore_errors=True)
        print(f"assemble failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"published version {version}")
    print(f"flows={len(flows)} segments={nseg}")
    for fid in sorted(flows):
        r = results[fid]
        c = r["counts"]
        print(f"  flow={fid} complete={str(r['complete']).lower()} "
              f"determined={c['determined']} gap={c['gap']} conflict={c['conflict']}")
    print(f"peak_rss_kb={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}")
    return 0


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------

def resolve_version_dir(out, version=None):
    if version:
        d = os.path.join(out, "versions", version)
    else:
        link = os.path.join(out, "current")
        if not os.path.lexists(link):
            raise InputError(f"no published version at {link}")
        d = os.path.realpath(link)
    if not os.path.isfile(os.path.join(d, "manifest.json")):
        raise InputError(f"no manifest found in {d}")
    return d


def _fmt_interval(iv):
    return "none" if iv is None else f"offset={iv['offset']} length={iv['length']}"


def cmd_inspect(args):
    try:
        vdir = resolve_version_dir(args.out, args.version)
    except InputError as e:
        print(f"inspect failed: {e}", file=sys.stderr)
        return 1
    with open(os.path.join(vdir, "manifest.json"), "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    if args.json:
        print(json.dumps(manifest, indent=2))
        return 0
    if args.segment:
        with open(os.path.join(vdir, "segments.jsonl"), "r", encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                if rec["segment_id"] == args.segment:
                    print(json.dumps(rec, indent=2))
                    return 0
        print(f"segment {args.segment!r} not found", file=sys.stderr)
        return 1
    if args.conflicts:
        shown = 0
        with open(os.path.join(vdir, "conflicts.jsonl"), "r", encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                if args.flow and rec["flow_id"] != args.flow:
                    continue
                print(json.dumps(rec, sort_keys=True))
                shown += 1
                if shown >= args.limit:
                    print(f"... (limited to {args.limit} rows)")
                    break
        if shown == 0:
            print("no conflicts")
        return 0
    print(f"version {os.path.basename(vdir)}")
    for f in manifest["flows"]:
        if args.flow and f["flow_id"] != args.flow:
            continue
        c = f["counts"]
        print(f"flow={f['flow_id']} base_seq={f['base_seq']} "
              f"total_length={f['total_length']} complete={str(f['complete']).lower()}")
        print(f"  counts: determined={c['determined']} gap={c['gap']} conflict={c['conflict']}")
        print(f"  max_determined_interval: {_fmt_interval(f['max_determined_interval'])}")
        print(f"  max_gap:                 {_fmt_interval(f['max_gap'])}")
        print(f"  max_conflict_interval:   {_fmt_interval(f['max_conflict_interval'])}")
        print(f"  determined_intervals: {len(f['intervals'])}")
        for it in f["intervals"]:
            print(f"    offset={it['offset']} length={it['length']} "
                  f"sha256={it['sha256']} file={it['file']}")
        if f["complete_file"]:
            print(f"  complete_file: {f['complete_file']} sha256={f['complete_sha256']}")
    return 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def deep_verify(errors, vdir, manifest, flows, segments_path, seg_recs):
    """Independent per-byte dictionary reference model."""
    model = {fid: {} for fid in flows}          # pos -> [value, {values}, [(sid, byte)]]
    seglist = []
    seen_sids = set()
    with open(segments_path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                sid, fid, seq, payload = parse_segment_line(line, lineno)
            except InputError as e:
                errors.append(f"reference model: {e}")
                return
            if sid in seen_sids:
                errors.append(f"reference model: duplicate segment_id {sid!r}")
                return
            seen_sids.add(sid)
            fl = flows.get(fid)
            if fl is None:
                errors.append(f"reference model: line {lineno}: unknown flow_id {fid!r}")
                return
            try:
                off = segment_offset(fl, seq, len(payload), lineno)
            except InputError as e:
                errors.append(f"reference model: {e}")
                return
            m = model[fid]
            for i, b in enumerate(payload):
                p = off + i
                e = m.get(p)
                if e is None:
                    m[p] = [b, {b}, [(sid, b)]]
                else:
                    e[1].add(b)
                    e[2].append((sid, b))
            seglist.append((sid, fid, seq, off, len(payload)))

    mflows = {f["flow_id"]: f for f in manifest["flows"]}
    for fid in sorted(flows):
        total = flows[fid]["total_length"]
        m = model[fid]
        mf = mflows.get(fid)
        if mf is None:
            errors.append(f"{fid}: missing from manifest")
            continue
        counts = [0, 0, 0]
        best = [None, None, None]
        intervals = []                          # (start, length, content bytes)
        pos = 0
        run_c = None
        run_s = 0
        buf = bytearray()

        def close_run(c, s, e):
            ln = e - s
            b = best[c]
            if b is None or ln > b[1]:
                best[c] = (s, ln)
            if c == CL_DET:
                intervals.append((s, ln, bytes(buf)))

        while pos < total:
            e = m.get(pos)
            c = CL_GAP if e is None else (CL_CON if len(e[1]) > 1 else CL_DET)
            if run_c is None:
                run_c, run_s, buf = c, pos, bytearray()
            elif c != run_c:
                close_run(run_c, run_s, pos)
                run_c, run_s, buf = c, pos, bytearray()
            counts[c] += 1
            if c == CL_DET:
                buf.append(m[pos][0])
            pos += 1
        if run_c is not None:
            close_run(run_c, run_s, pos)

        exp_counts = {"determined": counts[CL_DET], "gap": counts[CL_GAP],
                      "conflict": counts[CL_CON]}
        if mf["counts"] != exp_counts:
            errors.append(f"{fid}: counts {mf['counts']} != reference {exp_counts}")

        def fmt(b):
            return None if b is None else {"offset": b[0], "length": b[1]}
        for key, cls in (("max_determined_interval", CL_DET),
                         ("max_gap", CL_GAP),
                         ("max_conflict_interval", CL_CON)):
            if mf[key] != fmt(best[cls]):
                errors.append(f"{fid}: {key} {mf[key]} != reference {fmt(best[cls])}")

        if len(mf["intervals"]) != len(intervals):
            errors.append(f"{fid}: {len(mf['intervals'])} intervals != "
                          f"reference {len(intervals)}")
        for it, (s, ln, content) in zip(mf["intervals"], intervals):
            if it["offset"] != s or it["length"] != ln:
                errors.append(f"{fid}: interval ({it['offset']},{it['length']}) "
                              f"!= reference ({s},{ln})")
                continue
            if it["sha256"] != hashlib.sha256(content).hexdigest():
                errors.append(f"{fid}: interval @{s} sha256 mismatch")
            try:
                with open(os.path.join(vdir, it["file"]), "rb") as fh:
                    actual = fh.read()
            except OSError:
                errors.append(f"{fid}: interval file {it['file']} unreadable")
                continue
            if actual != content:
                errors.append(f"{fid}: interval file @{s} content mismatch")

        complete = counts[CL_DET] == total
        if mf["complete"] != complete:
            errors.append(f"{fid}: complete={mf['complete']} != reference {complete}")
        if complete:
            content = bytes(total) if total == 0 else intervals[0][2]
            sha = hashlib.sha256(content).hexdigest()
            if mf["complete_sha256"] != sha:
                errors.append(f"{fid}: complete sha256 mismatch")
            try:
                with open(os.path.join(vdir, mf["complete_file"]), "rb") as fh:
                    actual = fh.read()
            except OSError:
                errors.append(f"{fid}: complete file unreadable")
            else:
                if actual != content:
                    errors.append(f"{fid}: complete file content mismatch")

    # per-segment contributions
    ref_stats = {}
    for sid, fid, seq, off, ln in seglist:
        m = model[fid]
        sole = shared = conf = 0
        for p in range(off, off + ln):
            e = m[p]
            if len(e[1]) > 1:
                conf += 1
            elif len(e[2]) == 1:
                sole += 1
            else:
                shared += 1
        ref_stats[sid] = (fid, seq, off, ln, sole, shared, conf)
    if set(seg_recs) != set(ref_stats):
        errors.append("segments.jsonl segment set does not match input")
    for sid, (fid, seq, off, ln, sole, shared, conf) in sorted(ref_stats.items()):
        rec = seg_recs.get(sid)
        if rec is None:
            continue
        want = {"segment_id": sid, "flow_id": fid, "seq": seq, "offset": off,
                "length": ln, "sole_covered": sole, "shared_covered": shared,
                "conflict_covered": conf}
        if rec != want:
            errors.append(f"segment {sid}: {rec} != reference {want}")

    # conflict details
    conf_ref = []
    for fid in sorted(flows):
        for p in sorted(model[fid]):
            e = model[fid][p]
            if len(e[1]) > 1:
                pairs = sorted(set(e[2]))
                conf_ref.append({
                    "flow_id": fid,
                    "offset": p,
                    "values": [f"{b:02x}" for b in sorted(e[1])],
                    "segments": sorted({s for s, _ in pairs}),
                    "detail": [[s, f"{b:02x}"] for s, b in pairs],
                })
    conf_actual = []
    with open(os.path.join(vdir, "conflicts.jsonl"), "r", encoding="utf-8") as fh:
        for line in fh:
            conf_actual.append(json.loads(line))
    if conf_actual != conf_ref:
        errors.append(f"conflicts.jsonl does not match reference "
                      f"({len(conf_actual)} vs {len(conf_ref)} rows)")


def cmd_verify(args):
    try:
        vdir = resolve_version_dir(args.out, args.version)
    except InputError as e:
        print(f"verify failed: {e}", file=sys.stderr)
        return 1
    with open(os.path.join(vdir, "manifest.json"), "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    errors = []

    # 1. every published file must match its manifest hash
    for rel, sha in sorted(manifest["files"].items()):
        p = os.path.join(vdir, rel)
        try:
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != sha:
                errors.append(f"{rel}: sha256 mismatch with manifest")
        except OSError:
            errors.append(f"{rel}: missing or unreadable")

    # 2. config + internal consistency
    try:
        flows = load_config(args.config)
    except InputError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    mflows = {f["flow_id"]: f for f in manifest["flows"]}
    if set(mflows) != set(flows):
        errors.append("manifest flow set does not match config")
    for fid, mf in sorted(mflows.items()):
        if fid not in flows:
            continue
        total = flows[fid]["total_length"]
        c = mf["counts"]
        if c["determined"] + c["gap"] + c["conflict"] != total:
            errors.append(f"{fid}: class counts do not sum to total_length")
        if mf["complete"] != (c["determined"] == total):
            errors.append(f"{fid}: complete flag inconsistent with counts")
        off = 0
        for it in mf["intervals"]:
            if it["offset"] < off:
                errors.append(f"{fid}: intervals overlap or out of order")
            off = it["offset"] + it["length"]
            if off > total:
                errors.append(f"{fid}: interval extends beyond total_length")
        if mf["complete"] and total > 0:
            iv = mf["intervals"]
            if len(iv) != 1 or iv[0]["offset"] != 0 or iv[0]["length"] != total:
                errors.append(f"{fid}: complete but not a single full interval")
            elif mf["complete_sha256"] != iv[0]["sha256"]:
                errors.append(f"{fid}: complete hash != interval hash")
    seg_recs = {}
    with open(os.path.join(vdir, "segments.jsonl"), "r", encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if (rec["sole_covered"] + rec["shared_covered"] + rec["conflict_covered"]
                    != rec["length"]):
                errors.append(f"segment {rec['segment_id']}: contribution sum != length")
            seg_recs[rec["segment_id"]] = rec

    # 3. independent per-byte reference model (small inputs, or on demand)
    size = os.path.getsize(args.segments)
    do_deep = args.force or args.deep or size <= (96 << 20)
    if do_deep and size > (96 << 20) and not args.force:
        print("note: large input, skipping deep reference model (use --force)")
        do_deep = False
    if do_deep:
        deep_verify(errors, vdir, manifest, flows, args.segments, seg_recs)

    if errors:
        print(f"verify FAILED ({len(errors)} problem(s))")
        for e in errors[:50]:
            print(" -", e)
        return 3
    print("verify OK" + (" (deep reference model)" if do_deep
                         else " (hashes + consistency)"))
    return 0


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as fh:
        for obj in records:
            fh.write(json.dumps(obj, sort_keys=True))
            fh.write("\n")


def _seg(sid, fid, seq, data):
    return {"segment_id": sid, "flow_id": fid, "seq": seq, "payload": data.hex()}


def gen_small(out):
    flows = [
        {"flow_id": "wrap", "base_seq": 0xFFFFFFF8, "total_length": 48},
        {"flow_id": "retrans", "base_seq": 1000, "total_length": 32},
        {"flow_id": "overlap", "base_seq": 5000, "total_length": 40},
        {"flow_id": "conflict3", "base_seq": 0, "total_length": 16},
        {"flow_id": "gaps", "base_seq": 0, "total_length": 64},
        {"flow_id": "empty", "base_seq": 0, "total_length": 0},
        {"flow_id": "never", "base_seq": 0, "total_length": 24},
        {"flow_id": "zerolen", "base_seq": 0, "total_length": 8},
    ]
    wrap_data = bytes(range(40))
    rp = bytes((i * 7 + 3) & 0xFF for i in range(32))
    o1 = bytes(range(24))
    o2 = o1[16:24] + bytes(range(24, 40))
    segs = [
        # 32-bit sequence wrap past zero, deliberately out of arrival order
        _seg("w3", "wrap", 24, wrap_data[32:40]),
        _seg("w1", "wrap", 0xFFFFFFF8, wrap_data[0:16]),
        _seg("w2", "wrap", 8, wrap_data[16:32]),
        # full retransmission, identical bytes twice
        _seg("r2", "retrans", 1000, rp),
        _seg("r1", "retrans", 1000, rp),
        # partial identical overlap
        _seg("o2", "overlap", 5016, o2),
        _seg("o1", "overlap", 5000, o1),
        # three different byte values at the same positions
        _seg("c2", "conflict3", 4, b"\xbb" * 8),
        _seg("c1", "conflict3", 0, b"\xaa" * 16),
        _seg("c3", "conflict3", 4, b"\xcc" * 8),
        # head and tail gaps
        _seg("g1", "gaps", 16, bytes(range(32))),
        # zero-length segment (legal, covers nothing)
        _seg("z1", "zerolen", 0, b""),
        _seg("z2", "zerolen", 4, b"\x01\x02\x03\x04"),
    ]
    with open(os.path.join(out, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"flows": flows}, fh, indent=2)
    _write_jsonl(os.path.join(out, "segments.jsonl"), segs)


def gen_invalid(out):
    flows = [{"flow_id": "f", "base_seq": 0, "total_length": 64}]
    with open(os.path.join(out, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"flows": flows}, fh, indent=2)
    with open(os.path.join(out, "config_bad.json"), "w", encoding="utf-8") as fh:
        json.dump({"flows": [{"flow_id": "f", "base_seq": 0,
                              "total_length": 1 << 31}]}, fh)   # 2^31 not allowed
    ok = [_seg("a1", "f", 0, b"\x01" * 8), _seg("a2", "f", 8, b"")]
    _write_jsonl(os.path.join(out, "valid.jsonl"), ok)
    _write_jsonl(os.path.join(out, "dup.jsonl"),
                 ok + [_seg("a1", "f", 16, b"\x02" * 4)])                    # duplicate id
    _write_jsonl(os.path.join(out, "badhex_odd.jsonl"),
                 [_seg("b1", "f", 0, b"\x01" * 4),
                  {"segment_id": "b2", "flow_id": "f", "seq": 4, "payload": "abc"}])
    _write_jsonl(os.path.join(out, "badhex_char.jsonl"),
                 [{"segment_id": "b1", "flow_id": "f", "seq": 0, "payload": "0g00"}])
    _write_jsonl(os.path.join(out, "toolong.jsonl"),
                 [{"segment_id": "b1", "flow_id": "f", "seq": 0,
                   "payload": "00" * (MAX_PAYLOAD + 1)}])                   # 64 KiB + 1
    _write_jsonl(os.path.join(out, "range.jsonl"),
                 [_seg("b1", "f", 60, b"\x01" * 8)])                          # 60+8 > 64
    _write_jsonl(os.path.join(out, "offset_half.jsonl"),
                 [_seg("b1", "f", 1 << 31, b"")])                             # offset == 2^31
    _write_jsonl(os.path.join(out, "unknown_flow.jsonl"),
                 [_seg("b1", "nope", 0, b"\x00" * 4)])
    _write_jsonl(os.path.join(out, "bad_seq.jsonl"),
                 [_seg("b1", "f", 1 << 32, b"\x00" * 4)])                     # seq > 32 bits


def gen_sparse(out, seed, total, nseg, size):
    rng = random.Random(seed)
    base = 777777777
    flows = [{"flow_id": "sparse", "base_seq": base, "total_length": total}]
    stride = total // nseg
    segs = []
    for k in range(nseg):
        off = k * stride + rng.randrange(1000)
        data = random.Random(seed * 100000 + k).randbytes(size)
        segs.append(_seg(f"s{k:04d}", "sparse", (base + off) % U32, data))
    segs.append(_seg("z0", "sparse", base, b""))
    segs.append(_seg("z1", "sparse", (base + total) % U32, b""))
    rng.shuffle(segs)
    with open(os.path.join(out, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"flows": flows}, fh, indent=2)
    _write_jsonl(os.path.join(out, "segments.jsonl"), segs)
    with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"seed": seed, "total_length": total, "nseg": nseg, "size": size}, fh)


def big_chunk(seed, i):
    return random.Random(seed * 10000000 + i).randbytes(PAGE)


def gen_big(out, seed, total):
    base = 4_000_000_000
    nch = total // PAGE
    n_retrans = max(1, nch // 10)
    flows = [{"flow_id": "big", "base_seq": base, "total_length": total}]
    rng = random.Random(seed + 1)

    def records():
        recs = [_seg(f"b{i:05d}", "big", (base + i * PAGE) % U32, big_chunk(seed, i))
                for i in range(nch)]
        for j in range(n_retrans):                      # full-content retransmissions
            i = rng.randrange(nch)
            recs.append(_seg(f"r{j:05d}", "big", (base + i * PAGE) % U32,
                             big_chunk(seed, i)))
        rng.shuffle(recs)
        return recs

    with open(os.path.join(out, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"flows": flows}, fh, indent=2)
    _write_jsonl(os.path.join(out, "segments.jsonl"), records())
    with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"seed": seed, "base_seq": base, "total_length": total,
                   "n_chunks": nch, "n_retrans": n_retrans}, fh)


def cmd_generate(args):
    os.makedirs(args.out, exist_ok=True)
    if args.scenario == "small":
        gen_small(args.out)
    elif args.scenario == "invalid":
        gen_invalid(args.out)
    elif args.scenario == "sparse":
        gen_sparse(args.out, args.seed,
                   args.length if args.length is not None else HALF - 1,
                   args.segments or 64, args.size or 1024)
    elif args.scenario == "big":
        gen_big(args.out, args.seed,
                args.length if args.length is not None else 80 << 20)
    print(f"generated scenario {args.scenario!r} in {args.out}")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="reasm", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="generate sample/stress inputs")
    g.add_argument("--scenario", required=True,
                   choices=["small", "invalid", "sparse", "big"])
    g.add_argument("--out", required=True)
    g.add_argument("--seed", type=int, default=1)
    g.add_argument("--length", type=int, default=None,
                   help="total_length of the generated flow")
    g.add_argument("--segments", type=int, default=None,
                   help="number of segments (sparse)")
    g.add_argument("--size", type=int, default=None,
                   help="payload size per segment (sparse)")
    g.set_defaults(fn=cmd_generate)

    a = sub.add_parser("assemble", help="validate input and publish a version")
    a.add_argument("--config", required=True)
    a.add_argument("--segments", required=True)
    a.add_argument("--out", required=True)
    a.set_defaults(fn=cmd_assemble)

    i = sub.add_parser("inspect", help="read a published version")
    i.add_argument("--out", required=True)
    i.add_argument("--version", default=None)
    i.add_argument("--flow", default=None)
    i.add_argument("--segment", default=None)
    i.add_argument("--conflicts", action="store_true")
    i.add_argument("--limit", type=int, default=20)
    i.add_argument("--json", action="store_true")
    i.set_defaults(fn=cmd_inspect)

    v = sub.add_parser("verify", help="verify a published version against the input")
    v.add_argument("--out", required=True)
    v.add_argument("--version", default=None)
    v.add_argument("--config", required=True)
    v.add_argument("--segments", required=True)
    v.add_argument("--deep", action="store_true",
                   help="force the independent per-byte reference model")
    v.add_argument("--force", action="store_true",
                   help="allow the deep model even for large inputs")
    v.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
