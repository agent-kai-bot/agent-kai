"""Webhook secret resolution for the daemon.

This module abstracts over the source of the shared HMAC secret used by
:mod:`daemon.webhook_auth`. Production deployments fetch the secret from
HashiCorp Vault using ``VAULT_ADDR`` and ``VAULT_TOKEN``; local tests can
inject the secret directly through an environment variable or by passing
a custom provider.

The provider interface is intentionally narrow so tests can supply a
fake without bringing in a Vault dependency. The default factory fails
closed when neither an environment override nor a Vault address are
configured.

Example:
    Build the production provider from environment configuration::

        from daemon.secrets import default_webhook_secret_provider

        provider = default_webhook_secret_provider()
        secret = provider.get_secret()
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Protocol

LOGGER = logging.getLogger(__name__)

ENV_DIRECT_SECRET = "KAI_TASKBOARD_WEBHOOK_SECRET"
ENV_VAULT_PATH = "KAI_TASKBOARD_WEBHOOK_SECRET_VAULT_PATH"
ENV_VAULT_ADDR = "VAULT_ADDR"
ENV_VAULT_TOKEN = "VAULT_TOKEN"

DEFAULT_VAULT_PATH = "kai/taskboard-webhook-secret"
DEFAULT_VAULT_KEY = "secret"


class WebhookSecretProvider(Protocol):
    """Resolve the shared HMAC secret for taskboard webhooks."""

    def get_secret(self) -> bytes:
        """Return the current HMAC secret as raw bytes.

        Raises:
            WebhookSecretError: If the secret cannot be fetched.
        """


class WebhookSecretError(RuntimeError):
    """Raised when the webhook HMAC secret cannot be loaded."""


class StaticWebhookSecretProvider:
    """Provider that returns a pre-loaded secret value.

    This provider is suitable for tests and for any environment that has
    already resolved the secret out of band.

    Args:
        secret: Raw secret bytes to return.
    """

    def __init__(self, secret: bytes) -> None:
        if not secret:
            raise WebhookSecretError("static webhook secret must be non-empty")
        self._secret = secret

    def get_secret(self) -> bytes:
        return self._secret


class EnvWebhookSecretProvider:
    """Provider that reads the secret from a process environment variable.

    Args:
        env_var: Name of the environment variable holding the secret.
            Defaults to :data:`ENV_DIRECT_SECRET`.
    """

    def __init__(self, env_var: str = ENV_DIRECT_SECRET) -> None:
        self._env_var = env_var

    def get_secret(self) -> bytes:
        value = os.environ.get(self._env_var, "").strip()
        if not value:
            raise WebhookSecretError(
                f"environment variable {self._env_var} is not set"
            )
        return value.encode("utf-8")


class VaultWebhookSecretProvider:
    """Provider that fetches the secret from HashiCorp Vault on demand.

    The first successful fetch is cached in memory for the lifetime of
    the daemon process. The provider does not log secret material and
    redacts the path on errors to avoid leaking deployment topology.

    Args:
        vault_addr: Base URL of the Vault server, for example
            ``https://vault.example.com``.
        vault_token: Vault token with read access to ``path``.
        path: KV v2 path holding the webhook secret. The provider
            converts ``foo/bar`` to ``foo/data/bar`` automatically when
            the path does not already include a ``/data/`` segment, so
            callers can use the canonical short form.
        key: JSON key inside the Vault secret payload that holds the
            HMAC bytes. Defaults to :data:`DEFAULT_VAULT_KEY`.
        timeout_seconds: HTTP timeout for the Vault read.
    """

    def __init__(
        self,
        vault_addr: str,
        vault_token: str,
        path: str = DEFAULT_VAULT_PATH,
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
        if "/data/" in normalized:
            kv_path = normalized
        else:
            mount, _, rest = normalized.partition("/")
            if not rest:
                raise WebhookSecretError("vault path must include a key suffix")
            kv_path = f"{mount}/data/{rest}"
        return f"{self._vault_addr}/v1/{kv_path}"

    def get_secret(self) -> bytes:
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
        nested = (payload.get("data") or {}).get("data") or {}
        value = nested.get(self._key)
        if not value:
            raise WebhookSecretError(
                f"vault payload missing required key '{self._key}'"
            )
        secret = str(value).encode("utf-8")
        self._cached = secret
        return secret


def default_webhook_secret_provider(
    env: dict[str, str] | None = None,
) -> WebhookSecretProvider:
    """Build the production webhook secret provider from environment vars.

    The lookup order is:

    1. If :data:`ENV_DIRECT_SECRET` is set, return an
       :class:`EnvWebhookSecretProvider`. This is intended for local
       development and CI; production deployments should not set it.
    2. If :data:`ENV_VAULT_ADDR` and :data:`ENV_VAULT_TOKEN` are set,
       return a :class:`VaultWebhookSecretProvider`. The Vault path
       defaults to :data:`DEFAULT_VAULT_PATH` but can be overridden via
       :data:`ENV_VAULT_PATH`.
    3. Otherwise, raise :class:`WebhookSecretError`.

    Args:
        env: Optional environment mapping. Defaults to ``os.environ``.

    Returns:
        A configured :class:`WebhookSecretProvider`.

    Raises:
        WebhookSecretError: When no provider can be constructed.
    """

    source = env if env is not None else os.environ
    direct = source.get(ENV_DIRECT_SECRET, "").strip()
    if direct:
        return EnvWebhookSecretProvider()
    vault_addr = source.get(ENV_VAULT_ADDR, "").strip()
    vault_token = source.get(ENV_VAULT_TOKEN, "").strip()
    if vault_addr and vault_token:
        path = source.get(ENV_VAULT_PATH, "").strip() or DEFAULT_VAULT_PATH
        return VaultWebhookSecretProvider(
            vault_addr=vault_addr,
            vault_token=vault_token,
            path=path,
        )
    raise WebhookSecretError(
        "no webhook secret source configured: set "
        f"{ENV_DIRECT_SECRET} or {ENV_VAULT_ADDR}/{ENV_VAULT_TOKEN}"
    )
