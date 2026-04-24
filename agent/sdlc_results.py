"""Structured SDLC result helpers for taskboard-driven roles."""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError


Decision = Literal["APPROVED", "CHANGES_REQUESTED"]


class SDLCModel(BaseModel):
    """Base model for strict SDLC result schemas."""

    model_config = ConfigDict(extra="forbid")


class ReviewFinding(SDLCModel):
    """One code-review or security finding.

    Attributes:
        severity: Finding severity such as CRITICAL, HIGH, MEDIUM, LOW.
        category: Finding category such as MUST_FIX or auth.
        message: Finding description.
        fix: Concrete fix guidance.
        file: Optional file path.
        line: Optional line number.
    """

    severity: str
    category: str
    message: str
    fix: str = ""
    file: str | None = None
    line: int | None = None


class ReviewVerdict(SDLCModel):
    """Structured review/security verdict.

    Attributes:
        decision: ``APPROVED`` or ``CHANGES_REQUESTED``.
        role: Reporting role, for example Code Reviewer.
        summary: Concise verdict summary.
        findings: Review findings.
        tests_reviewed: Tests or evidence inspected.
        residual_risk: Remaining risk after the verdict.
    """

    decision: Decision
    role: str
    summary: str
    findings: list[ReviewFinding] = Field(default_factory=list)
    tests_reviewed: list[str] = Field(default_factory=list)
    residual_risk: str = ""


class DeveloperCompletion(SDLCModel):
    """Structured developer completion report.

    Attributes:
        status: Completion status.
        summary: Work summary.
        branch: Git branch name.
        pr_url: Pull request URL.
        tests: Test evidence records.
        changed_files: Files changed.
        risks: Known risks or follow-ups.
    """

    status: Literal["ready_for_review", "blocked", "needs_input"]
    summary: str
    branch: str = ""
    pr_url: str = ""
    tests: list[dict[str, str]] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


def parse_review_verdict(payload_json: str) -> str:
    """Validate a JSON review verdict.

    Args:
        payload_json: JSON object matching ``ReviewVerdict``.

    Returns:
        Canonical JSON result with ``ok`` and parsed verdict or validation
        errors.
    """

    try:
        verdict = ReviewVerdict.model_validate_json(payload_json)
    except ValidationError as exc:
        return json.dumps(
            {"ok": False, "errors": exc.errors()},
            ensure_ascii=False,
            sort_keys=True,
        )
    return json.dumps(
        {"ok": True, "verdict": verdict.model_dump()},
        ensure_ascii=False,
        sort_keys=True,
    )


def forgejo_event_for_decision(decision: str) -> str:
    """Map a taskboard review decision to a Forgejo review event.

    Args:
        decision: Review decision.

    Returns:
        JSON payload containing the Forgejo event or an error.
    """

    normalized = decision.strip().upper()
    if normalized == "APPROVED":
        return json.dumps({"ok": True, "event": "APPROVED"}, sort_keys=True)
    if normalized == "CHANGES_REQUESTED":
        return json.dumps(
            {"ok": True, "event": "REQUEST_CHANGES"},
            sort_keys=True,
        )
    return json.dumps(
        {"ok": False, "error": "decision must be APPROVED or CHANGES_REQUESTED"},
        sort_keys=True,
    )


def render_review_comment(payload_json: str) -> str:
    """Render a structured review verdict as taskboard markdown.

    Args:
        payload_json: JSON object matching ``ReviewVerdict``.

    Returns:
        Markdown comment, or a JSON validation error payload.
    """

    try:
        verdict = ReviewVerdict.model_validate_json(payload_json)
    except ValidationError as exc:
        return json.dumps(
            {"ok": False, "errors": exc.errors()},
            ensure_ascii=False,
            sort_keys=True,
        )

    prefix = "[APPROVED]" if verdict.decision == "APPROVED" else "[CHANGES_REQUESTED]"
    lines = [
        f"{prefix} {verdict.role} {verdict.decision}",
        "",
        f"Decision: {verdict.decision}",
        "",
        "## Summary",
        verdict.summary,
    ]
    if verdict.findings:
        lines.extend(["", "## Findings"])
        for index, finding in enumerate(verdict.findings, start=1):
            location = ""
            if finding.file:
                location = f" ({finding.file}"
                if finding.line is not None:
                    location += f":{finding.line}"
                location += ")"
            lines.append(
                f"- F{index} [{finding.severity}] {finding.category}: "
                f"{finding.message}{location}"
            )
            if finding.fix:
                lines.append(f"  Fix: {finding.fix}")
    else:
        lines.extend(["", "## Findings", "- No blocking findings."])

    if verdict.tests_reviewed:
        lines.extend(["", "## Evidence"])
        lines.extend(f"- {item}" for item in verdict.tests_reviewed)
    if verdict.residual_risk:
        lines.extend(["", "## Residual Risk", verdict.residual_risk])
    return "\n".join(lines).strip()


def parse_developer_completion(payload_json: str) -> str:
    """Validate a JSON developer completion report.

    Args:
        payload_json: JSON object matching ``DeveloperCompletion``.

    Returns:
        Canonical JSON result with ``ok`` and parsed report or validation
        errors.
    """

    try:
        report = DeveloperCompletion.model_validate_json(payload_json)
    except ValidationError as exc:
        return json.dumps(
            {"ok": False, "errors": exc.errors()},
            ensure_ascii=False,
            sort_keys=True,
        )
    return json.dumps(
        {"ok": True, "completion": report.model_dump()},
        ensure_ascii=False,
        sort_keys=True,
    )


def create_sdlc_result_tools() -> list[StructuredTool]:
    """Create structured SDLC result helper tools.

    Returns:
        List of LangChain tools for verdict validation and rendering.
    """

    return [
        StructuredTool.from_function(
            func=parse_review_verdict,
            name="sdlc_parse_review_verdict",
            description="Validate a structured review verdict JSON object.",
        ),
        StructuredTool.from_function(
            func=render_review_comment,
            name="sdlc_render_review_comment",
            description="Render a structured review verdict as taskboard markdown.",
        ),
        StructuredTool.from_function(
            func=forgejo_event_for_decision,
            name="sdlc_forgejo_event_for_decision",
            description="Map APPROVED/CHANGES_REQUESTED to a Forgejo review event.",
        ),
        StructuredTool.from_function(
            func=parse_developer_completion,
            name="sdlc_parse_developer_completion",
            description="Validate a structured developer completion JSON object.",
        ),
    ]
