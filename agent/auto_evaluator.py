"""Tool-less auto-response evaluator for autonomous-mode continuations.

The evaluator is intentionally daemon-bounded.  It never executes tools and it
never authors arbitrary follow-up prompts; it only returns a strict structured
classification that the session loop may accept after its normal safety gates
pass.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

ParsedAutoState = Literal["done", "continue", "pause", "unknown"]
EvaluationDecision = Literal["STOP", "CONTINUE", "PAUSE", "ACCEPT_MAIN_STATE"]
AutoReplyTemplate = Literal[
    "continue_next_safe_step",
    "proceed_readonly_analysis",
    "finish_requested_artifact",
]

DECISIONS = frozenset({"STOP", "CONTINUE", "PAUSE", "ACCEPT_MAIN_STATE"})
PATTERNS = frozenset(
    {
        "permission_deflection",
        "declared_next_step",
        "incomplete_artifact",
        "malformed_footer_recoverable",
        "main_done_accepted",
        "safety_pause",
        "unknown",
    }
)
AUTO_REPLY_TEMPLATE_NAMES = frozenset(
    {
        "continue_next_safe_step",
        "proceed_readonly_analysis",
        "finish_requested_artifact",
    }
)
READONLY_AUTO_REPLY_TEMPLATES = frozenset({"proceed_readonly_analysis"})


@dataclass(frozen=True)
class ToolCallSummary:
    """Sanitized summary of one tool call from the just-finished turn."""

    name: str
    input_key: str


@dataclass(frozen=True)
class AutoEvaluationInput:
    """Input contract for bounded auto-response evaluation."""

    session_name: str
    agent_name: str
    auto_mode: bool
    readonly: bool
    main_response: str
    parsed_auto_state: ParsedAutoState
    parsed_auto_reason: str | None
    runtime_pause_reason: str | None
    turn_tool_calls: list[ToolCallSummary]
    consecutive_no_tool_turns: int
    repeated_final_detected: bool
    iterations_remaining: int
    elapsed_seconds: float


@dataclass(frozen=True)
class AutoEvaluationDecision:
    """Strict evaluator output consumed by daemon safety gates."""

    decision: EvaluationDecision
    confidence: float
    reason: str
    pattern: str
    auto_reply_template: AutoReplyTemplate | None = None

    def to_event_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "decision": self.decision,
            "confidence": round(float(self.confidence), 3),
            "pattern": self.pattern,
            "reason": self.reason,
        }
        if self.auto_reply_template is not None:
            payload["auto_reply_template"] = self.auto_reply_template
        return payload


AUTO_REPLY_TEMPLATES: dict[AutoReplyTemplate, str] = {
    "continue_next_safe_step": "Continue with the next safe step.",
    "proceed_readonly_analysis": "Proceed with the read-only analysis you just described.",
    "finish_requested_artifact": "Finish the artifact or final answer requested by the task.",
}

MIN_CONTINUE_CONFIDENCE = 0.85

_PERMISSION_RE = re.compile(
    r"\b(?:if you want|if you'd like|shall i|should i|would you like me to|"
    r"do you want me to|i can (?:now )?|i could (?:now )?)\b",
    re.IGNORECASE,
)
_NEXT_STEP_RE = re.compile(
    r"\b(?:next|remaining|todo|to do|follow-?up|still need(?:s)?|i will|i'll|"
    r"need to|needs to)\b",
    re.IGNORECASE,
)
_ARTIFACT_RE = re.compile(
    r"\b(?:artifact|final answer|deliverable|implementation|patch|tests?|pr|pull request|"
    r"write|create|update|finish|complete)\b",
    re.IGNORECASE,
)
_READONLY_RE = re.compile(
    r"\b(?:read-only|readonly|inspect|check|review|analy[sz]e|scan|list|view|fetch|"
    r"read|validate|run (?:the )?(?:existing )?tests?)\b",
    re.IGNORECASE,
)
_MUTATING_RE = re.compile(
    r"\b(?:place order|trade|buy|sell|delete|remove|rm -rf|drop table|push|deploy|"
    r"merge|commit|write|edit|modify|change|create|update)\b",
    re.IGNORECASE,
)


def render_auto_reply(template: AutoReplyTemplate | None) -> str | None:
    """Return a daemon-owned hidden auto-reply for an allowed template."""

    if template is None:
        return None
    return AUTO_REPLY_TEMPLATES.get(template)


def stop_decision(reason: str, *, pattern: str = "unknown") -> AutoEvaluationDecision:
    """Build a conservative STOP decision for malformed evaluator output."""

    return AutoEvaluationDecision("STOP", 1.0, reason, pattern)


def parse_auto_evaluation_decision(raw: str | bytes | dict[str, Any]) -> AutoEvaluationDecision:
    """Parse and schema-validate strict evaluator JSON.

    Malformed/freeform output, missing fields, invalid enum values, and
    confidence outside ``0..1`` all resolve to a conservative STOP decision.
    """

    try:
        payload: Any
        if isinstance(raw, dict):
            payload = raw
        else:
            payload = json.loads(raw)
        if not isinstance(payload, dict):
            return stop_decision("auto evaluator returned non-object JSON")

        decision = payload.get("decision")
        confidence = payload.get("confidence")
        reason = payload.get("reason")
        pattern = payload.get("pattern")
        template = payload.get("auto_reply_template")

        if decision not in DECISIONS:
            return stop_decision("auto evaluator returned invalid decision")
        if not isinstance(confidence, int | float):
            return stop_decision("auto evaluator returned invalid confidence")
        confidence_float = float(confidence)
        if confidence_float < 0.0 or confidence_float > 1.0:
            return stop_decision("auto evaluator confidence out of range")
        if not isinstance(reason, str) or not reason.strip():
            return stop_decision("auto evaluator returned invalid reason")
        if pattern not in PATTERNS:
            return stop_decision("auto evaluator returned invalid pattern")
        if template is None or template == "null":
            template_value = None
        elif template in AUTO_REPLY_TEMPLATE_NAMES:
            template_value = template
        else:
            return stop_decision("auto evaluator returned invalid reply template")

        return AutoEvaluationDecision(
            decision,  # type: ignore[arg-type]
            confidence_float,
            reason.strip()[:240],
            pattern,  # type: ignore[arg-type]
            template_value,  # type: ignore[arg-type]
        )
    except Exception:
        return stop_decision("auto evaluator output malformed")


def validate_auto_evaluation_decision(
    decision: AutoEvaluationDecision,
    *,
    readonly: bool,
    min_confidence: float = MIN_CONTINUE_CONFIDENCE,
) -> AutoEvaluationDecision:
    """Apply daemon-side hard constraints to an evaluator decision."""

    if decision.decision != "CONTINUE":
        return decision
    if decision.confidence < min_confidence:
        return stop_decision("auto evaluator confidence below threshold")
    if decision.auto_reply_template not in AUTO_REPLY_TEMPLATE_NAMES:
        return stop_decision("auto evaluator returned invalid reply template")
    if readonly and decision.auto_reply_template not in READONLY_AUTO_REPLY_TEMPLATES:
        return stop_decision("auto evaluator returned non-readonly reply template")
    return decision


class AutoResponseEvaluator:
    """Deterministic v1 evaluator for safe auto-continuation patterns.

    A future tool-less LLM critic can sit behind this interface.  The daemon
    boundary should still schema-validate and treat malformed/low-confidence
    output as STOP.
    """

    min_continue_confidence = MIN_CONTINUE_CONFIDENCE

    def evaluate(self, data: AutoEvaluationInput) -> AutoEvaluationDecision:
        text = data.main_response or ""
        lower = text.lower()

        if data.runtime_pause_reason:
            return AutoEvaluationDecision(
                "PAUSE",
                1.0,
                data.runtime_pause_reason,
                "safety_pause",
            )

        if data.iterations_remaining <= 0:
            return stop_decision("iteration budget exhausted")

        if data.repeated_final_detected:
            return stop_decision("loop detected: repeated final response")

        if data.parsed_auto_state == "pause":
            return AutoEvaluationDecision(
                "ACCEPT_MAIN_STATE",
                1.0,
                data.parsed_auto_reason or "paused",
                "safety_pause",
            )

        if data.parsed_auto_state == "continue":
            return AutoEvaluationDecision(
                "ACCEPT_MAIN_STATE",
                1.0,
                "main requested continuation",
                "unknown",
            )

        if data.parsed_auto_state == "done":
            if self._looks_incomplete(text):
                return AutoEvaluationDecision(
                    "CONTINUE",
                    0.9,
                    "main marked done but described remaining in-scope work",
                    "declared_next_step",
                    self._template_for(text, readonly=data.readonly),
                )
            return AutoEvaluationDecision(
                "ACCEPT_MAIN_STATE",
                1.0,
                data.parsed_auto_reason or "task complete",
                "main_done_accepted",
            )

        if self._asks_permission_for_safe_step(text, readonly=data.readonly):
            return AutoEvaluationDecision(
                "CONTINUE",
                0.91,
                "main asked permission for a safe next step",
                "permission_deflection",
                self._template_for(text, readonly=data.readonly),
            )

        if self._looks_incomplete(text):
            return AutoEvaluationDecision(
                "CONTINUE",
                0.87,
                "missing/malformed footer but response describes incomplete work",
                "malformed_footer_recoverable",
                self._template_for(text, readonly=data.readonly),
            )

        if "auto_state" in lower or "[auto" in lower:
            return stop_decision("malformed AUTO_STATE footer")

        return stop_decision("missing or malformed AUTO_STATE footer")

    @staticmethod
    def _looks_incomplete(text: str) -> bool:
        return bool(_NEXT_STEP_RE.search(text) and _ARTIFACT_RE.search(text))

    @staticmethod
    def _asks_permission_for_safe_step(text: str, *, readonly: bool) -> bool:
        if not _PERMISSION_RE.search(text):
            return False
        if readonly:
            return bool(_READONLY_RE.search(text)) and not bool(_MUTATING_RE.search(text))
        if _MUTATING_RE.search(text) and not _READONLY_RE.search(text):
            return False
        return bool(_READONLY_RE.search(text) or _NEXT_STEP_RE.search(text))

    @staticmethod
    def _template_for(text: str, *, readonly: bool) -> AutoReplyTemplate:
        if readonly or _READONLY_RE.search(text):
            return "proceed_readonly_analysis"
        if _ARTIFACT_RE.search(text):
            return "finish_requested_artifact"
        return "continue_next_safe_step"
