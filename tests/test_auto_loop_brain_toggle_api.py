from __future__ import annotations

import json
import asyncio
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from agent.auto_evaluator import stop_decision
from agent.auto_loop_brain import AutoLoopBrainConfig, LLMResult
from daemon.runtime_config_store import RuntimeConfigStore
from daemon.server import DaemonServer, create_app


class _FakeBus:
    def __init__(self, url: str, agent_name: str) -> None:
        self.url = url
        self.agent_name = agent_name

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


class _FakeRunner:
    async def run(self, user_input: str, **kwargs):
        del kwargs
        yield {"type": "final", "data": f"done:{user_input}"}

    def set_auto_mode(self, enabled: bool, max_iterations: int = 40) -> None:
        return None

    def consume_auto_pause_reason(self):
        return None


class _ProbeClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def complete_json(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        return LLMResult(text="pong", model_id=str(kwargs.get("model") or "gpt-5.5"))


class _FakeEvaluator:
    def __init__(self, config: AutoLoopBrainConfig, probe_client: _ProbeClient) -> None:
        self.config = config
        self.llm_client = probe_client
        self.last_metadata = {"evaluator_kind": "regex"}
        self._llm_calls_this_session = 0

    def evaluate(self, data):
        del data
        if self.config.enabled:
            self._llm_calls_this_session += 1
        return stop_decision("test evaluator")


def _write_base_config(path: Path, *, enabled: bool = False) -> None:
    path.write_text(
        json.dumps(
            {
                "daemon": {
                    "auto_loop_brain": {
                        "enabled": enabled,
                        "client": "codex-cli",
                        "endpoint": None,
                        "model_id": "gpt-5.5",
                        "codex_reasoning_effort": "medium",
                        "max_history_tokens": 16000,
                        "temperature": 0.0,
                        "min_continue_confidence": 0.85,
                        "timeout_seconds": 20.0,
                        "max_output_tokens": 512,
                        "max_llm_critic_calls_per_session": 20,
                        "max_consecutive_llm_critic_calls": 5,
                    }
                },
                "endpoints": {"codex-cli": {"provider": "codex-cli"}},
            }
        ),
        encoding="utf-8",
    )


def _store(tmp_path: Path, *, enabled: bool = False) -> RuntimeConfigStore:
    base_path = tmp_path / "agent-config.json"
    _write_base_config(base_path, enabled=enabled)
    return RuntimeConfigStore(
        base_config_path=base_path,
        overrides_path=tmp_path / "workspaces" / "runtime_overrides.json",
    )


def _client(
    tmp_path: Path,
    store: RuntimeConfigStore,
    *,
    allow_unauthenticated_local: bool = False,
) -> TestClient:
    token_path = tmp_path / "daemon-token.txt"
    token_path.write_text("secret-token\n", encoding="utf-8")
    app = create_app(
        agent_name="kai",
        nats_url="nats://unit-test",
        bus_factory=_FakeBus,
        token_path=token_path,
        allow_unauthenticated_local=allow_unauthenticated_local,
        db_path=tmp_path / "daemon.sqlite3",
        taskboard_dispatcher_enabled=False,
        runtime_config_store=store,
    )
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret-token"}


def test_auto_loop_brain_config_get_patch_and_health(tmp_path: Path) -> None:
    store = _store(tmp_path)
    probe = _ProbeClient()

    with mock.patch(
        "daemon.server.build_auto_response_evaluator",
        side_effect=lambda **kwargs: _FakeEvaluator(kwargs["config"], probe),
    ):
        with _client(tmp_path, store) as client:
            initial = client.get(
                "/api/daemon/config/auto_loop_brain",
                headers=_auth(),
            )
            assert initial.status_code == 200
            assert initial.json()["enabled"] is False

            patched = client.patch(
                "/api/daemon/config/auto_loop_brain",
                headers=_auth(),
                json={"enabled": True},
            )

            assert patched.status_code == 200
            assert patched.json()["enabled"] is True
            assert probe.calls[-1]["system"] == "ping"

            health = client.get("/api/health", headers=_auth())
            assert health.status_code == 200
            brain = health.json()["auto_loop_brain"]
            assert brain["enabled"] is True
            assert brain["effective_client"] == "codex-cli"
            assert brain["effective_model"] == "gpt-5.5"
            assert brain["boot_probe_last_ok"] is True


def test_auto_loop_brain_patch_rejects_invalid_body(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with _client(tmp_path, store) as client:
        missing = client.patch(
            "/api/daemon/config/auto_loop_brain",
            headers=_auth(),
            json={},
        )
        extra = client.patch(
            "/api/daemon/config/auto_loop_brain",
            headers=_auth(),
            json={"enabled": True, "model_id": "gpt-5.4"},
        )

    assert missing.status_code == 422
    assert extra.status_code == 422


def test_auto_loop_brain_patch_requires_authorized_bearer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with _client(tmp_path, store) as client:
        no_token = client.patch(
            "/api/daemon/config/auto_loop_brain",
            json={"enabled": False},
        )
        wrong_token = client.patch(
            "/api/daemon/config/auto_loop_brain",
            headers={"Authorization": "Bearer wrong-token"},
            json={"enabled": False},
        )

    assert no_token.status_code == 401
    assert wrong_token.status_code == 401


def test_auto_loop_brain_kill_switch_blocks_enable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    monkeypatch.setenv("KAI_AUTO_LOOP_BRAIN_KILL_SWITCH", "1")

    with _client(tmp_path, store) as client:
        response = client.patch(
            "/api/daemon/config/auto_loop_brain",
            headers=_auth(),
            json={"enabled": True},
        )
        health = client.get("/api/health.auto_loop_brain", headers=_auth())

    assert response.status_code == 400
    assert "kill switch" in response.json()["detail"]
    assert health.json()["enabled"] is False
    assert health.json()["kill_switch_active"] is True
    assert not store.overrides_path.exists()


def test_auto_loop_brain_probe_failure_leaves_prior_state_intact(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    probe = _ProbeClient(
        error=RuntimeError("Authorization: Bearer super-secret-token\ntoken=raw-secret")
    )

    with mock.patch(
        "daemon.server.build_auto_response_evaluator",
        side_effect=lambda **kwargs: _FakeEvaluator(kwargs["config"], probe),
    ):
        with _client(tmp_path, store) as client:
            response = client.patch(
                "/api/daemon/config/auto_loop_brain",
                headers=_auth(),
                json={"enabled": True},
            )
            config = client.get(
                "/api/daemon/config/auto_loop_brain",
                headers=_auth(),
            )

    assert response.status_code == 400
    assert "super-secret-token" not in response.json()["detail"]
    assert "raw-secret" not in response.json()["detail"]
    assert config.json()["enabled"] is False
    assert not store.overrides_path.exists()


def test_auto_loop_brain_toggle_refreshes_live_session_on_next_turn(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    probe = _ProbeClient()

    with mock.patch("daemon.server.Session.attach_runtime", autospec=True) as attach:
        attach.side_effect = lambda session, **kwargs: setattr(
            session, "agent_runner", _FakeRunner()
        ) or session.agent_runner
        with mock.patch(
            "daemon.server.build_auto_response_evaluator",
            side_effect=lambda **kwargs: _FakeEvaluator(kwargs["config"], probe),
        ), mock.patch(
            "daemon.core.SESSIONS_ROOT_DIR",
            tmp_path / "sessions",
        ), mock.patch(
            "daemon.core.SESSION_INDEX_PATH",
            tmp_path / "sessions" / "index.json",
        ):
            with _client(tmp_path, store) as client:
                created = client.post(
                    "/api/sessions",
                    headers=_auth(),
                    json={"name": "alpha"},
                )
                assert created.status_code == 201

                daemon: DaemonServer = client.app.state.daemon_server
                managed = daemon.sessions["alpha"]
                assert managed.session.auto_response_evaluator.config.enabled is False

                enabled = client.patch(
                    "/api/daemon/config/auto_loop_brain",
                    headers=_auth(),
                    json={"enabled": True},
                )
                assert enabled.status_code == 200
                managed.session.auto_response_evaluator = _FakeEvaluator(
                    AutoLoopBrainConfig(enabled=False),
                    probe,
                )

                result = asyncio.run(daemon.run_input(managed, "hello"))

                assert result.error is None
                assert managed.session.auto_response_evaluator.config.enabled is True

                disabled = client.patch(
                    "/api/daemon/config/auto_loop_brain",
                    headers=_auth(),
                    json={"enabled": False},
                )
                assert disabled.status_code == 200
                assert managed.session.auto_response_evaluator.config.enabled is False


def test_auto_loop_brain_toggle_persists_across_daemon_restart(
    tmp_path: Path,
) -> None:
    first_store = _store(tmp_path)
    probe = _ProbeClient()

    with mock.patch(
        "daemon.server.build_auto_response_evaluator",
        side_effect=lambda **kwargs: _FakeEvaluator(kwargs["config"], probe),
    ):
        with _client(tmp_path, first_store) as client:
            response = client.patch(
                "/api/daemon/config/auto_loop_brain",
                headers=_auth(),
                json={"enabled": True},
            )
            assert response.status_code == 200

        second_store = RuntimeConfigStore(
            base_config_path=first_store.base_config_path,
            overrides_path=first_store.overrides_path,
        )
        with _client(tmp_path, second_store) as restarted:
            response = restarted.get(
                "/api/daemon/config/auto_loop_brain",
                headers=_auth(),
            )

    assert response.status_code == 200
    assert response.json()["enabled"] is True
