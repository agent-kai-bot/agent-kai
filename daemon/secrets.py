"""Webhook secret resolution for daemon ingress routes.

This module abstracts over the source of the shared HMAC secrets used by
the daemon's signed webhook routes. Taskboard and Forgejo each use their
own environment variable and Vault path, but share the same provider
interface and Vault client implementation.

Example:
    Build the production providers from environment configuration::

        from daemon.secrets import (
            default_forgejo_webhook_secret_provider,
            default_taskboard_webhook_secret_provider,
        )

        taskboard_secret = default_taskboard_webhook_secret_provider().get_secret()
        forgejo_secret = default_forgejo_webhook_secret_provider().get_secret()
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Protocol

LOGGER = logging.getLogger(__name__)

ENV_TASKBOARD_WEBHOOK_SECRET = "KAI_TASKBOARD_WEBHOOK_SECRET"
ENV_TASKBOARD_WEBHOOK_SECRET_VAULT_PATH = "KAI_TASKBOARD_WEBHOOK_SECRET_VAULT_PATH"
ENV_FORGEJO_WEBHOOK_SECRET = "KAI_FORGEJO_WEBHOOK_SECRET"
ENV_FORGEJO_WEBHOOK_SECRET_VAULT_PATH = "KAI_FORGEJO_WEBHOOK_SECRET_VAULT_PATH"
ENV_VAULT_ADDR = "VAULT_ADDR"
ENV_VAULT_TOKEN = "VAULT_TOKEN"

DEFAULT_TASKBOARD_WEBHOOK_VAULT_PATH = "kai/taskboard-webhook-secret"
DEFAULT_FORGEJO_WEBHOOK_VAULT_PATH = "kai/forgejo-webhook-secret"
DEFAULT_VAULT_KEY = "secret"
FALLBACK_VAULT_KEYS = ("secret", "password", "value")

# Backwards-compatible aliases for the original taskboard provider names.
ENV_DIRECT_SECRET = ENV_TASKBOARD_WEBHOOK_SECRET
ENV_VAULT_PATH = ENV_TASKBOARD_WEBHOOK_SECRET_VAULT_PATH
DEFAULT_VAULT_PATH = DEFAULT_TASKBOARD_WEBHOOK_VAULT_PATH


class WebhookSecretProvider(Protocol):
    """Protocol for resolving a webhook HMAC secret.

    Example:
        Provide a test double by implementing ``get_secret``::

            class TestProvider:
                def get_secret(self) -> bytes:
                    return b"shared-secret"
    """

    def get_secret(self) -> bytes:
        """Return the current HMAC secret as raw bytes.

        Raises:
            WebhookSecretError: If the secret cannot be fetched.
        """


class WebhookSecretError(RuntimeError):
    """Raised when a webhook HMAC secret cannot be loaded."""


class StaticWebhookSecretProvider:
    """Provider that returns a pre-loaded secret value.

    Args:
        secret: Raw secret bytes to return for each lookup.

    Raises:
        WebhookSecretError: If ``secret`` is empty.

    Example:
        Use a deterministic secret in tests::

            provider = StaticWebhookSecretProvider(b"unit-test-secret")
    """

    def __init__(self, secret: bytes) -> None:
        if not secret:
            raise WebhookSecretError("static webhook secret must be non-empty")
        self._secret = secret

    def get_secret(self) -> bytes:
        """Return the configured static secret.

        Returns:
            Raw HMAC secret bytes.
        """

        return self._secret


class EnvWebhookSecretProvider:
    """Provider that reads the secret from a process environment variable.

    Args:
        env_var: Name of the environment variable holding the secret.
            Defaults to the taskboard direct-secret variable for
            compatibility with the original provider.
    """

    def __init__(self, env_var: str = ENV_TASKBOARD_WEBHOOK_SECRET) -> None:
        self._env_var = env_var

    def get_secret(self) -> bytes:
        """Read and encode the environment secret.

        Returns:
            UTF-8 encoded HMAC secret bytes.

        Raises:
            WebhookSecretError: If the environment variable is unset.
        """

        value = os.environ.get(self._env_var, "").strip()
        if not value:
            raise WebhookSecretError(
                f"environment variable {self._env_var} is not set"
            )
        return value.encode("utf-8")


class VaultWebhookSecretProvider:
    """Provider that fetches a webhook secret from HashiCorp Vault.

    The first successful fetch is cached in memory for the lifetime of
    the daemon process. The provider does not log secret material.

    Args:
        vault_addr: Base URL of the Vault server, for example
            ``https://vault.example.com``.
        vault_token: Vault token with read access to ``path``.
        path: KV v2 path holding the webhook secret. Short paths such as
            ``kai/taskboard-webhook-secret`` are expanded to
            ``kai/data/taskboard-webhook-secret``.
        key: Preferred JSON key inside the Vault payload.
        timeout_seconds: HTTP timeout for the Vault read.

    Raises:
        WebhookSecretError: If required configuration values are empty.
    """

    def __init__(
        self,
        vault_addr: str,
        vault_token: str,
        path: str = DEFAULT_TASKBOARD_WEBHOOK_VAULT_PATH,
        *,
        key: str = DEFAULT_VAULT_KEY,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not vault_addr:
            raise WebhookSecretError("vault_addr is required")
        if not vault_token:
            raise WebhookSecretError("vault_token is required")
        if not path:
            raise WebhookSecretError("vault path is required")
        self._vault_addr = vault_addr.rstrip("/")
        self._vault_token = vault_token
        self._path = path
        self._key = key
        self._timeout = timeout_seconds
        self._cached: bytes | None = None

    def _build_url(self) -> str:
        normalized = self._path.lstrip("/")
        if normalized.startswith("v1/"):
            return f"{self._vault_addr}/{normalized}"
        if "/data/" in normalized:
            kv_path = normalized
        else:
            mount, _, rest = normalized.partition("/")
            if not rest:
                raise WebhookSecretError("vault path must include a key suffix")
            kv_path = f"{mount}/data/{rest}"
        return f"{self._vault_addr}/v1/{kv_path}"

    def get_secret(self) -> bytes:
        """Fetch and cache the Vault-backed secret.

        Returns:
            Raw HMAC secret bytes.

        Raises:
            WebhookSecretError: If Vault cannot be reached or the
                response does not contain a supported secret key.
        """

        if self._cached is not None:
            return self._cached
        url = self._build_url()
        req = urllib.request.Request(  # noqa: S310 - URL is operator-controlled
            url,
            headers={"X-Vault-Token": self._vault_token},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raise WebhookSecretError(
                f"vault read failed with status {exc.code}"
            ) from None
        except urllib.error.URLError as exc:
            raise WebhookSecretError(
                f"vault read failed: {exc.reason}"
            ) from None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebhookSecretError("vault response was not valid JSON") from exc
        secret = _extract_secret_value(payload, preferred_key=self._key)
        self._cached = secret
        return secret


def _candidate_secret_maps(payload: dict[str, Any]) -> list[dict[str, Any]]:
    nested_data = payload.get("data")
    candidates: list[dict[str, Any]] = []
    if isinstance(nested_data, dict):
        kv2_data = nested_data.get("data")
        if isinstance(kv2_data, dict):
            candidates.append(kv2_data)
        candidates.append(nested_data)
    candidates.append(payload)
    return candidates


def _extract_secret_value(payload: dict[str, Any], *, preferred_key: str) -> bytes:
    keys = tuple(dict.fromkeys((preferred_key, *FALLBACK_VAULT_KEYS)))
    for candidate in _candidate_secret_maps(payload):
        for key in keys:
            value = candidate.get(key)
            if value:
                return str(value).encode("utf-8")
    raise WebhookSecretError("vault payload missing webhook secret value")


def _default_provider(
    *,
    env: dict[str, str] | None,
    direct_env_var: str,
    vault_path_env_var: str,
    default_vault_path: str,
    route_name: str,
) -> WebhookSecretProvider:
    source = env if env is not None else os.environ
    direct = source.get(direct_env_var, "").strip()
    if direct:
        if env is None:
            return EnvWebhookSecretProvider(direct_env_var)
        return StaticWebhookSecretProvider(direct.encode("utf-8"))

    vault_addr = source.get(ENV_VAULT_ADDR, "").strip()
    vault_token = source.get(ENV_VAULT_TOKEN, "").strip()
    if vault_addr and vault_token:
        path = source.get(vault_path_env_var, "").strip() or default_vault_path
        return VaultWebhookSecretProvider(
            vault_addr=vault_addr,
            vault_token=vault_token,
            path=path,
        )

    raise WebhookSecretError(
        f"no {route_name} webhook secret source configured: set "
        f"{direct_env_var} or {ENV_VAULT_ADDR}/{ENV_VAULT_TOKEN}"
    )


def default_taskboard_webhook_secret_provider(
    env: dict[str, str] | None = None,
) -> WebhookSecretProvider:
    """Build the default taskboard webhook secret provider.

    Args:
        env: Optional environment mapping. Defaults to ``os.environ``.

    Returns:
        A provider backed by ``KAI_TASKBOARD_WEBHOOK_SECRET`` when set,
        or by Vault path ``kai/taskboard-webhook-secret`` when
        ``VAULT_ADDR`` and ``VAULT_TOKEN`` are configured.

    Raises:
        WebhookSecretError: If no supported secret source is configured.
    """

    return _default_provider(
        env=env,
        direct_env_var=ENV_TASKBOARD_WEBHOOK_SECRET,
        vault_path_env_var=ENV_TASKBOARD_WEBHOOK_SECRET_VAULT_PATH,
        default_vault_path=DEFAULT_TASKBOARD_WEBHOOK_VAULT_PATH,
        route_name="taskboard",
    )


def default_forgejo_webhook_secret_provider(
    env: dict[str, str] | None = None,
) -> WebhookSecretProvider:
    """Build the default Forgejo webhook secret provider.

    Args:
        env: Optional environment mapping. Defaults to ``os.environ``.

    Returns:
        A provider backed by ``KAI_FORGEJO_WEBHOOK_SECRET`` when set, or
        by Vault path ``kai/forgejo-webhook-secret`` when ``VAULT_ADDR``
        and ``VAULT_TOKEN`` are configured.

    Raises:
        WebhookSecretError: If no supported secret source is configured.
    """

    return _default_provider(
        env=env,
        direct_env_var=ENV_FORGEJO_WEBHOOK_SECRET,
        vault_path_env_var=ENV_FORGEJO_WEBHOOK_SECRET_VAULT_PATH,
        default_vault_path=DEFAULT_FORGEJO_WEBHOOK_VAULT_PATH,
        route_name="forgejo",
    )


def default_webhook_secret_provider(
    env: dict[str, str] | None = None,
) -> WebhookSecretProvider:
    """Build the default taskboard webhook secret provider.

    This compatibility wrapper preserves the original taskboard factory
    name while callers migrate to
    :func:`default_taskboard_webhook_secret_provider`.
    """

    return default_taskboard_webhook_secret_provider(env=env)
