"""Apply migrations and report what is applied.

`main.py initdb` is expected to be run repeatedly -- on a fresh database,
after a pull, and by someone who isn't sure whether they already ran it.
So it has to be idempotent in two independent ways:

  1. Bookkeeping: a migration recorded in schema_migration is skipped.
  2. The DDL itself: every statement in 001_init is CREATE ... IF NOT
     EXISTS or a DO block that swallows duplicate_object, so re-running it
     against a populated database is a no-op even if the bookkeeping row is
     missing. Belt and braces, because the bookkeeping table is itself
     created by the migration it would have to record.

A migration's sha256 is stored. Editing a file that has already been
applied somewhere is the mistake this catches: the edit silently does not
reach any database that already ran it, and that divergence is invisible
until a query fails in production. initdb warns rather than refuses --
during development, before the file has reached a real server, editing it
is perfectly reasonable.
"""

from __future__ import annotations

import hashlib
import logging
import re

from .schema import migration_files

log = logging.getLogger(__name__)

# A name that sorts the way a human expects. Lexical order is the apply
# order, so an unpadded prefix ('9_x' sorting after '10_x') would apply
# migrations out of sequence.
MIGRATION_NAME_RE = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    pass


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_names(paths=None) -> list:
    """Reject anything that would apply out of order. Returns the problems
    rather than raising, so initdb can list all of them at once."""
    paths = migration_files() if paths is None else paths
    return [f"{p.name}: must match {MIGRATION_NAME_RE.pattern}"
            for p in paths if not MIGRATION_NAME_RE.match(p.name)]


def applied(conn) -> dict:
    """{name: sha256} already recorded, or {} if the bookkeeping table does
    not exist yet (a fresh database, which is not an error).

    The rollback matters: in Postgres a failed statement aborts the whole
    transaction, so without it every subsequent statement on this
    connection would fail with "current transaction is aborted".
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, sha256 FROM schema_migration")
            return {name: sha for name, sha in cur.fetchall()}
    except Exception:
        conn.rollback()
        return {}


def apply_migrations(conn, dry_run=False) -> list:
    """Apply every pending migration, newest last. Returns an action log of
    (name, action, detail) tuples for the caller to print.

    Each migration runs in its own transaction: a failure leaves the
    migrations before it applied and recorded, which is what you want when
    the failure is in the one you just wrote.
    """
    paths = migration_files()
    problems = validate_names(paths)
    if problems:
        raise MigrationError("; ".join(problems))
    if not paths:
        raise MigrationError(
            "no migrations found -- src/db/migrations/ is empty or missing")

    already = applied(conn)
    actions = []

    for path in paths:
        sql = path.read_text(encoding="utf-8")
        digest = _sha256(sql)

        # Membership, not `already.get(...) is not None`: the sha256 column
        # is nullable, so a row written without a hash would otherwise read
        # as "never applied" and the migration would run a second time. The
        # shipped DDL is idempotent, but an ALTER or an INSERT would not be.
        if path.name in already:
            recorded = already[path.name]
            if recorded and recorded != digest:
                actions.append((path.name, "changed",
                                f"recorded sha256 {recorded[:12]} != file "
                                f"{digest[:12]}; this database did not run "
                                f"the current contents"))
            else:
                actions.append((path.name, "skipped", "already applied"))
            continue

        if dry_run:
            actions.append((path.name, "pending", f"{len(sql)} bytes"))
            continue

        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                # Recorded in the same transaction as the DDL it describes,
                # so a crash between the two cannot leave a migration
                # applied but unrecorded.
                cur.execute(
                    "INSERT INTO schema_migration (name, sha256) "
                    "VALUES (%s, %s) ON CONFLICT (name) DO UPDATE "
                    "SET sha256 = EXCLUDED.sha256",
                    (path.name, digest),
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise MigrationError(f"{path.name} failed: {e}") from e

        actions.append((path.name, "applied", f"{len(sql)} bytes"))

    return actions


def status(conn) -> dict:
    """What is applied, what is pending, and what diverged."""
    paths = migration_files()
    already = applied(conn)
    pending, changed = [], []
    for path in paths:
        if path.name not in already:
            pending.append(path.name)
            continue
        recorded = already[path.name]
        if recorded and recorded != _sha256(path.read_text(encoding="utf-8")):
            changed.append(path.name)
    return {
        "applied": sorted(already),
        "pending": pending,
        "changed": changed,
        "orphaned": sorted(set(already) - {p.name for p in paths}),
    }
