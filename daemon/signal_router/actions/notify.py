"""Notification action executor and push backend chain."""

from __future__ import annotations

import os
from typing import Any

import requests

from daemon.signal_router.domain_model import ActionDescriptor

from .base import (
    ACTION_STATUS_FAILED,
    ACTION_STATUS_FIRED,
    ACTION_STATUS_SKIPPED,
    ActionResult,
    ExecutionContext,
    ValidationError,
    _dispatch_maybe_async,
    dry_run_result,
    emit_telemetry,
    event_payload,
    render_template,
    write_audit,
)

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"
NTFY_BASE_URL = "https://ntfy.sh"
DEFAULT_PUSH_BACKENDS = ["pushover", "ntfy", "log_only"]


class NotifyExecutor:
    """Dispatch small notification effects."""

    kind = "notify"

    def validate(self, action: ActionDescriptor) -> list[ValidationError]:
        target = (action.target or "").lower()
        if target == "chat":
            return []
        if target == "nats":
            return self._validate_nats(action)
        if target == "webhook":
            return self._validate_webhook(action)
        if target == "push":
            return self._validate_push(action)
        return [ValidationError("target", f"unknown notify target {action.target!r}")]

    def execute(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> ActionResult:
        if context.dry_run:
            emit_telemetry(
                context,
                "auto.signal_router.shadow.notify.would_fire",
                {"target": action.target, "channel": context.channel},
            )
            return dry_run_result(action, metrics={"target": action.target})
        target = (action.target or "").lower()
        if target == "chat":
            return self._execute_chat(action, envelope, context)
        if target == "nats":
            return self._execute_nats(action, envelope, context)
        if target == "webhook":
            return self._execute_webhook(action, envelope, context)
        if target == "push":
            return self._execute_push(action, envelope, context)
        return ActionResult(self.kind, action.target, ACTION_STATUS_FAILED, "unknown_target", {})

    def _execute_chat(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> ActionResult:
        message = self._message(action, envelope, context)
        if context.chat_logger is not None:
            context.chat_logger(message)
        else:
            for entry in context.sessions.values():
                session = getattr(entry, "session", entry)
                session.publish_event("chat.message", {"message": message})
        return ActionResult(self.kind, action.target, ACTION_STATUS_FIRED, "chat_message", {"chars": len(message)})

    def _execute_nats(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> ActionResult:
        if context.nats_publisher is None:
            return ActionResult(self.kind, action.target, ACTION_STATUS_FAILED, "nats_publisher_unavailable", {})
        subject = str(action.params.get("subject") or "")
        payload = dict(envelope) if action.params.get("raw_event") else {"message": self._message(action, envelope, context)}
        _dispatch_maybe_async(context.nats_publisher(subject, payload))
        return ActionResult(self.kind, action.target, ACTION_STATUS_FIRED, "nats_publish", {"subject": subject})

    def _execute_webhook(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> ActionResult:
        url = str(action.params.get("url") or "")
        payload = {"message": self._message(action, envelope, context), "event": event_payload(envelope)}
        try:
            if context.webhook_poster is not None:
                context.webhook_poster(url, payload)
            else:
                response = requests.post(url, json=payload, timeout=10)
                response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            emit_telemetry(
                context,
                "auto.notify.webhook.failed",
                {"url": url, "error": str(exc), "route_name": context.route_name},
            )
            return ActionResult(self.kind, action.target, ACTION_STATUS_FAILED, str(exc), {"url": url})
        return ActionResult(self.kind, action.target, ACTION_STATUS_FIRED, "webhook_post", {"url": url})

    def _execute_push(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> ActionResult:
        failures: list[str] = []
        for backend in self._backends(action):
            try:
                if backend == "pushover":
                    self._send_pushover(action, envelope, context)
                elif backend == "ntfy":
                    self._send_ntfy(action, envelope, context)
                elif backend == "log_only":
                    write_audit(
                        context,
                        {
                            "kind": "notify",
                            "target": "push",
                            "backend": "log_only",
                            "message": self._message(action, envelope, context),
                            "route_name": context.route_name,
                        },
                    )
                    failures.append("log_only:audit_written")
                    continue
                else:
                    failures.append(f"{backend}:unknown_backend")
                    continue
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{backend}:{exc}")
                continue
            return ActionResult(
                self.kind,
                action.target,
                ACTION_STATUS_FIRED,
                backend,
                {"backend": backend, "fallback_failures": failures},
            )
        write_audit(
            context,
            {
                "kind": "notify",
                "target": "push",
                "status": "failed",
                "failures": failures,
                "route_name": context.route_name,
            },
        )
        emit_telemetry(
            context,
            "auto.notify.push.failed",
            {"failures": failures, "route_name": context.route_name},
        )
        return ActionResult(self.kind, action.target, ACTION_STATUS_FAILED, "all_push_backends_failed", {"failures": failures})

    def _send_pushover(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> None:
        config = action.params.get("pushover") if isinstance(action.params.get("pushover"), dict) else {}
        user = os.getenv(str(config.get("user_env") or "KAI_PUSHOVER_USER"), "")
        token = os.getenv(str(config.get("token_env") or "KAI_PUSHOVER_TOKEN"), "")
        if not user or not token:
            raise RuntimeError("missing pushover env credentials")
        data = {
            "user": user,
            "token": token,
            "message": self._message(action, envelope, context),
            "priority": int(config.get("priority", 0)),
        }
        if config.get("url"):
            data["url"] = render_template(str(config["url"]), envelope, context)
        if config.get("url_title"):
            data["url_title"] = render_template(str(config["url_title"]), envelope, context)
        response = self._http_post(context, PUSHOVER_API_URL, data=data, timeout=10)
        self._raise_for_status(response)

    def _send_ntfy(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> None:
        config = action.params.get("ntfy") if isinstance(action.params.get("ntfy"), dict) else {}
        topic = os.getenv(str(config.get("topic_env") or "KAI_NTFY_TOPIC"), "")
        if not topic:
            raise RuntimeError("missing ntfy topic env")
        priority = int(config.get("priority", 3))
        response = self._http_post(
            context,
            f"{NTFY_BASE_URL}/{topic}",
            data=self._message(action, envelope, context).encode("utf-8"),
            headers={"Priority": str(priority)},
            timeout=10,
        )
        self._raise_for_status(response)

    @staticmethod
    def _http_post(context: ExecutionContext, url: str, **kwargs: Any) -> Any:
        poster = context.http_poster or requests.post
        return poster(url, **kwargs)

    @staticmethod
    def _raise_for_status(response: Any) -> None:
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
            return
        status_code = int(getattr(response, "status_code", 200) or 200)
        if status_code >= 400:
            raise RuntimeError(f"http_status_{status_code}")

    def _message(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> str:
        template = str(action.params.get("template_inline") or "")
        if template:
            return render_template(template, envelope, context)
        return str(event_payload(envelope))

    @staticmethod
    def _backends(action: ActionDescriptor) -> list[str]:
        raw = action.params.get("backends") or DEFAULT_PUSH_BACKENDS
        if not isinstance(raw, list):
            return list(DEFAULT_PUSH_BACKENDS)
        return [str(item) for item in raw]

    @staticmethod
    def _validate_nats(action: ActionDescriptor) -> list[ValidationError]:
        errors: list[ValidationError] = []
        if not str(action.params.get("subject") or "").strip():
            errors.append(ValidationError("subject", "notify target=nats requires subject"))
        if not action.params.get("raw_event") and not action.params.get("template_inline"):
            errors.append(
                ValidationError(
                    "template_inline",
                    "notify target=nats requires template_inline or raw_event",
                )
            )
        return errors

    @staticmethod
    def _validate_webhook(action: ActionDescriptor) -> list[ValidationError]:
        errors: list[ValidationError] = []
        if not str(action.params.get("url") or "").strip():
            errors.append(ValidationError("url", "notify target=webhook requires url"))
        if not action.params.get("template_inline"):
            errors.append(
                ValidationError("template_inline", "notify target=webhook requires template_inline")
            )
        return errors

    def _validate_push(self, action: ActionDescriptor) -> list[ValidationError]:
        errors: list[ValidationError] = []
        for field in ("user", "token", "topic"):
            if field in action.params:
                errors.append(ValidationError(field, "push credentials/topics must come from env vars"))
        pushover = action.params.get("pushover") if isinstance(action.params.get("pushover"), dict) else {}
        ntfy = action.params.get("ntfy") if isinstance(action.params.get("ntfy"), dict) else {}
        for field in ("user", "token"):
            if field in pushover:
                errors.append(ValidationError(f"pushover.{field}", "pushover credentials must come from env vars"))
        if "topic" in ntfy:
            errors.append(ValidationError("ntfy.topic", "ntfy topic must come from an env var"))
        priority = _safe_int(pushover.get("priority", 0), 0)
        if priority not in {0, 1, 2}:
            errors.append(ValidationError("pushover.priority", "pushover priority must be 0, 1, or 2"))
        ntfy_priority = _safe_int(ntfy.get("priority", 3), 3)
        if ntfy_priority < 1 or ntfy_priority > 5:
            errors.append(ValidationError("ntfy.priority", "ntfy priority must be 1 through 5"))
        for backend in self._backends(action):
            if backend == "pushover" and (
                not os.getenv(str(pushover.get("user_env") or "KAI_PUSHOVER_USER"))
                or not os.getenv(str(pushover.get("token_env") or "KAI_PUSHOVER_TOKEN"))
            ):
                errors.append(ValidationError("pushover.env", "pushover backend is missing env credentials"))
            if backend == "ntfy" and not os.getenv(str(ntfy.get("topic_env") or "KAI_NTFY_TOPIC")):
                errors.append(ValidationError("ntfy.env", "ntfy backend is missing topic env"))
        return errors


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
