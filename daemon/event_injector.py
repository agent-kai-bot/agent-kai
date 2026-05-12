"""Shared daemon event-to-prompt injection helpers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from agent_logger import get_logger
from daemon.core import message_timestamp_now


class SafeFormatDict(dict):
    """Format mapping that keeps unknown placeholders visible."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass(frozen=True)
class EventInjectionTemplate:
    """Git-tracked prompt template rendered for daemon event turns."""

    name: str
    path: Path
    content: str

    @classmethod
    def load(cls, template_path: str | Path) -> "EventInjectionTemplate":
        path = Path(template_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / path
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise FileNotFoundError(
                f"event prompt template is not readable: {path}"
            ) from exc
        return cls(name=path.name, path=path, content=content)

    def render_map(self, values: dict[str, Any]) -> str:
        return self.content.format_map(SafeFormatDict(values))

    def render(self, event: Any | None = None, **values: Any) -> str:
        render_values = dict(values)
        if event is not None:
            if hasattr(event, "to_template_values"):
                render_values.update(event.to_template_values())
            elif hasattr(event, "to_event_payload"):
                render_values.update(event.to_event_payload())
            elif isinstance(event, dict):
                render_values.update(event)
        return self.render_map(render_values)


@dataclass(frozen=True)
class EventInjectionPolicy:
    """Controls suppression and run behavior for one daemon event source."""

    source: str
    drop_topic: str
    injected_topic: str
    active_attr: str
    timestamp_attr: str
    max_injected_turns_per_hour: int = 0
    require_subscription_attr: str | None = None
    require_auto_mode: bool = True
    suppress_drop_reasons: frozenset[str] = field(default_factory=frozenset)
    busy_reason: str = "busy"
    active_reason: str = "event_turn_active"
    single_auto_iteration: bool = True
    prefetch_polymarket_bbo: bool = False
    prefetch_polymarket_token_info: bool = False


@dataclass(frozen=True)
class EventInjectionRequest:
    """One prompt-injection request for an already selected target session."""

    event: Any
    template: EventInjectionTemplate
    policy: EventInjectionPolicy
    render_values: dict[str, Any]
    seq: int | str
    monotonic_seconds: float
    job_id: str
    task_name: str
    injected_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EventInjectionDecision:
    ok: bool
    reason: str


class EventInjector:
    """Shared guarded prompt-injection coordinator for daemon event sources."""

    def __init__(
        self,
        *,
        run_input: Callable[..., Awaitable[Any]],
        background_exception_handler: Callable[[asyncio.Task[Any]], None] | None = None,
        log: Any | None = None,
    ) -> None:
        self._run_input = run_input
        self._background_exception_handler = background_exception_handler
        self.log = log or get_logger("daemon.event_injector")

    def injection_decision(self, managed: Any, request: EventInjectionRequest) -> EventInjectionDecision:
        session = managed.session
        policy = request.policy
        if policy.require_subscription_attr is not None and not bool(
            getattr(session, policy.require_subscription_attr, False)
        ):
            return EventInjectionDecision(False, "not_subscribed")
        if policy.require_auto_mode and not session.auto_mode:
            return EventInjectionDecision(False, "auto_mode_disabled")
        if policy.max_injected_turns_per_hour <= 0:
            return EventInjectionDecision(False, "rate_limit_disabled")
        if managed.input_lock.locked() or (
            managed.current_input_task is not None and not managed.current_input_task.done()
        ):
            return EventInjectionDecision(False, policy.busy_reason)
        runner = session.agent_runner
        if runner is None:
            return EventInjectionDecision(False, "runtime_not_attached")
        if bool(getattr(runner, "_is_auto_continuation", False)):
            return EventInjectionDecision(False, "auto_continuing")
        if bool(getattr(runner, "tool_call_active", False)) or getattr(runner, "_active_recorder", None) is not None:
            return EventInjectionDecision(False, "mid_tool_call")
        if bool(getattr(session, policy.active_attr, False)):
            return EventInjectionDecision(False, policy.active_reason)
        self._prune_injections(session, policy.timestamp_attr, request.monotonic_seconds)
        if len(getattr(session, policy.timestamp_attr)) >= policy.max_injected_turns_per_hour:
            return EventInjectionDecision(False, "rate_limited")
        return EventInjectionDecision(True, "ok")

    async def handle(self, managed: Any, request: EventInjectionRequest) -> EventInjectionDecision:
        decision = self.injection_decision(managed, request)
        if not decision.ok:
            self.publish_drop(managed, request, decision.reason)
            return decision
        setattr(managed.session, request.policy.active_attr, True)
        task = asyncio.create_task(
            self.run_turn(managed, request),
            name=request.task_name,
        )
        if self._background_exception_handler is not None:
            task.add_done_callback(self._background_exception_handler)
        return decision

    def publish_drop(self, managed: Any, request: EventInjectionRequest, reason: str) -> None:
        if reason in request.policy.suppress_drop_reasons:
            return
        managed.session.publish_event(
            request.policy.drop_topic,
            {"seq": request.seq, "reason": reason},
        )

    async def run_turn(self, managed: Any, request: EventInjectionRequest) -> None:
        session = managed.session
        policy = request.policy
        try:
            if managed.input_lock.locked() or (
                managed.current_input_task is not None and not managed.current_input_task.done()
            ):
                self.publish_drop(managed, request, policy.busy_reason)
                return
            from datetime import datetime, timezone
            request.render_values.setdefault(
                "inject_ts", datetime.now(timezone.utc).isoformat()
            )
            if policy.prefetch_polymarket_bbo:
                bbo = await _fetch_polymarket_bbo_safe(
                    request.render_values.get("token_id")
                )
                request.render_values["live_bbo_ts"] = bbo["ts"]
                request.render_values["live_bbo_bid"] = bbo["bid"]
                request.render_values["live_bbo_ask"] = bbo["ask"]
                request.render_values["live_bbo_source"] = bbo["source"]
                request.render_values["live_bbo_stale"] = bbo["stale"]
                request.render_values["live_bbo_error"] = bbo["error"]
            if policy.prefetch_polymarket_token_info:
                tok_info = _resolve_polymarket_token_safe(
                    request.render_values.get("token_id")
                )
                request.render_values["token_market_slug"] = tok_info["slug"]
                request.render_values["token_market_title"] = tok_info["title"]
                request.render_values["token_outcome"] = tok_info["outcome"]
                request.render_values["token_category"] = tok_info["category"]
                request.render_values["token_summary"] = tok_info["summary"]
                request.render_values["token_resolve_error"] = tok_info["error"]
            try:
                prompt = request.template.render_map(request.render_values)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("%s prompt render failed for %s: %s", policy.source, session.name, exc)
                self.publish_drop(managed, request, "template_render_failed")
                return

            session.chat_history.append(HumanMessage(
                content=prompt,
                additional_kwargs={"timestamp": message_timestamp_now()},
            ))
            session.agent_runner.chat_history = session.chat_history
            self._record_injection(session, policy.timestamp_attr, request.monotonic_seconds)
            injected_payload = {**request.injected_payload}
            injected_payload.setdefault("seq", request.seq)
            injected_payload.setdefault("template_name", request.template.name)
            injected_payload.setdefault("chars_injected", len(prompt))
            session.publish_event(policy.injected_topic, injected_payload)
            await self._run_input(
                managed,
                prompt,
                source=policy.source,
                job_id=request.job_id,
                single_auto_iteration=policy.single_auto_iteration,
                pre_injected_input=True,
            )
        finally:
            setattr(session, policy.active_attr, False)

    @staticmethod
    def _record_injection(session: Any, timestamp_attr: str, monotonic_seconds: float) -> None:
        if timestamp_attr == "heartbeat_injection_timestamps" and hasattr(session, "record_heartbeat_injection"):
            session.record_heartbeat_injection(monotonic_seconds)
            return
        getattr(session, timestamp_attr).append(float(monotonic_seconds))

    @staticmethod
    def _prune_injections(session: Any, timestamp_attr: str, now: float, *, window_seconds: float = 3600.0) -> None:
        if timestamp_attr == "heartbeat_injection_timestamps" and hasattr(session, "prune_heartbeat_injections"):
            session.prune_heartbeat_injections(now, window_seconds=window_seconds)
            return
        timestamps = getattr(session, timestamp_attr)
        cutoff = float(now) - float(window_seconds)
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()


def stable_json(value: Any) -> str:
    """Return deterministic JSON for template payload fields."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


_LOCAL_FIRST_PATH = "/home/atc/git/OPS/vpn-stack/scripts/lib"
_AGENT_KAI_SHARED_PATH = "/home/atc/git/OPS/agent-kai-shared"


def _resolve_polymarket_token_safe(token_id: Any) -> dict[str, str]:
    """Pre-resolve polymarket token_id -> {slug, title, outcome, category}.

    Two-tier cached (in-process LRU + on-disk 24h TTL via token_resolver).
    Always returns string values for template rendering. Errors caught.
    """
    out = {
        "slug": "",
        "title": "",
        "outcome": "",
        "category": "",
        "summary": "",
        "error": "",
    }
    if not token_id:
        out["error"] = "no_token_id"
        return out
    try:
        import sys
        if _AGENT_KAI_SHARED_PATH not in sys.path:
            sys.path.insert(0, _AGENT_KAI_SHARED_PATH)
        from token_resolver import resolve_token  # type: ignore
        info = resolve_token(str(token_id))
        if isinstance(info, dict):
            out["slug"] = str(info.get("slug", ""))
            out["title"] = str(info.get("title", ""))
            out["outcome"] = str(info.get("outcome", ""))
            out["category"] = str(info.get("category", ""))
            out["summary"] = (
                f"[{out['category']}] {out['title']} :: {out['outcome']}"
            )
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


async def _fetch_polymarket_bbo_safe(token_id: Any) -> dict[str, str]:
    """Pre-fetch live polymarket BBO for inject-time enrichment.

    Always returns string values for template rendering. Errors are
    caught and reported in the `error` field; render still proceeds.
    """
    from datetime import datetime, timezone
    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "bid": "",
        "ask": "",
        "source": "",
        "stale": "",
        "error": "",
    }
    if not token_id:
        out["error"] = "no_token_id"
        return out
    try:
        import sys
        if _LOCAL_FIRST_PATH not in sys.path:
            sys.path.insert(0, _LOCAL_FIRST_PATH)
        from local_first import polymarket  # type: ignore
        result = await polymarket.best_bid_ask_async(
            str(token_id), allow_rest_fallback=True
        )
        if isinstance(result, dict):
            if result.get("ok"):
                out["bid"] = str(result.get("bid", ""))
                out["ask"] = str(result.get("ask", ""))
                out["source"] = str(result.get("source", ""))
                out["stale"] = str(bool(result.get("stale", False)))
                if result.get("ts_event"):
                    out["ts"] = str(result["ts_event"])
            else:
                out["error"] = str(result.get("error") or result.get("detail") or "unknown")
                out["source"] = str(result.get("source", ""))
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
