from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from daemon.alert_subscriber import (
    AlertSubscriptionConfig,
    MalformedAlertPayload,
    load_alert_subscriber_config,
    normalize_alert_event,
    render_alert_prompt,
)


class AlertSubscriberConfigTests(unittest.TestCase):
    def test_defaults_include_disabled_polymarket_subscription(self):
        config = load_alert_subscriber_config({})
        self.assertTrue(config.enabled)
        self.assertFalse(config.kill_switch)
        sub = config.subscriptions[0]
        self.assertEqual(sub.subject_pattern, "polymarket.alpha.alarm.>")
        self.assertEqual(sub.prompt_template_path, "prompts/alerts/polymarket.md.tmpl")
        self.assertEqual(sub.target_session, "kai")
        self.assertEqual(sub.max_injected_turns_per_hour, 10)
        self.assertFalse(sub.enabled)

    def test_env_overrides_and_kill_switch(self):
        with mock.patch.dict(
            "os.environ",
            {
                "KAI_ALERT_SUBSCRIBER_ENABLED": "0",
                "KAI_ALERT_SUBSCRIBER_KILL_SWITCH": "1",
                "KAI_ALERT_SUBSCRIBER_ENABLE_POLYMARKET": "1",
                "KAI_ALERT_SUBSCRIBER_POLYMARKET_TARGET_SESSION": "alerts",
            },
        ):
            config = load_alert_subscriber_config({})
        self.assertFalse(config.enabled)
        self.assertTrue(config.kill_switch)
        self.assertTrue(config.subscriptions[0].enabled)
        self.assertEqual(config.subscriptions[0].target_session, "alerts")

    def test_enabled_subscription_missing_template_fails_fast(self):
        with self.assertRaises(FileNotFoundError):
            load_alert_subscriber_config(
                {
                    "daemon": {
                        "alert_subscriber": {
                            "enabled": True,
                            "subscriptions": [
                                {
                                    "name": "missing",
                                    "enabled": True,
                                    "subject_pattern": "x.>",
                                    "prompt_template_path": "prompts/alerts/nope.md.tmpl",
                                    "target_session": "kai",
                                    "max_injected_turns_per_hour": 10,
                                }
                            ],
                        }
                    }
                }
            )

    def test_alerts_yaml_overlay_is_optional_and_can_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            overlay = Path(tmpdir) / "alerts.yaml"
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
        self.assertTrue(config.subscriptions[0].enabled)
        self.assertEqual(config.subscriptions[0].subject_pattern, "polymarket.custom.>")


class AlertSubscriberTemplateTests(unittest.TestCase):
    def test_template_render_required_and_optional_fields(self):
        sub = AlertSubscriptionConfig()
        prompt = render_alert_prompt(
            sub,
            "polymarket.alpha.alarm.large",
            {
                "source": "polymarket",
                "alert_type": "market_move",
                "severity": "critical",
                "title": "Large move",
                "summary": "YES odds moved quickly",
                "timestamp": "2026-05-07T19:05:00Z",
                "data": {"market": "abc"},
            },
        )
        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertIn("Alert from polymarket: Large move", prompt)
        self.assertIn("Severity: critical", prompt)
        self.assertIn("Subject: polymarket.alpha.alarm.large", prompt)
        self.assertIn("YES odds moved quickly", prompt)

    def test_malformed_payload_drops(self):
        sub = AlertSubscriptionConfig()
        self.assertIsNone(render_alert_prompt(sub, "polymarket.alpha.alarm.large", {"raw": "not json"}))
        with self.assertRaises(MalformedAlertPayload):
            normalize_alert_event(sub, "polymarket.alpha.alarm.large", {"data": []})


if __name__ == "__main__":
    unittest.main()
