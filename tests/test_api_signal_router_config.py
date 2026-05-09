from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

from daemon.runtime_config_store import RuntimeConfigStore
from daemon.server import create_app


class _FakeBus:
    def __init__(self, url: str, agent_name: str) -> None:
        self.url = url
        self.agent_name = agent_name
        self.subscriptions: list[tuple[str, Any]] = []
        self.messages: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def subscribe(self, subject: str, handler: Any) -> object:
        self.subscriptions.append((subject, handler))
        return object()

    async def request(self, subject: str, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        self.messages.append((subject, payload))
        return {"ok": True, "timeout": timeout}

    def on_message(self, callback: Any) -> None:
        self.messages.append(("on_message", {"callback": callback}))


def _signal_router_block(tmp_path: Path) -> dict[str, Any]:
    return {
        "mode": "shadow",
        "live_trades_enabled": False,
        "channels": {
            "polymarket_alarms": {
                "subjects": ["alerts.>"],
                "schema": "polymarket_alarm",
            }
        },
        "routes": [
            {
                "name": "polymarket-alarm-response",
                "enabled": True,
                "channel": "polymarket_alarms",
                "match": {},
                "actions": [
                    {
                        "kind": "spawn_agent",
                        "pack": "kai-alert-response",
                        "timeout_seconds": 300,
                        "cooldown_key_template": "{rule_id}:{token_id}",
                        "cooldown_seconds": 600,
                        "daily_cap": 50,
                    }
                ],
            }
        ],
        "agent_packs_dir": str(tmp_path / "agent-packs"),
        "dedup_table_path": str(tmp_path / "router_dedup.sqlite3"),
    }


def _agent_config(tmp_path: Path) -> dict[str, Any]:
    return {
        "daemon": {
            "signal_router": _signal_router_block(tmp_path),
            "heartbeat": {"enabled": False},
            "auto_loop_brain": {"enabled": False},
        },
        "endpoints": {"codex-cli": {"provider": "codex-cli"}},
        "agents": {},
        "signal_handlers": [],
    }


def _store(tmp_path: Path) -> RuntimeConfigStore:
    base_path = tmp_path / "agent-config.json"
    base_path.write_text(
        json.dumps({"daemon": {"signal_router": _signal_router_block(tmp_path)}}),
        encoding="utf-8",
    )
    return RuntimeConfigStore(
        base_config_path=base_path,
        overrides_path=tmp_path / "runtime_config.json",
    )


def _client(tmp_path: Path, store: RuntimeConfigStore) -> TestClient:
    token_path = tmp_path / "daemon-token.txt"
    token_path.write_text("secret-token\n", encoding="utf-8")
    app = create_app(
        agent_name="kai",
        nats_url="nats://unit-test",
        bus_factory=_FakeBus,
        token_path=token_path,
        allow_unauthenticated_local=False,
        db_path=tmp_path / "daemon.sqlite3",
        taskboard_dispatcher_enabled=False,
        runtime_config_store=store,
    )
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret-token"}


def test_get_signal_router_config_returns_expected_shape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with mock.patch("daemon.server.get_agent_config", return_value=_agent_config(tmp_path)):
        with _client(tmp_path, store) as client:
            response = client.get("/api/daemon/config/signal_router", headers=_auth())

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "shadow"
    assert payload["live_trades_enabled"] is False
    assert payload["kill_switch_active"] is False
    assert payload["dedup_stats"] == {
        "keys_count": 0,
        "cooldown_hits_24h": 0,
        "cap_hits_24h": 0,
    }
    route = payload["routes"][0]
    assert route["name"] == "polymarket-alarm-response"
    assert route["channel"] == "polymarket_alarms"
    assert route["actions"][0]["kind"] == "spawn_agent"
    assert route["enabled"] is True


def test_patch_live_trades_enabled_flips_subsequent_get(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with mock.patch("daemon.server.get_agent_config", return_value=_agent_config(tmp_path)):
        with _client(tmp_path, store) as client:
            patched = client.patch(
                "/api/daemon/config/signal_router",
                headers=_auth(),
                json={"live_trades_enabled": True},
            )
            subsequent = client.get("/api/daemon/config/signal_router", headers=_auth())

    assert patched.status_code == 200
    assert patched.json()["live_trades_enabled"] is True
    assert subsequent.json()["live_trades_enabled"] is True


def test_patch_signal_router_rejects_invalid_types(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with mock.patch("daemon.server.get_agent_config", return_value=_agent_config(tmp_path)):
        with _client(tmp_path, store) as client:
            response = client.patch(
                "/api/daemon/config/signal_router",
                headers=_auth(),
                json={"live_trades_enabled": "yes"},
            )

    assert response.status_code == 400
    assert "boolean" in response.json()["detail"]


def test_patch_signal_router_kill_switch_returns_403(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    monkeypatch.setenv("KAI_SIGNAL_ROUTER_KILL_SWITCH", "1")

    with mock.patch("daemon.server.get_agent_config", return_value=_agent_config(tmp_path)):
        with _client(tmp_path, store) as client:
            response = client.patch(
                "/api/daemon/config/signal_router",
                headers=_auth(),
                json={"live_trades_enabled": True},
            )

    assert response.status_code == 403
    assert store.get_signal_router_live_trades_enabled() is False


def test_patch_route_disable_reflected_and_skips_dispatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with mock.patch("daemon.server.get_agent_config", return_value=_agent_config(tmp_path)):
        with _client(tmp_path, store) as client:
            patched = client.patch(
                "/api/daemon/config/signal_router",
                headers=_auth(),
                json={"routes": {"polymarket-alarm-response": {"enabled": False}}},
            )
            server = client.app.state.daemon_server
            decisions = server.signal_router.decide(
                {
                    "subject": "alerts.cross_above_0_65",
                    "payload": {"rule_id": "cross_above_0_65", "token_id": "824"},
                }
            )

    assert patched.status_code == 200
    assert patched.json()["routes"][0]["enabled"] is False
    assert decisions == []
