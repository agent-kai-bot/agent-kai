"""Tests for Phase 0 of epic #10030 (#10247) Change 4.

Per-role max_iterations cascade resolved by
``_resolve_max_iterations_for_role``:

  1. env ``KAI_MAX_ITERATIONS_<ROLE_UPPER>``       (per-role escape hatch)
  2. ``agents.{role}.max_iterations`` in agent-config.json
  3. env ``KAI_MAX_ITERATIONS_DEFAULT``            (fleet-wide override)
  4. ``FLEET_MAX_ITERATIONS_FLOOR``                (hardcoded 200)
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from agent import taskboard_dispatcher as td


class ResolveMaxIterationsTests(unittest.TestCase):
    def setUp(self) -> None:
        # Strip every related env var; tests opt-in by patching os.environ.
        self._env_patcher = patch.dict(
            "os.environ",
            {},
            clear=True,
        )
        self._env_patcher.start()

    def tearDown(self) -> None:
        self._env_patcher.stop()

    # ------------------------------------------------------------------
    # Layer 4 — hardcoded floor
    # ------------------------------------------------------------------

    def test_returns_floor_when_no_role_no_env_no_config(self) -> None:
        with patch("config.get_agent_config", side_effect=KeyError):
            self.assertEqual(td._resolve_max_iterations_for_role(None), 200)
            self.assertEqual(td.FLEET_MAX_ITERATIONS_FLOOR, 200)

    # ------------------------------------------------------------------
    # Layer 3 — KAI_MAX_ITERATIONS_DEFAULT
    # ------------------------------------------------------------------

    def test_default_env_overrides_floor(self) -> None:
        with patch.dict("os.environ", {"KAI_MAX_ITERATIONS_DEFAULT": "150"}):
            self.assertEqual(td._resolve_max_iterations_for_role(None), 150)

    # ------------------------------------------------------------------
    # Layer 2 — agent-config.json
    # ------------------------------------------------------------------

    def test_agent_config_overrides_default_env(self) -> None:
        cfg = {"max_iterations": 80}
        with patch.dict("os.environ", {"KAI_MAX_ITERATIONS_DEFAULT": "150"}):
            with patch("config.get_agent_config", return_value=cfg):
                self.assertEqual(
                    td._resolve_max_iterations_for_role("code-reviewer"),
                    80,
                )

    def test_agent_config_with_no_role_falls_through_to_default(self) -> None:
        # role=None must skip the per-role lookup entirely (no spurious call)
        with patch.dict("os.environ", {"KAI_MAX_ITERATIONS_DEFAULT": "120"}):
            with patch("config.get_agent_config") as mock_cfg:
                self.assertEqual(td._resolve_max_iterations_for_role(None), 120)
                mock_cfg.assert_not_called()

    # ------------------------------------------------------------------
    # Layer 1 — KAI_MAX_ITERATIONS_<ROLE_UPPER>
    # ------------------------------------------------------------------

    def test_per_role_env_wins_over_agent_config(self) -> None:
        cfg = {"max_iterations": 80}
        with patch.dict(
            "os.environ",
            {
                "KAI_MAX_ITERATIONS_CODE_REVIEWER": "12",
                "KAI_MAX_ITERATIONS_DEFAULT": "150",
            },
        ):
            with patch("config.get_agent_config", return_value=cfg):
                self.assertEqual(
                    td._resolve_max_iterations_for_role("code-reviewer"),
                    12,
                )

    def test_per_role_env_normalizes_dashes_and_dots(self) -> None:
        # Role names like "qa.runner" or "code-reviewer" both normalize
        # to underscore-uppercase env keys.
        with patch.dict(
            "os.environ",
            {"KAI_MAX_ITERATIONS_QA_RUNNER": "60"},
        ):
            with patch("config.get_agent_config", return_value={}):
                self.assertEqual(
                    td._resolve_max_iterations_for_role("qa.runner"),
                    60,
                )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def test_invalid_env_value_falls_through(self) -> None:
        # Non-integer per-role env => skip layer 1; non-integer default => floor.
        with patch.dict(
            "os.environ",
            {
                "KAI_MAX_ITERATIONS_CODE_REVIEWER": "abc",
                "KAI_MAX_ITERATIONS_DEFAULT": "not-a-number",
            },
        ):
            with patch("config.get_agent_config", return_value={}):
                self.assertEqual(
                    td._resolve_max_iterations_for_role("code-reviewer"),
                    200,
                )

    def test_zero_or_negative_env_value_falls_through(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "KAI_MAX_ITERATIONS_CODE_REVIEWER": "0",
                "KAI_MAX_ITERATIONS_DEFAULT": "-5",
            },
        ):
            with patch("config.get_agent_config", return_value={}):
                self.assertEqual(
                    td._resolve_max_iterations_for_role("code-reviewer"),
                    200,
                )

    def test_agent_config_zero_or_negative_falls_through(self) -> None:
        """codex CR follow-up: agent-config max_iterations of 0 or negative
        must fall through to the next layer instead of being trusted.
        """
        with patch.dict("os.environ", {"KAI_MAX_ITERATIONS_DEFAULT": "75"}):
            with patch("config.get_agent_config", return_value={"max_iterations": 0}):
                self.assertEqual(
                    td._resolve_max_iterations_for_role("code-reviewer"),
                    75,
                )
            with patch("config.get_agent_config", return_value={"max_iterations": -10}):
                self.assertEqual(
                    td._resolve_max_iterations_for_role("code-reviewer"),
                    75,
                )

    def test_agent_config_non_integer_falls_through(self) -> None:
        with patch.dict("os.environ", {"KAI_MAX_ITERATIONS_DEFAULT": "75"}):
            with patch(
                "config.get_agent_config",
                return_value={"max_iterations": "lots"},
            ):
                self.assertEqual(
                    td._resolve_max_iterations_for_role("code-reviewer"),
                    75,
                )

    def test_get_agent_config_failure_does_not_crash_cascade(self) -> None:
        # If config lookup blows up, we still return a usable value.
        with patch.dict("os.environ", {"KAI_MAX_ITERATIONS_DEFAULT": "75"}):
            with patch(
                "config.get_agent_config",
                side_effect=RuntimeError("config bus down"),
            ):
                self.assertEqual(
                    td._resolve_max_iterations_for_role("code-reviewer"),
                    75,
                )


if __name__ == "__main__":
    unittest.main()
