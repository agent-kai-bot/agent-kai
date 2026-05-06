"""Typed taskboard lifecycle tools for taskboard-spawned agents."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests
from langchain_core.tools import StructuredTool


DEFAULT_TIMEOUT_SECONDS = 20
REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class TaskboardContext:
    """Connection metadata for taskboard API calls.

    Attributes:
        base_url: Taskboard base URL.
        bearer_token: Bearer token accepted by the taskboard.
        session_token: Optional active task session token.
        session_generation: Optional active task session generation.
        agent_name: Optional display name to use for lifecycle calls.
    """

    base_url: str
    bearer_token: str = ""
    session_token: str = ""
    session_generation: int | None = None
    agent_name: str = ""


def _context_from_environment() -> TaskboardContext:
    """Build taskboard context from environment variables.

    Returns:
        Taskboard connection context.
    """

    return TaskboardContext(
        base_url=os.getenv("TASKBOARD_URL", "http://localhost:8080"),
        bearer_token=(
            os.getenv("TASKBOARD_BEARER_TOKEN", "").strip()
            or os.getenv("OPENCLAW_GATEWAY_TOKEN", "").strip()
            or os.getenv("OPENCLAW_TOKEN", "").strip()
        ),
        session_token=os.getenv("TASKBOARD_SESSION_TOKEN", "").strip(),
        session_generation=_parse_optional_int(
            os.getenv("TASKBOARD_SESSION_GENERATION", "").strip()
        ),
        agent_name=os.getenv("TASKBOARD_AGENT_NAME", "").strip(),
    )


def _parse_optional_int(value: str) -> int | None:
    """Parse an optional integer.

    Args:
        value: Candidate integer string.

    Returns:
        Parsed integer, otherwise ``None``.
    """

    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _redact(text: str, context: TaskboardContext) -> str:
    """Redact known secrets from tool output.

    Args:
        text: Tool output text.
        context: Taskboard connection context.

    Returns:
        Text with known tokens replaced.
    """

    redacted = text
    for secret in (context.bearer_token, context.session_token):
        if secret:
            redacted = redacted.replace(secret, REDACTED)
    return redacted


def _format_response(response: requests.Response, context: TaskboardContext) -> str:
    """Format an HTTP response for agent consumption.

    Args:
        response: HTTP response from taskboard.
        context: Taskboard connection context for token redaction.

    Returns:
        Compact JSON or text response with status.
    """

    try:
        payload: Any = response.json()
    except ValueError:
        payload = response.text
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return _redact(
        json.dumps(
            {
                "ok": 200 <= response.status_code < 300,
                "status_code": response.status_code,
                "body": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if len(body) <= 20_000
        else json.dumps(
            {
                "ok": 200 <= response.status_code < 300,
                "status_code": response.status_code,
                "body_preview": body[:20_000],
                "truncated": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        context,
    )


class TaskboardClient:
    """Small taskboard API client used by LangChain tools.

    Args:
        context: Taskboard connection context.
        timeout_seconds: Per-request timeout.
    """

    def __init__(
        self,
        context: TaskboardContext,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.context = context
        self.timeout_seconds = timeout_seconds

    def get_task(self, task_id: int) -> str:
        """Fetch a task by id.

        Args:
            task_id: Taskboard task id.

        Returns:
            Formatted taskboard response.
        """

        return self._request("GET", f"/api/tasks/{int(task_id)}")

    def get_comments(self, task_id: int) -> str:
        """Fetch task comments.

        Args:
            task_id: Taskboard task id.

        Returns:
            Formatted taskboard response.
        """

        return self._request("GET", f"/api/tasks/{int(task_id)}/comments")

    def get_dependencies(self, task_id: int) -> str:
        """Fetch task dependency information.

        Args:
            task_id: Taskboard task id.

        Returns:
            Formatted taskboard response.
        """

        return self._request("GET", f"/api/tasks/{int(task_id)}/dependencies")

    def start_work(
        self,
        task_id: int,
        agent: str = "",
        token: str = "",
        generation: int | None = None,
    ) -> str:
        """Start work on a task with session metadata.

        Args:
            task_id: Taskboard task id.
            agent: Agent display name. Defaults to context agent.
            token: Session token. Defaults to context session token.
            generation: Session generation. Defaults to context generation.

        Returns:
            Formatted taskboard response.
        """

        params = self._session_params(token=token, generation=generation)
        params["agent"] = agent or self.context.agent_name
        return self._request(
            "POST",
            f"/api/tasks/{int(task_id)}/start-work",
            params=params,
        )

    def comment(
        self,
        task_id: int,
        agent: str,
        content: str,
        token: str = "",
        generation: int | None = None,
    ) -> str:
        """Post a task comment.

        Args:
            task_id: Taskboard task id.
            agent: Comment author display name.
            content: Comment markdown/text.
            token: Optional session token.
            generation: Optional session generation.

        Returns:
            Formatted taskboard response.
        """

        return self._request(
            "POST",
            f"/api/tasks/{int(task_id)}/comments",
            params=self._session_params(token=token, generation=generation),
            json_body={"agent": agent, "content": content},
        )

    def move(
        self,
        task_id: int,
        status: str,
        reason: str = "",
        agent: str = "",
        token: str = "",
        generation: int | None = None,
        force_code_review: bool = False,
        force_security_audit: bool = False,
    ) -> str:
        """Move a task through the taskboard workflow.

        Args:
            task_id: Taskboard task id.
            status: New task status. Accepts legacy values plus SPEC v23
                canonical statuses such as ``Code Review``,
                ``Security Audit``, ``QA``, and ``Ready to Merge``.
            reason: Optional transition reason.
            agent: Actor display name.
            token: Optional session token.
            generation: Optional session generation.
            force_code_review: Whether to request code review spawn.
            force_security_audit: Whether to request security audit spawn.

        Returns:
            Formatted taskboard response.
        """

        params = self._session_params(token=token, generation=generation)
        params.update(
            {
                "status": status,
                "agent": agent or self.context.agent_name,
                "reason": reason,
                "force_code_review": str(bool(force_code_review)).lower(),
                "force_security_audit": str(bool(force_security_audit)).lower(),
            }
        )
        return self._request("POST", f"/api/tasks/{int(task_id)}/move", params=params)

    def stop_work(
        self,
        task_id: int,
        token: str = "",
        generation: int | None = None,
        agent: str = "",
        reason: str = "",
    ) -> str:
        """Stop work on a task.

        Args:
            task_id: Taskboard task id.
            token: Optional session token.
            generation: Optional session generation.
            agent: Optional actor display name.
            reason: Optional stop reason.

        Returns:
            Formatted taskboard response.
        """

        params = self._session_params(token=token, generation=generation)
        if agent or self.context.agent_name:
            params["agent"] = agent or self.context.agent_name
        if reason:
            params["reason"] = reason
        return self._request(
            "POST",
            f"/api/tasks/{int(task_id)}/stop-work",
            params=params,
        )

    def create_action_item(
        self,
        task_id: int,
        agent: str,
        content: str,
        item_type: str = "question",
        comment_id: int | None = None,
    ) -> str:
        """Create a task action item.

        Args:
            task_id: Taskboard task id.
            agent: Action item author.
            content: Action item text.
            item_type: Action item type such as question, completion, blocker.
            comment_id: Optional related comment id.

        Returns:
            Formatted taskboard response.
        """

        body: dict[str, Any] = {
            "agent": agent,
            "content": content,
            "item_type": item_type,
        }
        if comment_id is not None:
            body["comment_id"] = int(comment_id)
        return self._request(
            "POST",
            f"/api/tasks/{int(task_id)}/action-items",
            json_body=body,
        )

    def _session_params(
        self,
        *,
        token: str = "",
        generation: int | None = None,
    ) -> dict[str, Any]:
        """Build session token/generation query parameters.

        Args:
            token: Optional explicit session token.
            generation: Optional explicit session generation.

        Returns:
            Query parameter dictionary.
        """

        params: dict[str, Any] = {}
        effective_token = token or self.context.session_token
        effective_generation = (
            generation
            if generation is not None
            else self.context.session_generation
        )
        if effective_token:
            params["token"] = effective_token
        if effective_generation is not None:
            params["generation"] = int(effective_generation)
        return params

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> str:
        """Send one HTTP request to the taskboard.

        Args:
            method: HTTP method.
            path: Absolute API path.
            params: Optional query parameters.
            json_body: Optional JSON request body.

        Returns:
            Formatted taskboard response or error payload.
        """

        url = urljoin(self.context.base_url.rstrip("/") + "/", path.lstrip("/"))
        headers = {"Content-Type": "application/json"}
        if self.context.bearer_token:
            headers["Authorization"] = f"Bearer {self.context.bearer_token}"
        try:
            response = requests.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=self.timeout_seconds,
            )
            return _format_response(response, self.context)
        except requests.RequestException as exc:
            return _redact(
                json.dumps(
                    {
                        "ok": False,
                        "error": str(exc),
                        "method": method,
                        "path": path,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                self.context,
            )


def create_taskboard_tools(
    context: TaskboardContext | None = None,
) -> list[StructuredTool]:
    """Create typed taskboard tools.

    Args:
        context: Optional explicit context. Environment values are used when
            omitted.

    Returns:
        List of LangChain structured tools for taskboard lifecycle calls.
    """

    client = TaskboardClient(context or _context_from_environment())

    async def _async_call(method_name: str, *args: Any, **kwargs: Any) -> str:
        """Run a synchronous client method without blocking the event loop.

        Args:
            method_name: Name of the client method to call.
            *args: Positional method arguments.
            **kwargs: Keyword method arguments.

        Returns:
            Client method result.
        """

        method = getattr(client, method_name)
        return await asyncio.to_thread(method, *args, **kwargs)

    async def _create_action_item_async(
        task_id: int,
        agent: str,
        content: str,
        item_type: str = "question",
        comment_id: int | None = None,
    ) -> str:
        """Create an action item without blocking the event loop.

        Args:
            task_id: Taskboard task id.
            agent: Action item author.
            content: Action item text.
            item_type: Action item type.
            comment_id: Optional related comment id.

        Returns:
            Client method result.
        """

        return await _async_call(
            "create_action_item",
            task_id,
            agent,
            content,
            item_type=item_type,
            comment_id=comment_id,
        )

    return [
        StructuredTool.from_function(
            func=client.get_task,
            coroutine=lambda task_id: _async_call("get_task", task_id),
            name="taskboard_get_task",
            description="Fetch task details from the taskboard. Input: task_id.",
        ),
        StructuredTool.from_function(
            func=client.get_comments,
            coroutine=lambda task_id: _async_call("get_comments", task_id),
            name="taskboard_get_comments",
            description="Fetch task comments from the taskboard. Input: task_id.",
        ),
        StructuredTool.from_function(
            func=client.get_dependencies,
            coroutine=lambda task_id: _async_call("get_dependencies", task_id),
            name="taskboard_get_dependencies",
            description="Fetch task dependency information. Input: task_id.",
        ),
        StructuredTool.from_function(
            func=client.start_work,
            coroutine=lambda task_id, agent="", token="", generation=None: _async_call(
                "start_work",
                task_id,
                agent=agent,
                token=token,
                generation=generation,
            ),
            name="taskboard_start_work",
            description=(
                "Start work on a task. Inputs: task_id, agent, token, "
                "generation."
            ),
        ),
        StructuredTool.from_function(
            func=client.comment,
            coroutine=(
                lambda task_id,
                agent,
                content,
                token="",
                generation=None: _async_call(
                    "comment",
                    task_id,
                    agent,
                    content,
                    token=token,
                    generation=generation,
                )
            ),
            name="taskboard_comment",
            description=(
                "Post a task comment. Inputs: task_id, agent, content, "
                "token, generation."
            ),
        ),
        StructuredTool.from_function(
            func=client.move,
            coroutine=(
                lambda task_id,
                status,
                reason="",
                agent="",
                token="",
                generation=None,
                force_code_review=False,
                force_security_audit=False: _async_call(
                    "move",
                    task_id,
                    status,
                    reason=reason,
                    agent=agent,
                    token=token,
                    generation=generation,
                    force_code_review=force_code_review,
                    force_security_audit=force_security_audit,
                )
            ),
            name="taskboard_move",
            description=(
                "Move a task through workflow. Inputs: task_id, status, reason, "
                "agent, token, generation, force_code_review, force_security_audit. "
                "Status accepts legacy values plus SPEC v23 canonical states like "
                "Code Review, Security Audit, QA, and Ready to Merge."
            ),
        ),
        StructuredTool.from_function(
            func=client.stop_work,
            coroutine=(
                lambda task_id,
                token="",
                generation=None,
                agent="",
                reason="": _async_call(
                    "stop_work",
                    task_id,
                    token=token,
                    generation=generation,
                    agent=agent,
                    reason=reason,
                )
            ),
            name="taskboard_stop_work",
            description=(
                "Stop work on a task. Inputs: task_id, token, generation, "
                "agent, reason."
            ),
        ),
        StructuredTool.from_function(
            func=client.create_action_item,
            coroutine=_create_action_item_async,
            name="taskboard_create_action_item",
            description=(
                "Create a task action item. Inputs: task_id, agent, content, "
                "item_type, comment_id."
            ),
        ),
    ]
