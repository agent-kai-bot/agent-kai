from __future__ import annotations

from daemon.alert_subscriber import AlertSubscriberConfig, AlertSubscriptionConfig
from daemon.signal_router.shim_alert_subscriber import translate_alert_subscriber_config


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
