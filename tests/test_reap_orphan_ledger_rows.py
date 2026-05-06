"""Tests for ``reap_orphan_ledger_rows`` (Router v2 #10275)."""

from __future__ import annotations

from typing import Any

import pytest

from agent.taskboard_dispatcher import reap_orphan_ledger_rows


class _FakeAgentRunsClient:
    """Minimal stub mirroring the AgentRunsClient surface used by the sweep."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        rows_by_status: dict[str, list[dict[str, Any]]] | None = None,
        list_failures: set[str] | None = None,
        patch_failures: set[int] | None = None,
        patch_returns_none: set[int] | None = None,
    ) -> None:
        self.enabled = enabled
        self._rows_by_status = rows_by_status or {}
        self._list_failures = list_failures or set()
        self._patch_failures = patch_failures or set()
        self._patch_returns_none = patch_returns_none or set()
        self.list_calls: list[str] = []
        self.patch_calls: list[tuple[int, dict[str, Any]]] = []

    def list_by_status(self, status: str, *, limit: int = 200) -> list[dict[str, Any]] | None:
        self.list_calls.append(status)
        if status in self._list_failures:
            raise RuntimeError(f"simulated network error on status={status}")
        return list(self._rows_by_status.get(status, []))

    def patch(self, run_id: int, body: dict[str, Any]) -> dict[str, Any] | None:
        self.patch_calls.append((run_id, dict(body)))
        if run_id in self._patch_failures:
            raise RuntimeError(f"simulated patch error run_id={run_id}")
        if run_id in self._patch_returns_none:
            return None
        return {"id": run_id, **body}


def test_returns_zero_counts_for_disabled_client() -> None:
    client = _FakeAgentRunsClient(enabled=False)
    counts = reap_orphan_ledger_rows(client)
    assert counts == {
        "cancelled": 0,
        "failed": 0,
        "skipped_live": 0,
        "errors": 0,
    }
    assert client.list_calls == []
    assert client.patch_calls == []


def test_queued_and_dispatching_rows_marked_cancelled() -> None:
    client = _FakeAgentRunsClient(
        rows_by_status={
            "queued": [{"id": 11, "status": "queued", "session_id": ""}],
            "dispatching": [{"id": 12, "status": "dispatching", "session_id": ""}],
        }
    )
    counts = reap_orphan_ledger_rows(client)
    assert counts["cancelled"] == 2
    assert counts["failed"] == 0
    assert counts["errors"] == 0
    assert sorted(call[0] for call in client.patch_calls) == [11, 12]
    for _, body in client.patch_calls:
        assert body["status"] == "cancelled"
        assert "failure_class" not in body  # cancelled is not a failure status


def test_spawning_and_running_rows_marked_failed_with_failure_class() -> None:
    client = _FakeAgentRunsClient(
        rows_by_status={
            "spawning": [{"id": 21, "status": "spawning", "session_id": "s-21"}],
            "running": [{"id": 22, "status": "running", "session_id": "s-22"}],
        }
    )
    counts = reap_orphan_ledger_rows(client)
    assert counts["failed"] == 2
    assert counts["cancelled"] == 0
    assert counts["errors"] == 0
    for _, body in client.patch_calls:
        assert body["status"] == "failed"
        assert body["failure_class"] == "session_stuck_no_progress"
        assert body["failure_detail"] == "daemon_restart_casualty"


def test_live_session_ids_are_skipped() -> None:
    client = _FakeAgentRunsClient(
        rows_by_status={
            "running": [
                {"id": 31, "status": "running", "session_id": "live-session-A"},
                {"id": 32, "status": "running", "session_id": "zombie-B"},
            ],
        }
    )
    counts = reap_orphan_ledger_rows(client, live_session_ids={"live-session-A"})
    assert counts["skipped_live"] == 1
    assert counts["failed"] == 1
    assert counts["errors"] == 0
    assert client.patch_calls == [
        (
            32,
            {
                "status": "failed",
                "failure_class": "session_stuck_no_progress",
                "failure_detail": "daemon_restart_casualty",
            },
        )
    ]


def test_list_failure_increments_errors_and_continues_other_statuses() -> None:
    client = _FakeAgentRunsClient(
        rows_by_status={
            "spawning": [{"id": 41, "status": "spawning", "session_id": ""}],
        },
        list_failures={"queued"},
    )
    counts = reap_orphan_ledger_rows(client)
    assert counts["errors"] == 1
    assert counts["failed"] == 1
    assert client.patch_calls == [
        (
            41,
            {
                "status": "failed",
                "failure_class": "session_stuck_no_progress",
                "failure_detail": "daemon_restart_casualty",
            },
        )
    ]


def test_patch_failure_increments_errors_and_continues() -> None:
    client = _FakeAgentRunsClient(
        rows_by_status={
            "running": [
                {"id": 51, "status": "running", "session_id": ""},
                {"id": 52, "status": "running", "session_id": ""},
            ]
        },
        patch_failures={51},
    )
    counts = reap_orphan_ledger_rows(client)
    assert counts["errors"] == 1
    assert counts["failed"] == 1
    assert sorted(c[0] for c in client.patch_calls) == [51, 52]


def test_patch_returns_none_increments_errors() -> None:
    client = _FakeAgentRunsClient(
        rows_by_status={
            "queued": [{"id": 61, "status": "queued", "session_id": ""}],
        },
        patch_returns_none={61},
    )
    counts = reap_orphan_ledger_rows(client)
    assert counts["errors"] == 1
    assert counts["cancelled"] == 0


def test_dedupes_rows_appearing_in_multiple_statuses() -> None:
    """A row racing through statuses during the sweep is patched at most once."""

    same_row = {"id": 71, "status": "running", "session_id": ""}
    client = _FakeAgentRunsClient(
        rows_by_status={
            "spawning": [same_row],
            "running": [same_row],
        }
    )
    counts = reap_orphan_ledger_rows(client)
    assert len(client.patch_calls) == 1
    assert counts["failed"] == 1


def test_unknown_row_status_is_ignored() -> None:
    """Rows whose status doesn't match the active set are left alone."""

    client = _FakeAgentRunsClient(
        rows_by_status={
            "running": [
                {"id": 81, "status": "succeeded", "session_id": ""},
                {"id": 82, "status": "running", "session_id": ""},
            ]
        }
    )
    counts = reap_orphan_ledger_rows(client)
    assert counts["failed"] == 1
    assert client.patch_calls == [
        (
            82,
            {
                "status": "failed",
                "failure_class": "session_stuck_no_progress",
                "failure_detail": "daemon_restart_casualty",
            },
        )
    ]


def test_list_returning_none_increments_errors() -> None:
    class _NoneListingClient(_FakeAgentRunsClient):
        def list_by_status(self, status: str, *, limit: int = 200):
            self.list_calls.append(status)
            return None

    client = _NoneListingClient()
    counts = reap_orphan_ledger_rows(client)
    assert counts["errors"] == 4  # one per ledger active status
