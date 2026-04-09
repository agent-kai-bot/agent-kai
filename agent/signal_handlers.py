"""Declarative signal-handler runtime.

When a signal arrives via NATS (``signals.{strategy}.{symbol}``) the
``SignalConsumer`` adds it to the ring buffer and fires the
``on_signal`` callback. This module provides a configurable layer on
top of that callback so the user can declare in ``agent-config.json``
which signals should trigger which actions, without writing Python.

Example config block::

    "signal_handlers": [
      {
        "name": "clucmay BUY -> analyst",
        "enabled": true,
        "match": {
          "strategy": "clucmay02",
          "signal_type": "BUY",
          "symbol": ["BTC", "ETH", "SOL"]
        },
        "action": "dispatch_agent",
        "agent": "analyst",
        "task_template": "{strategy} {signal_type} on {symbol} at ${price} — independent multi-timeframe read please.",
        "cooldown_seconds": 300,
        "requires_autotrade": false
      }
    ]

The runtime evaluates every handler against every incoming signal,
fires the action when match + cooldown allow, and gates trader
dispatches behind a global ``autotrade_enabled`` flag the user
toggles via the ``/autotrade`` slash command.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Action types ────────────────────────────────────────────
#
# Five built-in action verbs. Each handler config picks one and the
# dispatcher routes to the matching async callable. Adding a new
# action = adding a new entry to ACTION_DISPATCHERS in
# ``SignalHandlerRunner.__init__``.

ACTION_DISPATCH_AGENT = "dispatch_agent"   # nats_request to a sub-agent
ACTION_DISPATCH_KAI = "dispatch_kai"       # send as a chat turn to the main agent
ACTION_CHAT_MESSAGE = "chat_message"       # post a styled message in the chat panel
ACTION_PUBLISH = "publish"                 # republish to a NATS topic
ACTION_WEBHOOK = "webhook"                 # POST signal to an external URL

VALID_ACTIONS = {
    ACTION_DISPATCH_AGENT,
    ACTION_DISPATCH_KAI,
    ACTION_CHAT_MESSAGE,
    ACTION_PUBLISH,
    ACTION_WEBHOOK,
}

# Sub-agents whose dispatch should ALWAYS be gated behind the
# /autotrade flag — even if the handler config doesn't set
# requires_autotrade=true. The trader is the obvious one: it can
# place real (or paper) orders, and accidentally enabling a
# signals.BUY.BTC -> trader handler in the wrong state would be
# bad. The risk-manager and analyst are read-only by design and
# don't need this gate.
AUTOTRADE_GATED_AGENTS = {"trader"}


@dataclass
class SignalHandler:
    """One signal-handler config row, parsed from agent-config.json."""

    name: str
    enabled: bool = True
    match: Dict[str, Any] = field(default_factory=dict)
    action: str = ACTION_CHAT_MESSAGE
    agent: Optional[str] = None
    task_template: str = ""
    template: str = ""              # used by chat_message + webhook
    subject: str = ""               # used by publish
    url: str = ""                   # used by webhook
    cooldown_seconds: int = 0
    requires_autotrade: bool = False

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SignalHandler":
        """Build a SignalHandler from a config dict, with defaults.

        Unknown keys are ignored (forward-compat). Missing required
        keys raise — the caller should catch and log so one bad
        handler doesn't kill the whole list.
        """
        if not isinstance(raw, dict):
            raise ValueError(f"handler entry must be a dict, got {type(raw).__name__}")
        name = raw.get("name") or "unnamed"
        action = raw.get("action") or ACTION_CHAT_MESSAGE
        if action not in VALID_ACTIONS:
            raise ValueError(f"handler {name!r}: unknown action {action!r}")
        return cls(
            name=str(name),
            enabled=bool(raw.get("enabled", True)),
            match=dict(raw.get("match") or {}),
            action=action,
            agent=raw.get("agent"),
            task_template=str(raw.get("task_template", "")),
            template=str(raw.get("template", "")),
            subject=str(raw.get("subject", "")),
            url=str(raw.get("url", "")),
            cooldown_seconds=int(raw.get("cooldown_seconds", 0) or 0),
            requires_autotrade=bool(raw.get("requires_autotrade", False)),
        )


# ── Pattern matching ────────────────────────────────────────
#
# The match dict supports:
#   - "field": "exact-value"               (string or numeric exact)
#   - "field": ["a", "b", "c"]             (any-of)
#   - "details.confidence": "high"         (dotted-path access)
# Missing fields = no match (conservative — never match by accident).
# All match keys must pass for the handler to fire (AND across keys).


def _flatten_signal(sig) -> Dict[str, Any]:
    """Render a Signal (or dict) as a flat dict for matching + templating.

    Accepts either a ``Signal`` dataclass instance (which has a
    ``.to_dict()`` method that flattens ``details`` for us) or a
    plain dict. Always returns a dict with all top-level fields
    plus the ``details`` subkeys promoted.
    """
    if hasattr(sig, "to_dict"):
        return sig.to_dict()
    if isinstance(sig, dict):
        flat = dict(sig)
        details = flat.pop("details", None)
        if isinstance(details, dict):
            for k, v in details.items():
                flat.setdefault(k, v)
        return flat
    return {}


def _resolve_field(flat: Dict[str, Any], path: str) -> Any:
    """Read a field from the flat dict, supporting dotted paths.

    ``"strategy"`` -> flat["strategy"]
    ``"details.confidence"`` -> flat["details"]["confidence"]

    Returns ``None`` if any part of the path is missing or the
    intermediate value isn't a dict.
    """
    if "." not in path:
        return flat.get(path)
    parts = path.split(".")
    current: Any = flat
    for p in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(p)
        if current is None:
            return None
    return current


def matches(handler: SignalHandler, sig) -> bool:
    """Return True if every match key on the handler matches the signal.

    String comparisons are case-insensitive — agents publish with
    inconsistent casing (``BUY`` vs ``buy``, ``BTC`` vs ``btc``)
    and we don't want a handler to silently miss because of it.
    """
    if not handler.match:
        return True
    flat = _flatten_signal(sig)
    for key, expected in handler.match.items():
        actual = _resolve_field(flat, key)
        if actual is None:
            return False
        if isinstance(expected, list):
            if not _any_of_match(actual, expected):
                return False
        else:
            if not _scalar_match(actual, expected):
                return False
    return True


def _scalar_match(actual: Any, expected: Any) -> bool:
    """Compare two scalar values, case-insensitive for strings."""
    if isinstance(actual, str) and isinstance(expected, str):
        return actual.casefold() == expected.casefold()
    return actual == expected


def _any_of_match(actual: Any, options: List[Any]) -> bool:
    """Return True if actual matches any value in options."""
    return any(_scalar_match(actual, opt) for opt in options)


# ── Template rendering ──────────────────────────────────────


def render_template(template: str, sig) -> str:
    """Format a template string against the flattened signal dict.

    Uses ``str.format_map`` with a default-empty fallback so a
    template that references ``{confidence}`` against a signal
    that doesn't have a confidence field doesn't crash — it
    just renders an empty string for that placeholder.
    """
    if not template:
        return ""
    flat = _flatten_signal(sig)
    return template.format_map(_DefaultDict(flat))


class _DefaultDict(dict):
    """dict subclass that returns ``""`` for missing keys.

    Used by ``render_template`` so missing fields don't raise
    KeyError mid-format. The behavior matches what a user would
    expect from a templated config: "if the field isn't there,
    just leave it blank."
    """

    def __missing__(self, key: str) -> str:
        return ""


# ── Cooldown tracker ────────────────────────────────────────


class CooldownTracker:
    """Per-(handler, symbol) last-fire timestamp tracker.

    Cooldowns prevent the same handler from firing repeatedly
    on every minor signal — e.g. the scanner re-emits the same
    BUY every bar while the setup is active, but you only want
    one analyst dispatch per setup. The cooldown key includes
    the symbol so a handler that fires on BTC doesn't suppress
    a separate firing on ETH.

    The tracker is in-memory only — restart the TUI and all
    cooldowns reset. That's intentional: cooldowns are about
    rate-limiting within a session, not about long-term
    deduplication (which is the scanner's job upstream).
    """

    def __init__(self) -> None:
        self._last_fired: Dict[tuple, float] = {}

    def can_fire(self, handler_name: str, symbol: str, cooldown_seconds: int) -> bool:
        """Return True if the cooldown has elapsed for this (handler, symbol)."""
        if cooldown_seconds <= 0:
            return True
        key = (handler_name, (symbol or "").upper())
        last = self._last_fired.get(key, 0.0)
        return (time.time() - last) >= cooldown_seconds

    def mark_fired(self, handler_name: str, symbol: str) -> None:
        """Record that the handler just fired for this symbol."""
        key = (handler_name, (symbol or "").upper())
        self._last_fired[key] = time.time()


# ── The runner ──────────────────────────────────────────────


class SignalHandlerRunner:
    """Evaluates incoming signals against the configured handler list.

    Constructed once at TUI startup with:
      - the parsed list of SignalHandler configs
      - an action_dispatcher (per-action async callable map)
      - the autotrade-state callable (returns bool)
      - a logger callable for chat surfacing

    On every signal arrival the consumer's on_signal callback calls
    ``run(sig)`` which walks the handler list, runs the matcher,
    checks the cooldown, checks the autotrade gate, and fires the
    action via the dispatcher map.

    All side effects are scheduled — no await happens inside the
    consumer callback, so the consumer's ring buffer ingestion is
    never blocked by a slow handler dispatch.
    """

    def __init__(
        self,
        handlers: List[SignalHandler],
        action_dispatchers: Dict[str, Callable[[SignalHandler, dict], Awaitable[None]]],
        autotrade_enabled: Callable[[], bool],
        chat_log: Callable[[str], None],
        run_async: Callable[[Awaitable[None]], None],
    ) -> None:
        self.handlers = handlers
        self.action_dispatchers = action_dispatchers
        self.autotrade_enabled = autotrade_enabled
        self.chat_log = chat_log
        self.run_async = run_async
        self.cooldown = CooldownTracker()

    def run(self, sig) -> int:
        """Evaluate every handler against ``sig`` and fire matches.

        Returns the number of handlers that fired. Synchronous —
        the dispatchers themselves are async and get scheduled via
        ``self.run_async`` so this returns immediately.
        """
        if not self.handlers:
            return 0

        flat = _flatten_signal(sig)
        symbol = (flat.get("symbol") or "?").upper()
        fired = 0

        for handler in self.handlers:
            if not handler.enabled:
                continue
            try:
                if not matches(handler, sig):
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "signal_handler %r match failed: %s", handler.name, exc
                )
                continue

            # Cooldown check
            if not self.cooldown.can_fire(
                handler.name, symbol, handler.cooldown_seconds
            ):
                continue

            # Autotrade gate
            if self._needs_autotrade(handler) and not self.autotrade_enabled():
                self.chat_log(
                    f"[dim italic][handler:{handler.name}] gated — autotrade is OFF "
                    f"(/autotrade on to enable). Signal: {symbol} {flat.get('signal_type','?')}[/]"
                )
                continue

            # Dispatch
            dispatcher = self.action_dispatchers.get(handler.action)
            if dispatcher is None:
                logger.warning(
                    "signal_handler %r: no dispatcher for action %r",
                    handler.name, handler.action,
                )
                continue

            self.cooldown.mark_fired(handler.name, symbol)
            fired += 1

            self.chat_log(
                f"[dim italic][handler:{handler.name}] fired -> "
                f"{handler.action}{f' agent={handler.agent}' if handler.agent else ''} "
                f"on {symbol} {flat.get('signal_type','?')}[/]"
            )
            try:
                self.run_async(dispatcher(handler, flat))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "signal_handler %r dispatch failed: %s", handler.name, exc
                )

        return fired

    def _needs_autotrade(self, handler: SignalHandler) -> bool:
        """Decide whether this handler is gated behind /autotrade.

        Two paths to gating:
          1. The handler config explicitly sets ``requires_autotrade: true``
          2. The handler dispatches to an agent in ``AUTOTRADE_GATED_AGENTS``
             (currently just ``trader``) — implicit gate so the user
             can't accidentally configure away the safety net.
        """
        if handler.requires_autotrade:
            return True
        if (
            handler.action == ACTION_DISPATCH_AGENT
            and handler.agent
            and handler.agent.lower() in AUTOTRADE_GATED_AGENTS
        ):
            return True
        return False


# ── Config loading ──────────────────────────────────────────


def load_handlers_from_config(config: Dict[str, Any]) -> List[SignalHandler]:
    """Parse the ``signal_handlers`` block of agent-config.json.

    A bad entry is logged and skipped — one malformed handler
    shouldn't disable every other one.
    """
    raw_list = config.get("signal_handlers") or []
    if not isinstance(raw_list, list):
        logger.warning("signal_handlers must be a list, got %s", type(raw_list).__name__)
        return []
    parsed: List[SignalHandler] = []
    for i, raw in enumerate(raw_list):
        try:
            parsed.append(SignalHandler.from_dict(raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("signal_handlers[%d] invalid: %s", i, exc)
    return parsed
