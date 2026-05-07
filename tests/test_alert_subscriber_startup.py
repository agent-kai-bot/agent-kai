from __future__ import annotations

import unittest
from unittest import mock

from daemon.server import DaemonServer


class AlertSubscriberStartupTests(unittest.TestCase):
    def test_daemon_construction_fails_fast_for_enabled_missing_alert_template(self):
        config = {
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
                },
                "heartbeat": {"enabled": False},
            }
        }
        with mock.patch("daemon.server.get_agent_config", return_value=config):
            with self.assertRaises(FileNotFoundError):
                DaemonServer(agent_name="kai", nats_url="nats://unit-test", taskboard_dispatcher_enabled=False)


if __name__ == "__main__":
    unittest.main()
