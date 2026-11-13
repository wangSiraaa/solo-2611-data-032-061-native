"""Simulated HTTP origin service driven by a JSON scenario.

The server answers GET for scenario resources, can emit 200/304/error or
deliberately truncated/mis-framed responses on successive requests, and
records every resource request (headers, status, served bytes digest) so
verification can inspect the actual wire traffic rather than only status
codes.

Scenario semantics (see also :mod:`cachefetch.scenarios`):

* Each resource has a current representation (content, ETag, Cache-Control,
  Vary rule, content type, extra headers) plus optional ``variants`` that
  override those fields when their ``match`` header values match.
* ``steps`` are consumed one per resource request in arrival order.  A
  persistent field in a step updates the representation for all later
  requests; a transient field (``conditional``, ``truncate``, ``error``,
  ``force_200``) affects only that one response.
* A conditional GET whose ``If-None-Match`` matches the current ETag gets a
  304 unless the step forces 200 or asks for a protocol violation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .headers import header_value, normalize_headers
from .scenarios import Scenario, load_scenario


VARY_HEADER_NAME = {"accept": "Accept", "accept-language": "Accept-Language"}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _raw_header_pairs(message) -> list[tuple[str, str]]:
    raw_items = getattr(message, "raw_items", None)
    if callable(raw_items):
        return [(str(name), str(value)) for name, value in raw_items()]
    return [(str(name), str(value)) for name, value in message.items()]


def _if_none_match_hits(value: str | None, etag: str | None) -> bool:
    if not value or etag is None:
        return False
    for token in value.split(","):
        token = token.strip(" \t")
        if token == "*" or token == etag:
            return True
    return False


def _vary_lines(rule: list[str]) -> list[tuple[str, str]]:
    if not rule:
        return []
    if rule == ["*"]:
        return [("Vary", "*")]
    return [
        ("Vary", ", ".join(VARY_HEADER_NAME.get(name, name) for name in rule))
    ]


class _Representation:
    """Effective representation after merging the base and a matched variant."""

    def __init__(self, base: dict, variant: dict | None):
        variant = variant or {}
        self.content: bytes = variant.get("content", base["content"])
        self.etag: str | None = variant.get("etag", base["etag"])
        self.cache_control: str | None = variant.get(
            "cache_control", base["cache_control"]
        )
        self.content_type: str = variant.get("content_type", base["content_type"])
        headers = dict(base.get("extra_headers", {}))
        headers.update(variant.get("headers", {}))
        self.extra_headers: dict[str, str] = headers


class ResourceState:
    def __init__(self, resource: dict):
        self.base = copy.deepcopy(resource["base"])
        self.variants = copy.deepcopy(resource["variants"])
        self.steps = resource["steps"]
        self.step_index = 0

    @property
    def vary_rule(self) -> list[str]:
        return list(self.base["vary_rule"])

    def consume_step(self) -> tuple[int | None, dict]:
        """Consume the next step; returns ``(index, step)`` (index None when done)."""
        if self.step_index >= len(self.steps):
            return None, {}
        index = self.step_index
        step = self.steps[index]
        self.step_index += 1
        self._apply(step)
        return index, step

    def _apply(self, step: dict) -> None:
        for field in ("content", "etag", "cache_control", "content_type"):
            if field in step:
                self.base[field] = step[field]
        if "headers" in step:
            self.base["extra_headers"] = dict(step["headers"])
        if "vary_rule" in step:
            self.base["vary_rule"] = list(step["vary_rule"])
            self.variants = []
        if "variants" in step:
            self.variants = copy.deepcopy(step["variants"])

    def select_variant(self, headers: dict[str, str]) -> dict | None:
        for variant in self.variants:
            match = variant.get("match", {})
            if all(
                header_value(headers, name) == value.strip(" \t")
                for name, value in match.items()
            ):
                return variant
        return None

    def representation(self, headers: dict[str, str]) -> _Representation:
        return _Representation(self.base, self.select_variant(headers))


class ScenarioServer:
    """Thread-safe runtime state for one scenario (also usable in-process)."""

    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self.lock = threading.Lock()
        self.resources = {
            path: ResourceState(resource)
            for path, resource in scenario.resources.items()
        }
        self.requests: list[dict] = []
        self._sequence = 0

    def record(self, entry: dict) -> None:
        with self.lock:
            self._sequence += 1
            entry = dict(entry)
            entry["seq"] = self._sequence
            self.requests.append(entry)

    def take_requests(self, clear: bool) -> list[dict]:
        with self.lock:
            entries = list(self.requests)
            if clear:
                self.requests = []
            return entries

    def prepare(self, path: str, headers: dict[str, str]):
        """Return ``(step_index, step, representation, vary_rule)``."""
        resource = self.resources[path]
        step_index, step = resource.consume_step()
        representation = resource.representation(headers)
        return step_index, step, representation, resource.vary_rule


_REASONS = {
    200: "OK",
    304: "Not Modified",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CacheFetchSim/1.0"
    sys_version = ""

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def _write_head(
        self, status: int, headers: list[tuple[str, str]], length: int | None
    ) -> None:
        lines = [f"HTTP/1.1 {status} {_REASONS.get(status, 'Unknown')}"]
        for name, value in headers:
            lines.append(f"{name}: {value}")
        if length is not None:
            lines.append(f"Content-Length: {length}")
        lines.append("Connection: close")
        self.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1"))

    def _send(
        self,
        status: int,
        headers: list[tuple[str, str]],
        body: bytes,
        *,
        declared_length: int | None = None,
        truncate: int | None = None,
        extra_body: int = 0,
    ) -> None:
        length = len(body) if declared_length is None else declared_length
        self._write_head(status, headers, length)
        payload = body[:truncate] if truncate is not None else body
        try:
            self.wfile.write(payload)
            if extra_body:
                self.wfile.write(b"X" * extra_body)
            self.wfile.flush()
        except OSError:
            pass

    def _method_not_allowed(self) -> None:
        body = b"only GET is supported\n"
        self._send(405, [("Allow", "GET"), ("Content-Type", "text/plain")], body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.close_connection = True
        self._write_head(405, [("Allow", "GET"), ("Content-Type", "text/plain")], 0)

    def do_POST(self) -> None:  # noqa: N802
        self.close_connection = True
        self._method_not_allowed()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST

    def do_GET(self) -> None:  # noqa: N802
        self.close_connection = True
        parsed = urlsplit(self.path)
        path = parsed.path
        runtime: ScenarioServer = self.server.runtime  # type: ignore[attr-defined]
        raw_pairs = _raw_header_pairs(self.headers)
        headers = normalize_headers(raw_pairs)
        if path == "/_health":
            self._send(200, [("Content-Type", "application/json")], b'{"ok":true}\n')
            return
        if path == "/_requests":
            entries = runtime.take_requests("clear" in parse_qs(parsed.query))
            self._send(
                200,
                [("Content-Type", "application/json")],
                _json_bytes({"requests": entries}),
            )
            return

        if path not in runtime.resources:
            body = b"not found\n"
            self._send(404, [("Content-Type", "text/plain")], body)
            runtime.record(
                {
                    "path": self.path,
                    "method": "GET",
                    "request_headers": headers,
                    "status": 404,
                    "body_sha256": sha256_hex(body),
                }
            )
            return

        with runtime.lock:
            step_index, step, representation, vary_rule = runtime.prepare(path, headers)

        record = {
            "path": self.path,
            "method": "GET",
            "request_headers": headers,
            "request_headers_raw": raw_pairs,
            "if_none_match": headers.get("if-none-match"),
            "step_index": step_index,
            "vary_rule": vary_rule,
            "server_etag": representation.etag,
        }

        if "error" in step:
            status = step["error"]
            body = step.get("error_body", f"simulated error {status}\n".encode())
            self._send(status, [("Content-Type", "text/plain; charset=utf-8")], body)
            runtime.record(
                {
                    **record,
                    "status": status,
                    "body_sha256": sha256_hex(body),
                    "served_bytes": len(body),
                }
            )
            return

        conditional_hit = _if_none_match_hits(record["if_none_match"], representation.etag)
        mode = step.get("conditional")
        force_200 = step.get("force_200", False)

        if mode == "no_entry":
            self._emit_304(runtime, record, representation, vary_rule, step, forced=True)
            return
        if conditional_hit and not force_200:
            self._emit_304(runtime, record, representation, vary_rule, step)
            return
        if mode == "raw_bad_length" or mode == "raw_extra_body":
            self._emit_200(runtime, record, representation, vary_rule, step, mode)
            return
        self._emit_200(runtime, record, representation, vary_rule, step, None)

    def _emit_200(
        self,
        runtime: ScenarioServer,
        record: dict,
        representation: _Representation,
        vary_rule: list[str],
        step: dict,
        mode: str | None,
    ) -> None:
        headers: list[tuple[str, str]] = [
            ("Content-Type", representation.content_type)
        ]
        if representation.etag is not None:
            headers.append(("ETag", representation.etag))
        if representation.cache_control is not None:
            headers.append(("Cache-Control", representation.cache_control))
        headers.extend(_vary_lines(vary_rule))
        for name, value in representation.extra_headers.items():
            headers.append((name, value))

        body = representation.content
        declared: int | None = None
        extra = 0
        if mode == "raw_bad_length":
            declared = max(0, len(body) - 10)
        elif mode == "raw_extra_body":
            extra = 7
        truncate = step.get("truncate")

        self._send(
            200,
            headers,
            body,
            declared_length=declared,
            truncate=truncate,
            extra_body=extra,
        )
        served = body[:truncate] if truncate is not None else body
        runtime.record(
            {
                **record,
                "status": 200,
                "body_sha256": sha256_hex(served),
                "body_length": len(served),
                "served_bytes": len(served) + extra,
                "declared_length": declared if declared is not None else len(body),
                "truncated": truncate is not None,
                "extra_body": extra,
                "cache_control_header": representation.cache_control,
            }
        )

    def _emit_304(
        self,
        runtime: ScenarioServer,
        record: dict,
        representation: _Representation,
        vary_rule: list[str],
        step: dict,
        forced: bool = False,
    ) -> None:
        mode = step.get("conditional")
        headers: list[tuple[str, str]] = []
        etag = representation.etag
        if mode == "etag_absent":
            etag = None
        elif mode == "etag_mismatch":
            etag = '"mismatched-v1"'
        if etag is not None:
            headers.append(("ETag", etag))

        if mode == "no_vary":
            pass
        elif mode == "vary_mismatch":
            headers.append(("Vary", "Accept"))
        else:
            headers.extend(_vary_lines(vary_rule))

        if "cache_control" in step and step["cache_control"] is not None:
            headers.append(("Cache-Control", step["cache_control"]))
        self._send(304, headers, b"")
        runtime.record(
            {
                **record,
                "status": 304,
                "forced": forced,
                "conditional_mode": mode,
                "response_etag": etag,
                "response_headers": dict(headers),
                "served_bytes": 0,
            }
        )


class SimHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, scenario: Scenario):
        super().__init__(address, _Handler)
        self.runtime = ScenarioServer(scenario)


def make_server(scenario_path: str, host: str = "127.0.0.1", port: int = 0) -> SimHTTPServer:
    return SimHTTPServer((host, port), load_scenario(scenario_path))


def serve_forever(scenario_path: str, host: str = "127.0.0.1", port: int = 0) -> None:
    server = make_server(scenario_path, host, port)
    bound_host, bound_port = server.server_address[:2]
    print(
        f"serving scenario {server.runtime.scenario.name} "
        f"on http://{bound_host}:{bound_port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
