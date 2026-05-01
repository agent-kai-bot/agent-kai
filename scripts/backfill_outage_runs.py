#!/usr/bin/env python3
"""Retroactively populate the agent_runs ledger with the silent-outage period.

Between 2026-04-25 (last KAI daemon start before today's restart) and
2026-04-30 18:04 UTC (today's restart), the daemon ran a stale
``agent-config.json`` that mapped role agents to a dead ``kai-smart``
endpoint with placeholder API key. Every CR / SA / QA / Architect / Developer
fire died in 1.67-2 seconds with `Primary endpoint failed: Connection error`
and produced an empty response. The taskboard recorded only the spawn
comments, so the silent-outage period is invisible in the audit trail.

This script reads every ``run_*.json`` artifact in
``workspaces/taskboard-runs/``, derives the terminal :class:`agent.run_outcome.RunOutcome`
from its event stream, and writes:

* one ``agent_runs`` row per artifact, with ``source_component=backfill``,
  the derived terminal status / failure_class, and timestamps from the JSON;
* one System audit comment on the linked task summarising the outcome
  (unless ``--no-comments`` is passed).

The script is idempotent — it tracks which runs it has reaped in
``workspaces/run_outcome_reaper.sqlite3`` and skips already-seen runs.

Usage::

    # Dry run: print histogram, write nothing.
    .venv/bin/python scripts/backfill_outage_runs.py --dry-run

    # Real run: write rows + post audit comments.
    .venv/bin/python scripts/backfill_outage_runs.py

    # Real run, no comment posting (rows only).
    .venv/bin/python scripts/backfill_outage_runs.py --no-comments

The taskboard ``agent_runs`` ledger must be deployed first (PR #176;
migration ``m_003_agent_runs.py``).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running from repo root without installing.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from agent.agent_runs_client import AgentRunsClient  # noqa: E402
from agent.run_outcome_reaper import (  # noqa: E402
    DEFAULT_RUN_DIR,
    DEFAULT_STATE_DB,
    ReaperStateStore,
    reap_directory,
    summarize_results,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill the taskboard agent_runs ledger with the 2026-04-25 → "
            "2026-04-30 silent-outage period (epic #10028, follow-up to "
            "Phase 1 #10223)."
        )
    )
    parser.add_argument(
        "--directory",
        default=str(DEFAULT_RUN_DIR),
        help="Directory containing run_*.json artifacts",
    )
    parser.add_argument(
        "--state-db",
        default=str(DEFAULT_STATE_DB),
        help="State DB tracking already-reaped runs",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse + derive outcomes; do not POST to the taskboard or update state DB",
    )
    parser.add_argument(
        "--no-comments",
        action="store_true",
        help=(
            "Write ledger rows but skip the per-task audit comments. Useful when "
            "the operator wants to fold the outage backfill into the ledger "
            "without spamming task histories."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after processing N artifacts (mostly for sampling)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print one line per processed artifact",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    directory = Path(args.directory).resolve()
    state_db = Path(args.state_db).resolve()
    if not directory.exists():
        print(f"directory does not exist: {directory}")
        return 1

    client = AgentRunsClient.from_env()
    if not args.dry_run and not client.enabled:
        print(
            "TASKBOARD_URL + TASKBOARD_BEARER_TOKEN must be set for a real run "
            "(use --dry-run to validate without writes)"
        )
        return 2

    if args.no_comments and not args.dry_run:

        class _NoCommentClient:
            """Wrap AgentRunsClient and short-circuit comment posting."""

            def __init__(self, inner: AgentRunsClient) -> None:
                self._inner = inner
                self.enabled = inner.enabled

            def list_for_task(self, *args, **kwargs):
                return self._inner.list_for_task(*args, **kwargs)

            def patch(self, *args, **kwargs):
                return self._inner.patch(*args, **kwargs)

            def create(self, *args, **kwargs):
                return self._inner.create(*args, **kwargs)

            def post_audit_comment(self, *args, **kwargs) -> bool:
                return False  # silently no-op

        client = _NoCommentClient(client)  # type: ignore[assignment]

    state = ReaperStateStore(state_db)

    results = reap_directory(
        client=client,
        state=state,
        directory=directory,
        dry_run=args.dry_run,
        limit=args.limit,
        create_if_missing=True,  # backfill creates rows from scratch
        source_component="backfill",
    )

    histogram = summarize_results(results)
    print(f"=== backfill summary (dry_run={args.dry_run}, no_comments={args.no_comments}) ===")
    print(f"directory: {directory}")
    print(f"state_db:  {state_db}")
    print(f"processed: {len(results)} artifacts")
    for status in sorted(histogram.keys()):
        print(f"  {status:32s}  {histogram[status]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
