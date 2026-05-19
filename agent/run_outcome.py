"""Map raw dispatcher / agent-runtime events to ``agent_runs`` ledger statuses.

The taskboard's ``agent_runs`` ledger (Phase 1 of epic #10028, taskboard task
#10223) uses two closed enums — ``status`` and ``failure_class`` — to record
agent run lifecycles in a way that can be queried, alerted on, and surfaced
in operator UX. This module is the single place that turns the raw events the
KAI dispatcher and agent runtime produce into rows the ledger accepts.

Without this central derivation, every callsite would invent its own mapping
from "the agent died with this Python exception" to "this is the failure
class," and we would inevitably drift back into the silent-string-of-the-day
pattern that hid the recent 5-day outage.

Inputs handled:

* AgentRunner events (``error``, ``final``, ``auto_stopped``, ``token``,
  ``tool_start``) — see ``agent/core.py``.
* Spawn-side preflight failures (config validation, endpoint smoke).
* Stuck-session detector aborts.
* Manual cancellations from operator commands.

Outputs:

* ``RunOutcome(status, failure_class, failure_detail)`` — the canonical tuple
  the dispatcher writes to the ledger via PATCH.

Both ``status`` and ``failure_class`` come from the closed enums
duplicated in :mod:`agent.run_outcome`. They MUST stay in lockstep with the
taskboard side (``app.py:AGENT_RUN_STATUSES`` /
``AGENT_RUN_FAILURE_CLASSES``); a regression test in
``tests/test_run_outcome.py`` enforces equality.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional


# ---------------------------------------------------------------------------
# Closed enum reproductions. Keep these in lockstep with the taskboard:
#   /home/atc/git/OPS/openclawdev-taskboard/app.py
# A regression test asserts the sets are equal across the two sides so config
# drift between the two repos can't reintroduce silent skips.
# ---------------------------------------------------------------------------

AGENT_RUN_STATUSES: frozenset[str] = frozenset({
    "queued",
    "dispatching",
    "preflight_failed",
    "spawning",
    "running",
    "succeeded",
    "failed",
    "requires_approval_blocked",
    "endpoint_failed",
    "config_invalid",
    "taskboard_write_failed",
    "forgejo_failed",
    "timeout",
    "stuck_aborted",
    "duplicate_suppressed",
    "cancelled",
})

AGENT_RUN_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "preflight_failed",
    "succeeded",
    "failed",
    "requires_approval_blocked",
    "endpoint_failed",
    "config_invalid",
    "taskboard_write_failed",
    "forgejo_failed",
    "timeout",
    "stuck_aborted",
    "duplicate_suppressed",
    "cancelled",
})

AGENT_RUN_FAILURE_STATUSES: frozenset[str] = AGENT_RUN_TERMINAL_STATUSES - {
    "succeeded",
    "duplicate_suppressed",
    "cancelled",
}

AGENT_RUN_FAILURE_CLASSES: frozenset[str] = frozenset({
    "endpoint_unreachable",
    "endpoint_unauthorized",
    "endpoint_empty_response",
    "endpoint_rate_limited",
    "endpoint_timeout",
    "endpoint_invalid_response",
    "endpoint_transport_drop",
    "config_placeholder_value",
    "config_missing_required",
    "config_unresolved_hostname",
    "config_stale",
    "auth_taskboard_token_invalid",
    "auth_forgejo_token_invalid",
    "auth_agent_identity_missing",
    "tool_approval_blocked",
    "tool_filesystem_denied",
    "tool_runtime_exception",
    "tool_unknown_failure",
    "forgejo_pr_not_found",
    "forgejo_branch_diverged",
    "taskboard_status_conflict",
    "session_exceeded_iterations",
    "wall_clock_budget_exceeded",
    "session_stuck_no_progress",
    "manual_cancellation",
    "outage_period_silent_failure",
})


@dataclass(frozen=True)
class RunOutcome:
    """The triple of fields the dispatcher writes to ``agent_runs`` PATCH.

    Attributes:
        status: One of :data:`AGENT_RUN_STATUSES`. For non-terminal cases the
            dispatcher should not call this module — it should pass values
            like ``"running"`` directly. This module is the source of truth
            for terminal status derivation.
        failure_class: One of :data:`AGENT_RUN_FAILURE_CLASSES` if status is a
            failure status, otherwise ``None``.
        failure_detail: Human-readable single-line detail. Truncated to 500
            chars to keep the ledger row compact.
    """

    status: str
    failure_class: Optional[str]
    failure_detail: Optional[str]

    def __post_init__(self) -> None:
        if self.status not in AGENT_RUN_STATUSES:
            raise ValueError(
                f"RunOutcome.status {self.status!r} not in AGENT_RUN_STATUSES"
            )
        if self.status in AGENT_RUN_FAILURE_STATUSES:
            if self.failure_class is None:
                raise ValueError(
                    f"failure_class is required for status={self.status!r}"
                )
            if self.failure_class not in AGENT_RUN_FAILURE_CLASSES:
                raise ValueError(
                    f"failure_class {self.failure_class!r} not in "
                    "AGENT_RUN_FAILURE_CLASSES"
                )
        else:
            if self.failure_class is not None:
                raise ValueError(
                    f"failure_class must be None for status={self.status!r}"
                )


_DETAIL_MAX_CHARS = 500


def _truncate(detail: Optional[str]) -> Optional[str]:
    if detail is None:
        return None
    text = str(detail).strip()
    if not text:
        return None
    if len(text) <= _DETAIL_MAX_CHARS:
        return text
    return text[: _DETAIL_MAX_CHARS - 1] + "…"


def _normalize_data_for_match(data: Any) -> str:
    """Serialize an event data field to a lowercase searchable string."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data.lower()
    if isinstance(data, Mapping):
        return " ".join(f"{k}={v}" for k, v in data.items()).lower()
    return str(data).lower()


# ---------------------------------------------------------------------------
# Derivation entry points
# ---------------------------------------------------------------------------


def derive_outcome_from_agent_events(
    events: Iterable[Mapping[str, Any]],
    *,
    final_text: Optional[str] = None,
) -> RunOutcome:
    """Derive a :class:`RunOutcome` from a finished agent's event stream.

    The agent runtime emits an event stream that ends with one of:

    * ``{"type": "final", "data": "..."}`` — agent produced a non-empty
      response. This is success unless the response is the canonical empty
      sentinel (``Error: agent returned an empty response.``).
    * ``{"type": "error", "data": "..."}`` — wrapped exception from the LLM
      endpoint or executor.
    * ``{"type": "auto_stopped", "data": {"reason": "..."}}`` — auto-mode
      policy halted the run before completion.

    This function inspects the *last* relevant event of each kind and returns
    the most-specific terminal outcome.

    Args:
        events: Event dicts produced by the agent runtime in chronological
            order. The last error/auto_stopped/final wins.
        final_text: Optional final text the runtime captured. When non-empty
            and not the canonical empty-response sentinel, it implies success.

    Returns:
        The derived :class:`RunOutcome`.
    """

    last_error: Optional[Mapping[str, Any]] = None
    last_auto_stopped: Optional[Mapping[str, Any]] = None
    last_final: Optional[Mapping[str, Any]] = None

    for event in events:
        wrapped = event.get("event") if isinstance(event, Mapping) else None
        ev = wrapped if isinstance(wrapped, Mapping) else event
        if not isinstance(ev, Mapping):
            continue
        ev_type = ev.get("type")
        if ev_type == "error":
            last_error = ev
        elif ev_type == "auto_stopped":
            last_auto_stopped = ev
        elif ev_type == "final":
            last_final = ev

    # Order of precedence:
    #   1. A final [AUTO_STATE: done] beats any late auto_stopped sentinel.
    #      The runtime can emit both when a last turn completes successfully
    #      and then an auto-mode guard also fires.
    #   2. exhausted Codex transport retries beat the later malformed-footer
    #      auto_stopped symptom.
    #   3. auto_stopped beats other cases (we want the gate reason)
    #   4. error beats final (a final after error is the "agent returned
    #      an empty response" sentinel)
    #   5. final implies success unless empty sentinel
    if (
        last_auto_stopped is not None
        and last_error is None
        and _final_text_declares_done(last_final, final_text)
    ):
        return RunOutcome(
            status="succeeded",
            failure_class=None,
            failure_detail=None,
        )
    if last_error is not None and _error_is_codex_transport_drop(last_error):
        return _outcome_from_error(last_error)
    if last_auto_stopped is not None:
        return _outcome_from_auto_stopped(last_auto_stopped)
    if last_error is not None:
        return _outcome_from_error(last_error)
    if last_final is not None:
        return _outcome_from_final(last_final, final_text)

    # No terminal events -> the run was likely killed externally.
    return RunOutcome(
        status="stuck_aborted",
        failure_class="session_stuck_no_progress",
        failure_detail=_truncate(final_text or "no terminal event observed"),
    )


def derive_outcome_from_preflight_failure(
    *,
    reason: str,
    detail: str,
) -> RunOutcome:
    """Build a terminal outcome for a preflight failure (#10221).

    Args:
        reason: Short kebab-case reason matching one of the preflight
            failure classes (``config_placeholder_value``,
            ``config_missing_required``, ``config_unresolved_hostname``,
            ``config_stale``, etc).
        detail: Human-readable single-line detail.
    """
    if reason == "config_stale":
        failure_class = "config_stale"
    elif reason in ("config_missing_required", "config_placeholder_value", "config_unresolved_hostname"):
        failure_class = reason
    elif reason == "endpoint_unreachable":
        failure_class = "endpoint_unreachable"
    elif reason == "endpoint_unauthorized":
        failure_class = "endpoint_unauthorized"
    elif reason == "auth_agent_identity_missing":
        failure_class = "auth_agent_identity_missing"
    else:
        failure_class = "config_missing_required"
    return RunOutcome(
        status="preflight_failed",
        failure_class=failure_class,
        failure_detail=_truncate(detail),
    )


def derive_outcome_from_stuck_session(detail: str) -> RunOutcome:
    """Build a terminal outcome for a stuck-session detector abort."""
    return RunOutcome(
        status="stuck_aborted",
        failure_class="session_stuck_no_progress",
        failure_detail=_truncate(detail),
    )


def derive_outcome_from_manual_cancel(detail: str) -> RunOutcome:
    """Build a terminal outcome for an operator-initiated cancel."""
    return RunOutcome(
        status="cancelled",
        failure_class=None,
        failure_detail=_truncate(detail),
    )


def derive_outcome_from_duplicate(detail: str) -> RunOutcome:
    """Build a terminal outcome for a deduped fire."""
    return RunOutcome(
        status="duplicate_suppressed",
        failure_class=None,
        failure_detail=_truncate(detail),
    )


def derive_outcome_from_outage_backfill(detail: str) -> RunOutcome:
    """Build a terminal outcome for backfilling silent-outage runs.

    Used by the scripts/backfill_outage_runs.py helper to retroactively
    record runs that died during the 2026-04-25 → 2026-04-30 stale-config
    window.
    """
    return RunOutcome(
        status="endpoint_failed",
        failure_class="outage_period_silent_failure",
        failure_detail=_truncate(detail),
    )


# ---------------------------------------------------------------------------
# Internal: classify error / auto_stopped / final events
# ---------------------------------------------------------------------------

_EMPTY_RESPONSE_SENTINEL = "error: agent returned an empty response."
_AUTO_STATE_DONE_RE = re.compile(
    r"^\[AUTO_STATE:\s*done(?:\s*\|[^\]]*)?\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_PYTHON_EXCEPTION_RE = re.compile(
    r"(?:^|[\s(])(?P<class>[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))\s*:",
    re.MULTILINE,
)
_TRANSPORT_DROP_MARKERS = (
    "peer closed connection",
    "incomplete chunked read",
    "connection reset",
)


def _final_text_declares_done(
    event: Optional[Mapping[str, Any]],
    explicit_final_text: Optional[str],
) -> bool:
    data = event.get("data") if isinstance(event, Mapping) else None
    final_text = (
        explicit_final_text
        if explicit_final_text not in (None, "")
        else (data if isinstance(data, str) else "")
    )
    return bool(final_text and _AUTO_STATE_DONE_RE.search(str(final_text).strip()))


def _auto_stopped_reason(event: Mapping[str, Any]) -> str:
    data = event.get("data")
    if isinstance(data, Mapping):
        return str(data.get("reason") or "").strip().lower()
    return _normalize_data_for_match(data).strip()


def _auto_stop_detail(data: Any, *, fallback: str = "auto_stopped") -> str:
    if not isinstance(data, Mapping):
        return str(data or fallback).strip() or fallback

    reason = str(data.get("reason") or fallback).strip() or fallback
    elapsed = data.get("elapsed_seconds")
    if isinstance(elapsed, (int, float)):
        return f"{reason}; elapsed={float(elapsed):.1f}s"
    return reason


def _error_is_codex_transport_drop(event: Mapping[str, Any]) -> bool:
    raw = _normalize_data_for_match(event.get("data"))
    return "codex transport retry exhausted" in raw or (
        "primary endpoint failed" in raw
        and any(marker in raw for marker in _TRANSPORT_DROP_MARKERS)
    )


def derive_outcome_from_exception(exc: BaseException) -> RunOutcome:
    """Build a failed outcome for an uncaught runtime exception.

    The taskboard failure class stays a closed, queryable enum while the detail
    preserves the raw Python exception class that identifies the crash.
    """

    return RunOutcome(
        status="failed",
        failure_class="tool_runtime_exception",
        failure_detail=_truncate(f"{type(exc).__name__}: {exc}"),
    )


def _outcome_from_error(event: Mapping[str, Any]) -> RunOutcome:
    raw = _normalize_data_for_match(event.get("data"))
    detail = str(event.get("data") or "").strip() or "primary endpoint failed"

    # The "Primary endpoint returned an empty response." string comes from
    # agent/core.py:836 — handle it before the "primary endpoint failed"
    # branch so it doesn't fall through to the generic invalid-response class.
    if "primary endpoint returned an empty response" in raw or "empty response" in raw:
        return RunOutcome(
            status="endpoint_failed",
            failure_class="endpoint_empty_response",
            failure_detail=_truncate(detail),
        )

    # Match by substring against the canonical wrapped error strings the
    # agent runtime emits in agent/core.py.
    if "primary endpoint failed" in raw:
        # Drill into the underlying cause keyword.
        if "codex transport retry exhausted" in raw or any(
            marker in raw for marker in _TRANSPORT_DROP_MARKERS
        ):
            return RunOutcome(
                status="endpoint_failed",
                failure_class="endpoint_transport_drop",
                failure_detail=_truncate(detail),
            )
        if "connection error" in raw or "connecterror" in raw:
            return RunOutcome(
                status="endpoint_failed",
                failure_class="endpoint_unreachable",
                failure_detail=_truncate(detail),
            )
        if "401" in raw or "unauthorized" in raw or "missing-" in raw:
            return RunOutcome(
                status="endpoint_failed",
                failure_class="endpoint_unauthorized",
                failure_detail=_truncate(detail),
            )
        if "429" in raw or "rate limit" in raw:
            return RunOutcome(
                status="endpoint_failed",
                failure_class="endpoint_rate_limited",
                failure_detail=_truncate(detail),
            )
        if "timeout" in raw or "timed out" in raw:
            return RunOutcome(
                status="endpoint_failed",
                failure_class="endpoint_timeout",
                failure_detail=_truncate(detail),
            )
        if "empty response" in raw:
            return RunOutcome(
                status="endpoint_failed",
                failure_class="endpoint_empty_response",
                failure_detail=_truncate(detail),
            )
        return RunOutcome(
            status="endpoint_failed",
            failure_class="endpoint_invalid_response",
            failure_detail=_truncate(detail),
        )

    if "endpoint #" in raw and "failed" in raw:
        # Fallback chain hit. Treat similarly to primary failure.
        if "connection" in raw:
            return RunOutcome(
                status="endpoint_failed",
                failure_class="endpoint_unreachable",
                failure_detail=_truncate(detail),
            )
        return RunOutcome(
            status="endpoint_failed",
            failure_class="endpoint_invalid_response",
            failure_detail=_truncate(detail),
        )

    if "iterations_remaining=0" in raw or "iteration budget" in raw:
        return RunOutcome(
            status="timeout",
            failure_class="session_exceeded_iterations",
            failure_detail=_truncate(detail),
        )

    if "wall-clock budget exceeded" in raw or "wall clock budget exceeded" in raw:
        return RunOutcome(
            status="failed",
            failure_class="wall_clock_budget_exceeded",
            failure_detail=_truncate(detail),
        )

    if "timed out after" in raw and ("codex cli" in raw or "claude cli" in raw):
        return RunOutcome(
            status="failed",
            failure_class="wall_clock_budget_exceeded",
            failure_detail=_truncate(detail),
        )

    if _PYTHON_EXCEPTION_RE.search(detail):
        return RunOutcome(
            status="failed",
            failure_class="tool_runtime_exception",
            failure_detail=_truncate(detail),
        )

    return RunOutcome(
        status="failed",
        failure_class="tool_unknown_failure",
        failure_detail=_truncate(detail),
    )


_AUTO_STOPPED_SUCCESS_REASONS = (
    "task complete",
    "task completed",
    "completed",
    "done",
    "finished",
    "auto_state: done",
)


def _outcome_from_auto_stopped(event: Mapping[str, Any]) -> RunOutcome:
    data = event.get("data")
    if isinstance(data, Mapping):
        reason = str(data.get("reason") or "").strip().lower()
        detail = _auto_stop_detail(data, fallback="auto_stopped (no reason)")
    else:
        reason = _normalize_data_for_match(data)
        detail = str(data or "auto_stopped").strip() or "auto_stopped"

    # Phase 1 fix (#10229 follow-up): an auto_stopped with a "task complete"
    # / "done" / "finished" reason is the agent runtime's *positive* signal
    # — the agent self-reported successful task completion via the AUTO_STATE
    # footer. Earlier versions of this matcher fell through to
    # tool_unknown_failure, mis-recording successful runs as failures and
    # spamming the audit trail. Map success reasons to succeeded explicitly.
    if any(needle in reason for needle in _AUTO_STOPPED_SUCCESS_REASONS):
        return RunOutcome(
            status="succeeded",
            failure_class=None,
            failure_detail=None,
        )

    if "requires approval for" in reason:
        return RunOutcome(
            status="requires_approval_blocked",
            failure_class="tool_approval_blocked",
            failure_detail=_truncate(detail),
        )
    if "readonly blocks" in reason or "filesystem" in reason:
        return RunOutcome(
            status="failed",
            failure_class="tool_filesystem_denied",
            failure_detail=_truncate(detail),
        )
    if "iterations_remaining" in reason or "iteration budget" in reason:
        return RunOutcome(
            status="timeout",
            failure_class="session_exceeded_iterations",
            failure_detail=_truncate(detail),
        )
    if (
        "wall-clock budget exceeded" in reason
        or "wall clock budget exceeded" in reason
    ):
        return RunOutcome(
            status="failed",
            failure_class="wall_clock_budget_exceeded",
            failure_detail=_truncate(detail),
        )
    if "missing or malformed auto_state" in reason:
        # The agent didn't emit the AUTO_STATE footer the runtime expected.
        # That's typically a downstream symptom of the LLM call failing; treat
        # as endpoint_invalid_response so the operator notices the upstream cause.
        return RunOutcome(
            status="endpoint_failed",
            failure_class="endpoint_invalid_response",
            failure_detail=_truncate(detail),
        )

    return RunOutcome(
        status="failed",
        failure_class="tool_unknown_failure",
        failure_detail=_truncate(detail),
    )


def _outcome_from_final(
    event: Mapping[str, Any], explicit_final_text: Optional[str]
) -> RunOutcome:
    data = event.get("data")
    final_text = (
        explicit_final_text
        if explicit_final_text not in (None, "")
        else (data if isinstance(data, str) else "")
    )
    if not final_text:
        return RunOutcome(
            status="endpoint_failed",
            failure_class="endpoint_empty_response",
            failure_detail="agent emitted final event with empty body",
        )
    if final_text.strip().lower() == _EMPTY_RESPONSE_SENTINEL:
        return RunOutcome(
            status="endpoint_failed",
            failure_class="endpoint_empty_response",
            failure_detail=_truncate(final_text),
        )
    return RunOutcome(
        status="succeeded",
        failure_class=None,
        failure_detail=None,
    )


# ---------------------------------------------------------------------------
# Audit comment formatting
# ---------------------------------------------------------------------------


def format_terminal_comment(
    *,
    role: str,
    outcome: RunOutcome,
    session_id: Optional[str],
    fire_generation: Optional[int],
    elapsed_seconds: Optional[float],
) -> str:
    """Build a single-line audit comment to post to the linked task.

    Format (success):

        ``[KAI] COMPLETED <role>: <verdict or "ok"> in <s>s session=<id> generation=<n>``

    Format (failure):

        ``[KAI] FAILED <role>: <failure_class>: <detail> session=<id> generation=<n> elapsed=<s>s``

    The leading ``[KAI]`` token makes these comments greppable in the taskboard
    audit trail. The `<failure_class>` matches the closed enum in the ledger
    so operators can correlate by class.
    """
    elapsed = (
        f"{elapsed_seconds:.1f}s" if isinstance(elapsed_seconds, (int, float)) else "?"
    )
    session = session_id or "?"
    generation = fire_generation if fire_generation is not None else "?"
    if outcome.status == "succeeded":
        return (
            f"[KAI] COMPLETED {role}: ok in {elapsed} "
            f"session={session} generation={generation}"
        )
    if outcome.status == "duplicate_suppressed":
        return (
            f"[KAI] DUPLICATE-SUPPRESSED {role}: "
            f"{outcome.failure_detail or 'duplicate fire'} "
            f"session={session} generation={generation}"
        )
    if outcome.status == "cancelled":
        return (
            f"[KAI] CANCELLED {role}: "
            f"{outcome.failure_detail or 'manual cancel'} "
            f"session={session} generation={generation}"
        )
    detail = outcome.failure_detail or "(no detail)"
    return (
        f"[KAI] FAILED {role}: {outcome.failure_class}: {detail} "
        f"session={session} generation={generation} elapsed={elapsed}"
    )


# ---------------------------------------------------------------------------
# Helper to resolve a run-id-shaped dict to a PATCH body
# ---------------------------------------------------------------------------


def outcome_to_patch_body(outcome: RunOutcome) -> Dict[str, Any]:
    """Build the JSON body for a terminal ``PATCH /api/agent-runs/{id}`` call.

    Returns only the fields the API accepts at the closing transition;
    callers may add more (``finished_at``, ``model``, etc) before sending.
    """
    body: Dict[str, Any] = {"status": outcome.status}
    if outcome.failure_class is not None:
        body["failure_class"] = outcome.failure_class
    if outcome.failure_detail is not None:
        body["failure_detail"] = outcome.failure_detail
    return body
