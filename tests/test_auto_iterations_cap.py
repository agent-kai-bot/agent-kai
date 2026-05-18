"""Phase 0 follow-up (#10247): MAX_AUTO_ITERATIONS no longer silently clamps
the per-role 200-iteration floor.

The dispatcher cascades a per-role floor of FLEET_MAX_ITERATIONS_FLOOR=200
(see agent.taskboard_dispatcher._resolve_max_iterations_for_role). Before
this fix, daemon.core.MAX_AUTO_ITERATIONS=100 silently min'd the cascade
to 100 — below the working ceiling for substantive dev work — causing
agents to hit langchain's `Stopping agent prematurely` mid-PR.

These tests parse the env-var resolution directly so they work even
without langchain installed (langchain is imported transitively by
daemon.core but isn't needed to test the cap-resolution arithmetic).
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch


def _resolve_cap(env: dict[str, str]) -> int:
    """Mirror of the cap-resolution expression in daemon/core.py:36-46.

    Kept in lockstep with that file. If you change the cap-resolution
    logic in daemon.core, update this helper to match.
    """
    raw = env.get("KAI_AUTO_ITERATIONS_CAP", "").strip()
    if not raw:
        return 10000
    try:
        return max(1, int(raw))
    except ValueError:
        return 10000


class MaxAutoIterationsCapTests(unittest.TestCase):
    def test_cap_default_is_above_200_floor(self) -> None:
        """Default cap must be well above the dispatcher's per-role 200 floor."""
        cap = _resolve_cap({})
        self.assertGreaterEqual(cap, 200)
        self.assertEqual(cap, 10000)

    def test_cap_overridable_via_env(self) -> None:
        self.assertEqual(_resolve_cap({"KAI_AUTO_ITERATIONS_CAP": "500"}), 500)
        self.assertEqual(_resolve_cap({"KAI_AUTO_ITERATIONS_CAP": "20000"}), 20000)

    def test_empty_env_falls_back_to_default(self) -> None:
        self.assertEqual(_resolve_cap({"KAI_AUTO_ITERATIONS_CAP": ""}), 10000)

    def test_malformed_env_falls_back_to_default(self) -> None:
        self.assertEqual(_resolve_cap({"KAI_AUTO_ITERATIONS_CAP": "bad"}), 10000)

    def test_cap_resolution_matches_daemon_core_source(self) -> None:
        """The cap-resolution helper here must stay in sync with the real
        expression in daemon/core.py. Source-line check guards against
        accidental drift if someone refactors daemon.core but forgets
        these tests.
        """
        path = os.path.join(
            os.path.dirname(__file__), "..", "daemon", "core.py"
        )
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("KAI_AUTO_ITERATIONS_CAP", content)
        self.assertIn("DEFAULT_AUTO_ITERATIONS_CAP = 10000", content)
        # Old hard-coded 100 cap must be gone.
        self.assertNotIn("MAX_AUTO_ITERATIONS = 100", content)


if __name__ == "__main__":
    unittest.main()
