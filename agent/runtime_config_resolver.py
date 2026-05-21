"""Vault-backed runtime config resolution for taskboard-spawned agents."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

import requests

LOGGER = logging.getLogger(__name__)
REDACTED = "[REDACTED]"
DEFAULT_TTL_SECONDS = 60.0
DEFAULT_VAULT_ADDRS = ("http://localhost:8484", "http://host.docker.internal:8484")
DEFAULT_TOKEN_FILE = Path("/home/ubuntu/.openclaw/vault/.tokens")
TOKEN_FILE_KEYS = ("bootstrap_master", "bootstrap-master", "kai-main", "kai_main")
ALLOW_MISSING_PAT_ROLES = frozenset({"orchestrator"})
DEFAULT_ROLE_VAULT_PATHS = {
    "developer": "forgejo/agent-developer",
    "code-reviewer": "forgejo/agent-code-reviewer",
    "security-auditor": "forgejo/agent-security-auditor",
    "qa-agent": "forgejo/agent-qa",
    "orchestrator": "forgejo/agent-orchestrator",
}
DEFAULT_TASKBOARD_ROLE_VAULT_PATHS = {
    "code-reviewer": "taskboard/agent-code-reviewer",
    "security-auditor": "taskboard/agent-security-auditor",
    "qa-agent": "taskboard/agent-qa",
}


class RuntimeConfigError(RuntimeError):
    """Raised when runtime config cannot be resolved for a role."""


class VaultReadError(RuntimeError):
    """Raised when a Vault read fails or misses."""


class VaultClient(Protocol):
    def read(self, path: str) -> dict[str, Any]:
        """Read one secret dictionary from Vault."""


@dataclass(frozen=True)
class RoleRuntimeConfig:
    """Resolved per-role runtime configuration."""

    role: str
    forgejo_pat: str = field(default="", repr=False)
    forgejo_user: str = ""
    forgejo_base_url: str = ""
    taskboard_base_url: str = ""
    taskboard_bearer_token: str = field(default="", repr=False)
    taskboard_mint_bearer_token: str = field(default="", repr=False)
    taskboard_session_token: str = field(default="", repr=False)
    taskboard_session_generation: int | None = None
    taskboard_agent_name: str = ""
    source: str = ""
    vault_path: str | None = None
    taskboard_vault_path: str | None = None

    def __repr__(self) -> str:
        return (
            "RoleRuntimeConfig("
            f"role={self.role!r}, forgejo_user={self.forgejo_user!r}, "
            f"forgejo_pat={'set' if self.forgejo_pat else 'missing'}, "
            "taskboard_bearer_token="
            f"{'set' if self.taskboard_bearer_token else 'missing'}, "
            "taskboard_mint_bearer_token="
            f"{'set' if self.taskboard_mint_bearer_token else 'missing'}, "
            "taskboard_session_token="
            f"{'set' if self.taskboard_session_token else 'missing'}, "
            f"taskboard_session_generation={self.taskboard_session_generation!r}, "
            f"source={self.source!r}, vault_path={self.vault_path!r}, "
            f"taskboard_vault_path={self.taskboard_vault_path!r})"
        )

    def with_taskboard_session(
        self,
        *,
        session_token: str = "",
        session_generation: int | None = None,
        agent_name: str = "",
    ) -> RoleRuntimeConfig:
        return replace(
            self,
            taskboard_session_token=str(session_token or "").strip(),
            taskboard_session_generation=session_generation,
            taskboard_agent_name=str(
                agent_name or self.taskboard_agent_name or ""
            ).strip(),
        )

    def env_overlay(self) -> dict[str, str]:
        overlay: dict[str, str] = {"KAI_AGENT_ROLE": self.role}
        suffix = role_env_suffix(self.role)
        if self.forgejo_pat:
            overlay.update(
                {
                    f"FORGEJO_TOKEN_{suffix}": self.forgejo_pat,
                    "FORGEJO_TOKEN": self.forgejo_pat,
                    "GITEA_TOKEN": self.forgejo_pat,
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "credential.helper",
                    "GIT_CONFIG_VALUE_0": _git_credential_helper(),
                }
            )
        if self.forgejo_user:
            overlay[f"FORGEJO_USER_{suffix}"] = self.forgejo_user
            overlay["FORGEJO_USER"] = self.forgejo_user
        for key, value in (
            ("FORGEJO_URL", self.forgejo_base_url),
            ("TASKBOARD_URL", self.taskboard_base_url),
            ("TASKBOARD_BEARER_TOKEN", self.taskboard_bearer_token),
            ("TASKBOARD_SESSION_TOKEN", self.taskboard_session_token),
            ("TASKBOARD_AGENT_NAME", self.taskboard_agent_name),
        ):
            if value:
                overlay[key] = str(value)
        if self.taskboard_session_generation is not None:
            overlay["TASKBOARD_SESSION_GENERATION"] = str(
                int(self.taskboard_session_generation)
            )
        return overlay


@dataclass(frozen=True)
class _CacheEntry:
    config: RoleRuntimeConfig
    expires_at: float


class LocalVaultClient:
    """HTTP client for KAI's local runtime Vault."""

    def __init__(
        self,
        *,
        addrs: tuple[str, ...] = DEFAULT_VAULT_ADDRS,
        token: str | None = None,
        token_file: Path = DEFAULT_TOKEN_FILE,
        timeout_seconds: float = 2.0,
        session: Any | None = None,
    ) -> None:
        self.addrs = tuple(addr.rstrip("/") for addr in addrs if addr)
        self.token = token if token is not None else _resolve_vault_token(token_file)
        self.timeout_seconds = timeout_seconds
        self.session = session or requests

    def read(self, path: str) -> dict[str, Any]:
        if not self.token:
            raise VaultReadError("vault token is not configured")
        normalized_path = str(path or "").strip().lstrip("/")
        if not normalized_path:
            raise VaultReadError("vault path is empty")

        errors: list[str] = []
        for addr in self.addrs:
            for url in _vault_urls(addr, normalized_path):
                try:
                    response = self.session.get(
                        url,
                        headers={"X-Vault-Token": self.token},
                        timeout=self.timeout_seconds,
                    )
                except requests.RequestException as exc:
                    errors.append(f"{addr}: {exc.__class__.__name__}")
                    continue
                if response.status_code == 404:
                    errors.append(f"{addr}: 404")
                    continue
                if not 200 <= response.status_code < 300:
                    errors.append(f"{addr}: status={response.status_code}")
                    continue
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise VaultReadError("vault response was not valid JSON") from exc
                secret = _extract_secret_map(payload)
                if secret:
                    return secret
                raise VaultReadError("vault payload did not contain a secret map")

        detail = "; ".join(errors) if errors else "no vault addresses configured"
        raise VaultReadError(f"vault read failed for path={normalized_path}: {detail}")


class RuntimeConfigResolver:
    """Resolve per-role runtime config with Vault preferred and env fallback."""

    def __init__(
        self,
        *,
        vault_client: VaultClient | None = None,
        role_vault_paths: Mapping[str, str] | None = None,
        taskboard_role_vault_paths: Mapping[str, str] | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.vault_client = vault_client or LocalVaultClient()
        self.role_vault_paths = {
            normalize_role_key(role): str(path)
            for role, path in dict(role_vault_paths or DEFAULT_ROLE_VAULT_PATHS).items()
        }
        self.taskboard_role_vault_paths = {
            normalize_role_key(role): str(path)
            for role, path in dict(
                taskboard_role_vault_paths or DEFAULT_TASKBOARD_ROLE_VAULT_PATHS
            ).items()
        }
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.env = env if env is not None else os.environ
        self.clock = clock or time.monotonic
        self.log = logger or LOGGER
        self._cache: dict[str, _CacheEntry] = {}

    def startup_diagnostics(self) -> dict[str, Any]:
        return {
            "cache_ttl_seconds": int(self.ttl_seconds),
            "vault_paths": dict(sorted(self.role_vault_paths.items())),
            "taskboard_vault_paths": dict(
                sorted(self.taskboard_role_vault_paths.items())
            ),
            "vault_client": self.vault_client.__class__.__name__,
            "env_fallbacks_present": _env_fallback_presence(self.env),
        }

    def log_startup_diagnostics(self) -> None:
        self.log.info(
            "runtime_config_resolver_startup diagnostics=%s",
            self.startup_diagnostics(),
        )

    def clear_cache(self) -> None:
        self._cache.clear()

    def resolve_for_role(
        self,
        role: str,
        *,
        allow_missing_forgejo_pat: bool | None = None,
    ) -> RoleRuntimeConfig:
        role_key = normalize_role_key(role)
        if not role_key:
            raise RuntimeConfigError("role is required for runtime config resolution")

        now = self.clock()
        cached = self._cache.get(role_key)
        if cached is not None and now < cached.expires_at:
            self.log.info(
                "runtime_config_resolver role=%s source=cache hit=true",
                role_key,
            )
            return cached.config

        config = self._resolve_uncached(role_key)
        allow_missing = (
            role_key in ALLOW_MISSING_PAT_ROLES
            if allow_missing_forgejo_pat is None
            else bool(allow_missing_forgejo_pat)
        )
        if not config.forgejo_pat and not allow_missing:
            suffix = role_env_suffix(role_key)
            raise RuntimeConfigError(
                "unable to resolve Forgejo PAT for role "
                f"{role_key}: checked Vault path {config.vault_path or '<none>'}, "
                f"FORGEJO_TOKEN_{suffix}, FORGEJO_TOKEN, and GITEA_TOKEN"
            )

        self._cache[role_key] = _CacheEntry(
            config=config,
            expires_at=now + self.ttl_seconds,
        )
        return config

    def _resolve_uncached(self, role_key: str) -> RoleRuntimeConfig:
        vault_path = self.role_vault_paths.get(role_key)
        taskboard_vault_path = self.taskboard_role_vault_paths.get(role_key)
        if vault_path:
            secret = self._read_vault(role_key, vault_path)
            pat = _first_nonempty(secret, "password", "token", "pat")
            if pat:
                self.log.info(
                    "runtime_config_resolver role=%s source=vault hit=true path=%s",
                    role_key,
                    vault_path,
                )
                return self._config_from_parts(
                    role_key,
                    forgejo_pat=pat,
                    forgejo_user=_first_nonempty(
                        secret,
                        "username",
                        "user",
                        "login",
                        "name",
                    ),
                    taskboard_bearer_token=self._resolve_taskboard_bearer_token(
                        role_key,
                        taskboard_vault_path,
                    ),
                    source="vault",
                    vault_path=vault_path,
                    taskboard_vault_path=taskboard_vault_path,
                )
            self.log.info(
                "runtime_config_resolver role=%s source=vault hit=false "
                "path=%s error=missing_password_key",
                role_key,
                vault_path,
            )

        config = self._config_from_env(
            role_key,
            vault_path=vault_path,
            taskboard_vault_path=taskboard_vault_path,
            taskboard_bearer_token=self._resolve_taskboard_bearer_token(
                role_key,
                taskboard_vault_path,
            ),
        )
        if config.forgejo_pat:
            self.log.info(
                "runtime_config_resolver role=%s source=%s hit=true",
                role_key,
                config.source,
            )
        return config

    def _read_vault(self, role_key: str, vault_path: str) -> dict[str, Any]:
        try:
            return self.vault_client.read(vault_path)
        except Exception as exc:  # noqa: BLE001
            self.log.info(
                "runtime_config_resolver role=%s source=vault hit=false "
                "path=%s error=%s",
                role_key,
                vault_path,
                redact_known_runtime_secrets(
                    f"{exc.__class__.__name__}: {exc}",
                    env=self.env,
                ),
            )
            return {}

    def _resolve_taskboard_bearer_token(
        self,
        role_key: str,
        vault_path: str | None,
    ) -> str:
        if not vault_path:
            return ""
        secret = self._read_vault(role_key, vault_path)
        token = _first_nonempty(
            secret,
            "taskboard_bearer_token",
            "taskboard_token",
            "TASKBOARD_BEARER_TOKEN",
            "bearer_token",
            "bearer",
            "token",
            "password",
            "pat",
        )
        if token:
            self.log.info(
                "runtime_config_resolver role=%s source=taskboard_vault hit=true path=%s",
                role_key,
                vault_path,
            )
        return token

    def _config_from_env(
        self,
        role_key: str,
        *,
        vault_path: str | None,
        taskboard_vault_path: str | None,
        taskboard_bearer_token: str = "",
    ) -> RoleRuntimeConfig:
        suffix = role_env_suffix(role_key)
        role_token = str(self.env.get(f"FORGEJO_TOKEN_{suffix}", "")).strip()
        global_token = (
            str(self.env.get("FORGEJO_TOKEN", "")).strip()
            or str(self.env.get("GITEA_TOKEN", "")).strip()
        )
        role_taskboard_token = (
            str(self.env.get(f"TASKBOARD_BEARER_TOKEN_{suffix}", "")).strip()
            or str(self.env.get(f"TASKBOARD_TOKEN_{suffix}", "")).strip()
        )
        if not role_taskboard_token and role_key == "qa-agent":
            role_taskboard_token = (
                str(self.env.get("TASKBOARD_BEARER_TOKEN_QA", "")).strip()
                or str(self.env.get("TASKBOARD_TOKEN_QA", "")).strip()
            )
        source = (
            "env_role" if role_token else ("env_global" if global_token else "missing")
        )
        return self._config_from_parts(
            role_key,
            forgejo_pat=role_token or global_token,
            forgejo_user=(
                str(self.env.get(f"FORGEJO_USER_{suffix}", "")).strip()
                or str(self.env.get("FORGEJO_USER", "")).strip()
                or str(self.env.get("GITEA_USER", "")).strip()
            ),
            taskboard_bearer_token=taskboard_bearer_token or role_taskboard_token,
            source=source,
            vault_path=vault_path,
            taskboard_vault_path=taskboard_vault_path,
        )

    def _config_from_parts(
        self,
        role_key: str,
        *,
        forgejo_pat: str,
        forgejo_user: str = "",
        taskboard_bearer_token: str = "",
        source: str,
        vault_path: str | None,
        taskboard_vault_path: str | None,
    ) -> RoleRuntimeConfig:
        taskboard_mint_bearer_token = self._global_taskboard_bearer_token()
        return RoleRuntimeConfig(
            role=role_key,
            forgejo_pat=str(forgejo_pat or "").strip(),
            forgejo_user=str(forgejo_user or "").strip(),
            forgejo_base_url=(
                str(self.env.get("FORGEJO_URL", "")).strip()
                or str(self.env.get("GITEA_URL", "")).strip()
            ),
            taskboard_base_url=str(
                self.env.get("TASKBOARD_URL", "http://localhost:8080")
            ).strip(),
            taskboard_bearer_token=str(taskboard_bearer_token or "").strip(),
            taskboard_mint_bearer_token=taskboard_mint_bearer_token,
            taskboard_agent_name=str(self.env.get("TASKBOARD_AGENT_NAME", "")).strip(),
            source=source,
            vault_path=vault_path,
            taskboard_vault_path=taskboard_vault_path,
        )

    def _global_taskboard_bearer_token(self) -> str:
        return (
            str(self.env.get("TASKBOARD_BEARER_TOKEN", "")).strip()
            or str(self.env.get("OPENCLAW_GATEWAY_TOKEN", "")).strip()
            or str(self.env.get("OPENCLAW_TOKEN", "")).strip()
        )


def normalize_role_key(role: str | None) -> str:
    text = re.sub(r"[-_]+", " ", str(role or "").strip().lower())
    text = re.sub(r"\s+", " ", text)
    aliases = {
        "code reviewer": "code-reviewer",
        "security auditor": "security-auditor",
        "qa agent": "qa-agent",
    }
    return aliases.get(text, text.replace(" ", "-"))


def role_env_suffix(role: str | None) -> str:
    return re.sub(
        r"[^A-Z0-9]+",
        "_",
        normalize_role_key(role).upper(),
    ).strip("_")


def redact_known_runtime_secrets(
    text: str,
    *,
    env: Mapping[str, str] | None = None,
    configs: list[RoleRuntimeConfig] | None = None,
) -> str:
    source = env if env is not None else os.environ
    secrets: list[str] = []
    for key, value in source.items():
        key_text = str(key)
        if (
            key_text in {"FORGEJO_TOKEN", "GITEA_TOKEN"}
            or key_text.startswith("FORGEJO_TOKEN_")
            or key_text in {"TASKBOARD_BEARER_TOKEN", "TASKBOARD_SESSION_TOKEN"}
            or key_text.startswith("TASKBOARD_BEARER_TOKEN_")
            or key_text.startswith("TASKBOARD_TOKEN_")
        ) and value:
            secrets.append(str(value))
    for config in configs or []:
        secrets.extend(
            [
                config.forgejo_pat,
                config.taskboard_bearer_token,
                config.taskboard_mint_bearer_token,
                config.taskboard_session_token,
            ]
        )

    redacted = str(text)
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        redacted = redacted.replace(secret, REDACTED)
    return redacted


def _git_credential_helper() -> str:
    return (
        "!f() { "
        "test \"$1\" = get || exit 0; "
        "printf 'username=%s\\n' \"${FORGEJO_USER:-kai-agent}\"; "
        "printf 'password=%s\\n' \"${FORGEJO_TOKEN:-$GITEA_TOKEN}\"; "
        "}; f"
    )


def _resolve_vault_token(token_file: Path) -> str:
    return (
        os.getenv("VAULT_TOKEN", "").strip()
        or os.getenv("KAI_VAULT_TOKEN", "").strip()
        or _read_token_file(token_file)
    )


def _read_token_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text:
        return ""

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, Mapping):
        token = _token_from_mapping(parsed)
        if token:
            return token

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=", ":", " "):
            if sep in line:
                key, _, value = line.partition(sep)
                if key.strip() in TOKEN_FILE_KEYS and value.strip():
                    return value.strip().strip('"').strip("'")
        if len(line.split()) == 1:
            return line
    return ""


def _token_from_mapping(mapping: Mapping[str, Any]) -> str:
    for key in TOKEN_FILE_KEYS:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = mapping.get("tokens")
    return _token_from_mapping(nested) if isinstance(nested, Mapping) else ""


def _vault_urls(addr: str, path: str) -> tuple[str, ...]:
    direct = f"{addr}/v1/{path}"
    if path.startswith("v1/"):
        return (f"{addr}/{path}",)
    if "/data/" in path:
        return (direct,)
    mount, _, rest = path.partition("/")
    return (direct, f"{addr}/v1/{mount}/data/{rest}") if mount and rest else (direct,)


def _extract_secret_map(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("data")
        if isinstance(nested, dict):
            return dict(nested)
        return dict(data)
    return dict(payload)


def _first_nonempty(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _env_fallback_presence(env: Mapping[str, str]) -> dict[str, bool]:
    return {
        "FORGEJO_TOKEN": bool(str(env.get("FORGEJO_TOKEN", "")).strip()),
        "GITEA_TOKEN": bool(str(env.get("GITEA_TOKEN", "")).strip()),
        "TASKBOARD_BEARER_TOKEN": bool(
            str(env.get("TASKBOARD_BEARER_TOKEN", "")).strip()
        ),
        "role_taskboard_tokens": any(
            (
                str(key).startswith("TASKBOARD_BEARER_TOKEN_")
                or str(key).startswith("TASKBOARD_TOKEN_")
            )
            and bool(str(value).strip())
            for key, value in env.items()
        ),
        "role_forgejo_tokens": any(
            str(key).startswith("FORGEJO_TOKEN_") and bool(str(value).strip())
            for key, value in env.items()
        ),
    }
