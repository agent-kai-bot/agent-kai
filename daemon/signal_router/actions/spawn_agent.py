"""Spawn-agent action executor."""

from __future__ import annotations

import asyncio
import inspect
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from daemon.signal_router.agent_pack import (
    AgentPackError,
    load_pack,
    register_pack_role,
)
from daemon.signal_router.audit_writer import RouterAuditWriter
from daemon.signal_router.domain_model import ActionDescriptor

from .base import (
    ACTION_STATUS_FAILED,
    ACTION_STATUS_FIRED,
    ACTION_STATUS_SUPPRESSED_DRY_RUN,
    ActionResult,
    ExecutionContext,
    SafeFormatDict,
    ValidationError,
    emit_telemetry,
    event_payload,
    render_template,
    stable_json,
)

STATUS_SUPPRESSED_COOLDOWN = "suppressed_cooldown"
STATUS_SUPPRESSED_DAILY_CAP = "suppressed_daily_cap"
STATUS_SUPPRESSED_HOURLY_CAP = "suppressed_hourly_cap"
STATUS_TIMED_OUT = "timed_out"


class SpawnAgentExecutor:
    """Spawn a pack-backed sub-agent and deliver the routed event as its first task."""

    kind = "spawn_agent"

    def __init__(
        self,
        sub_agent_manager: Any | None = None,
        dedup_table: Any | None = None,
        audit_writer: Any | None = None,
        pack_loader: Callable[[str], Any] | None = None,
        *,
        nats_request: Callable[..., Any] | None = None,
        role_registry: dict[str, Any] | None = None,
        role_registrar: Callable[..., Any] | None = register_pack_role,
    ) -> None:
        self.sub_agent_manager = sub_agent_manager
        self.dedup_table = dedup_table
        self.audit_writer = audit_writer or RouterAuditWriter()
        self.pack_loader = pack_loader or load_pack
        self.nats_request = nats_request
        self.role_registry = role_registry
        self.role_registrar = role_registrar

    def validate(self, action: ActionDescriptor) -> list[ValidationError]:
        errors: list[ValidationError] = []
        pack_name = self._pack_name(action)
        if not pack_name:
            errors.append(ValidationError("pack", "spawn_agent requires pack"))
        else:
            try:
                pack = self.pack_loader(pack_name)
            except AgentPackError as exc:
                errors.append(ValidationError("pack", str(exc)))
            except Exception as exc:  # noqa: BLE001
                errors.append(ValidationError("pack", f"agent-pack load failed: {exc}"))
            else:
                role_errors = self._validate_or_register_role(pack)
                errors.extend(role_errors)
        errors.extend(self._validate_non_negative_int(action, "cooldown_seconds"))
        errors.extend(self._validate_non_negative_int(action, "daily_cap"))
        errors.extend(self._validate_non_negative_int(action, "hourly_cap"))
        errors.extend(self._validate_positive_int(action, "timeout_seconds"))
        env_passthrough = action.params.get("env_passthrough", [])
        if env_passthrough is None:
            env_passthrough = []
        if not isinstance(env_passthrough, list) or not all(
            isinstance(item, str) for item in env_passthrough
        ):
            errors.append(ValidationError("env_passthrough", "env_passthrough must be list[str]"))
        elif env_passthrough:
            missing = [name for name in env_passthrough if not os.getenv(name)]
            if missing:
                # SubAgentManager inherits the daemon process environment. The
                # allowlist is informational for audit/config review in Phase 4.
                action.params.setdefault("_env_passthrough_unset", missing)
        return errors

    def execute(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> ActionResult:
        route_name = context.route_name or "unknown"
        pack_name = self._pack_name(action)
        role_name = pack_name
        timeout_seconds = self._int_param(action, "timeout_seconds", 300)
        cooldown_seconds = self._int_param(action, "cooldown_seconds", 0)
        daily_cap = self._int_param(action, "daily_cap", 0)
        hourly_cap = self._int_param(action, "hourly_cap", 0)
        cooldown_key = self._cooldown_key(action, envelope, context)
        base_record = self._audit_record(
            action,
            envelope,
            context,
            cooldown_key=cooldown_key,
            decision="pending",
        )

        dedup_table = self.dedup_table or context.dedup_table
        if dedup_table is not None:
            if daily_cap > 0 and not dedup_table.check_daily_cap(route_name, daily_cap):
                return self._suppressed(
                    action,
                    context,
                    base_record,
                    STATUS_SUPPRESSED_DAILY_CAP,
                    "daily_cap",
                    "auto.signal_router.spawn_agent.suppressed_daily_cap",
                )
            if hourly_cap > 0 and not dedup_table.check_hourly_cap(route_name, hourly_cap):
                return self._suppressed(
                    action,
                    context,
                    base_record,
                    STATUS_SUPPRESSED_HOURLY_CAP,
                    "hourly_cap",
                    "auto.signal_router.spawn_agent.suppressed_hourly_cap",
                )
            if cooldown_seconds > 0 and not dedup_table.check_and_record_cooldown(
                cooldown_key,
                cooldown_seconds,
            ):
                return self._suppressed(
                    action,
                    context,
                    base_record,
                    STATUS_SUPPRESSED_COOLDOWN,
                    "cooldown",
                    "auto.signal_router.spawn_agent.suppressed_cooldown",
                )

        if context.dry_run:
            record = {**base_record, "decision": ACTION_STATUS_SUPPRESSED_DRY_RUN, "shadow": True}
            self._write_audit(action, context, record)
            emit_telemetry(
                context,
                "auto.signal_router.shadow.spawn_agent.would_fire",
                self._telemetry_payload(action, envelope, context, cooldown_key),
            )
            return ActionResult(
                self.kind,
                role_name,
                ACTION_STATUS_SUPPRESSED_DRY_RUN,
                "would_fire",
                {"pack": pack_name, "cooldown_key": cooldown_key},
            )

        manager = self.sub_agent_manager or context.sub_agent_manager
        request = self.nats_request or context.nats_request
        if manager is None:
            return self._failed(action, context, base_record, "sub_agent_manager_unavailable")
        if request is None:
            return self._failed(action, context, base_record, "nats_request_unavailable")

        emit_telemetry(
            context,
            "auto.signal_router.spawn_agent.fired",
            self._telemetry_payload(action, envelope, context, cooldown_key),
        )
        task = self._task_payload(action, envelope, context, cooldown_key)
        try:
            response = _run_blocking(
                self._spawn_and_request(manager, request, role_name, task, timeout_seconds),
                timeout_seconds=timeout_seconds,
            )
        except TimeoutError as exc:
            record = {**base_record, "decision": STATUS_TIMED_OUT, "reason": str(exc)}
            self._write_audit(action, context, record)
            emit_telemetry(
                context,
                "auto.signal_router.spawn_agent.timed_out",
                {**self._telemetry_payload(action, envelope, context, cooldown_key), "error": str(exc)},
            )
            return ActionResult(
                self.kind,
                role_name,
                STATUS_TIMED_OUT,
                str(exc),
                {"pack": pack_name, "cooldown_key": cooldown_key},
            )
        except Exception as exc:  # noqa: BLE001
            return self._failed(action, context, base_record, str(exc), cooldown_key=cooldown_key)

        if dedup_table is not None:
            dedup_table.record_fire(route_name, cooldown_key)
        record = {**base_record, "decision": ACTION_STATUS_FIRED, "reason": None}
        self._write_audit(action, context, record)
        emit_telemetry(
            context,
            "auto.signal_router.spawn_agent.exit_ok",
            {**self._telemetry_payload(action, envelope, context, cooldown_key), "response": response},
        )
        return ActionResult(
            self.kind,
            role_name,
            ACTION_STATUS_FIRED,
            "spawn_agent_completed",
            {"pack": pack_name, "cooldown_key": cooldown_key},
        )

    async def _spawn_and_request(
        self,
        manager: Any,
        nats_request: Callable[..., Any],
        role_name: str,
        task: str,
        timeout_seconds: int,
    ) -> Any:
        await _maybe_await(manager.spawn(role_name))
        return await _call_nats_request(nats_request, role_name, task, timeout_seconds)

    def _validate_or_register_role(self, pack: Any) -> list[ValidationError]:
        registry = self.role_registry
        if registry is None:
            try:
                from config import AGENTS
            except Exception as exc:  # noqa: BLE001
                return [ValidationError("pack", f"agent role registry unavailable: {exc}")]
            registry = AGENTS
        if pack.name in registry:
            if self.role_registrar is not None:
                self.role_registrar(pack, agents=registry)
            return []
        if self.role_registrar is None:
            return [
                ValidationError(
                    "pack",
                    f"agent-pack {pack.name!r} has no registered role and cannot be auto-registered",
                )
            ]
        self.role_registrar(pack, agents=registry)
        return []

    def _suppressed(
        self,
        action: ActionDescriptor,
        context: ExecutionContext,
        base_record: dict[str, Any],
        status: str,
        reason: str,
        topic: str,
    ) -> ActionResult:
        record = {**base_record, "decision": status, "reason": reason}
        self._write_audit(action, context, record)
        emit_telemetry(context, topic, {"route_name": context.route_name, "cooldown_key": base_record.get("cooldown_key")})
        return ActionResult(
            self.kind,
            self._pack_name(action),
            status,
            reason,
            {"cooldown_key": base_record.get("cooldown_key")},
        )

    def _failed(
        self,
        action: ActionDescriptor,
        context: ExecutionContext,
        base_record: dict[str, Any],
        reason: str,
        *,
        cooldown_key: str | None = None,
    ) -> ActionResult:
        record = {**base_record, "decision": ACTION_STATUS_FAILED, "reason": reason}
        self._write_audit(action, context, record)
        emit_telemetry(
            context,
            "auto.signal_router.spawn_agent.exit_failed",
            {"route_name": context.route_name, "cooldown_key": cooldown_key or base_record.get("cooldown_key"), "error": reason},
        )
        return ActionResult(
            self.kind,
            self._pack_name(action),
            ACTION_STATUS_FAILED,
            reason,
            {"cooldown_key": cooldown_key or base_record.get("cooldown_key")},
        )

    def _write_audit(
        self,
        action: ActionDescriptor,
        context: ExecutionContext,
        record: dict[str, Any],
    ) -> None:
        writer = context.audit_writer or self.audit_writer
        path_template = str(
            action.params.get("audit_path_template")
            or "${AGENTKAI_HOME}/audit/router_{date}.jsonl"
        )
        try:
            if hasattr(writer, "write"):
                writer.write(record, path_template=path_template)
            else:
                writer({**record, "audit_path_template": path_template})
        except Exception as exc:  # noqa: BLE001
            emit_telemetry(
                context,
                "auto.signal_router.audit.failed",
                {"route_name": context.route_name, "action_kind": self.kind, "error": str(exc)},
            )

    def _audit_record(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
        *,
        cooldown_key: str,
        decision: str,
    ) -> dict[str, Any]:
        payload = event_payload(envelope)
        return {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "route": context.route_name,
            "channel": context.channel or envelope.get("channel"),
            "subject": context.subject or envelope.get("subject"),
            "action_kind": self.kind,
            "pack": self._pack_name(action),
            "decision": decision,
            "reason": None,
            "rule_id": payload.get("rule_id"),
            "token_id": payload.get("token_id"),
            "cooldown_key": cooldown_key,
            "shadow": bool(context.dry_run),
        }

    def _task_payload(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
        cooldown_key: str,
    ) -> str:
        if action.params.get("template_inline"):
            return render_template(str(action.params["template_inline"]), envelope, context)
        payload = {
            "subject": context.subject or envelope.get("subject"),
            "channel": context.channel or envelope.get("channel"),
            "route_name": context.route_name,
            "action_kind": self.kind,
            "pack": self._pack_name(action),
            "cooldown_key": cooldown_key,
            "event": event_payload(envelope),
            "router": {
                "dry_run": context.dry_run,
                "received_at": envelope.get("received_at"),
            },
        }
        return stable_json(payload)

    @staticmethod
    def _telemetry_payload(
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
        cooldown_key: str,
    ) -> dict[str, Any]:
        payload = event_payload(envelope)
        return {
            "route_name": context.route_name,
            "channel": context.channel or envelope.get("channel"),
            "subject": context.subject or envelope.get("subject"),
            "pack": action.params.get("pack") or action.target,
            "cooldown_key": cooldown_key,
            "rule_id": payload.get("rule_id"),
            "token_id": payload.get("token_id"),
        }

    def _cooldown_key(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> str:
        template = str(action.params.get("cooldown_key_template") or "{route_name}")
        values = event_payload(envelope)
        values.setdefault("channel", context.channel or envelope.get("channel", ""))
        values.setdefault("subject", context.subject or envelope.get("subject", ""))
        values.setdefault("route_name", context.route_name or "")
        values.setdefault("payload_json", stable_json(values))
        values.setdefault("raw_event", stable_json(envelope))
        return template.format_map(SafeFormatDict(values))

    @staticmethod
    def _pack_name(action: ActionDescriptor) -> str:
        return str(action.params.get("pack") or action.target or "").strip()

    @staticmethod
    def _int_param(action: ActionDescriptor, key: str, default: int) -> int:
        value = action.params.get(key, default)
        if value is None or value == "":
            return default
        return int(value)

    def _validate_non_negative_int(
        self,
        action: ActionDescriptor,
        key: str,
    ) -> list[ValidationError]:
        try:
            value = self._int_param(action, key, 0)
        except (TypeError, ValueError):
            return [ValidationError(key, f"{key} must be an integer")]
        if value < 0:
            return [ValidationError(key, f"{key} must be non-negative")]
        return []

    def _validate_positive_int(
        self,
        action: ActionDescriptor,
        key: str,
    ) -> list[ValidationError]:
        if key not in action.params:
            return []
        try:
            value = self._int_param(action, key, 1)
        except (TypeError, ValueError):
            return [ValidationError(key, f"{key} must be an integer")]
        if value <= 0:
            return [ValidationError(key, f"{key} must be greater than 0")]
        return []


async def _call_nats_request(
    nats_request: Callable[..., Any],
    role_name: str,
    task: str,
    timeout_seconds: int,
) -> Any:
    try:
        result = nats_request(role_name, task, timeout_seconds=timeout_seconds)
    except TypeError:
        result = nats_request(role_name, task, timeout=timeout_seconds)
    return await _maybe_await(result)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _run_blocking(coro: Any, *, timeout_seconds: int) -> Any:
    async def runner() -> Any:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(runner())
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"spawn_agent timed out after {timeout_seconds}s") from exc

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.run(runner()))
        try:
            return future.result(timeout=timeout_seconds + 1)
        except TimeoutError:
            raise TimeoutError(f"spawn_agent timed out after {timeout_seconds}s") from None
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"spawn_agent timed out after {timeout_seconds}s") from exc
