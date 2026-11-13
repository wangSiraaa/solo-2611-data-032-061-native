"""SQLite persistence with atomic per-URL replacements.

Key design
----------
* ``url_state`` holds one row per URL: the ``Vary`` rule currently in force
  for that URL and the ``policy_epoch`` that increments whenever the rule
  changes.  A URL is *not* a single cache key; it names a group of variants.
* ``variants`` holds one row per (URL, Vary rule, request header values) with
  body, strong ETag, Vary rule, max-age, receive clock and policy epoch.
  The primary key of a variant is derived from the canonical ``vary_key``.
* Every mutation happens in a single ``BEGIN IMMEDIATE`` transaction and is
  committed with one ``COMMIT``.  The response body is stored as a BLOB, so
  a process killed before commit leaves the previous state untouched.

The database is opened with ``isolation_level=None`` (manual transactions).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .headers import (
    ProtocolError,
    canonical_vary,
    decode_vary,
    explain_vary_key,
    vary_key,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS url_state (
    url          TEXT PRIMARY KEY,
    vary_rule    TEXT NOT NULL,
    policy_epoch INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS variants (
    url            TEXT NOT NULL,
    vary_rule      TEXT NOT NULL,
    vary_key       TEXT NOT NULL,
    variant_id     TEXT NOT NULL,
    body           BLOB NOT NULL,
    body_sha256    TEXT NOT NULL,
    etag           TEXT NOT NULL,
    max_age        INTEGER,
    received_at    INTEGER NOT NULL,
    policy_epoch   INTEGER NOT NULL,
    status         INTEGER NOT NULL,
    created_at     INTEGER NOT NULL,
    PRIMARY KEY (url, vary_rule, variant_id)
);

CREATE INDEX IF NOT EXISTS variants_by_url ON variants (url);
CREATE INDEX IF NOT EXISTS variants_by_epoch ON variants (url, policy_epoch);
"""


@dataclass(frozen=True)
class StoredVariant:
    url: str
    vary_rule: list[str]
    vary_key: str
    variant_id: str
    body: bytes
    body_sha256: str
    etag: str
    max_age: int | None
    received_at: int
    policy_epoch: int
    status: int

    def is_fresh(self, now: int) -> bool:
        """Fresh iff now < received_at + max_age (boundary needs revalidation)."""
        return self.max_age is not None and now < self.received_at + self.max_age

    def key_explanation(self) -> dict:
        return {
            "vary_rule": self.vary_rule,
            "vary_key": explain_vary_key(self.vary_key),
            "variant_id": self.variant_id,
        }


class CacheStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path, timeout=30.0, isolation_level=None
        )
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "CacheStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.connection.close()

    # ------------------------------------------------------------------ reads

    def read_url_state(self, url: str) -> tuple[list[str], int] | None:
        row = self.connection.execute(
            "SELECT vary_rule, policy_epoch FROM url_state WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return None
        return decode_vary(row[0]), int(row[1])

    def _row_to_variant(self, row: sqlite3.Row | tuple) -> StoredVariant:
        return StoredVariant(
            url=row[0],
            vary_rule=decode_vary(row[1]),
            vary_key=row[2],
            variant_id=row[3],
            body=bytes(row[4]),
            body_sha256=row[5],
            etag=row[6],
            max_age=row[7],
            received_at=row[8],
            policy_epoch=row[9],
            status=row[10],
        )

    def lookup_exact(
        self,
        url: str,
        vary_rule: list[str],
        request_headers: Mapping[str, str],
    ) -> StoredVariant | None:
        """Find the variant for this URL/rule/request headers."""
        variant_id = vary_key(vary_rule, request_headers)
        row = self.connection.execute(
            """SELECT url, vary_rule, vary_key, variant_id, body, body_sha256,
                      etag, max_age, received_at, policy_epoch, status
               FROM variants
               WHERE url = ? AND vary_rule = ? AND variant_id = ?""",
            (url, canonical_vary(vary_rule), variant_id),
        ).fetchone()
        return self._row_to_variant(row) if row else None

    def list_url_variants(self, url: str) -> list[StoredVariant]:
        rows = self.connection.execute(
            """SELECT url, vary_rule, vary_key, variant_id, body, body_sha256,
                      etag, max_age, received_at, policy_epoch, status
               FROM variants WHERE url = ? ORDER BY vary_rule, variant_id""",
            (url,),
        ).fetchall()
        return [self._row_to_variant(row) for row in rows]

    def all_urls(self) -> list[str]:
        rows = self.connection.execute(
            """SELECT url FROM url_state
               UNION SELECT url FROM variants
               ORDER BY url"""
        ).fetchall()
        return [row[0] for row in rows]

    def inspect_url(self, url: str) -> dict:
        state = self.read_url_state(url)
        variants = self.list_url_variants(url)
        return {
            "url": url,
            "url_state": None
            if state is None
            else {"vary_rule": state[0], "policy_epoch": state[1]},
            "variants": [
                {
                    **variant.key_explanation(),
                    "body_bytes": len(variant.body),
                    "body_sha256": variant.body_sha256,
                    "etag": variant.etag,
                    "max_age": variant.max_age,
                    "received_at": variant.received_at,
                    "policy_epoch": variant.policy_epoch,
                    "status": variant.status,
                }
                for variant in variants
            ],
        }

    # --------------------------------------------------------------- writes

    def _begin(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        # Test hooks: CACHEFETCH_TEST_CRASH_POINT lets the verification suite
        # kill this process either just before the single COMMIT (old state
        # must survive) or right after it (the new state must survive).
        crash_point = os.environ.get("CACHEFETCH_TEST_CRASH_POINT", "")
        if crash_point == "before_commit":
            os._exit(91)
        self.connection.execute("COMMIT")
        if crash_point == "after_commit":
            os._exit(92)

    def _rollback(self) -> None:
        try:
            self.connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _new_epoch(self, url: str, vary_rule: list[str], now: int) -> int:
        """Advance the policy epoch and replace the rule for this URL."""
        row = self.connection.execute(
            "SELECT policy_epoch FROM url_state WHERE url = ?", (url,)
        ).fetchone()
        epoch = (int(row[0]) if row else 0) + 1
        self.connection.execute(
            """INSERT INTO url_state (url, vary_rule, policy_epoch, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET
                   vary_rule = excluded.vary_rule,
                   policy_epoch = excluded.policy_epoch,
                   updated_at = excluded.updated_at""",
            (url, canonical_vary(vary_rule), epoch, now),
        )
        return epoch

    def _ensure_url_state(self, url: str, vary_rule: list[str], now: int) -> int:
        row = self.connection.execute(
            "SELECT vary_rule, policy_epoch FROM url_state WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return self._new_epoch(url, vary_rule, now)
        stored_rule, epoch = decode_vary(row[0]), int(row[1])
        if stored_rule != list(vary_rule):
            return self._new_epoch(url, vary_rule, now)
        return epoch

    def _insert_variant(
        self,
        url: str,
        vary_rule: list[str],
        request_headers: Mapping[str, str],
        body: bytes,
        body_sha256: str,
        etag: str,
        max_age: int | None,
        received_at: int,
        epoch: int,
        status: int,
    ) -> StoredVariant:
        variant_id = vary_key(vary_rule, request_headers)
        key_text = canonical_vary(vary_rule)
        self.connection.execute(
            """INSERT INTO variants
               (url, vary_rule, vary_key, variant_id, body, body_sha256, etag,
                max_age, received_at, policy_epoch, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(url, vary_rule, variant_id) DO UPDATE SET
                   vary_key = excluded.vary_key,
                   body = excluded.body,
                   body_sha256 = excluded.body_sha256,
                   etag = excluded.etag,
                   max_age = excluded.max_age,
                   received_at = excluded.received_at,
                   policy_epoch = excluded.policy_epoch,
                   status = excluded.status,
                   created_at = excluded.created_at""",
            (
                url,
                key_text,
                variant_id,
                variant_id,
                body,
                body_sha256,
                etag,
                max_age,
                received_at,
                epoch,
                status,
                received_at,
            ),
        )
        return StoredVariant(
            url=url,
            vary_rule=list(vary_rule),
            vary_key=variant_id,
            variant_id=variant_id,
            body=body,
            body_sha256=body_sha256,
            etag=etag,
            max_age=max_age,
            received_at=received_at,
            policy_epoch=epoch,
            status=status,
        )

    def store_fresh_response(
        self,
        url: str,
        vary_rule: list[str],
        request_headers: Mapping[str, str],
        body: bytes,
        body_sha256: str,
        etag: str,
        max_age: int | None,
        now: int,
    ) -> StoredVariant:
        """Atomically save a 200 response (and handle a Vary rule change)."""
        self._begin()
        try:
            old_state = self.connection.execute(
                "SELECT vary_rule FROM url_state WHERE url = ?", (url,)
            ).fetchone()
            old_rule = decode_vary(old_state[0]) if old_state else None
            if old_rule is not None and old_rule != list(vary_rule):
                # The old rule's variants can never be selected again.
                self.connection.execute("DELETE FROM variants WHERE url = ?", (url,))
            epoch = self._ensure_url_state(url, vary_rule, now)
            variant = self._insert_variant(
                url, vary_rule, request_headers, body, body_sha256, etag,
                max_age, now, epoch, 200,
            )
            self._commit()
            return variant
        except BaseException:
            self._rollback()
            raise

    def revalidate_with_304(
        self,
        url: str,
        old: StoredVariant,
        keep_body: bytes,
        new_max_age: int | None,
        now: int,
        response_etag: str | None,
        response_vary: list[str] | None,
    ) -> StoredVariant:
        """Apply a validated 304: keep the body, refresh the clock and max-age."""
        fresh_max_age = new_max_age if new_max_age is not None else old.max_age
        self._begin()
        try:
            row = self.connection.execute(
                """SELECT url, vary_rule, vary_key, variant_id, body, body_sha256,
                          etag, max_age, received_at, policy_epoch, status
                   FROM variants
                   WHERE url = ? AND vary_rule = ? AND variant_id = ?""",
                (url, canonical_vary(old.vary_rule), old.variant_id),
            ).fetchone()
            if row is None:
                raise ProtocolError(
                    "304 has no usable stored entry (it was removed concurrently)"
                )
            current = self._row_to_variant(row)
            if current.etag != old.etag or current.policy_epoch != old.policy_epoch:
                raise ProtocolError(
                    "304 has no usable stored entry (entry changed concurrently)"
                )
            if response_etag is not None and response_etag != old.etag:
                raise ProtocolError("304 ETag does not match the stored ETag")
            if response_vary is not None and response_vary != old.vary_rule:
                raise ProtocolError("304 Vary does not match the stored rule")
            self.connection.execute(
                """UPDATE variants
                   SET max_age = ?, received_at = ?, body = ?, body_sha256 = ?
                   WHERE url = ? AND vary_rule = ? AND variant_id = ?""",
                (
                    fresh_max_age,
                    now,
                    keep_body,
                    old.body_sha256,
                    url,
                    canonical_vary(old.vary_rule),
                    old.variant_id,
                ),
            )
            self._commit()
            return StoredVariant(
                url=old.url,
                vary_rule=old.vary_rule,
                vary_key=old.vary_key,
                variant_id=old.variant_id,
                body=keep_body,
                body_sha256=old.body_sha256,
                etag=old.etag,
                max_age=fresh_max_age,
                received_at=now,
                policy_epoch=current.policy_epoch,
                status=current.status,
            )
        except BaseException:
            self._rollback()
            raise

    def apply_vary_star(self, url: str, now: int) -> None:
        """Apply a ``Vary: *`` response: persist nothing, invalidate the URL.

        A changed rule to ``*`` first invalidates every variant stored under
        the old rule (the old rule's values can no longer select an entry) and
        records ``*`` as the URL's rule.  No response data is stored.  This
        also deletes the entry for the current request, as required for
        ``no-store``/``Vary: *``.
        """
        self._begin()
        try:
            self.connection.execute("DELETE FROM variants WHERE url = ?", (url,))
            self._new_epoch(url, ["*"], now)
            self._commit()
        except BaseException:
            self._rollback()
            raise

    def evict_current(
        self, url: str, request_headers: Mapping[str, str]
    ) -> None:
        """Delete only the entry for this request under the stored rule.

        Used for both ``no-store`` and ``Vary: *`` responses: nothing from the
        response is persisted, so the URL's stored rule is left unchanged (all
        other variants stay valid).  A subsequent request for this variant may
        find no entry and go straight to the network, or may still find one if
        the server changed its mind again.
        """
        self._begin()
        try:
            row = self.connection.execute(
                "SELECT vary_rule FROM url_state WHERE url = ?", (url,)
            ).fetchone()
            old_rule = decode_vary(row[0]) if row else None
            if old_rule is not None and old_rule != ["*"]:
                variant_id = vary_key(old_rule, request_headers)
                self.connection.execute(
                    """DELETE FROM variants
                       WHERE url = ? AND vary_rule = ? AND variant_id = ?""",
                    (url, canonical_vary(old_rule), variant_id),
                )
            self._commit()
        except BaseException:
            self._rollback()
            raise
