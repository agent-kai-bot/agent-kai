#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN=${PYTHON_BIN:-}
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x ".venv/bin/python" ]]; then
    PYTHON_BIN=".venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

exec "$PYTHON_BIN" - "$@" <<'PY'
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request

ACTIVE_STATUSES = ("queued", "dispatching", "spawning", "running")
TERMINAL_STATUS = "stuck_aborted"
FAILURE_CLASS = "session_stuck_no_progress"
DEFAULT_STUCK_AFTER_SECONDS = int(os.environ.get("KAI_STUCK_AFTER_SECONDS", "3600"))


@dataclass
class RunRow:
    id: int
    task_id: int | None
    role: str | None
    status: str
    session_id: str | None
    created_at: str | None
    started_at: str | None


@dataclass
class PatchPlan:
    run_id: int
    body: dict[str, Any]
    row: RunRow
    age_seconds: int


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_seconds(row: RunRow, now: datetime) -> int | None:
    baseline = parse_ts(row.started_at) or parse_ts(row.created_at)
    if baseline is None:
        return None
    return max(0, int((now - baseline).total_seconds()))


def make_patch(row: RunRow, *, age: int, threshold: int, now: datetime) -> PatchPlan:
    body = {
        "status": TERMINAL_STATUS,
        "failure_class": FAILURE_CLASS,
        "failure_detail": (
            f"preflight cleanup: {row.status} row older than {threshold}s "
            f"(age={age}s) before cutover"
        ),
        "finished_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    return PatchPlan(run_id=row.id, body=body, row=row, age_seconds=age)


def print_header(args: argparse.Namespace, now: datetime) -> None:
    mode = "sqlite" if args.sqlite_db else "taskboard-api"
    print(f"mode={mode}")
    print(f"stuck_after_seconds={args.stuck_after_seconds}")
    print(f"active_statuses={','.join(ACTIVE_STATUSES)}")
    print(f"apply={'yes' if args.apply else 'no'}")
    print(f"now={now.replace(microsecond=0).isoformat().replace('+00:00', 'Z')}")


def sqlite_rows(db_path: Path) -> list[RunRow]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, task_id, role, status, session_id, created_at, started_at
            FROM agent_runs
            WHERE status IN ({})
            ORDER BY COALESCE(started_at, created_at) ASC, id ASC
            """.format(",".join("?" for _ in ACTIVE_STATUSES)),
            ACTIVE_STATUSES,
        ).fetchall()
        return [
            RunRow(
                id=int(row["id"]),
                task_id=int(row["task_id"]) if row["task_id"] is not None else None,
                role=str(row["role"]) if row["role"] is not None else None,
                status=str(row["status"]),
                session_id=str(row["session_id"]) if row["session_id"] is not None else None,
                created_at=str(row["created_at"]) if row["created_at"] is not None else None,
                started_at=str(row["started_at"]) if row["started_at"] is not None else None,
            )
            for row in rows
        ]
    finally:
        conn.close()


def sqlite_apply(db_path: Path, patches: list[PatchPlan]) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            for patch in patches:
                conn.execute(
                    """
                    UPDATE agent_runs
                    SET status = ?,
                        failure_class = ?,
                        failure_detail = ?,
                        finished_at = ?
                    WHERE id = ?
                    """,
                    (
                        patch.body["status"],
                        patch.body["failure_class"],
                        patch.body["failure_detail"],
                        patch.body["finished_at"],
                        patch.run_id,
                    ),
                )
    finally:
        conn.close()


def sqlite_active_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM agent_runs WHERE status IN ({})".format(
                ",".join("?" for _ in ACTIVE_STATUSES)
            ),
            ACTIVE_STATUSES,
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def api_request_json(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = request.Request(url, method=method, data=data)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"network error for {url}: {exc.reason}") from exc


def api_rows(base_url: str, token: str) -> list[RunRow]:
    rows: list[RunRow] = []
    base = base_url.rstrip("/")
    for status in ACTIVE_STATUSES:
        payload = api_request_json(
            "GET",
            f"{base}/api/agent-runs?status={status}&limit=200",
            token,
        )
        if not isinstance(payload, list):
            raise RuntimeError(f"unexpected list_by_status payload for status={status}: {type(payload)!r}")
        for row in payload:
            rows.append(
                RunRow(
                    id=int(row.get("id")),
                    task_id=int(row["task_id"]) if row.get("task_id") is not None else None,
                    role=str(row.get("role")) if row.get("role") is not None else None,
                    status=str(row.get("status") or ""),
                    session_id=str(row.get("session_id")) if row.get("session_id") is not None else None,
                    created_at=str(row.get("created_at")) if row.get("created_at") is not None else None,
                    started_at=str(row.get("started_at")) if row.get("started_at") is not None else None,
                )
            )
    rows.sort(key=lambda row: (parse_ts(row.started_at) or parse_ts(row.created_at) or utc_now(), row.id))
    return rows


def api_apply(base_url: str, token: str, patches: list[PatchPlan]) -> None:
    base = base_url.rstrip("/")
    for patch in patches:
        api_request_json("PATCH", f"{base}/api/agent-runs/{patch.run_id}", token, patch.body)


def api_active_count(base_url: str, token: str) -> int:
    count = 0
    for status in ACTIVE_STATUSES:
        payload = api_request_json(
            "GET",
            f"{base_url.rstrip('/')}/api/agent-runs?status={status}&limit=200",
            token,
        )
        if not isinstance(payload, list):
            raise RuntimeError("unexpected active-count payload")
        count += len(payload)
    return count


def build_plans(rows: list[RunRow], *, threshold: int, now: datetime) -> tuple[list[PatchPlan], list[RunRow]]:
    stale: list[PatchPlan] = []
    unknown_age: list[RunRow] = []
    for row in rows:
        age = age_seconds(row, now)
        if age is None:
            unknown_age.append(row)
            continue
        if age >= threshold:
            stale.append(make_patch(row, age=age, threshold=threshold, now=now))
    return stale, unknown_age


def print_observations(rows: list[RunRow], stale: list[PatchPlan], unknown_age: list[RunRow], threshold: int, api_mode: bool, base_url: str) -> None:
    print(f"active_rows_total={len(rows)}")
    print(f"stale_rows_total={len(stale)}")
    print(f"unknown_age_rows_total={len(unknown_age)}")
    print(f"gate={'blocked' if stale else 'clear'}")
    if not rows:
        return
    print("audit:")
    now = utc_now()
    for row in rows:
        age = age_seconds(row, now)
        age_text = "unknown" if age is None else str(age)
        flag = "STALE" if age is not None and age >= threshold else "live"
        print(
            f"- id={row.id} task={row.task_id or '-'} role={row.role or '-'} "
            f"status={row.status} age_seconds={age_text} session={row.session_id or '-'} flag={flag}"
        )
    if unknown_age:
        print("note=rows_missing_started_at_and_created_at_are_not_auto-cleaned")
    print("plan:")
    if not stale:
        print("- no cleanup required")
        return
    for patch in stale:
        if api_mode:
            body_json = json.dumps(patch.body, sort_keys=True)
            print(
                f"- PATCH {base_url.rstrip('/')}/api/agent-runs/{patch.run_id} {body_json}"
            )
        else:
            detail = patch.body["failure_detail"].replace("'", "''")
            finished_at = patch.body["finished_at"]
            print(
                "- SQL UPDATE agent_runs "
                f"SET status='{patch.body['status']}', "
                f"failure_class='{patch.body['failure_class']}', "
                f"failure_detail='{detail}', finished_at='{finished_at}' "
                f"WHERE id={patch.run_id};"
            )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit queued/dispatching/spawning/running agent_runs rows older than "
            "KAI_STUCK_AFTER_SECONDS and optionally clean them before cutover."
        )
    )
    parser.add_argument("--apply", action="store_true", help="apply the cleanup plan")
    parser.add_argument(
        "--sqlite-db",
        default="",
        help="operate directly on a sqlite agent_runs ledger instead of the taskboard API",
    )
    parser.add_argument(
        "--taskboard-url",
        default=os.environ.get("TASKBOARD_URL", ""),
        help="taskboard base URL (default: TASKBOARD_URL env)",
    )
    parser.add_argument(
        "--bearer-token",
        default=os.environ.get("TASKBOARD_BEARER_TOKEN", ""),
        help="taskboard bearer token (default: TASKBOARD_BEARER_TOKEN env)",
    )
    parser.add_argument(
        "--stuck-after-seconds",
        type=int,
        default=DEFAULT_STUCK_AFTER_SECONDS,
        help="age threshold for zombie rows (default: KAI_STUCK_AFTER_SECONDS or 3600)",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    now = utc_now()
    print_header(args, now)
    if args.sqlite_db:
        db_path = Path(args.sqlite_db).resolve()
        if not db_path.exists():
            print(f"error=sqlite_db_not_found path={db_path}", file=sys.stderr)
            return 2
        rows = sqlite_rows(db_path)
        stale, unknown_age = build_plans(rows, threshold=args.stuck_after_seconds, now=now)
        print_observations(rows, stale, unknown_age, args.stuck_after_seconds, False, "")
        if not args.apply:
            print("result=dry-run")
            return 0 if not stale else 3
        sqlite_apply(db_path, stale)
        after = sqlite_active_count(db_path)
        print(f"patched_rows={len(stale)}")
        print(f"capacity_after={after}")
        print(f"gate={'clear' if after == 0 else 'blocked'}")
        return 0 if after == 0 else 4

    if not args.taskboard_url or not args.bearer_token:
        print(
            "error=missing_taskboard_credentials set TASKBOARD_URL and TASKBOARD_BEARER_TOKEN or use --sqlite-db",
            file=sys.stderr,
        )
        return 2
    rows = api_rows(args.taskboard_url, args.bearer_token)
    stale, unknown_age = build_plans(rows, threshold=args.stuck_after_seconds, now=now)
    print_observations(rows, stale, unknown_age, args.stuck_after_seconds, True, args.taskboard_url)
    if not args.apply:
        print("result=dry-run")
        return 0 if not stale else 3
    api_apply(args.taskboard_url, args.bearer_token, stale)
    after = api_active_count(args.taskboard_url, args.bearer_token)
    print(f"patched_rows={len(stale)}")
    print(f"capacity_after={after}")
    print(f"gate={'clear' if after == 0 else 'blocked'}")
    return 0 if after == 0 else 4


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
PY
