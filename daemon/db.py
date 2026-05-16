"""SQLite state store for the daemon.

This module owns the ``daemon-state.sqlite3`` database used by webhook
ingress, dispatcher de-dup, and other daemon-internal persistence. The
same migration runner owns both taskboard delivery tables and Forgejo PR
delivery tables, so daemon startup has one database initialization path.

The runner reads ``daemon/migrations/*.sql`` files in lexical order and
applies any that are not already recorded in the ``schema_migrations``
table. The runner is idempotent so it can run on every daemon boot.

Example:
    Apply migrations during daemon startup::

        from daemon.db import apply_migrations, get_db_path

        apply_migrations(get_db_path())
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from config import WORKSPACES_DIR

LOGGER = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(WORKSPACES_DIR) / "daemon-state.sqlite3"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def get_db_path() -> Path:
    """Return the default daemon state database path.

    Returns:
        Absolute path to ``workspaces/daemon-state.sqlite3``.
    """

    return DEFAULT_DB_PATH


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection for the daemon state database.

    Args:
        db_path: Optional override for the database file. Defaults to
            :func:`get_db_path`.

    Returns:
        A configured ``sqlite3.Connection`` with foreign keys enabled
        and ``row_factory`` set to ``sqlite3.Row``.
    """

    path = Path(db_path) if db_path is not None else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    """Create the migration bookkeeping table if it does not yet exist."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Return the set of migration versions already recorded as applied."""

    cur = conn.execute("SELECT version FROM schema_migrations")
    return {int(row["version"]) for row in cur.fetchall()}


def _quote_identifier(identifier: str) -> str:
    """Return a double-quoted SQLite identifier."""

    if not identifier or "\x00" in identifier:
        raise ValueError(f"invalid SQLite identifier: {identifier!r}")
    return '"' + identifier.replace('"', '""') + '"'


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return column names for ``table``."""

    quoted_table = _quote_identifier(table)
    cur = conn.execute(f"PRAGMA table_info({quoted_table})")
    return {str(row["name"]) for row in cur.fetchall()}


def _add_column_if_missing(
    conn: sqlite3.Connection,
    *,
    table: str,
    column: str,
    definition: str,
) -> None:
    """Add a SQLite column only when the target table lacks it."""

    if column in _table_columns(conn, table):
        return
    quoted_table = _quote_identifier(table)
    quoted_column = _quote_identifier(column)
    conn.execute(f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_column} {definition}")


def _prepare_migration(conn: sqlite3.Connection, version: int) -> None:
    """Run guarded Python-side preparation required by a migration."""

    if version == 6:
        _add_column_if_missing(
            conn,
            table="sessions",
            column="last_progress_at",
            definition="TEXT",
        )


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, str, Path]]:
    """Return ``(version, name, path)`` triples for every migration file.

    Migration filenames must start with a zero-padded integer followed by
    an underscore (for example, ``001_taskboard_webhook_deliveries.sql``).
    Files that do not match this pattern are ignored.
    """

    discovered: list[tuple[int, str, Path]] = []
    if not migrations_dir.is_dir():
        return discovered
    for entry in sorted(migrations_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".sql":
            continue
        stem = entry.stem
        version_part, sep, name_part = stem.partition("_")
        if not sep or not version_part.isdigit():
            continue
        discovered.append((int(version_part), name_part, entry))
    return discovered


def apply_migrations(
    db_path: Path | str | None = None,
    *,
    migrations_dir: Path | None = None,
) -> list[int]:
    """Apply any pending SQL migrations to the daemon state database.

    Args:
        db_path: Optional override for the database file.
        migrations_dir: Optional override for the migrations directory,
            primarily used by tests.

    Returns:
        The list of migration versions newly applied during this call.
        The list is empty when the database is already up to date.

    Raises:
        sqlite3.DatabaseError: If a migration fails to apply.
    """

    target_dir = migrations_dir or MIGRATIONS_DIR
    newly_applied: list[int] = []
    conn = connect(db_path)
    try:
        _ensure_schema_migrations(conn)
        applied = _applied_versions(conn)
        for version, name, path in _discover_migrations(target_dir):
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            timestamp = datetime.now(timezone.utc).isoformat()
            # ``executescript`` issues its own ``COMMIT`` before running the
            # migration body, so a Python-level ``BEGIN`` cannot wrap it.
            # Migration SQL and guarded Python pre-steps must be idempotent
            # because ``schema_migrations`` is updated only after the script
            # completes successfully, so a re-run must resume safely.
            _prepare_migration(conn, version)
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at)"
                " VALUES (?, ?, ?)",
                (version, name, timestamp),
            )
            newly_applied.append(version)
            LOGGER.info("applied daemon migration %03d %s", version, name)
    finally:
        conn.close()
    return newly_applied
