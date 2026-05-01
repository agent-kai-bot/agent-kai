"""Tests for agent.preflight — Phase 2 of epic #10028 (#10221).

Catches the exact stale-config / placeholder-key / unresolved-URL failure
modes that hid the recent 5-day silent outage.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.preflight import (
    HISTORICAL_DEFAULT_URLS,
    PLACEHOLDER_API_KEY_PATTERNS,
    PreflightConfig,
    PreflightFailure,
    check_config_drift,
    check_endpoint_placeholder,
    check_required_env,
    check_service_url,
    collect_active_endpoints,
    compute_agent_config_sha256,
    run_preflight,
    run_preflight_or_exit,
)


def _write_config(directory: Path, payload: dict) -> Path:
    path = directory / "agent-config.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _good_config() -> dict:
    """A canonical good config used by happy-path tests."""
    return {
        "endpoints": {
            "codex-cli": {
                "provider": "codex-cli",
                "base_url": "https://chatgpt.com/backend-api/codex",
            },
            "real-llm": {
                "provider": "openai",
                "base_url": "https://example.test/v1",
                "api_key": "sk-real-prod-key",
            },
        },
        "agents": {
            "developer": {"endpoint": "codex-cli"},
            "code-reviewer": {"endpoint": "real-llm"},
        },
    }


class ComputeShaTests(unittest.TestCase):
    def test_sha_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(Path(tmp), {"a": 1})
            sha = compute_agent_config_sha256(path)
            self.assertIsInstance(sha, str)
            self.assertEqual(len(sha), 64)

    def test_sha_none_on_missing(self) -> None:
        self.assertIsNone(compute_agent_config_sha256(Path("/no/such/file.json")))


class CheckConfigDriftTests(unittest.TestCase):
    def test_in_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"x": [1, 2, 3]}
            path = _write_config(Path(tmp), cfg)
            result = check_config_drift(loaded_config=cfg, config_path=path)
            self.assertTrue(result.ok)

    def test_drift_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            on_disk = {"x": "old"}
            in_memory = {"x": "new"}
            path = _write_config(Path(tmp), on_disk)
            result = check_config_drift(loaded_config=in_memory, config_path=path)
            self.assertFalse(result.ok)
            self.assertEqual(result.failure_class, "config_stale")

    def test_unreadable(self) -> None:
        result = check_config_drift(
            loaded_config={"x": 1}, config_path=Path("/no/such/file.json")
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_class, "config_missing_required")


class CheckRequiredEnvTests(unittest.TestCase):
    def test_present(self) -> None:
        with patch.dict(os.environ, {"FOO_REQUIRED": "value", "BAR_REQUIRED": "v2"}):
            results = check_required_env(required=["FOO_REQUIRED", "BAR_REQUIRED"])
        self.assertTrue(all(r.ok for r in results))

    def test_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZZ_DEFINITELY_NOT_SET", None)
            results = check_required_env(required=["ZZ_DEFINITELY_NOT_SET"])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].failure_class, "config_missing_required")

    def test_empty_env_treated_as_missing(self) -> None:
        with patch.dict(os.environ, {"WHITESPACE_ONLY": "   "}):
            results = check_required_env(required=["WHITESPACE_ONLY"])
        self.assertFalse(results[0].ok)


class CheckServiceUrlTests(unittest.TestCase):
    def test_ok(self) -> None:
        result = check_service_url(
            name="taskboard_url", url="http://srv01:18180", dev_mode=False
        )
        self.assertTrue(result.ok)

    def test_missing(self) -> None:
        result = check_service_url(name="taskboard_url", url="", dev_mode=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_class, "config_missing_required")

    def test_historical_default_rejected(self) -> None:
        for url in HISTORICAL_DEFAULT_URLS:
            with self.subTest(url=url):
                result = check_service_url(name="taskboard_url", url=url, dev_mode=False)
                self.assertFalse(result.ok)
                self.assertEqual(result.failure_class, "config_unresolved_hostname")

    def test_dev_mode_allows_historical_default(self) -> None:
        result = check_service_url(
            name="taskboard_url", url="http://taskboard:8080", dev_mode=True
        )
        self.assertTrue(result.ok)


class CheckEndpointPlaceholderTests(unittest.TestCase):
    def test_ok_with_real_key(self) -> None:
        result = check_endpoint_placeholder(
            endpoint_id="real",
            endpoint_cfg={
                "provider": "openai",
                "base_url": "https://example.test/v1",
                "api_key": "sk-real-prod-key",
            },
            dev_mode=False,
        )
        self.assertTrue(result.ok)

    def test_codex_cli_no_key_required(self) -> None:
        result = check_endpoint_placeholder(
            endpoint_id="codex-cli",
            endpoint_cfg={"provider": "codex-cli"},
            dev_mode=False,
        )
        self.assertTrue(result.ok)

    def test_missing_kai_api_key_caught(self) -> None:
        """The exact placeholder that hid the 5-day outage."""
        result = check_endpoint_placeholder(
            endpoint_id="kai-smart",
            endpoint_cfg={
                "provider": "openai",
                "base_url": "https://agent-k.ai/v1",
                "api_key": "missing-kai-api-key",
            },
            dev_mode=False,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_class, "config_placeholder_value")

    def test_changeme_caught(self) -> None:
        result = check_endpoint_placeholder(
            endpoint_id="ep",
            endpoint_cfg={"provider": "openai", "api_key": "changeme"},
            dev_mode=False,
        )
        self.assertFalse(result.ok)

    def test_env_var_resolution(self) -> None:
        with patch.dict(os.environ, {"REAL_KEY_ENV": "sk-from-env"}):
            result = check_endpoint_placeholder(
                endpoint_id="ep",
                endpoint_cfg={
                    "provider": "openai",
                    "api_key_env": "REAL_KEY_ENV",
                },
                dev_mode=False,
            )
        self.assertTrue(result.ok)

    def test_missing_env_var_caught(self) -> None:
        os.environ.pop("ZZ_NEVER_SET", None)
        result = check_endpoint_placeholder(
            endpoint_id="ep",
            endpoint_cfg={"provider": "openai", "api_key_env": "ZZ_NEVER_SET"},
            dev_mode=False,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_class, "config_missing_required")


class CollectActiveEndpointsTests(unittest.TestCase):
    def test_collects_only_referenced_endpoints(self) -> None:
        cfg = {
            "endpoints": {
                "active": {"provider": "openai"},
                "stale": {"provider": "openai"},
            },
            "agents": {
                "developer": {"endpoint": "active"},
                "qa-agent": {"endpoint": "active"},
            },
        }
        active = collect_active_endpoints(
            loaded_config=cfg, roles=["developer", "qa-agent"]
        )
        self.assertEqual(set(active.keys()), {"active"})


class RunPreflightTests(unittest.TestCase):
    """Integration-style tests: run the full check chain on synthesized configs."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_good_config_passes(self) -> None:
        cfg = _good_config()
        path = _write_config(self.dir, cfg)
        with patch.dict(
            os.environ,
            {
                "TASKBOARD_URL": "http://srv01:18180",
                "TASKBOARD_BEARER_TOKEN": "real-bearer",
                "FORGEJO_URL": "http://forgejo.home",
                "PREFLIGHT_SMOKE_TIMEOUT_SECONDS": "1",
            },
        ):
            preflight_cfg = PreflightConfig(
                config_path=path,
                required_env_vars=("TASKBOARD_URL", "TASKBOARD_BEARER_TOKEN"),
                roles_to_check=("developer",),  # codex-cli only; no HTTP smoke
                smoke_timeout_seconds=1.0,
                dev_mode=False,
            )
            results = run_preflight(loaded_config=cfg, preflight_cfg=preflight_cfg)
        # codex-cli endpoint smoke depends on local OAuth state; allow it
        # to fail in CI but assert all NON-codex checks pass.
        non_codex_failures = [
            r
            for r in results
            if not r.ok and "codex" not in r.name
        ]
        self.assertEqual(non_codex_failures, [])

    def test_placeholder_key_blocks_startup(self) -> None:
        cfg = {
            "endpoints": {
                "kai-smart": {
                    "provider": "openai",
                    "base_url": "https://agent-k.ai/v1",
                    "api_key": "missing-kai-api-key",
                }
            },
            "agents": {"developer": {"endpoint": "kai-smart"}},
        }
        path = _write_config(self.dir, cfg)
        with patch.dict(
            os.environ,
            {"TASKBOARD_URL": "http://srv01:18180", "TASKBOARD_BEARER_TOKEN": "x"},
        ):
            preflight_cfg = PreflightConfig(
                config_path=path,
                required_env_vars=("TASKBOARD_URL", "TASKBOARD_BEARER_TOKEN"),
                roles_to_check=("developer",),
                smoke_timeout_seconds=1.0,
                dev_mode=False,
            )
            with self.assertRaises(PreflightFailure) as ctx:
                run_preflight_or_exit(loaded_config=cfg, preflight_cfg=preflight_cfg)
        failures = ctx.exception.results
        classes = {r.failure_class for r in failures}
        self.assertIn("config_placeholder_value", classes)

    def test_historical_taskboard_url_blocks_startup(self) -> None:
        cfg = _good_config()
        path = _write_config(self.dir, cfg)
        with patch.dict(
            os.environ,
            {
                "TASKBOARD_URL": "http://taskboard:8080",  # the literal footgun
                "TASKBOARD_BEARER_TOKEN": "real-bearer",
            },
        ):
            preflight_cfg = PreflightConfig(
                config_path=path,
                required_env_vars=("TASKBOARD_URL", "TASKBOARD_BEARER_TOKEN"),
                roles_to_check=("developer",),
                smoke_timeout_seconds=1.0,
                dev_mode=False,
            )
            with self.assertRaises(PreflightFailure) as ctx:
                run_preflight_or_exit(loaded_config=cfg, preflight_cfg=preflight_cfg)
        classes = {r.failure_class for r in ctx.exception.results}
        self.assertIn("config_unresolved_hostname", classes)

    def test_missing_required_env_blocks_startup(self) -> None:
        cfg = _good_config()
        path = _write_config(self.dir, cfg)
        # Clear required env vars.
        env = {k: v for k, v in os.environ.items() if k not in ("TASKBOARD_URL", "TASKBOARD_BEARER_TOKEN")}
        with patch.dict(os.environ, env, clear=True):
            preflight_cfg = PreflightConfig(
                config_path=path,
                required_env_vars=("TASKBOARD_URL", "TASKBOARD_BEARER_TOKEN"),
                roles_to_check=("developer",),
                smoke_timeout_seconds=1.0,
                dev_mode=False,
            )
            with self.assertRaises(PreflightFailure) as ctx:
                run_preflight_or_exit(loaded_config=cfg, preflight_cfg=preflight_cfg)
        classes = {r.failure_class for r in ctx.exception.results}
        self.assertIn("config_missing_required", classes)

    def test_drift_between_loaded_and_disk_blocks_startup(self) -> None:
        on_disk = _good_config()
        in_memory = _good_config()
        in_memory["agents"]["developer"]["endpoint"] = "real-llm"  # drift
        path = _write_config(self.dir, on_disk)
        with patch.dict(
            os.environ,
            {
                "TASKBOARD_URL": "http://srv01:18180",
                "TASKBOARD_BEARER_TOKEN": "x",
            },
        ):
            preflight_cfg = PreflightConfig(
                config_path=path,
                required_env_vars=("TASKBOARD_URL", "TASKBOARD_BEARER_TOKEN"),
                roles_to_check=("developer",),
                smoke_timeout_seconds=1.0,
                dev_mode=False,
            )
            with self.assertRaises(PreflightFailure) as ctx:
                run_preflight_or_exit(
                    loaded_config=in_memory, preflight_cfg=preflight_cfg
                )
        classes = {r.failure_class for r in ctx.exception.results}
        self.assertIn("config_stale", classes)


if __name__ == "__main__":
    unittest.main()
