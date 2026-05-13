"""Close the agent_runs ledger loop: watch run JSON artifacts, write terminal status.

Phase 1 of epic #10028 (taskboard task #10223) added the agent_runs ledger and
the dispatcher hooks that write ``queued`` and ``spawning`` rows. This module
closes the lifecycle by watching the run JSON artifacts that the agent runtime
writes to ``workspaces/taskboard-runs/run_*.json`` after each session ends,
deriving the terminal :class:`agent.run_outcome.RunOutcome` from the captured
event stream, PATCHing the matching ledger row to a terminal status, and
posting the canonical ``[KAI] COMPLETED`` / ``[KAI] FAILED`` audit comment
back to the linked task.

The reaper has two entry points:

* :func:`reap_directory` — sweep a directory once. Used by the dispatcher's
  periodic loop (``await reaper.reap_directory(...)`` every poll tick).
* :func:`reap_one` — process a single run file. Used for tests, replays, and
  the backfill script.

Both are best-effort: failures to write the ledger or post the comment are
logged and swallowed. The reaper records its own per-file processed set in
SQLite so a restart doesn't duplicate audit comments.

Why a reaper rather than synchronous in-dispatcher writes?

* The agent runtime is asynchronous and produces multiple events between
  ``running`` and ``terminal`` — wiring every event into the ledger from the
  dispatcher would couple the two state machines and add latency to user-facing
  spawn calls.
* The dispatcher today doesn't observe the agent runtime's final event stream
  directly — it spawns and forgets. The run JSON is the canonical post-mortem
  artifact, and the reaper turns it into a ledger row.
* Backfill (the 5-day outage replay) reuses the same code path.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from agent.agent_runs_client import AgentRunsClient
from agent.run_outcome import (
    AGENT_RUN_TERMINAL_STATUSES,
    RunOutcome,
    derive_outcome_from_agent_events,
    format_terminal_comment,
    outcome_to_patch_body,
)


LOGGER = logging.getLogger("agent.run_outcome_reaper")

DEFAULT_RUN_DIR = Path("workspaces/taskboard-runs").resolve()
DEFAULT_STATE_DB = Path("workspaces/run_outcome_reaper.sqlite3").resolve()
_REAPED_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS reaped_runs (
    run_id TEXT PRIMARY KEY,
    ended_at TEXT,
    ledger_run_id INTEGER,
    terminal_status TEXT,
    failure_class TEXT,
    reaped_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""

# Roles eligible for the agent_runs ledger. The main `kai` chat agent
# also writes run JSON files but is not part of the autonomous SDLC
# pipeline — backfill skips those rather than failing role validation.
_BACKFILL_ELIGIBLE_ROLES = frozenset(
    {"developer", "code-reviewer", "security-auditor", "qa-agent", "architect", "orchestrator"}
)


def _backfill_transition_path(terminal_status: str) -> tuple[str, ...]:
    """Return the chain of intermediates needed before the terminal PATCH.

    The taskboard ``agent_runs`` state-machine requires:
      queued → dispatching → spawning → (running →)? terminal

    For success / non-blocked terminals we need to pass through ``running``;
    for ``preflight_failed`` we go straight from ``dispatching``. For
    ``duplicate_suppressed`` and ``cancelled`` from ``queued`` we don't need
    any intermediate (those statuses are reachable directly from queued in
    the state machine).
    """
    if terminal_status == "preflight_failed":
        return ("dispatching",)
    if terminal_status in ("duplicate_suppressed", "cancelled"):
        return ()
    # spawning → running covers most agent-runtime terminal statuses.
    return ("dispatching", "spawning", "running")


@dataclass(frozen=True)
class RunArtifact:
    """Parsed metadata from one ``run_*.json`` file.

    Attributes:
        path: On-disk path to the run JSON file.
        run_id: openclaw-gateway run id (``run_xxx``); not the ledger row id.
        session_key: Full session key (``agent:<role>:task-<task_id>:run_xxx``).
        task_id: Linked taskboard task id (parsed from session_key).
        role: Workforce role (developer / code-reviewer / etc).
        status: Run status string from the JSON ('completed' / 'running' / etc).
        started_at: Wall clock start.
        ended_at: Wall clock end (None when still running).
        events: Raw event list from the JSON file.
        final_text: ``final_text`` field from the JSON file.
    """

    path: Path
    run_id: str
    session_key: str
    task_id: Optional[int]
    role: Optional[str]
    status: str
    started_at: Optional[str]
    ended_at: Optional[str]
    events: Sequence[Mapping[str, Any]]
    final_text: Optional[str]

    @property
    def is_terminal(self) -> bool:
        """Return whether the artifact represents a finished run."""
        return self.status not in {"running", "spawning", "queued"}

    @property
    def elapsed_seconds(self) -> Optional[float]:
        """Compute run wall-clock duration when both timestamps are present."""
        if not self.started_at or not self.ended_at:
            return None
        try:
            start = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(self.ended_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return (end - start).total_seconds()


def parse_run_artifact(path: Path) -> Optional[RunArtifact]:
    """Parse a single ``run_*.json`` file into a :class:`RunArtifact`.

    Returns ``None`` when the file can't be read or doesn't have the
    minimum fields the reaper needs (``run_id``, ``status``).
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("reaper.parse_run_artifact failed path=%s error=%s", path, exc)
        return None
    if not isinstance(payload, Mapping):
        return None

    run_id = str(payload.get("run_id") or "").strip()
    status = str(payload.get("status") or "").strip()
    session_key = str(payload.get("session_key") or "").strip()
    if not run_id or not status:
        return None

    task_id, role = _parse_session_key(session_key)
    # Some artifacts also carry top-level task_id / requested_agent_id.
    if task_id is None and isinstance(payload.get("task_id"), int):
        task_id = int(payload["task_id"])
    if role is None:
        requested = str(payload.get("requested_agent_id") or "").strip()
        if requested:
            role = requested

    events_raw = payload.get("events") or []
    if not isinstance(events_raw, list):
        events_raw = []

    return RunArtifact(
        path=path,
        run_id=run_id,
        session_key=session_key,
        task_id=task_id,
        role=role,
        status=status,
        started_at=payload.get("started_at"),
        ended_at=payload.get("ended_at"),
        events=events_raw,
        final_text=payload.get("final_text"),
    )


def _parse_session_key(session_key: str) -> tuple[Optional[int], Optional[str]]:
    """Extract task_id + role from ``agent:<role>:task-<id>:run_xxx``."""
    if not session_key:
        return None, None
    parts = session_key.split(":")
    role: Optional[str] = None
    task_id: Optional[int] = None
    for index, segment in enumerate(parts):
        if segment == "agent" and index + 1 < len(parts):
            role = parts[index + 1]
        if segment.startswith("task-"):
            tail = segment[len("task-") :]
            try:
                task_id = int(tail.split("-", 1)[0])
            except ValueError:
                task_id = None
    return task_id, role


# ---------------------------------------------------------------------------
# State store: track which run files we've already reaped so we don't
# double-PATCH the ledger or re-post audit comments after a daemon restart.
# ---------------------------------------------------------------------------


class ReaperStateStore:
    """SQLite-backed set of (run_id, ended_at) we have already terminated."""

    def __init__(self, db_path: Path | str = DEFAULT_STATE_DB) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._ensure_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(_REAPED_RUNS_SCHEMA)

    def has_seen(self, run_id: str) -> bool:
        with self._connect() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT 1 FROM reaped_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return row is not None

    def record(
        self,
        *,
        run_id: str,
        ended_at: Optional[str],
        ledger_run_id: Optional[int],
        terminal_status: str,
        failure_class: Optional[str],
    ) -> None:
        with self._connect() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                INSERT OR REPLACE INTO reaped_runs (
                    run_id, ended_at, ledger_run_id, terminal_status, failure_class
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, ended_at, ledger_run_id, terminal_status, failure_class),
            )
            conn.commit()


# ---------------------------------------------------------------------------
# The reaper itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReapResult:
    """One reaper outcome."""

    run_id: str
    task_id: Optional[int]
    role: Optional[str]
    outcome: Optional[RunOutcome]
    ledger_run_id: Optional[int]
    audit_posted: bool
    skipped: bool
    skip_reason: Optional[str] = None


def _resolve_ledger_run_id(
    artifact: RunArtifact, client: AgentRunsClient
) -> Optional[int]:
    """Find the ledger row this artifact terminates.

    Lookup strategy: list runs for the artifact's task and find the row whose
    ``session_id`` matches the artifact's ``session_key``. The dispatcher's
    ``_record_agent_run_spawning`` PATCHed ``session_id`` so this match is
    deterministic for runs that came through the dispatcher path.
    """
    if not client.enabled or artifact.task_id is None:
        return None
    rows = client.list_for_task(artifact.task_id, limit=200)
    if not rows:
        return None
    # Match by session_id first (dispatcher path), fall back to (role + role)
    # heuristic for backfill.
    target = artifact.session_key
    for row in rows:
        if str(row.get("session_id") or "") == target:
            return int(row["id"])
    return None


def reap_one(
    artifact: RunArtifact,
    *,
    client: AgentRunsClient,
    state: ReaperStateStore,
    dry_run: bool = False,
    create_if_missing: bool = False,
    source_component: str = "kai-dispatcher",
) -> ReapResult:
    """Process one :class:`RunArtifact`. See module docstring for semantics.

    Args:
        artifact: The parsed run JSON file.
        client: Taskboard ledger client.
        state: Per-run-id de-dup state store.
        dry_run: When True, derive outcome but make no taskboard writes and
            do not record the run as reaped.
        create_if_missing: When True and no existing ledger row matches the
            artifact's ``session_key``, POST a new row with the terminal
            status directly (used by backfill / replay flows). When False
            (the dispatcher's normal mode), unmatched artifacts still get
            their outcome derived but no row is written, on the assumption
            that the dispatcher already wrote a ``queued`` row to be PATCHed.
        source_component: Tag for newly-created rows when create_if_missing
            kicks in. Defaults to ``kai-dispatcher``; backfill scripts should
            override to ``backfill``.
    """
    if state.has_seen(artifact.run_id) and not dry_run:
        return ReapResult(
            run_id=artifact.run_id,
            task_id=artifact.task_id,
            role=artifact.role,
            outcome=None,
            ledger_run_id=None,
            audit_posted=False,
            skipped=True,
            skip_reason="already_reaped",
        )
    if not artifact.is_terminal:
        return ReapResult(
            run_id=artifact.run_id,
            task_id=artifact.task_id,
            role=artifact.role,
            outcome=None,
            ledger_run_id=None,
            audit_posted=False,
            skipped=True,
            skip_reason="run_not_terminal",
        )

    outcome = derive_outcome_from_agent_events(
        artifact.events, final_text=artifact.final_text
    )

    ledger_run_id: Optional[int] = None
    if not dry_run:
        ledger_run_id = _resolve_ledger_run_id(artifact, client)

    audit_posted = False
    if not dry_run and ledger_run_id is not None:
        body = outcome_to_patch_body(outcome)
        if artifact.ended_at:
            body["finished_at"] = artifact.ended_at
        client.patch(ledger_run_id, body)
    elif (
        not dry_run
        and ledger_run_id is None
        and create_if_missing
        and artifact.task_id is not None
        and artifact.role is not None
    ):
        # Skip non-workforce roles (the main `kai` chat agent leaves runs
        # under the same dir but isn't part of the SDLC ledger).
        if artifact.role not in _BACKFILL_ELIGIBLE_ROLES:
            return ReapResult(
                run_id=artifact.run_id,
                task_id=artifact.task_id,
                role=artifact.role,
                outcome=outcome,
                ledger_run_id=None,
                audit_posted=False,
                skipped=True,
                skip_reason=f"role_not_workforce:{artifact.role}",
            )
        # Backfill / replay path: synthesize a terminal row from scratch by
        # walking the closed state-machine from queued through every required
        # intermediate (dispatching, spawning, running) before the terminal
        # PATCH. The taskboard's state-machine validator rejects skipping
        # required intermediates, so we step through each one.
        post_body = {
            "task_id": int(artifact.task_id),
            "role": str(artifact.role),
            "source_component": source_component,
            "status": "queued",
            "session_id": artifact.session_key or None,
            "started_at": artifact.started_at,
        }
        try:
            ledger_run_id = client.create(post_body)
        except ValueError as exc:
            LOGGER.warning("reaper.reap_one create failed: %s", exc)
            ledger_run_id = None
        if ledger_run_id is not None:
            for transition in _backfill_transition_path(outcome.status):
                client.patch(ledger_run_id, {"status": transition})
            terminal_body = outcome_to_patch_body(outcome)
            if artifact.ended_at:
                terminal_body["finished_at"] = artifact.ended_at
            client.patch(ledger_run_id, terminal_body)

    if not dry_run and artifact.task_id is not None:
        comment = format_terminal_comment(
            role=artifact.role or "?",
            outcome=outcome,
            session_id=artifact.session_key,
            fire_generation=None,
            elapsed_seconds=artifact.elapsed_seconds,
        )
        audit_posted = client.post_audit_comment(artifact.task_id, comment)

    if not dry_run:
        state.record(
            run_id=artifact.run_id,
            ended_at=artifact.ended_at,
            ledger_run_id=ledger_run_id,
            terminal_status=outcome.status,
            failure_class=outcome.failure_class,
        )

    return ReapResult(
        run_id=artifact.run_id,
        task_id=artifact.task_id,
        role=artifact.role,
        outcome=outcome,
        ledger_run_id=ledger_run_id,
        audit_posted=audit_posted,
        skipped=False,
    )


def iter_run_files(
    directory: Path = DEFAULT_RUN_DIR,
    *,
    pattern: str = "run_*.json",
) -> Iterator[Path]:
    """Yield ``run_*.json`` paths sorted by mtime ascending (oldest first)."""
    if not directory.exists():
        return iter(())
    paths_with_mtime: list[tuple[float, Path]] = []
    for path in directory.glob(pattern):
        try:
            paths_with_mtime.append((path.stat().st_mtime, path))
        except OSError as exc:
            LOGGER.warning("reaper.iter_run_files skipped path=%s error=%s", path, exc)
    paths = [path for _, path in sorted(paths_with_mtime, key=lambda item: item[0])]
    return iter(paths)


def reap_directory(
    *,
    client: Optional[AgentRunsClient] = None,
    state: Optional[ReaperStateStore] = None,
    directory: Path = DEFAULT_RUN_DIR,
    dry_run: bool = False,
    limit: Optional[int] = None,
    create_if_missing: bool = False,
    source_component: str = "kai-dispatcher",
) -> list[ReapResult]:
    """Sweep ``directory`` once and process every unseen terminal artifact."""
    client = client or AgentRunsClient.from_env()
    state = state or ReaperStateStore()

    results: list[ReapResult] = []
    count = 0
    for path in iter_run_files(directory):
        if limit is not None and count >= limit:
            break
        artifact = parse_run_artifact(path)
        if artifact is None:
            continue
        results.append(
            reap_one(
                artifact,
                client=client,
                state=state,
                dry_run=dry_run,
                create_if_missing=create_if_missing,
                source_component=source_component,
            )
        )
        count += 1
    return results


def summarize_results(results: Iterable[ReapResult]) -> dict[str, int]:
    """Return a small histogram of reap outcomes for logging / kaictl."""
    summary: dict[str, int] = {}
    for r in results:
        if r.skipped:
            key = f"skipped:{r.skip_reason or 'unknown'}"
        elif r.outcome is None:
            key = "no_outcome"
        else:
            key = r.outcome.status
        summary[key] = summary.get(key, 0) + 1
    return summary
