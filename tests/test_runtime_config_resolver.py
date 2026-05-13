from __future__ import annotations

import logging
import unittest

from agent.runtime_config_resolver import (
    RuntimeConfigError,
    RuntimeConfigResolver,
)


class _FakeVaultClient:
    def __init__(self, payloads=None, *, error: Exception | None = None) -> None:
        self.payloads = dict(payloads or {})
        self.error = error
        self.reads: list[str] = []

    def read(self, path: str):
        self.reads.append(path)
        if self.error is not None:
            raise self.error
        if path not in self.payloads:
            raise RuntimeError("missing vault path")
        return dict(self.payloads[path])


class RuntimeConfigResolverTests(unittest.TestCase):
    def test_vault_hit_returns_pat_and_ttl_cache_hits_subsequent_reads(self) -> None:
        now = 100.0
        vault = _FakeVaultClient(
            {
                "forgejo/agent-developer": {
                    "username": "agent-developer",
                    "password": "vault-pat",
                }
            }
        )
        resolver = RuntimeConfigResolver(
            vault_client=vault,
            env={"FORGEJO_URL": "http://forgejo.local"},
            clock=lambda: now,
        )

        first = resolver.resolve_for_role("Developer")
        second = resolver.resolve_for_role("developer")

        self.assertEqual(first.forgejo_pat, "vault-pat")
        self.assertEqual(first.forgejo_user, "agent-developer")
        self.assertEqual(second.forgejo_pat, "vault-pat")
        self.assertEqual(vault.reads, ["forgejo/agent-developer"])

    def test_vault_miss_falls_back_to_role_env_then_global_then_raises(self) -> None:
        vault = _FakeVaultClient(error=RuntimeError("vault unavailable"))

        role_env = RuntimeConfigResolver(
            vault_client=vault,
            env={"FORGEJO_TOKEN_CODE_REVIEWER": "role-pat"},
        )
        self.assertEqual(
            role_env.resolve_for_role("Code Reviewer").forgejo_pat,
            "role-pat",
        )

        global_env = RuntimeConfigResolver(
            vault_client=vault,
            env={"FORGEJO_TOKEN": "global-pat"},
        )
        self.assertEqual(
            global_env.resolve_for_role("security-auditor").forgejo_pat,
            "global-pat",
        )

        missing = RuntimeConfigResolver(vault_client=vault, env={})
        with self.assertRaisesRegex(RuntimeConfigError, "unable to resolve Forgejo PAT"):
            missing.resolve_for_role("qa-agent")

        orchestrator = missing.resolve_for_role("orchestrator")
        self.assertEqual(orchestrator.forgejo_pat, "")
        self.assertEqual(orchestrator.role, "orchestrator")

    def test_secret_material_does_not_appear_in_repr_str_or_logs(self) -> None:
        logger = logging.getLogger("tests.runtime_config_resolver")
        vault = _FakeVaultClient(
            {
                "forgejo/agent-developer": {
                    "username": "agent-developer",
                    "password": "vault-super-secret",
                }
            }
        )
        resolver = RuntimeConfigResolver(
            vault_client=vault,
            env={"TASKBOARD_BEARER_TOKEN": "taskboard-super-secret"},
            logger=logger,
        )

        with self.assertLogs(logger, level="INFO") as logs:
            config = resolver.resolve_for_role("developer")
            logger.info("config=%s", config)

        rendered = "\n".join(logs.output)
        self.assertNotIn("vault-super-secret", repr(config))
        self.assertNotIn("vault-super-secret", str(config))
        self.assertNotIn("taskboard-super-secret", repr(config))
        self.assertNotIn("taskboard-super-secret", str(config))
        self.assertNotIn("vault-super-secret", rendered)
        self.assertNotIn("taskboard-super-secret", rendered)
        self.assertIn("source=vault", rendered)


if __name__ == "__main__":
    unittest.main()
