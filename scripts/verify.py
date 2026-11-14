"""End-to-end verification of the frozen-featurizer deliverable.

Runs real subprocesses (fit, batch, serve) and an independent hand-computed
oracle; the batch implementation and the HTTP implementation are never used
to certify each other. Exit code 0 means every matrix row passed.

Matrix rows
-----------
1. fit stats equal an independent hand calculation (small columns).
2. artifact re-validates after reload; digest changes on any tamper.
3. fitting shuffled training rows yields the identical digest/columns.
4. full batch, split batches, per-record HTTP and concurrent HTTP all equal
   the hand-computed oracle for the same records.
5. error records (bool, wrong type, non-finite, missing field, extra field,
   bad id, malformed JSON) get field-path errors and explicit status codes;
   a later legal request on the same server still succeeds (no contamination).
6. null / unseen category / literal "__UNKNOWN__" and "" strings are
   distinguishable through reserved vs real vocab codes.
7. all-missing column follows the deterministic frozen strategy on both paths.
8. processing a distribution-shifted file does not update the artifact:
   a shared record keeps identical features and the digest is unchanged.
9. a corrupted artifact refuses to serve (no ready port) and batch refuses it.
"""
from __future__ import annotations

import json
import math
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
DATA = RUNTIME / "data"
ART_DIR = RUNTIME / "artifacts"
OUT_DIR = RUNTIME / "out"
for d in (ART_DIR, OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

PY = str(ROOT / ".venv" / "bin" / "python")
PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []
_failed = False


def record(matrix_id: str, ok: bool, detail: str = "") -> None:
    global _failed
    results.append((matrix_id, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {matrix_id} {detail}")
    if not ok:
        _failed = True


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, timeout=60, **kw
    )


def fit(train: str, out: str, spec: str = "fixtures/spec.json") -> subprocess.CompletedProcess:
    return run([PY, "-m", "featurizer.fit", "--train", train, "--spec", spec, "--out", out])


def batch(artifact: str, inp: str, out: str) -> subprocess.CompletedProcess:
    return run([PY, "-m", "featurizer.batch", "--artifact", artifact, "--input", inp, "--output", out])


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerHandle:
    def __init__(self, artifact: str, port: int):
        self.proc = subprocess.Popen(
            [PY, "-u", "-m", "featurizer.serve", "--artifact", artifact, "--port", str(port)],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self._wait_ready()

    def _wait_ready(self, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        last_err: Exception | None = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"server exited early: {err}")
            try:
                with urllib.request.urlopen(f"{self.base}/health", timeout=1) as resp:
                    if resp.status == 200:
                        return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(0.1)
        raise RuntimeError(f"server not ready: {last_err}")

    def post(self, payload: dict | str) -> tuple[int, dict]:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        req = urllib.request.Request(
            f"{self.base}/transform",
            data=data.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


# ----------------------------------------------------------------------
# Independent oracle: hand-computed transform, no project code involved.
# ----------------------------------------------------------------------
def oracle(rows: list[dict], stats: dict, vocab: list[str]) -> dict[str, dict]:
    mean = {f: float(stats[f]["mean"]) for f in stats}
    std = {f: float(stats[f]["std"]) for f in stats}
    codes = {v: i + 2 for i, v in enumerate(vocab)}
    out: dict[str, dict] = {}
    for row in rows:
        feats: dict[str, object] = {}
        for f in ("amount", "constant"):
            v = row[f]
            if v is None:
                filled, missing = mean[f], 1
            else:
                filled, missing = float(v), 0
            feats[f"{f}_scaled"] = 0.0 if std[f] == 0.0 else (filled - mean[f]) / std[f]
            feats[f"{f}_missing"] = missing
        ch = row["channel"]
        if ch is None:
            code = 0
        else:
            code = codes.get(ch, 1)
        feats["channel_missing"] = 1 if code == 0 else 0
        feats["channel_known"] = 1 if code >= 2 else 0
        feats["channel_code"] = code
        out[row["record_id"]] = {
            k: feats[k]
            for k in [
                "amount_scaled",
                "amount_missing",
                "constant_scaled",
                "constant_missing",
                "channel_missing",
                "channel_known",
                "channel_code",
            ]
        }
    return out


def close_numeric(a: dict, b: dict) -> bool:
    if set(a) != set(b):
        return False
    for k in a:
        x, y = a[k], b[k]
        if isinstance(x, float) or isinstance(y, float):
            if not math.isclose(float(x), float(y), rel_tol=1e-12, abs_tol=1e-12):
                return False
        elif x != y:
            return False
    return True


def verify() -> None:
    # ---- M0 fit on shipped fixture ------------------------------------
    main_artifact = ART_DIR / "main.json"
    cp = fit("fixtures/train.jsonl", str(main_artifact))
    record("M0.1", cp.returncode == 0, cp.stdout.strip() or cp.stderr.strip())
    art = json.loads(main_artifact.read_text(encoding="utf-8"))
    digest_main = art["digest"]

    # ---- M1 hand-computed statistics ----------------------------------
    amount = [1, 3, 5, 7, 9]  # non-null training values
    hand_mean = sum(amount) / len(amount)
    hand_std = math.sqrt(sum((x - hand_mean) ** 2 for x in amount) / len(amount))
    ok = (
        math.isclose(art["numeric_stats"]["amount"]["mean"], hand_mean, abs_tol=1e-15)
        and math.isclose(art["numeric_stats"]["amount"]["std"], hand_std, abs_tol=1e-15)
        and art["numeric_stats"]["amount"]["observed_count"] == 5
        and art["numeric_stats"]["amount"]["missing_count"] == 1
        and art["numeric_stats"]["constant"]["mean"] == 5.0
        and art["numeric_stats"]["constant"]["std"] == 0.0
        and art["numeric_stats"]["constant"]["constant"] is True
    )
    record(
        "M1.1",
        ok,
        f"hand mean={hand_mean} std={hand_std:.15f} artifact="
        f"{art['numeric_stats']['amount']['mean']}/{art['numeric_stats']['amount']['std']:.15f}",
    )

    # vocabulary is sorted exact strings; reserved codes never collide
    ok = art["categorical"]["vocab"] == ["", "__UNKNOWN__", "store", "web"]
    ok &= art["categorical"]["codes"]["__UNKNOWN__"] == 3  # real trained string
    ok &= art["categorical"]["code_unknown"] == 1
    ok &= art["categorical"]["code_null"] == 0
    record("M1.2", ok, f"vocab={art['categorical']['vocab']}")

    # ---- M2 shuffled rows => identical artifact identity --------------
    shuffled_artifact = ART_DIR / "shuffled.json"
    cp = fit(str(DATA / "train_shuffled.jsonl"), str(shuffled_artifact))
    art2 = json.loads(shuffled_artifact.read_text(encoding="utf-8"))
    same = art2["digest"] == digest_main and art2["output_layout"] == art["output_layout"]
    # source path differs legitimately; compare everything else canonically
    a1 = {k: v for k, v in art.items() if k not in ("digest",)}
    a2 = {k: v for k, v in art2.items() if k not in ("digest",)}
    a1["training"] = {**a1["training"], "source": ""}
    a2["training"] = {**a2["training"], "source": ""}
    same &= json.dumps(a1, sort_keys=True) == json.dumps(a2, sort_keys=True)
    record("M2.1", same, f"digest identical across row order: {digest_main[:12]}…")

    # ---- M3 reload validation + tamper detection ----------------------
    cp = run([PY, "-c",
              "from featurizer.artifact import load_artifact; load_artifact(%r); print('ok')" % str(main_artifact)])
    record("M3.1", cp.returncode == 0 and "ok" in cp.stdout, "reload validation")

    tampered = ART_DIR / "tampered.json"
    bad = json.loads(main_artifact.read_text(encoding="utf-8"))
    bad["numeric_stats"]["amount"]["mean"] = 999.0
    tampered.write_text(json.dumps(bad), encoding="utf-8")
    cp = run([PY, "-c",
              "from featurizer.artifact import load_artifact; load_artifact(%r)" % str(tampered)])
    record("M3.2", cp.returncode != 0 and "digest mismatch" in cp.stderr, "tampered mean rejected")

    # ---- M4 happy-path matrix: oracle vs batch vs HTTP ----------------
    clean_rows = load_jsonl(DATA / "clean.jsonl")
    expected = oracle(
        clean_rows, art["numeric_stats"], art["categorical"]["vocab"]
    )

    # M4.1 full batch
    full_out = OUT_DIR / "clean.full.jsonl"
    cp = batch(str(main_artifact), str(DATA / "clean.jsonl"), str(full_out))
    full = {r["record_id"]: r for r in load_jsonl(full_out)}
    ok = cp.returncode == 0 and all(
        full[r]["artifact_digest"] == digest_main
        and full[r]["columns"] == art["output_layout"]["columns"]
        and close_numeric(full[r]["features"], expected[r])
        for r in expected
    )
    record("M4.1", ok, "full batch equals hand oracle")

    # M4.2 split batches (2+2+2 rows), concatenated must equal full batch
    split_paths = []
    for idx, start in enumerate(range(0, len(clean_rows), 2)):
        p = DATA / f"clean.part{idx}.jsonl"
        p.write_text(
            "".join(json.dumps(r) + "\n" for r in clean_rows[start:start + 2]),
            encoding="utf-8",
        )
        split_paths.append(p)
    merged: dict[str, dict] = {}
    for p in split_paths:
        o = OUT_DIR / f"{p.stem}.out.jsonl"
        cp = batch(str(main_artifact), str(p), str(o))
        if cp.returncode != 0:
            ok = False
            break
        for r in load_jsonl(o):
            merged[r["record_id"]] = r
    else:
        ok = set(merged) == set(full) and all(
            close_numeric(merged[r]["features"], full[r]["features"]) for r in full
        )
    record("M4.2", ok, "split batches concatenate to full batch")

    # M4.3 per-record HTTP, M4.4 concurrent HTTP
    server = ServerHandle(str(main_artifact), free_port())
    try:
        health_ok = False
        with urllib.request.urlopen(f"{server.base}/health") as resp:
            health = json.loads(resp.read())
            health_ok = health["status"] == "ready" and health["artifact_digest"] == digest_main
        record("M4.3a", health_ok, "GET /health reports frozen digest")

        sequential: dict[str, dict] = {}
        for row in clean_rows:
            status, body = server.post(row)
            ok_one = status == 200 and body["artifact_digest"] == digest_main
            ok_one &= body["columns"] == art["output_layout"]["columns"]
            ok_one &= body["record_id"] == row["record_id"]
            ok_one &= close_numeric(body["features"], expected[row["record_id"]])
            if not ok_one:
                record("M4.3b", False, f"record {row['record_id']} mismatch: {body}")
                break
            sequential[row["record_id"]] = body
        else:
            record("M4.3b", True, "per-record HTTP equals hand oracle")

        # concurrent requests with distinct payloads must not cross values
        barrier = threading.Barrier(len(clean_rows))
        concurrent: dict[str, dict] = {}
        errors: list[str] = []
        lock = threading.Lock()

        def worker(row: dict) -> None:
            barrier.wait()
            status, body = server.post(row)
            with lock:
                if status != 200:
                    errors.append(f"{row['record_id']} status {status}")
                    return
                if body["record_id"] != row["record_id"]:
                    errors.append("record_id swapped between concurrent requests")
                    return
                concurrent[row["record_id"]] = body

        threads = [threading.Thread(target=worker, args=(r,)) for r in clean_rows]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ok = not errors and set(concurrent) == set(expected) and all(
            close_numeric(concurrent[r]["features"], expected[r]) for r in expected
        )
        record("M4.4", ok, "; ".join(errors) or "concurrent HTTP isolated and correct")

        # ---- M5 error matrix over HTTP, then legal request still works --
        error_cases = [
            ("M5.1", {"record_id": "e1", "amount": True, "constant": 5, "channel": "web"},
             "amount", 400, "boolean"),
            ("M5.2", {"record_id": "e2", "amount": "3", "constant": 5, "channel": "web"},
             "amount", 400, "expected number"),
            ("M5.3", {"record_id": "e3", "amount": float("inf"), "constant": 5, "channel": "web"},
             "amount", 400, "non-finite"),
            ("M5.4", {"record_id": "e4", "amount": 1, "constant": 5},
             "channel", 400, "missing"),
            ("M5.5", {"record_id": "e5", "amount": 1, "constant": 5, "channel": "web", "sneaky": 1},
             "sneaky", 400, "extra"),
            ("M5.6", {"record_id": 7, "amount": 1, "constant": 5, "channel": "web"},
             "record_id", 400, "string"),
            ("M5.7", {"amount": 1, "constant": 5, "channel": "web"},
             "record_id", 400, "null"),
            ("M5.8", {"record_id": "e8", "amount": 1, "constant": None, "channel": 42},
             "channel", 400, "string or null"),
        ]
        for mid, payload, want_path, want_status, hint in error_cases:
            # NaN cannot survive JSON; emit it as a literal token instead.
            if hint == "non-finite":
                payload_text = '{"record_id":"e3","amount":Infinity,"constant":5,"channel":"web"}'
                status, body = server.post(payload_text)
            else:
                status, body = server.post(payload)
            ok = (
                status == want_status
                and body.get("error", {}).get("path") == want_path
                and hint.lower() in body.get("error", {}).get("message", "").lower()
            )
            record(mid, ok, f"status={status} path={body.get('error', {}).get('path')}")

        status, body = server.post("{not json")
        record("M5.9", status == 400 and body["error"]["path"] == "", f"malformed JSON -> {status}")
        status, body = server.post([1, 2, 3])
        record("M5.10", status == 400, f"non-object body -> {status}")

        # same errors through the batch path must also carry field paths
        bad_batch = DATA / "errors.jsonl"
        bad_batch.write_text(
            '{"record_id":"g1","amount":true,"constant":5,"channel":"web"}\n'
            '{"record_id":"g2","amount":1,"constant":5,"channel":"web"}\n',
            encoding="utf-8",
        )
        cp = batch(str(main_artifact), str(bad_batch), str(OUT_DIR / "errors.out.jsonl"))
        err = json.loads(cp.stderr.strip().splitlines()[-1])
        record(
            "M5.11",
            cp.returncode == 2 and err["error"]["path"] == "amount" and "row 1" in err["error"]["message"],
            "batch rejects before writing any output",
        )
        record("M5.12", not (OUT_DIR / "errors.out.jsonl").exists(), "no partial output file")

        # legal request after the error storm still succeeds
        status, body = server.post(clean_rows[0])
        record("M5.13", status == 200 and close_numeric(
            body["features"], expected["r1"]), "legal request after errors succeeds")

        # ---- M6 null / unseen / reserved-string distinction -------------
        probes = [
            ({"record_id": "p0", "amount": 2, "constant": 5, "channel": None},
             0, 0, 1),  # null: reserved code 0, missing=1, known=0
            ({"record_id": "p1", "amount": 2, "constant": 5, "channel": "partner"},
             1, 0, 0),  # unseen -> reserved code 1, known=0
            ({"record_id": "p2", "amount": 2, "constant": 5, "channel": "__UNKNOWN__"},
             3, 1, 0),  # real trained string, NOT the reserved unknown code
            ({"record_id": "p3", "amount": 2, "constant": 5, "channel": ""},
             2, 1, 0),  # empty string is a real category
        ]
        ok_all = True
        details = []
        for payload, want_code, want_known, want_missing in probes:
            status, body = server.post(payload)
            f = body["features"]
            good = (
                status == 200
                and f["channel_code"] == want_code
                and f["channel_known"] == want_known
                and f["channel_missing"] == want_missing
            )
            ok_all = ok_all and good
            details.append(f"{payload['channel']!r}->code{want_code}")
        record("M6.1", ok_all, "; ".join(details))
    finally:
        server.stop()

    # ---- M7 all-missing column strategy, both paths -------------------
    am_artifact = ART_DIR / "allmissing.json"
    cp = fit(
        str(DATA / "allmissing_train.jsonl"),
        str(am_artifact),
        spec=str(DATA / "allmissing_spec.json"),
    )
    am = json.loads(am_artifact.read_text(encoding="utf-8"))
    gap = am["numeric_stats"]["gap"]
    ok = cp.returncode == 0 and gap["all_missing"] is True and gap["mean"] == 0.0 and gap["std"] == 0.0
    record("M7.1", ok, f"gap stats: mean={gap['mean']} std={gap['std']} all_missing={gap['all_missing']}")

    am_out = OUT_DIR / "allmissing.out.jsonl"
    cp = batch(str(am_artifact), str(DATA / "allmissing_batch.jsonl"), str(am_out))
    am_rows = {r["record_id"]: r["features"] for r in load_jsonl(am_out)}
    # hand oracle for all-missing gap: fill 0, scaled 0, indicator 1
    hand_gap = {"gap_scaled": 0.0, "gap_missing": 1}
    ok = cp.returncode == 0 and all(
        am_rows[r]["gap_scaled"] == hand_gap["gap_scaled"]
        and am_rows[r]["gap_missing"] == hand_gap["gap_missing"]
        for r in am_rows
    )
    record("M7.2", ok, "batch all-missing column hand check")

    server = ServerHandle(str(am_artifact), free_port())
    try:
        status, body = server.post(
            {"record_id": "b2", "amount": None, "gap": None, "channel": "new"}
        )
        ok = (
            status == 200
            and body["features"]["gap_scaled"] == 0.0
            and body["features"]["gap_missing"] == 1
            and body["features"]["amount_missing"] == 1
            and body["features"]["channel_code"] == 1  # unseen in this vocab
        )
        record("M7.3", ok, f"HTTP all-missing + unseen: {body.get('features')}")
    finally:
        server.stop()

    # ---- M8 distribution shift must not update the artifact -----------
    digest_before = main_artifact.read_text(encoding="utf-8")
    shift_out = OUT_DIR / "shifted.jsonl"
    cp = batch(str(main_artifact), str(DATA / "shifted.jsonl"), str(shift_out))
    digest_after = main_artifact.read_text(encoding="utf-8")
    shifted = {r["record_id"]: r["features"] for r in load_jsonl(shift_out)}
    r1_before = full["r1"]["features"]
    ok = (
        cp.returncode == 0
        and digest_before == digest_after
        and close_numeric(shifted["r1"], r1_before)
        # extreme values still transform with ORIGINAL training mean/std
        and math.isclose(
            shifted["s1"]["amount_scaled"],
            (999.0 - hand_mean) / hand_std,
            rel_tol=1e-12,
        )
    )
    record("M8.1", ok, "artifact bytes unchanged; shared record identical; drift uses old stats")

    # refit on shifted data must produce a different digest (proof the test
    # has discriminating power, not a tautology)
    refit_artifact = ART_DIR / "refit.json"
    fit(str(DATA / "shifted.jsonl"), str(refit_artifact))
    refit_digest = json.loads(refit_artifact.read_text(encoding="utf-8"))["digest"]
    record("M8.2", refit_digest != digest_main, "a genuine refit does change the digest (control)")

    # ---- M9 corrupted artifact never serves ---------------------------
    corrupt_path = ART_DIR / "corrupt.json"
    corrupt_path.write_text("{ this is not valid json", encoding="utf-8")
    port = free_port()
    proc = subprocess.Popen(
        [PY, "-u", "-m", "featurizer.serve", "--artifact", str(corrupt_path), "--port", str(port)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    proc.wait(timeout=10)
    served = False
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
        served = True
    except Exception:  # noqa: BLE001
        pass
    err_text = proc.stderr.read()
    record(
        "M9.1",
        proc.returncode != 0 and not served and "refusing to start" in err_text,
        f"corrupt artifact exit={proc.returncode}, no port open",
    )
    cp = batch(str(corrupt_path), str(DATA / "clean.jsonl"), str(OUT_DIR / "x.jsonl"))
    record("M9.2", cp.returncode != 0, "batch refuses corrupt artifact too")

    # tampered-but-valid-JSON artifact also fails startup (digest guard)
    proc = subprocess.Popen(
        [PY, "-u", "-m", "featurizer.serve", "--artifact", str(tampered), "--port", str(free_port())],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    proc.wait(timeout=10)
    record(
        "M9.3",
        proc.returncode != 0 and "digest mismatch" in proc.stderr.read(),
        "tampered artifact refused",
    )

    # ---- summary -------------------------------------------------------
    total = len(results)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    print("\n=== verification matrix ===")
    for mid, status, detail in results:
        print(f"{status:4} {mid:7} {detail}")
    print(f"\n{total - failed}/{total} checks passed")
    if failed or _failed:
        sys.exit(1)


if __name__ == "__main__":
    verify()
