from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from daemon.heartbeat import HeartbeatConfig
from daemon.server import create_app
from daemon.signal_router.shim_alert_subscriber import (
    AlertSubscriberConfig,
    AlertSubscriptionConfig,
    load_alert_subscriber_config,
    translate_alert_subscriber_config,
)


def test_defaults_include_disabled_polymarket_subscription() -> None:
    config = load_alert_subscriber_config({})

    assert config.enabled is False
    assert config.kill_switch is False
    sub = config.subscriptions[0]
    assert sub.subject_pattern == "polymarket.alpha.alarm.>"
    assert sub.prompt_template_path == "prompts/alerts/polymarket.md.tmpl"
    assert sub.target_session == "kai"
    assert sub.max_injected_turns_per_hour == 10
    assert sub.enabled is False


def test_alerts_yaml_overlay_is_optional_and_can_override(tmp_path: Path) -> None:
    overlay = tmp_path / "alerts.yaml"
    overlay.write_text(
        "subscriptions:\n"
        "  - name: polymarket-default\n"
        "    enabled: true\n"
        "    subject_pattern: polymarket.custom.>\n"
        "    prompt_template_path: prompts/alerts/polymarket.md.tmpl\n"
        "    target_session: kai\n",
        encoding="utf-8",
    )

    config = load_alert_subscriber_config(
        {"daemon": {"alert_subscriber": {"alerts_yaml_path": str(overlay)}}}
    )

    assert config.subscriptions[0].enabled is True
    assert config.subscriptions[0].subject_pattern == "polymarket.custom.>"


def test_polymarket_subscription_translates_to_inject_session_route() -> None:
    config = AlertSubscriberConfig(
        enabled=True,
        subscriptions=(
            AlertSubscriptionConfig(
                name="polymarket-default",
                enabled=True,
                subject_pattern="polymarket.alpha.alarm.>",
                target_session="kai",
            ),
        ),
    )

    routes, errors = translate_alert_subscriber_config(config)

    assert errors == []
    assert len(routes) == 1
    route = routes[0]
    assert route.name == "alert-subscription:polymarket-default"
    assert route.channel == "polymarket_alarms"
    assert route.actions[0].kind == "inject_session"
    assert route.actions[0].target == "kai"


def test_alert_route_has_source_compat_annotation() -> None:
    config = AlertSubscriberConfig(
        subscriptions=(AlertSubscriptionConfig(name="custom"),),
    )

    routes, _ = translate_alert_subscriber_config(config)

    assert routes[0].config["source_compat"] == "alert_subscriber"


def test_multiple_subscriptions_translate_cleanly() -> None:
    config = AlertSubscriberConfig(
        enabled=True,
        subscriptions=(
            AlertSubscriptionConfig(name="one", enabled=True, subject_pattern="alerts.one.>"),
            AlertSubscriptionConfig(name="two", enabled=False, subject_pattern="alerts.two.*"),
        ),
    )

    routes, errors = translate_alert_subscriber_config(config)

    assert errors == []
    assert [route.name for route in routes] == [
        "alert-subscription:one",
        "alert-subscription:two",
    ]
    assert routes[0].enabled is True
    assert routes[1].enabled is False


def test_daemon_boots_with_legacy_alert_subscriber_block_router_only() -> None:
    config = {
        "daemon": {
            "signal_router": {
                "mode": "shadow",
                "channels": {
                    "polymarket_alarms": {
                        "subjects": ["alerts.>"],
                        "schema": "polymarket_alarm",
                    }
                },
                "routes": [],
            },
            "alert_subscriber": {
                "enabled": True,
                "subscriptions": [
                    {
                        "name": "legacy-polymarket",
                        "enabled": True,
                        "subject_pattern": "polymarket.alpha.alarm.>",
                        "prompt_template_path": "prompts/alerts/polymarket.md.tmpl",
                        "target_session": "kai",
                        "max_injected_turns_per_hour": 10,
                    }
                ],
            },
        }
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        with mock.patch("daemon.server.get_agent_config", return_value=config):
            app = create_app(
                agent_name="kai",
                nats_url="nats://unit-test",
                bus_factory=None,
                token_path=base / "daemon.token",
                allow_unauthenticated_local=True,
                include_taskboard_gateway=False,
                db_path=base / "daemon.db",
                taskboard_dispatcher_enabled=False,
                heartbeat_config=HeartbeatConfig(enabled=False),
            )
            with TestClient(app) as client:
                response = client.get("/api/health")

        assert response.status_code == 200
        server = app.state.daemon_server
        assert not hasattr(server, "alert_subscriber")
        assert not hasattr(server, "alert_subscriber_config")
        assert "alert-subscription:legacy-polymarket" in server.signal_router.routes
        assert (
            server.signal_router.routes["alert-subscription:legacy-polymarket"]
            .config["source_compat"]
            == "alert_subscriber"
        )


def test_daemon_boots_without_legacy_alert_subscriber_block() -> None:
    config = {
        "daemon": {
            "signal_router": {
                "mode": "shadow",
                "channels": {
                    "polymarket_alarms": {
                        "subjects": ["alerts.>"],
                        "schema": "polymarket_alarm",
                    }
                },
                "routes": [],
            }
        }
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        with mock.patch("daemon.server.get_agent_config", return_value=config):
            app = create_app(
                agent_name="kai",
                nats_url="nats://unit-test",
                bus_factory=None,
                token_path=base / "daemon.token",
                allow_unauthenticated_local=True,
                include_taskboard_gateway=False,
                db_path=base / "daemon.db",
                taskboard_dispatcher_enabled=False,
                heartbeat_config=HeartbeatConfig(enabled=False),
            )
            with TestClient(app) as client:
                response = client.get("/api/health")

        assert response.status_code == 200
        server = app.state.daemon_server
        assert not hasattr(server, "alert_subscriber")
        assert not hasattr(server, "alert_subscriber_config")
        assert all(
            not route_name.startswith("alert-subscription:")
            for route_name in server.signal_router.routes
        )
