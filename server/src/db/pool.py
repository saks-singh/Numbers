"""Database connection handling.

Deliberately NOT psycopg_pool: requirements pin `psycopg[binary]`, not
`psycopg[pool]`, and a dashboard with a handful of internal users plus one
worker does not need a real pool. What it does need is for a connection
that died while Postgres was restarting to not be handed to a web request,
so this is a small LIFO cache of connections that are health-checked on
checkout and discarded rather than repaired on any doubt.

The DSN may legitimately be empty. `get_db_dsn()` returning None is a valid
outcome: libpq then reads PGHOST/PGUSER/PGDATABASE and ~/.pgpass, which is
strictly more secure than anything this process can do with a password in a
config file. So "no DSN" means "let libpq decide", not "fail".
"""

from __future__ import annotations

import logging
import queue
import threading
from contextlib import contextmanager

log = logging.getLogger(__name__)

MAX_IDLE = 4

_dsn = None
_dsn_set = False
_idle: queue.LifoQueue = queue.LifoQueue(maxsize=MAX_IDLE)
_lock = threading.Lock()


class DatabaseError(RuntimeError):
    """Raised for a failure this process can explain, rather than letting a
    psycopg exception reach a Flask traceback."""


def set_dsn(dsn) -> None:
    """Pin the DSN for this process. Called once at startup."""
    global _dsn, _dsn_set
    with _lock:
        _dsn = dsn or ""
        _dsn_set = True
    drain()


def resolve_dsn() -> str:
    """The DSN to connect with: whatever set_dsn pinned, else the credential
    manager, else empty (which hands the decision to libpq)."""
    global _dsn, _dsn_set
    if _dsn_set:
        return _dsn
    from ..utils.credential_manager import get_db_dsn

    with _lock:
        if not _dsn_set:
            _dsn = get_db_dsn() or ""
            _dsn_set = True
    return _dsn


def _psycopg():
    try:
        import psycopg
    except ImportError as e:  # pragma: no cover - depends on the host
        raise DatabaseError(
            "psycopg is not installed. `pip install -r requirements.txt` on "
            "the coordinator; the Windows agent deliberately needs none of it."
        ) from e
    return psycopg


def _connect():
    psycopg = _psycopg()
    try:
        # autocommit=False: every caller goes through transaction() or
        # commits explicitly, so a crash mid-ingest leaves nothing partial.
        return psycopg.connect(resolve_dsn(), autocommit=False)
    except Exception as e:
        raise DatabaseError(f"could not connect to Postgres: {e}") from e


def _healthy(conn) -> bool:
    """Cheap liveness check. A cached connection may have been closed by a
    Postgres restart, an idle timeout, or a network blip, and discovering
    that inside a web request is worse than paying for a SELECT 1."""
    if conn.closed:
        return False
    try:
        conn.rollback()  # clear any aborted transaction left by a caller
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        return False


def _discard(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


@contextmanager
def connection():
    """A connection, reused if one is cached and still alive.

    Returned to the cache only if it is not in a failed transaction state.
    A connection whose transaction blew up is closed instead of cached --
    recycling is cheap, and a poisoned connection is not worth saving.
    """
    conn = None
    while conn is None:
        try:
            candidate = _idle.get_nowait()
        except queue.Empty:
            break
        if _healthy(candidate):
            conn = candidate
        else:
            _discard(candidate)
    if conn is None:
        conn = _connect()

    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        _discard(conn)
        raise
    else:
        try:
            _idle.put_nowait(conn)
        except queue.Full:
            _discard(conn)


@contextmanager
def transaction():
    """A connection inside one transaction: commit on success, roll back on
    any exception. Ingestion runs entirely inside one of these, so a run is
    either fully recorded or not recorded at all."""
    with connection() as conn:
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


def drain() -> None:
    """Close every cached connection. For tests, and for a worker that has
    just been told the DSN changed."""
    while True:
        try:
            _discard(_idle.get_nowait())
        except queue.Empty:
            return


def check() -> dict:
    """Liveness probe for `/healthz`. Returns a dict rather than raising:
    the health endpoint must answer even when the database is down, since
    "the database is down" is the single most useful thing it can say."""
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), version()")
                database, version = cur.fetchone()
                cur.execute("SELECT count(*) FROM schema_migration")
                (migrations,) = cur.fetchone()
        return {"ok": True, "database": database,
                "server_version": version.split(",")[0],
                "migrations_applied": migrations}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
