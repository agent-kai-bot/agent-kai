"""Action executor registry."""

from __future__ import annotations

from daemon.signal_router.domain_model import ActionDescriptor

from .alert import AlertExecutor
from .base import ActionExecutor, ValidationError
from .ignore import IgnoreExecutor
from .inject_session import InjectSessionExecutor
from .log import LogExecutor
from .notify import NotifyExecutor
from .spawn_agent import SpawnAgentExecutor
from .trade import TradeExecutor
from .ui_panel import UIPanelExecutor

EXECUTORS: dict[str, ActionExecutor] = {
    "alert": AlertExecutor(),
    "ignore": IgnoreExecutor(),
    "inject_session": InjectSessionExecutor(),
    "log": LogExecutor(),
    "notify": NotifyExecutor(),
    "spawn_agent": SpawnAgentExecutor(),
    "trade": TradeExecutor(),
    "ui_panel": UIPanelExecutor(),
}


def get_executor(kind: str) -> ActionExecutor:
    try:
        return EXECUTORS[kind]
    except KeyError as exc:
        raise KeyError(f"unknown signal_router action kind: {kind}") from exc


def validate_action(action: ActionDescriptor) -> list[ValidationError]:
    try:
        executor = get_executor(action.kind)
    except KeyError as exc:
        return [ValidationError("kind", str(exc))]
    return executor.validate(action)
