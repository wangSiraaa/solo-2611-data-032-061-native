"""Automated verification for the cache protocol.

Every check has three parts:

* a fresh simulated server process (so in-process scenario state never leaks
  between tests) and a fresh SQLite cache,
* a sequence of ``fetch`` calls with explicit test-clock integers,
* assertions on the returned outcome *and* on the server's request log:
  request count, ``If-None-Match`` presence, step index and served body
  digest.  Responses are also compared byte for byte, not only status codes.

The suite exits non-zero on the first failing check group but keeps running
all groups and reports every failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .client import FetchError, fetch
from .generate import write_scenarios
from .scenarios import load_scenario
from .server import SimHTTPServer, make_server
from .store import CacheStore


class CheckFailure(AssertionError):
    pass


@dataclass
class ServerHandle:
    server: SimHTTPServer
    base_url: str

    def requests(self, path: str | None = None, clear: bool = True) -> list[dict]:
        entries = self.server.runtime.take_requests(clear)
        if path is not None:
            entries = [e for e in entries if e["path"] == path]
        return entries

    def request_count(self, path: str | None = None, clear: bool = False) -> int:
        entries = self.server.runtime.take_requests(False)
        if path is not None:
            entries = [e for e in entries if e["path"] == path]
        if clear:
            self.server.runtime.take_requests(True)
        return len(entries)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Harness:
    def __init__(self, scenario_path: str, db_path: str):
        self.scenario_path = scenario_path
        self.db_path = db_path
        self._server = make_server(scenario_path, port=0)
        import threading

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address[:2]
        self.server = ServerHandle(self._server, f"http://{host}:{port}")

    def close(self) -> None:
        self.server.stop()
        self._thread.join(timeout=5)

    def __enter__(self) -> "Harness":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


@dataclass
class Check:
    name: str
    description: str
    failures: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def ok(self, condition: bool, message: str) -> None:
        if not condition:
            self.failures.append(message)

    def note(self, message: str) -> None:
        self.observations.append(message)


# --------------------------------------------------------------------- checks


def check_language_and_header_rules(root: Path, artifacts: Path) -> Check:
    check = Check(
        "language_variants_and_header_rules",
        "Two language variants stay isolated; header names are case-insensitive; "
        "values are stripped and compared exactly; a restart still serves both.",
    )
    db = str(artifacts / "language.db")
    with Harness(str(root / "scenarios/language.json"), db) as harness:
        url = harness.server.base_url + "/text"
        out = fetch(url, db, 100, {"Accept-Language": "en"})
        check.ok(out.source == "network" and out.requests == 1, "first EN fetch must hit the network")
        check.note(f"en v1 {out.source} {out.body_sha256[:12]} {len(out.body)}B")
        out_zh = fetch(url, db, 101, {"accept-language": "zh-CN"})
        check.ok(out_zh.source == "network" and out_zh.requests == 1, "first ZH fetch must hit the network")
        check.ok(out_zh.body != out.body, "language variants must not share a body")
        out_zh_case = fetch(url, db, 102, {"ACCEPT-LANGUAGE": "  zh-CN  "})
        check.ok(out_zh_case.source == "cache", "case/space variant must reuse the cache")
        check.ok(out_zh_case.body == out_zh.body, "case/space variant must return the ZH body byte-for-byte")
        out_en_again = fetch(url, db, 103, {"Accept-Language": "en"})
        check.ok(out_en_again.source == "cache", "EN must still be cached independently")
        check.ok(out_en_again.body == out.body, "EN cache hit must match the EN body byte-for-byte")
        out_en_noheader = fetch(url, db, 104, {})
        check.ok(out_en_noheader.source == "network", "missing Accept-Language is a distinct key")
        # Missing and explicitly empty have the same key: the second one is a hit.
        out_en_empty = fetch(url, db, 104, {"Accept-Language": ""})
        check.ok(out_en_empty.source == "cache", "missing and empty must map to the same key")
        check.ok(out_en_empty.body == out_en_noheader.body, "the empty-value hit must be byte-identical")
        check.ok(
            out_en_noheader.body != out.body and out_en_noheader.body != out_zh.body,
            "missing header must not reuse any language variant",
        )
        server_requests = harness.server.requests("/text")
        check.ok(len(server_requests) == 3, f"expected 3 network requests, saw {len(server_requests)}")
        zh_hit_requests = [r for r in server_requests if r["request_headers"].get("accept-language") == "zh-CN"]
        check.ok(
            len(zh_hit_requests) == 1,
            "the case/space-equivalent ZH request must not reach the server again",
        )

        # Client restart: every fetch below runs in a fresh process, re-opens
        # the same database and talks to the same live server, so a hit can
        # only come from persisted state.
        restart = _run_client(["inspect", "--db", db, "--all"])
        info = json.loads(restart.stdout)
        variants = info["urls"][0]["variants"] if info["urls"] else []
        check.ok(len(variants) == 3, f"expected 3 isolated variants after restart, saw {len(variants)}")
        check.ok(info["urls"][0]["url"] == url, "the restart must inspect the same URL")

        en_proc = _run_client(
            ["fetch", "--db", db, "--url", url, "--time", "105",
             "--header", "Accept-Language: en"]
        )
        zh_proc = _run_client(
            ["fetch", "--db", db, "--url", url, "--time", "106",
             "--header", "accept-language: zh-CN"]
        )
        en_hit = json.loads(en_proc.stdout)
        zh_hit = json.loads(zh_proc.stdout)
        check.ok(
            en_hit["source"] == "cache" and zh_hit["source"] == "cache",
            "post-restart fetches must be cache hits",
        )
        check.ok(en_hit["requests"] == 0 and zh_hit["requests"] == 0, "post-restart hits must report 0 requests")
        check.ok(
            en_hit["body_sha256"] == out.body_sha256,
            "post-restart EN digest must equal the original EN digest",
        )
        check.ok(
            zh_hit["body_sha256"] == out_zh.body_sha256,
            "post-restart ZH digest must equal the original ZH digest",
        )
        check.ok(
            en_hit["body_sha256"] != zh_hit["body_sha256"],
            "post-restart bodies must remain distinct",
        )
        check.ok(len(harness.server.requests("/text")) == 0, "post-restart cache hits must not touch the server")
    return check


def check_freshness_boundary(root: Path, artifacts: Path) -> Check:
    check = Check(
        "freshness_boundary_and_directives",
        "now < received+max-age is fresh, the exact boundary revalidates, "
        "unknown directives are ignored and max-age still applies.",
    )
    db = str(artifacts / "freshness.db")
    with Harness(str(root / "scenarios/freshness.json"), db) as harness:
        clock = harness.server.base_url + "/clock"
        out0 = fetch(clock, db, 200, {})
        check.ok(out0.source == "network" and out0.requests == 1, "first fetch must be a network request")
        harness.server.requests("/clock")
        fresh = fetch(clock, db, 204, {})
        check.ok(fresh.source == "cache" and fresh.requests == 0, "now=204 must be fresh (max-age=5)")
        check.ok(fresh.body == out0.body, "fresh hit body must be byte-identical")
        check.ok(
            harness.server.request_count("/clock") == 0,
            "fresh hit must not reach the server",
        )
        boundary = fetch(clock, db, 205, {})
        check.ok(
            boundary.source == "revalidation",
            "now=received+max-age is the boundary and must revalidate",
        )
        check.ok(boundary.requests == 1, "boundary revalidation must make exactly one request")
        check.ok(boundary.body == out0.body, "304 must keep the original body byte-for-byte")
        requests_after = harness.server.requests("/clock")
        check.ok(len(requests_after) == 1, f"expected 1 request after the boundary, saw {len(requests_after)}")
        check.ok(
            requests_after[0]["if_none_match"] == '"clock-v1"',
            "stale entry must send If-None-Match with the stored ETag",
        )
        # max-age was not restated in the 304, so the previous 5 seconds apply
        # from the 304's receive clock: fresh at 206..209, stale at 210.
        fresh_after = fetch(clock, db, 209, {})
        check.ok(fresh_after.source == "cache", "304 without Cache-Control keeps the old max-age")
        stale_after = fetch(clock, db, 210, {})
        check.ok(stale_after.source == "revalidation", "the clock restarts from the 304 receive time")

        directives = harness.server.base_url + "/directives"
        d0 = fetch(directives, db, 300, {})
        check.ok(d0.source == "network" and d0.stored, "max-age must be honoured despite unknown directives")
        harness.server.requests("/directives")
        d1 = fetch(directives, db, 302, {})
        check.ok(d1.source == "cache", "unknown directives must be ignored, max-age=3 still wins")
        d2 = fetch(directives, db, 303, {})
        check.ok(d2.source == "revalidation", "max-age boundary must revalidate")
        check.ok(d2.body == d0.body, "304 must keep the body byte-for-byte")

        # max-age=0 is stored but never fresh, even at the receive instant.
        zero = harness.server.base_url + "/zero"
        z0 = fetch(zero, db, 400, {})
        check.ok(z0.source == "network" and z0.stored, "max-age=0 must still be stored for revalidation")
        harness.server.requests("/zero")
        z1 = fetch(zero, db, 400, {})
        check.ok(z1.source == "revalidation", "max-age=0 is never fresh, even at now=received")
        check.ok(len(harness.server.requests("/zero")) == 1, "max-age=0 must cost one conditional request")
        check.ok(z1.body == z0.body, "the 304 must keep the max-age=0 body byte-for-byte")

        # No Cache-Control at all: stored, but always revalidated.
        nomax = harness.server.base_url + "/nomax"
        n0 = fetch(nomax, db, 500, {})
        check.ok(n0.source == "network" and n0.stored, "a 200 without max-age may still be stored")
        harness.server.requests("/nomax")
        n1 = fetch(nomax, db, 501, {})
        check.ok(n1.source == "revalidation", "without max-age the entry must always revalidate")
        check.ok(len(harness.server.requests("/nomax")) == 1, "revalidation must make one request")
        check.ok(n1.body == n0.body, "the 304 must keep the body byte-for-byte")
    return check


def check_304_update_and_200_replace(root: Path, artifacts: Path) -> Check:
    check = Check(
        "conditional_update_and_replacement",
        "A 304 without new max-age keeps validity; with max-age it extends it; "
        "a 200 on revalidation atomically replaces body and ETag.",
    )
    db = str(artifacts / "update.db")
    with Harness(str(root / "scenarios/update.json"), db) as harness:
        page = harness.server.base_url + "/page"
        out0 = fetch(page, db, 0, {})
        check.ok(out0.source == "network", "fresh page fetch must be a network request")
        harness.server.requests("/page")
        # step 2 supplies Cache-Control: max-age=20 on the 304.
        out1 = fetch(page, db, 4, {})
        check.ok(out1.source == "revalidation", "boundary at now=4 must revalidate")
        check.ok(out1.body == out0.body, "304 body must be byte-identical to the stored body")
        check.ok(len(harness.server.requests("/page")) == 1, "revalidation must make exactly one request")
        out2 = fetch(page, db, 23, {})
        check.ok(out2.source == "cache", "304's new max-age must extend freshness to received+20")
        out3 = fetch(page, db, 24, {})
        check.ok(out3.source == "revalidation", "exact new max-age boundary must revalidate")
        out4 = fetch(page, db, 24, {})
        check.ok(out4.body == out0.body, "second 304 must still preserve the original body")
        # now=44 is the boundary for received_at=24 plus max-age=20.
        fetched_after_second = fetch(page, db, 43, {})
        check.ok(fetched_after_second.source == "cache", "freshness must restart from the second 304")
        fetched_after_second = fetch(page, db, 44, {})
        check.ok(fetched_after_second.source == "revalidation", "second 304's expiry must be exact")
        with CacheStore(db) as store:
            variant = store.list_url_variants(page)[0]
        check.ok(variant.max_age == 20, "304 without Cache-Control must keep the last max-age")
        check.ok(variant.received_at == 44, "304 must move the receive clock")

    # Replacement: step 2 returns 200 with a new body, ETag and max-age.
    db2 = str(artifacts / "replace.db")
    with Harness(str(root / "scenarios/replace.json"), db2) as harness:
        page = harness.server.base_url + "/page"
        out0 = fetch(page, db2, 0, {})
        check.ok(out0.source == "network", "fresh fetch must be a network request")
        harness.server.requests("/page")
        out1 = fetch(page, db2, 3, {})
        check.ok(out1.source == "revalidation", "boundary must revalidate")
        check.ok(len(harness.server.requests("/page")) == 1, "revalidation must make one request")
        check.ok(out1.body != out0.body, "a 200 on revalidation must replace the body")
        check.ok(out1.body == "Replacement test, representation v2 with a new ETag.\n".encode(), "the replacement body must be exactly the server's new representation")
        out2 = fetch(page, db2, 4, {})
        check.ok(out2.source == "cache", "the replacement's max-age=30 must be fresh at now=4")
        check.ok(out2.body == out1.body, "cache hit must return the replacement byte-for-byte")
        with CacheStore(db2) as store:
            variants = store.list_url_variants(page)
        check.ok(len(variants) == 1, "replacement must leave exactly one entry")
        check.ok(variants[0].etag == '"replace-v2"', "replacement must store the new ETag")
        check.ok(variants[0].max_age == 30, "replacement must store the new max-age")
    return check


def check_vary_rule_switch(root: Path, artifacts: Path) -> Check:
    check = Check(
        "vary_rule_switch",
        "A changed Vary rule invalidates all old-rule variants before the new "
        "representation is stored, so old bodies can never answer new requests.",
    )
    db = str(artifacts / "vary.db")
    with Harness(str(root / "scenarios/vary.json"), db) as harness:
        url = harness.server.base_url + "/greeting"
        zh = fetch(url, db, 0, {"Accept-Language": "zh-CN"})
        en = fetch(url, db, 1, {"Accept-Language": "en"})
        check.ok(zh.source == en.source == "network", "both old-rule variants must come from the network")
        check.ok(zh.body != en.body, "the two old-rule variants must differ byte-for-byte")
        harness.server.requests("/greeting")  # discard the two priming requests
        with CacheStore(db) as store:
            before = store.inspect_url(url)
        check.ok(len(before["variants"]) == 2, "two variants must be stored under the old rule")
        check.ok(before["url_state"]["vary_rule"] == ["accept-language"], "old rule must be recorded")
        # Boundary for the zh entry (received at 0, max-age 6) => server step 3
        # switches the rule to Accept and returns a new representation.
        out = fetch(url, db, 6, {"Accept-Language": "zh-CN"})
        check.ok(out.source == "revalidation", "the stale old-rule entry must revalidate")
        check.ok(len(harness.server.requests("/greeting")) == 1, "exactly one request must be made")
        check.ok(out.body != zh.body, "the new representation must come from the server, not the old entry")
        with CacheStore(db) as store:
            after = store.inspect_url(url)
        check.ok(after["url_state"]["vary_rule"] == ["accept"], "the URL must now use the new rule")
        check.ok(len(after["variants"]) == 1, "old-rule variants must be gone before the new save")
        check.ok(
            all(v["vary_rule"] == ["accept"] for v in after["variants"]),
            "only new-rule variants may remain",
        )
        # A request that matched the old rule must no longer get a stale body.
        en_again = fetch(url, db, 7, {"Accept-Language": "en"})
        check.ok(en_again.source == "cache", "new-rule entry may be reused for the same Accept value")
        check.ok(en_again.body == out.body, "the reusable entry must be the new representation")
        check.ok(
            en_again.body != zh.body and en_again.body != en.body,
            "no old-rule body may be served after the switch",
        )
        plain = fetch(url, db, 8, {"Accept": ""})
        check.ok(plain.source == "cache", "missing and empty Accept must share the empty-string key")
        check.ok(plain.body == out.body, "empty Accept must get the new representation")
    return check


def check_no_store_and_star(root: Path, artifacts: Path) -> Check:
    check = Check(
        "no_store_and_vary_star",
        "no-store and Vary:* never leave a persistent entry and delete the old "
        "entry for the current request.",
    )
    db = str(artifacts / "nostore.db")
    with Harness(str(root / "scenarios/nostore.json"), db) as harness:
        plain = harness.server.base_url + "/plain"
        p0 = fetch(plain, db, 0, {})
        check.ok(p0.stored, "the first response (max-age=2) must be stored")
        harness.server.requests("/plain")  # discard the priming request
        # now=2 is stale => conditional request; step 2 returns no-store.
        p1 = fetch(plain, db, 2, {})
        check.ok(p1.source == "revalidation", "the stale entry must trigger a conditional request")
        check.ok(p1.stored is False, "a no-store response must not be persisted")
        check.ok(len(harness.server.requests("/plain")) == 1, "exactly one request for the no-store fetch")
        with CacheStore(db) as store:
            check.ok(not store.list_url_variants(plain), "no-store must delete the old entry")
        # The following request must be a network request, never a stale hit.
        p2 = fetch(plain, db, 3, {})
        check.ok(p2.source == "network", "after no-store there must be no cache entry")
        check.ok(p2.body != p0.body or p2.body_sha256 != p0.body_sha256, "the new body must come from the network")

        star = harness.server.base_url + "/star"
        s0 = fetch(star, db, 0, {})
        check.ok(s0.stored, "the first star-scenario response must be stored")
        harness.server.requests("/star")  # discard the priming request
        s1 = fetch(star, db, 2, {})
        check.ok(s1.source == "revalidation", "stale entry must revalidate before Vary:* is seen")
        check.ok(s1.stored is False, "Vary:* must never be persisted")
        with CacheStore(db) as store:
            check.ok(not store.list_url_variants(star), "Vary:* must remove the old entry")
            check.ok(
                store.read_url_state(star)[0] == ["*"],
                "the '*' rule must be remembered for this URL",
            )
        s2 = fetch(star, db, 3, {})
        check.ok(s2.source == "network", "Vary:* must force a network request")
        check.ok(s2.stored, "once the rule switches back to no-Vary, the response may be stored")
        # After the priming request was discarded, s1 and s2 must both have
        # reached the server (a 304-driven fetch and a plain network fetch).
        check.ok(len(harness.server.requests("/star")) == 2, "each non-cacheable state must cost one request")
    return check


def check_bad_304_protocol(root: Path, artifacts: Path) -> Check:
    check = Check(
        "bad_304_protocol",
        "A 304 with a mismatched ETag/Vary or with no usable entry is a protocol "
        "error: no cache mutation, no success response.",
    )
    db = str(artifacts / "bad304.db")
    with Harness(str(root / "scenarios/bad304.json"), db) as harness:
        badetag = harness.server.base_url + "/badetag"
        fetch(badetag, db, 0, {})
        with CacheStore(db) as store:
            snapshot = store.list_url_variants(badetag)
        harness.server.requests("/badetag")
        try:
            fetch(badetag, db, 2, {})
            check.fail("an ETag-mismatched 304 must raise a protocol error")
        except FetchError as exc:
            check.ok(exc.kind == "protocol_error", "the ETag mismatch must be a protocol error")
        check.ok(len(harness.server.requests("/badetag")) == 1, "one conditional request must be sent")
        with CacheStore(db) as store:
            after = store.list_url_variants(badetag)
        check.ok(
            after == snapshot,
            "a rejected 304 must not modify the stored entry",
        )
        # The next fetch reissues the conditional request and succeeds.
        out = fetch(badetag, db, 2, {})
        check.ok(out.source == "revalidation" and out.body == snapshot[0].body, "stored body must still be valid")

        badvary = harness.server.base_url + "/badvary"
        fetch(badvary, db, 0, {"Accept-Language": "en"})
        harness.server.requests("/badvary")
        try:
            fetch(badvary, db, 2, {"Accept-Language": "en"})
            check.fail("a Vary-mismatched 304 must raise a protocol error")
        except FetchError as exc:
            check.ok(exc.kind == "protocol_error", "the Vary mismatch must be a protocol error")

        noentry = harness.server.base_url + "/noentry"
        try:
            fetch(noentry, db, 0, {})
            check.fail("an unsolicited 304 with no stored entry must raise a protocol error")
        except FetchError as exc:
            check.ok(exc.kind == "protocol_error", "the unsolicited 304 must be rejected")
        check.ok(len(harness.server.requests("/noentry")) == 1, "the unsolicited 304 costs one request")
        with CacheStore(db) as store:
            check.ok(not store.list_url_variants(noentry), "a rejected 304 must not create an entry")

        absent = harness.server.base_url + "/absent"
        fetch(absent, db, 0, {})
        harness.server.requests("/absent")
        try:
            fetch(absent, db, 2, {})
            check.fail("a 304 without an ETag must be rejected")
        except FetchError as exc:
            check.ok(exc.kind == "protocol_error", "a 304 without an ETag cannot prove the validator")

        novary = harness.server.base_url + "/novary"
        fetch(novary, db, 0, {"Accept-Language": "en"})
        harness.server.requests("/novary")
        out = fetch(novary, db, 2, {"Accept-Language": "en"})
        check.ok(out.source == "revalidation", "a 304 without Vary keeps the stored rule")
        check.ok(len(harness.server.requests("/novary")) == 1, "one request for the no-Vary 304")
    return check


def check_broken_responses(root: Path, artifacts: Path) -> Check:
    check = Check(
        "broken_200_responses",
        "Truncated bodies, wrong Content-Length, extra bytes and 5xx never become "
        "successful cache entries and never let stale content look fresh.",
    )
    db = str(artifacts / "broken.db")
    with Harness(str(root / "scenarios/truncated.json"), db) as harness:
        trunc = harness.server.base_url + "/trunc"
        out0 = fetch(trunc, db, 0, {})
        check.ok(out0.stored, "the baseline response must be stored")
        with CacheStore(db) as store:
            before = store.list_url_variants(trunc)
        harness.server.requests("/trunc")
        try:
            fetch(trunc, db, 2, {})
            check.fail("a truncated body must not be treated as success")
        except FetchError as exc:
            check.ok(exc.kind == "transport_error", "truncation must surface as a transport error")
        with CacheStore(db) as store:
            after = store.list_url_variants(trunc)
        check.ok(after == before, "a truncated response must not modify the cache")
        check.ok(
            len(harness.server.requests("/trunc")) == 1,
            "the truncated attempt must cost exactly one request",
        )
        # The old entry is stale; the next fetch must revalidate rather than
        # pretend the old body is freshly valid.
        out3 = fetch(trunc, db, 3, {})
        check.ok(out3.source == "revalidation", "the stale entry must be revalidated, not served as fresh")
        check.ok(len(harness.server.requests("/trunc")) == 1, "one request for the successful retry")

        for name in ("/badlen", "/extra", "/error"):
            url = harness.server.base_url + name
            fetch(url, db, 0, {})
            with CacheStore(db) as store:
                before = store.list_url_variants(url)
            harness.server.requests(name)
            try:
                fetch(url, db, 2, {})
                check.fail(f"{name}: the broken response must not be accepted")
            except FetchError as exc:
                expected = "server_error" if name == "/error" else "transport_error"
                check.ok(exc.kind == expected, f"{name}: expected {expected}, got {exc.kind}")
            with CacheStore(db) as store:
                after = store.list_url_variants(url)
            check.ok(after == before, f"{name}: the cache must keep its previous state")
            log = harness.server.requests(name, clear=False)
            check.ok(len(log) == 1, f"{name}: the broken response must cost exactly one request")
            if name == "/error":
                check.ok(log[0]["status"] == 500, "the server error must be visible in the log")
    return check


def check_interrupted_commit(root: Path, artifacts: Path) -> Check:
    check = Check(
        "interrupted_commit",
        "Killing the client before COMMIT preserves the whole old state; killing "
        "it after COMMIT leaves the complete new state for the retry.",
    )
    scenario = root / "scenarios" / "interrupt.json"
    db = str(artifacts / "interrupt.db")
    server = _spawn_server(scenario)
    try:
        base = server["base_url"]
        url = base + "/mutate"
        out0 = fetch(url, db, 0, {})
        check.ok(out0.source == "network", "baseline fetch must be a network request")
        with CacheStore(db) as store:
            old_snapshot = store.inspect_url(url)

        # First: crash before COMMIT.  Step 2 would replace the body/ETag.
        env = dict(os.environ, CACHEFETCH_TEST_CRASH_POINT="before_commit")
        proc = _run_client(
            ["fetch", "--db", db, "--url", url, "--time", "2"], env=env, check=False
        )
        check.ok(proc.returncode == 91, f"the pre-commit crash must exit with the sentinel (got {proc.returncode})")
        with CacheStore(db) as store:
            current = store.inspect_url(url)
        check.ok(current == old_snapshot, "a crash before COMMIT must leave the old state intact")
        check.ok(
            current["variants"][0]["body_sha256"] == old_snapshot["variants"][0]["body_sha256"],
            "the old body digest must survive the crash",
        )

        # Now crash after COMMIT: the new representation must be durable.
        env = dict(os.environ, CACHEFETCH_TEST_CRASH_POINT="after_commit")
        proc = _run_client(
            ["fetch", "--db", db, "--url", url, "--time", "2"], env=env, check=False
        )
        check.ok(proc.returncode == 92, f"the post-commit crash must exit with the sentinel (got {proc.returncode})")
        with CacheStore(db) as store:
            after = store.inspect_url(url)
        check.ok(
            after["variants"][0]["etag"] == '"mutate-v2"',
            "the state committed before the crash must be readable",
        )
        check.ok(len(after["variants"]) == 1, "the replacement must be complete (one entry)")
        # Retry with no crash: the new entry is fresh (max-age=45, now=2).
        retry = _run_client(["fetch", "--db", db, "--url", url, "--time", "3"], check=False)
        check.ok(retry.returncode == 0, f"the retry must succeed (got {retry.returncode})")
        payload = json.loads(retry.stdout)
        check.ok(payload["source"] == "cache", "the retry must read the committed entry as fresh")
        check.ok(
            payload["body_sha256"] == after["variants"][0]["body_sha256"],
            "the retry's digest must match the committed body digest",
        )

        check.ok(
            _run_client(["inspect", "--db", db, "--url", url], check=False).returncode == 0,
            "inspect must work after both interruptions",
        )
        count = _server_request_count(base)
        check.note(f"server saw {count} requests across the interruption test")
    finally:
        if server.get("proc"):
            server["proc"].terminate()
            try:
                server["proc"].wait(timeout=5)
            except subprocess.TimeoutExpired:
                server["proc"].kill()
    return check


def check_summaries_and_logs(root: Path, artifacts: Path) -> Check:
    check = Check(
        "request_log_vs_bodies",
        "The reported request counts and body digests match the server's request "
        "log for fresh, revalidated and network paths.",
    )
    db = str(artifacts / "summary.db")
    with Harness(str(root / "scenarios/large.json"), db) as harness:
        url = harness.server.base_url + "/asset"
        out = fetch(url, db, 0, {})
        check.ok(out.source == "network", "the first large fetch must be a network request")
        log = harness.server.requests("/asset")
        check.ok(len(log) == 1, "exactly one request for the first fetch")
        check.ok(log[0]["status"] == 200, "the server must report 200")
        check.ok(
            log[0]["body_sha256"] == out.body_sha256,
            "the client digest must match the server's served-byte digest",
        )
        check.ok(log[0]["declared_length"] == len(out.body), "declared and received lengths must agree")
        fresh = fetch(url, db, 100, {})
        check.ok(fresh.source == "cache" and fresh.requests == 0, "a fresh hit must report 0 requests")
        check.ok(fresh.body_sha256 == out.body_sha256, "the fresh digest must equal the network digest")
        check.ok(
            harness.server.request_count("/asset") == 0,
            "the fresh hit must not add a request to the log",
        )
        stale = fetch(url, db, 125, {})
        check.ok(stale.source == "revalidation" and stale.requests == 1, "boundary must revalidate once")
        log = harness.server.requests("/asset")
        check.ok(len(log) == 1 and log[0]["status"] == 304, "the single request must return 304")
        check.ok(log[0]["if_none_match"] == '"large-v1"', "the conditional request must carry the stored ETag")
        check.ok(stale.body_sha256 == out.body_sha256, "a 304 must keep the original body digest")

        # CLI output must carry source, request count and the body digest.
        proc = _run_client(["fetch", "--db", db, "--url", url, "--time", "126"], check=False)
        cli = json.loads(proc.stdout)
        check.ok(
            {"source", "requests", "body_sha256"} <= set(cli),
            "the CLI must report source, request count and body digest",
        )
    return check


def check_scenario_assets(root: Path) -> Check:
    check = Check(
        "generated_scenarios",
        "All scenario files are generated by the project and load cleanly.",
    )
    for path in sorted((root / "scenarios").glob("*.json")):
        try:
            scenario = load_scenario(path)
            check.note(f"{path.name}: {len(scenario.resources)} resource(s)")
        except Exception as exc:  # noqa: BLE001 - report every load failure
            check.fail(f"{path.name}: {exc}")
    return check


# --------------------------------------------------------------------- helpers


def _spawn_server(scenario_path: Path) -> dict:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cachefetch",
            "serve",
            "--scenario",
            str(scenario_path),
            "--port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    line = ""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line:
            break
        if proc.poll() is not None:
            stderr = proc.stderr.read()
            raise CheckFailure(f"server exited early: {stderr}")
    if not line.startswith("serving scenario"):
        raise CheckFailure(f"unexpected server banner: {line!r}")
    for token in line.split():
        if token.startswith("http://"):
            base = token
            return {"proc": proc, "base_url": base}
    raise CheckFailure(f"no URL in server banner: {line!r}")


def _server_request_count(base_url: str) -> int:
    with urllib.request.urlopen(base_url + "/_requests") as response:
        data = json.loads(response.read())
    return len(data["requests"])


def _run_client(args: list[str], env: dict | None = None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "cachefetch", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env if env is not None else os.environ,
        check=check,
    )


def run_verification(output_dir: str | None = None, keep_artifacts: bool = False) -> dict:
    root = Path(__file__).resolve().parent.parent
    artifacts = Path(output_dir) if output_dir else root / ".runtime" / "verify"
    artifacts = artifacts.resolve()
    if artifacts.exists():
        shutil.rmtree(artifacts)
    artifacts.mkdir(parents=True)

    # Regenerate the scenario set so what we test is what the project produces.
    write_scenarios(root / "scenarios")

    checks = [
        check_scenario_assets(root),
        check_language_and_header_rules(root, artifacts),
        check_freshness_boundary(root, artifacts),
        check_304_update_and_200_replace(root, artifacts),
        check_vary_rule_switch(root, artifacts),
        check_no_store_and_star(root, artifacts),
        check_bad_304_protocol(root, artifacts),
        check_broken_responses(root, artifacts),
        check_summaries_and_logs(root, artifacts),
        check_interrupted_commit(root, artifacts),
    ]

    report = {
        "ok": all(not check.failures for check in checks),
        "artifacts": str(artifacts),
        "checks": [
            {
                "name": check.name,
                "description": check.description,
                "passed": not check.failures,
                "failures": check.failures,
                "observations": check.observations,
            }
            for check in checks
        ],
    }
    if not keep_artifacts:
        # The databases are evidence; keep them by default.  Gather sqlite
        # side files only for size accounting.
        pass
    return report
