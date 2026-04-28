"""SQLite state store for daemon-managed webhook ingress.

This module owns the daemon's local SQLite database used by signed
webhook ingress and later dispatcher workers. It provides a small
connection helper plus a numbered SQL migration runner.

Example:
    Apply daemon migrations on startup::

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
    """Return the default daemon SQLite database path.

    Returns:
        Absolute path to ``workspaces/daemon-state.sqlite3``.

    Example:
        Resolve the default path before opening a connection::

            db_path = get_db_path()
    """

    return DEFAULT_DB_PATH


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a configured SQLite connection for daemon state.

    Args:
        db_path: Optional database path. When omitted, the default
            daemon database path is used.

    Returns:
        A ``sqlite3.Connection`` with foreign keys enabled and rows
        returned as ``sqlite3.Row`` objects.

    Raises:
        sqlite3.DatabaseError: If SQLite cannot open or configure the
            requested database.

    Example:
        Query daemon state with automatic row-name access::

            conn = connect()
            try:
                row = conn.execute("SELECT 1 AS ok").fetchone()
            finally:
                conn.close()
    """

    path = Path(db_path) if db_path is not None else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_schema_migrations(conn: sqlite3.Connection) -> None:
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
    cur = conn.execute("SELECT version FROM schema_migrations")
    return {int(row["version"]) for row in cur.fetchall()}


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, str, Path]]:
    discovered: list[tuple[int, str, Path]] = []
    if not migrations_dir.is_dir():
        return discovered
    for entry in sorted(migrations_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".sql":
            continue
        version_part, sep, name_part = entry.stem.partition("_")
        if not sep or not version_part.isdigit():
            continue
        discovered.append((int(version_part), name_part, entry))
    return discovered


def apply_migrations(
    db_path: Path | str | None = None,
    *,
    migrations_dir: Path | None = None,
) -> list[int]:
    """Apply pending daemon SQL migrations.

    Args:
        db_path: Optional database path. When omitted, the default
            daemon database path is used.
        migrations_dir: Optional directory containing migration files.
            Tests can use this to point at isolated fixtures.

    Returns:
        Migration versions newly applied by this call. An empty list
        means the database was already up to date.

    Raises:
        sqlite3.DatabaseError: If a migration cannot be applied.

    Example:
        Run migrations during service startup::

            applied_versions = apply_migrations()
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
