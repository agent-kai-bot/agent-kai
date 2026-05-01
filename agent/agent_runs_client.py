"""Thin HTTP client for the taskboard ``agent_runs`` ledger.

The dispatcher uses this to write run lifecycle events
(``queued`` → ``dispatching`` → ``spawning`` → ``running`` → terminal)
to the taskboard so they are visible in the operator UX
(``kaictl runs``, taskboard UI) instead of buried in a JSON file on a host
disk.

Design notes:

* Failures to write the ledger MUST NOT crash the dispatcher's main flow.
  Without this guarantee, a temporary taskboard outage would also wedge the
  dispatcher. Methods log + return ``None`` on failure rather than raising.
* The client is optional: when ``base_url`` or ``bearer_token`` is unset, all
  methods become no-ops. This keeps tests + the ``main`` branch usable
  before the taskboard ledger migration deploys to prod.
* Bodies are validated client-side against the closed enums in
  :mod:`agent.run_outcome` before going over the wire — surface mistakes as
  ``ValueError`` early, not 422 from the taskboard.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

import httpx

from agent.run_outcome import (
    AGENT_RUN_FAILURE_CLASSES,
    AGENT_RUN_FAILURE_STATUSES,
    AGENT_RUN_STATUSES,
)


LOGGER = logging.getLogger("agent.agent_runs_client")

_DEFAULT_TIMEOUT = float(os.environ.get("KAI_AGENT_RUNS_TIMEOUT", "5.0"))
_DEFAULT_BASE_URL_ENV = "TASKBOARD_URL"
_DEFAULT_BEARER_ENV = "TASKBOARD_BEARER_TOKEN"


def _validate_create_body(body: Mapping[str, Any]) -> None:
    """Catch contract violations before we POST."""
    role = body.get("role")
    if not isinstance(role, str) or not role:
        raise ValueError("create body missing required 'role'")
    source = body.get("source_component")
    if not isinstance(source, str) or not source:
        raise ValueError("create body missing required 'source_component'")
    status = body.get("status", "queued")
    if status not in AGENT_RUN_STATUSES:
        raise ValueError(f"create body status {status!r} not in AGENT_RUN_STATUSES")
    if status in AGENT_RUN_FAILURE_STATUSES and not body.get("failure_class"):
        raise ValueError(
            f"create body status {status!r} requires failure_class"
        )
    failure_class = body.get("failure_class")
    if failure_class is not None and failure_class not in AGENT_RUN_FAILURE_CLASSES:
        raise ValueError(
            f"create body failure_class {failure_class!r} not in AGENT_RUN_FAILURE_CLASSES"
        )


def _validate_patch_body(body: Mapping[str, Any]) -> None:
    """Catch contract violations before we PATCH."""
    if not body:
        raise ValueError("patch body must contain at least one mutable field")
    if "status" in body:
        status = body["status"]
        if status not in AGENT_RUN_STATUSES:
            raise ValueError(f"patch body status {status!r} not in AGENT_RUN_STATUSES")
    failure_class = body.get("failure_class")
    if failure_class is not None and failure_class not in AGENT_RUN_FAILURE_CLASSES:
        raise ValueError(
            f"patch body failure_class {failure_class!r} not in AGENT_RUN_FAILURE_CLASSES"
        )


@dataclass(frozen=True)
class AgentRunsClient:
    """Best-effort HTTP client for ``/api/agent-runs`` endpoints.

    Attributes:
        base_url: e.g. ``http://srv01:18180`` (no trailing slash). When
            falsy, all methods are no-ops.
        bearer_token: Primary or agent-identity bearer token. When falsy,
            all methods are no-ops.
        timeout_seconds: HTTP timeout per request.

    Example:
        >>> client = AgentRunsClient.from_env()
        >>> run_id = client.create({
        ...     "task_id": 10213,
        ...     "role": "code-reviewer",
        ...     "source_component": "kai-dispatcher",
        ...     "fire_generation": 7,
        ...     "trigger_event_id": "wh-...",
        ... })
        >>> if run_id is not None:
        ...     client.patch(run_id, {"status": "running"})
    """

    base_url: str = ""
    bearer_token: str = ""
    timeout_seconds: float = _DEFAULT_TIMEOUT

    @classmethod
    def from_env(
        cls,
        *,
        base_url_env: str = _DEFAULT_BASE_URL_ENV,
        bearer_env: str = _DEFAULT_BEARER_ENV,
    ) -> "AgentRunsClient":
        """Build a client from the standard env vars."""
        return cls(
            base_url=os.environ.get(base_url_env, "").rstrip("/"),
            bearer_token=os.environ.get(bearer_env, "").strip(),
        )

    @property
    def enabled(self) -> bool:
        """Return whether the client is configured to actually write."""
        return bool(self.base_url) and bool(self.bearer_token)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def create(self, body: Mapping[str, Any]) -> Optional[int]:
        """POST a new ``agent_runs`` row.

        Returns the new row's id on success, or ``None`` on disabled / failure.
        Raises ``ValueError`` on contract violation before the request leaves.
        """
        _validate_create_body(body)
        if not self.enabled:
            LOGGER.debug("agent_runs_client.create skipped (client disabled)")
            return None
        url = f"{self.base_url}/api/agent-runs"
        try:
            with httpx.Client(timeout=self.timeout_seconds) as http:
                response = http.post(url, headers=self._headers(), json=dict(body))
        except httpx.HTTPError as exc:
            LOGGER.warning("agent_runs_client.create network error: %s", exc)
            return None
        if response.status_code != 201:
            LOGGER.warning(
                "agent_runs_client.create unexpected status=%s body=%s",
                response.status_code,
                response.text[:300],
            )
            return None
        try:
            return int(response.json()["id"])
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning("agent_runs_client.create decode error: %s", exc)
            return None

    def patch(self, run_id: int, body: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """PATCH an existing ``agent_runs`` row.

        Returns the updated row dict on success, or ``None`` on disabled /
        failure. Raises ``ValueError`` on contract violation.
        """
        _validate_patch_body(body)
        if not self.enabled:
            LOGGER.debug("agent_runs_client.patch skipped (client disabled)")
            return None
        if run_id is None:
            return None
        url = f"{self.base_url}/api/agent-runs/{int(run_id)}"
        try:
            with httpx.Client(timeout=self.timeout_seconds) as http:
                response = http.patch(url, headers=self._headers(), json=dict(body))
        except httpx.HTTPError as exc:
            LOGGER.warning("agent_runs_client.patch network error: %s", exc)
            return None
        if response.status_code in (404, 409):
            # Terminal-row updates and deleted rows are normal under retries;
            # log at info, not warning.
            LOGGER.info(
                "agent_runs_client.patch run_id=%s status=%s body=%s",
                run_id,
                response.status_code,
                response.text[:200],
            )
            return None
        if response.status_code != 200:
            LOGGER.warning(
                "agent_runs_client.patch run_id=%s unexpected status=%s body=%s",
                run_id,
                response.status_code,
                response.text[:300],
            )
            return None
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            LOGGER.warning("agent_runs_client.patch decode error: %s", exc)
            return None

    def list_for_task(
        self,
        task_id: int,
        *,
        role: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> Optional[list[Dict[str, Any]]]:
        """GET runs for a task. Returns list on success, ``None`` on failure."""
        if not self.enabled:
            return None
        params: Dict[str, Any] = {"limit": int(limit)}
        if role is not None:
            params["role"] = role
        if status is not None:
            params["status"] = status
        url = f"{self.base_url}/api/tasks/{int(task_id)}/agent-runs"
        try:
            with httpx.Client(timeout=self.timeout_seconds) as http:
                response = http.get(url, headers=self._headers(), params=params)
        except httpx.HTTPError as exc:
            LOGGER.warning("agent_runs_client.list_for_task network error: %s", exc)
            return None
        if response.status_code != 200:
            LOGGER.warning(
                "agent_runs_client.list_for_task task_id=%s status=%s",
                task_id,
                response.status_code,
            )
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            return None

    def list_by_status(
        self, status: str, *, limit: int = 50
    ) -> Optional[list[Dict[str, Any]]]:
        """GET runs by status across all tasks."""
        if not self.enabled:
            return None
        if status not in AGENT_RUN_STATUSES:
            raise ValueError(f"status {status!r} not in AGENT_RUN_STATUSES")
        url = f"{self.base_url}/api/agent-runs"
        try:
            with httpx.Client(timeout=self.timeout_seconds) as http:
                response = http.get(
                    url,
                    headers=self._headers(),
                    params={"status": status, "limit": int(limit)},
                )
        except httpx.HTTPError as exc:
            LOGGER.warning("agent_runs_client.list_by_status network error: %s", exc)
            return None
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            return None

    def post_audit_comment(self, task_id: int, content: str) -> bool:
        """POST a System-author comment to the linked task.

        Used by the dispatcher to surface terminal outcomes via the
        ``[KAI] COMPLETED ...`` / ``[KAI] FAILED ...`` audit comment format.
        Returns ``True`` on success.
        """
        if not self.enabled:
            return False
        url = f"{self.base_url}/api/tasks/{int(task_id)}/comments"
        try:
            with httpx.Client(timeout=self.timeout_seconds) as http:
                response = http.post(
                    url,
                    headers=self._headers(),
                    json={"agent": "System", "content": content},
                )
        except httpx.HTTPError as exc:
            LOGGER.warning("agent_runs_client.post_audit_comment network error: %s", exc)
            return False
        if response.status_code not in (200, 201):
            LOGGER.warning(
                "agent_runs_client.post_audit_comment task_id=%s status=%s body=%s",
                task_id,
                response.status_code,
                response.text[:200],
            )
            return False
        return True
