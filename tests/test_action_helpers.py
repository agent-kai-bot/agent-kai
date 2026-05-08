from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import asyncio


class FakeRunner:
    def __init__(self) -> None:
        self.chat_history = []
        self._is_auto_continuation = False
        self.tool_call_active = False


class FakeSession:
    def __init__(self, name: str = "kai") -> None:
        self.name = name
        self.auto_mode = True
        self.agent_runner = FakeRunner()
        self.chat_history = []
        self.events = []
        self.signal_router_event_injection_timestamps = deque()
        self._signal_router_event_turn_active = False

    def publish_event(self, topic, payload):
        self.events.append((topic, payload))


@dataclass
class FakeManaged:
    session: FakeSession
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_input_task: asyncio.Task | None = None
