"""Raw taskboard HTTP client for dispatcher service paths."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urljoin

import requests


DEFAULT_TIMEOUT_SECONDS = 20
REDACTED = "[REDACTED]"
SECRET_ENV_VARS = (
    "TASKBOARD_BEARER_TOKEN",
    "TASKBOARD_SESSION_TOKEN",
    "OPENCLAW_GATEWAY_TOKEN",
    "OPENCLAW_TOKEN",
)


class TaskboardServiceError(RuntimeError):
    """Raised when a raw taskboard service request fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class TaskboardServiceClient:
    """LangChain-free taskboard API client for dispatcher/orchestrator code."""

    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.bearer_token = bearer_token.strip()
        self.timeout_seconds = timeout_seconds

    def fetch_task(self, task_id: int) -> dict[str, Any]:
        """Fetch one taskboard task as a full JSON dictionary."""

        payload = self._request_json("GET", f"/api/tasks/{int(task_id)}")
        if not isinstance(payload, dict):
            raise TaskboardServiceError(
                "taskboard fetch_task response was not a JSON object",
                body=self._redact_body(payload),
            )
        return payload

    def post_audit_comment(self, task_id: int, content: str) -> dict[str, Any]:
        """Post a dispatcher audit comment and return the parsed JSON response."""

        payload = self._request_json(
            "POST",
            f"/api/tasks/{int(task_id)}/comments",
            params={},
            json_body={
                "agent": _audit_actor_for_content(content),
                "content": content,
            },
        )
        if not isinstance(payload, dict):
            raise TaskboardServiceError(
                "taskboard comment response was not a JSON object",
                body=self._redact_body(payload),
            )
        return payload

    def move_task_status(
        self,
        task_id: int,
        status: str,
        *,
        reason: str = "",
        agent: str = "Orchestrator",
    ) -> dict[str, Any]:
        """Move one task through the taskboard workflow as a service actor."""

        payload = self._request_json(
            "POST",
            f"/api/tasks/{int(task_id)}/move",
            params={
                "status": str(status),
                "agent": str(agent),
                "reason": str(reason),
            },
        )
        if not isinstance(payload, dict):
            raise TaskboardServiceError(
                "taskboard move response was not a JSON object",
                body=self._redact_body(payload),
            )
        return payload

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Send one raw taskboard request and return parsed JSON without truncation."""

        url = urljoin(self.base_url.rstrip("/") + "/", path.lstrip("/"))
        headers = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"

        try:
            response = requests.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise TaskboardServiceError(
                self._redact(f"taskboard request failed: {exc}"),
                status_code=None,
                body=None,
            ) from exc

        if not 200 <= response.status_code < 300:
            body = self._response_body(response)
            redacted_body = self._redact_body(body)
            diagnostic = self._redact(_stringify_body(redacted_body))
            raise TaskboardServiceError(
                (
                    f"taskboard request failed: {method} {path} "
                    f"status={response.status_code} body={diagnostic}"
                ),
                status_code=response.status_code,
                body=redacted_body,
            )

        try:
            return response.json()
        except ValueError as exc:
            body = response.text
            redacted_body = self._redact_body(body)
            diagnostic = self._redact(_stringify_body(redacted_body))
            raise TaskboardServiceError(
                (
                    f"taskboard response was not JSON: {method} {path} "
                    f"status={response.status_code} body={diagnostic}"
                ),
                status_code=response.status_code,
                body=redacted_body,
            ) from exc

    def _response_body(self, response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text

    def _redact(self, text: str) -> str:
        redacted = _redact_known_secrets(text)
        if self.bearer_token:
            redacted = redacted.replace(self.bearer_token, REDACTED)
        return redacted

    def _redact_body(self, body: Any) -> Any:
        return _redact_value(body, self._redact)


def _audit_actor_for_content(content: str) -> str:
    if content.startswith("[System]"):
        return "System"
    return "Orchestrator"


def _redact_known_secrets(text: str) -> str:
    redacted = str(text)
    for env_name in SECRET_ENV_VARS:
        secret = os.getenv(env_name, "").strip()
        if secret:
            redacted = redacted.replace(secret, REDACTED)
    return redacted


def _redact_value(value: Any, redactor: Any) -> Any:
    if isinstance(value, str):
        return redactor(value)
    if isinstance(value, list):
        return [_redact_value(item, redactor) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item, redactor) for key, item in value.items()}
    return value


def _stringify_body(body: Any) -> str:
    if isinstance(body, str):
        return body
    try:
        return json.dumps(body, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(body)
