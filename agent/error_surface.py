"""Typed operator-facing error classification for agent/runtime failures."""

from __future__ import annotations

from dataclasses import dataclass
import traceback
from typing import Any


@dataclass(frozen=True)
class SurfaceError:
    """Structured error payload safe to send to daemon clients."""

    error_class: str
    error_message: str
    actionable_hint: str
    underlying_traceback: str | None = None

    def display_message(self, prefix: str | None = None) -> str:
        head = f"[{self.error_class}] {self.error_message}"
        if prefix:
            head = f"{prefix}: {head}"
        if self.actionable_hint:
            return f"{head} Hint: {self.actionable_hint}"
        return head

    def to_event(self, *, prefix: str | None = None) -> dict[str, Any]:
        return {
            "type": "error",
            "data": self.display_message(prefix),
            "error_class": self.error_class,
            "error_message": self.error_message,
            "underlying_traceback": self.underlying_traceback,
            "actionable_hint": self.actionable_hint,
        }

    def to_envelope_fields(self) -> dict[str, Any]:
        return {
            "error_class": self.error_class,
            "error_message": self.error_message,
            "underlying_traceback": self.underlying_traceback,
            "actionable_hint": self.actionable_hint,
        }


def surface_exception(
    exc: BaseException,
    *,
    include_traceback: bool = True,
) -> SurfaceError:
    """Classify an exception and preserve the useful cause-chain detail."""

    error_message = _exception_chain_message(exc)
    error_class, actionable_hint = _classify_error(exc, error_message)
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return SurfaceError(
        error_class=error_class,
        error_message=error_message,
        actionable_hint=actionable_hint,
        underlying_traceback=tb if include_traceback else None,
    )


def surface_message(message: Any, *, fallback_class: str = "agent_runtime_exception") -> SurfaceError:
    """Classify a string message when the original exception is unavailable."""

    text = str(message or "").strip() or fallback_class
    error_class, actionable_hint = _classify_error_text(text, fallback_class=fallback_class)
    return SurfaceError(
        error_class=error_class,
        error_message=text,
        actionable_hint=actionable_hint,
        underlying_traceback=None,
    )


def log_fields(error: SurfaceError) -> dict[str, str]:
    """Return stable key/value fields for structured log callsites."""

    return {
        "error_class": error.error_class,
        "error_message": error.error_message,
        "actionable_hint": error.actionable_hint,
    }


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _exception_chain_message(exc: BaseException) -> str:
    parts: list[str] = []
    for item in _exception_chain(exc):
        message = str(item).strip() or item.__class__.__name__
        parts.append(f"{item.__class__.__name__}: {message}")
    return "; caused by ".join(parts)


def _classify_error(exc: BaseException, message: str) -> tuple[str, str]:
    class_chain = " ".join(item.__class__.__name__ for item in _exception_chain(exc))
    return _classify_error_text(f"{class_chain} {message}")


def _classify_error_text(
    message: str,
    *,
    fallback_class: str = "agent_runtime_exception",
) -> tuple[str, str]:
    text = message.lower()

    if "codex cli timed out" in text:
        return (
            "codex_cli_timeout",
            "Codex CLI did not answer before the configured timeout; retry after checking Codex auth and local CLI responsiveness.",
        )

    if "nats: timeout" in text:
        return (
            "nats_timeout",
            "Check that the target sub-agent is running and subscribed, then retry the NATS request.",
        )

    if (
        "empty response from server when expecting info message" in text
        or "unexpected eof" in text
        or "nats bus is not connected" in text
    ):
        return (
            "nats_connection_error",
            "Check the NATS broker process/container and daemon NATS URL; the daemon will try to reconnect.",
        )

    if "sent 1012" in text or "service restart" in text:
        return (
            "websocket_service_restart",
            "The daemon or websocket server restarted while the client was connected; reconnect the browser session.",
        )

    if (
        "connectionclosederror" in text
        or "websocketdisconnect" in text
        or "no close frame received or sent" in text
        or 'websocket is not connected. need to call "accept" first' in text
    ):
        return (
            "websocket_dropped",
            "The browser or proxy dropped the websocket; reconnect the session and check reverse-proxy websocket timeouts.",
        )

    if (
        "apiconnectionerror" in text
        or "connection error" in text
        or "connecterror" in text
        or "connection refused" in text
        or "name or service not known" in text
        or "temporary failure in name resolution" in text
    ):
        return (
            "codex_transport_connection_error",
            "Check Codex/ChatGPT endpoint connectivity, DNS, and local network reachability, then retry.",
        )

    if "timeout" in text or "timed out" in text or "readtimeout" in text:
        return (
            "endpoint_timeout",
            "The upstream endpoint did not answer before its timeout; retry or increase the endpoint timeout if this is expected.",
        )

    return (
        fallback_class,
        "Check the daemon log for the traceback and retry after the upstream failure is corrected.",
    )
