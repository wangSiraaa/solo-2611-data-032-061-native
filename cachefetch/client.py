"""Cache-aware fetch logic for the limited protocol.

Flow for one ``GET``:

1. Read the URL's current Vary rule from SQLite.  If a variant exists for
   this request under that rule and is fresh (``now < received_at + max_age``),
   return it without touching the network.
2. If the variant exists but is stale, or has no ``max-age``, send a
   conditional GET with ``If-None-Match: <stored ETag>``.  A 304 keeps
   the stored body and refreshes the clock/validity; a 200 replaces it.
3. Otherwise issue a plain GET.

Protocol errors (bad framing, a 304 without a usable entry, a 304 validator
mismatch, a malformed Cache-Control) abort the operation *without* mutating
the cache and are reported to the caller.  Server errors never produce a
cache entry and never let stale content masquerade as a fresh success.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from .headers import (
    ProtocolError,
    is_strong_etag,
    normalize_headers,
    parse_cache_control,
    parse_vary,
    strip_ows,
)
from .store import CacheStore, StoredVariant
from .wire import HTTPResponse, ShortResponse, WireError, http_exchange


class FetchError(Exception):
    """Fetch failed; the cache was not successfully updated."""

    def __init__(self, message: str, kind: str = "protocol_error", cause: BaseException | None = None):
        super().__init__(message)
        self.kind = kind
        self.cause = cause


@dataclass
class FetchOutcome:
    url: str
    now: int
    source: str  # "network" | "cache" | "revalidation"
    requests: int
    network_status: int | None
    status: int  # effective status (200 for a served/revalidated body)
    body: bytes
    body_sha256: str
    stored: bool
    cache_variant: dict | None
    request_headers: dict[str, str]
    response_headers: dict[str, list[str]]
    note: str | None = None

    def as_json(self) -> dict:
        return {
            "url": self.url,
            "now": self.now,
            "source": self.source,
            "requests": self.requests,
            "network_status": self.network_status,
            "status": self.status,
            "body_bytes": len(self.body),
            "body_sha256": self.body_sha256,
            "stored": self.stored,
            "cache_variant": self.cache_variant,
            "request_headers": self.request_headers,
            "response_headers": self.response_headers,
            "note": self.note,
        }


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def headers_map(lines: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, value in lines:
        out.setdefault(name.lower(), []).append(value)
    return out


def parse_header_option(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise ValueError(f"header must be 'Name: Value': {text!r}")
    name, value = text.split(":", 1)
    name = strip_ows(name)
    if not name or any(ord(ch) < 33 for ch in name) or any(ch in name for ch in "()<>@,;\\\"/[]?={}"):
        raise ValueError(f"invalid header name: {name!r}")
    return name, strip_ows(value)


def split_url(url: str) -> tuple[str, int, str]:
    """Return ``(host, port, request_target)`` for http URLs only."""
    parsed = urlsplit(url)
    if parsed.scheme != "http":
        raise FetchError(f"only plain http URLs are supported: {url}", kind="usage_error")
    if not parsed.hostname:
        raise FetchError(f"URL has no host: {url}", kind="usage_error")
    port = parsed.port or 80
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return parsed.hostname, port, target


def normalize_request_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Lowercase names, strip OWS from values; missing == empty by omission."""
    if not headers:
        return {}
    return normalize_headers(headers.items())


def _network_headers(
    url: str, headers: Mapping[str, str], validator: str | None
) -> list[tuple[str, str]]:
    parsed = urlsplit(url)
    lines: list[tuple[str, str]] = [
        ("Host", parsed.hostname or ""),
        ("Accept-Encoding", "identity"),
        ("Connection", "close"),
    ]
    if validator is not None:
        lines.append(("If-None-Match", validator))
    reserved = {"host", "connection", "accept-encoding", "if-none-match"}
    for name, value in headers.items():
        if name not in reserved:
            lines.append((name, value))
    return lines


def fetch(
    url: str,
    db_path: str,
    now: int,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
) -> FetchOutcome:
    headers = normalize_request_headers(headers)
    host, port, target = split_url(url)
    with CacheStore(db_path) as store:
        state = store.read_url_state(url)
        stored: StoredVariant | None = None
        if state is not None:
            rule, _epoch = state
            if rule != ["*"]:
                stored = store.lookup_exact(url, rule, headers)

        if stored is not None and stored.is_fresh(now):
            return FetchOutcome(
                url=url,
                now=now,
                source="cache",
                requests=0,
                network_status=None,
                status=200,
                body=stored.body,
                body_sha256=stored.body_sha256,
                stored=True,
                cache_variant=stored.key_explanation(),
                request_headers=dict(headers),
                response_headers={},
                note=f"fresh until {stored.received_at + (stored.max_age or 0)}",
            )

        validator = stored.etag if stored is not None else None
        request_headers = _network_headers(url, headers, validator)
        try:
            response = http_exchange(host, port, target, request_headers, timeout)
        except (ShortResponse, WireError, ProtocolError) as exc:
            raise FetchError(f"{exc}", kind="transport_error", cause=exc) from exc

        if response.status == 304:
            if stored is None:
                raise FetchError(
                    "received 304 but there is no usable stored entry for this URL/variant",
                    kind="protocol_error",
                )
            try:
                return _apply_304(store, url, now, headers, stored, response)
            except ProtocolError as exc:
                raise FetchError(str(exc), kind="protocol_error", cause=exc) from exc
        if response.status == 200:
            try:
                return _apply_200(
                    store, url, now, headers, response, revalidated=stored is not None
                )
            except ProtocolError as exc:
                raise FetchError(str(exc), kind="protocol_error", cause=exc) from exc
        if 500 <= response.status <= 599:
            raise FetchError(
                f"server returned {response.status}; no cache entry was written",
                kind="server_error",
            )
        raise FetchError(
            f"unexpected status {response.status}; only GET 200/304 are supported",
            kind="protocol_error",
        )


def _apply_304(
    store: CacheStore,
    url: str,
    now: int,
    headers: Mapping[str, str],
    stored: StoredVariant,
    response: HTTPResponse,
) -> FetchOutcome:
    response_etag = response.etag
    if response_etag is None:
        raise FetchError(
            "304 response did not include an ETag; the stored validator is required",
            kind="protocol_error",
        )
    if response_etag != stored.etag:
        raise FetchError(
            f"304 ETag {response_etag!r} does not match the stored ETag {stored.etag!r}",
            kind="protocol_error",
        )
    vary_lines = response.header_lines("Vary")
    parsed_vary = parse_vary(vary_lines) if vary_lines else None
    if parsed_vary is not None and parsed_vary != stored.vary_rule:
        raise FetchError(
            f"304 Vary {parsed_vary} does not match the stored rule {stored.vary_rule}",
            kind="protocol_error",
        )
    no_store = False
    new_max_age: int | None = None
    cache_control_lines = response.header_lines("Cache-Control")
    if cache_control_lines:
        no_store, new_max_age = parse_cache_control(cache_control_lines)

    if no_store:
        store.evict_current(url, headers)
        return FetchOutcome(
            url=url,
            now=now,
            source="revalidation",
            requests=1,
            network_status=304,
            status=200,
            body=stored.body,
            body_sha256=stored.body_sha256,
            stored=False,
            cache_variant=stored.key_explanation(),
            request_headers=dict(headers),
            response_headers=headers_map(response.headers),
            note="304 with no-store removed the stored entry",
        )

    updated = store.revalidate_with_304(
        url,
        stored,
        stored.body,
        new_max_age,
        now,
        response_etag,
        parsed_vary,
    )
    return FetchOutcome(
        url=url,
        now=now,
        source="revalidation",
        requests=1,
        network_status=304,
        status=200,
        body=updated.body,
        body_sha256=updated.body_sha256,
        stored=True,
        cache_variant=updated.key_explanation(),
        request_headers=dict(headers),
        response_headers=headers_map(response.headers),
        note=(
            "304 kept the stored body and updated max-age"
            if new_max_age is not None
            else "304 kept the stored body and the previous max-age"
        ),
    )


def _apply_200(
    store: CacheStore,
    url: str,
    now: int,
    headers: Mapping[str, str],
    response: HTTPResponse,
    revalidated: bool,
) -> FetchOutcome:
    try:
        response_vary = parse_vary(response.header_lines("Vary") or None)
    except ProtocolError as exc:
        raise FetchError(str(exc), kind="protocol_error") from exc
    try:
        no_store, max_age = parse_cache_control(
            response.header_lines("Cache-Control") or None
        )
    except ProtocolError as exc:
        raise FetchError(str(exc), kind="protocol_error") from exc

    etag = response.etag
    body = response.body
    if not is_strong_etag(etag):
        # The limited protocol requires a strong ETag on every cacheable
        # response.  A 200 without one is still a successful GET for this
        # request, but it cannot be validated later, so it is not persisted;
        # any old entry for this request is dropped so it can never be
        # mistaken for the new representation.
        store.evict_current(url, headers)
        return FetchOutcome(
            url=url,
            now=now,
            source="revalidation" if revalidated else "network",
            requests=1,
            network_status=200,
            status=200,
            body=body,
            body_sha256=digest(body),
            stored=False,
            cache_variant=None,
            request_headers=dict(headers),
            response_headers=headers_map(response.headers),
            note="200 response has no strong ETag: not persisted",
        )

    cacheable = not no_store and response_vary != ["*"]
    if not cacheable:
        if response_vary == ["*"]:
            # A Vary-* response invalidates the old rule's variants before
            # nothing is stored; the URL records the '*' rule.
            store.apply_vary_star(url, now)
            reason = "Vary '*'"
        else:
            # no-store persists nothing and removes only this request's
            # old entry; other variants of the same URL stay valid.
            store.evict_current(url, headers)
            reason = "no-store"
        return FetchOutcome(
            url=url,
            now=now,
            source="revalidation" if revalidated else "network",
            requests=1,
            network_status=200,
            status=200,
            body=body,
            body_sha256=digest(body),
            stored=False,
            cache_variant=None,
            request_headers=dict(headers),
            response_headers=headers_map(response.headers),
            note=f"{reason}: response not persisted and the old entry was removed",
        )

    variant = store.store_fresh_response(
        url=url,
        vary_rule=response_vary,
        request_headers=headers,
        body=body,
        body_sha256=digest(body),
        etag=etag,
        max_age=max_age,
        now=now,
    )
    return FetchOutcome(
        url=url,
        now=now,
        source="revalidation" if revalidated else "network",
        requests=1,
        network_status=200,
        status=200,
        body=body,
        body_sha256=variant.body_sha256,
        stored=True,
        cache_variant=variant.key_explanation(),
        request_headers=dict(headers),
        response_headers=headers_map(response.headers),
        note=(
            None
            if max_age is not None
            else "no max-age: the entry will require revalidation on the next use"
        ),
    )
