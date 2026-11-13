"""Shared helpers: JSON transport, crash injection, SQLite configuration, errors.

Every command in this demo speaks one line of JSON on stdout.  Diagnostics go
to stderr so that a parent process can always parse the reply unambiguously.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import typing


MAX_KEYS_PER_SIDE = 10
MAX_TXID_LEN = 128


class DemoError(Exception):
    """Protocol-level or validation failure that maps to a NO / error reply."""

    def __init__(self, message: str, code: str = "ERROR"):
        super().__init__(message)
        self.message = message
        self.code = code


class CrashInjected(BaseException):
    """Raised on purpose at a fault injection point.

    It derives from BaseException (not Exception) so no recovery-oriented
    ``except Exception`` accidentally swallows the simulated crash.
    """


def emit(obj: typing.Any) -> None:
    """Write one compact JSON object to stdout."""
    sys.stdout.write(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def info(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def parse_json(text: str) -> typing.Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DemoError(f"invalid JSON: {exc}") from exc


# --------------------------------------------------------------------------
# crash injection
# --------------------------------------------------------------------------

def crash_allowed(point: str) -> bool:
    """A crash point fires only when it was explicitly requested.

    ``--crash-at POINT`` is passed via argv to the worker process, and also
    honoured through the TWOPC_CRASH_AT environment variable so that the same
    code path can be exercised from a plain shell command.
    """
    requested = os.environ.get("TWOPC_CRASH_AT", "")
    wanted = {p for p in requested.split(",") if p}
    return point in wanted


def crash_if(point: str) -> None:
    if crash_allowed(point):
        info(f"[fault] exiting at crash point {point!r} without cleanup")
        # Simulate a hard process death: no unwinding, no graceful close.
        os._exit(70)


def crash_after(point: str) -> None:
    """Used after a database commit, before any reply is produced."""
    crash_if(point)


# --------------------------------------------------------------------------
# SQLite configuration
# --------------------------------------------------------------------------

# PRAGMAs applied on *every* connection.  The duplicates are intentional:
# SQLite silently ignores a scheduling pragma while a transaction is active,
# so setting them outside any transaction and again right after BEGIN makes
# the guarantee deterministic.
SQLITE_PRAGMAS = (
    "PRAGMA busy_timeout = 10000",      # wait for writer locks instead of failing
    "PRAGMA synchronous = FULL",        # fsync at commit and before WAL checkpoints
    "PRAGMA temp_store = MEMORY",
    "PRAGMA foreign_keys = ON",
    "PRAGMA trusted_schema = OFF",      # defensive schema parsing
)

JOURNAL_MODE = "WAL"


def configure_connection(conn: "sqlite3.Connection", *, read_only: bool = False) -> None:
    import sqlite3

    conn.row_factory = sqlite3.Row
    for pragma in SQLITE_PRAGMAS:
        conn.execute(pragma)
    if read_only:
        conn.execute("PRAGMA query_only = ON")


def open_database(path: str, *, read_only: bool = False) -> "sqlite3.Connection":
    import sqlite3

    # A read-only view must not create a database out of thin air.
    if read_only and not os.path.exists(path):
        raise DemoError(f"database not found: {path} (run the init command first)")
    # Note: the sqlite3 ``mode=ro`` URI flag cannot be used on a WAL database
    # whose -shm file is absent (the opener would need to create it).  Instead
    # open normally and apply PRAGMA query_only, which forbids writes to the
    # database while still allowing SQLite to build its shared-memory index.
    conn = sqlite3.connect(
        os.path.abspath(path), timeout=10.0, isolation_level=None
    )
    configure_connection(conn, read_only=read_only)
    return conn


def initialize_database(path: str) -> "sqlite3.Connection":
    """Create/upgrade an SQLite file and apply the WAL configuration."""
    import sqlite3

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(os.path.abspath(path), timeout=10.0, isolation_level=None)
    configure_connection(conn)
    # The journal mode is persistent in the file, but setting it requires no
    # active transaction.  WAL gives readers a consistent snapshot while a
    # writer commits, and synchronous=FULL keeps durability under WAL.
    conn.execute(f"PRAGMA journal_mode = {JOURNAL_MODE}")
    conn.execute(f"PRAGMA wal_autocheckpoint = 1000")
    conn.execute("PRAGMA journal_size_limit = 67108864")
    return conn


@contextlib.contextmanager
def write_transaction(conn: "sqlite3.Connection"):
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK around a block.

    IMMEDIATE takes the database write lock up front, so a read-then-write
    sequence cannot race.  rollback on any exception, including BaseException
    (crash injection of ordinary failures) is intentionally *not* done when a
    simulated crash fires: the simulated crash uses os._exit, which never
    unwinds, exactly like a real kill.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except CrashInjected:
        raise
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def stamp() -> str:
    """Millisecond UTC timestamp, used only for human-readable log columns."""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sqlite_config(path: str) -> dict:
    """Collect the effective persistent/connection settings for a file."""
    conn = open_database(path)
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        journal = row[0]
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        auto = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
        return {
            "path": path,
            "journal_mode": journal,
            "synchronous": {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}.get(sync, sync),
            "busy_timeout_ms": busy,
            "wal_autocheckpoint_pages": auto,
            "foreign_keys": bool(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
        }
    finally:
        conn.close()


def table_columns(conn: "sqlite3.Connection", table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def table_exists(conn: "sqlite3.Connection", table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None
