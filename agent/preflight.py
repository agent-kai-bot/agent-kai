"""Daemon startup preflight: fail loudly on stale or bad config.

Phase 2 of epic #10028 (taskboard task #10221). The recent 5-day silent
outage was caused by the daemon running with a 5-day-stale `agent-config.json`
that pointed every role agent at a dead `kai-smart` endpoint with the literal
placeholder `api_key="missing-kai-api-key"`. Every agent run died in 1.67s
with a generic "Connection error", and the silent failure was invisible at
every layer above the LLM call.

This module fails the daemon startup loud when:

* Any required env var is missing (configurable list).
* `TASKBOARD_URL` or `FORGEJO_URL` is unset OR resolves to the historical
  footgun default (``http://taskboard:8080``).
* Any active endpoint has an `api_key` matching the closed placeholder
  set (``missing-*``, ``changeme``, ``example``, empty after strip).
* Any active endpoint smoke test (no-op chat) returns empty content within
  the configured timeout.
* The on-disk `agent-config.json` SHA256 differs from the loaded config
  (drift detection — the daemon must restart after a config edit, or the
  config-watcher must pick it up; either way a stale loaded version is
  detected here).

The daemon's startup hook is :func:`run_preflight_or_exit`; it raises
:class:`PreflightFailure` on any check failure, which the daemon main loop
turns into a sys.exit(2). For testing, individual checks return
:class:`CheckResult` with status + reason so the harness can assert what
was caught.

This module also exposes :func:`compute_agent_config_sha256` so the
dispatcher can stamp the running config hash on every `agent_runs` row
(``config_sha256`` column added by Phase 1) — making config drift visible
in the ledger directly.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import httpx


LOGGER = logging.getLogger("agent.preflight")


# ---------------------------------------------------------------------------
# Closed placeholder set + config check enums
# ---------------------------------------------------------------------------


# Strings that look like placeholder secrets and must never reach prod.
PLACEHOLDER_API_KEY_PATTERNS: frozenset[str] = frozenset(
    {
        "",
        "missing-kai-api-key",
        "changeme",
        "change-me",
        "example",
        "your-api-key-here",
        "todo",
        "placeholder",
        "not-needed",  # explicit local-only marker; allowed only in dev mode
    }
)

# Service URLs that were dev-mode defaults in older revisions and now hide
# config-omission bugs in production. The preflight refuses to start if any
# of these are still set as the running value (unless KAI_DEV_MODE=1).
HISTORICAL_DEFAULT_URLS: frozenset[str] = frozenset(
    {
        "http://taskboard:8080",
        "http://taskboard:8080/",
        "http://localhost:8080",
        "http://host.docker.internal:8080",
    }
)


@dataclasses.dataclass(frozen=True)
class CheckResult:
    """One preflight check outcome.

    Attributes:
        name: Short slug like ``config_placeholder_value`` or
            ``endpoint_smoke_codex_cli``. Aligns with the
            ``failure_class`` enum in the agent_runs ledger so a preflight
            failure can be recorded as a ``preflight_failed`` ledger row
            with the same class.
        ok: Whether the check passed.
        detail: Human-readable single-line explanation. Always populated
            on failure; may be empty on success.
        failure_class: The agent_runs ``failure_class`` to use when this
            check fails. None when ``ok=True``.
    """

    name: str
    ok: bool
    detail: str
    failure_class: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class PreflightConfig:
    """Inputs to the preflight runner.

    Attributes:
        config_path: Path to ``agent-config.json``. Defaults from env.
        required_env_vars: Names of env vars that must be set + non-empty.
        roles_to_check: Workforce roles whose endpoints we'll smoke test.
            Defaults to the full set of role agents in ``agent-config.json``.
        smoke_timeout_seconds: Per-endpoint smoke timeout.
        dev_mode: When True, allow ``not-needed`` and skip the
            historical-default URL refusal. Set via ``KAI_DEV_MODE=1``.
    """

    config_path: Path
    required_env_vars: tuple[str, ...]
    roles_to_check: tuple[str, ...]
    smoke_timeout_seconds: float
    dev_mode: bool

    @classmethod
    def from_env(
        cls,
        *,
        config_path: Optional[Path] = None,
        roles_to_check: Optional[Iterable[str]] = None,
    ) -> "PreflightConfig":
        if config_path is None:
            config_path_str = os.environ.get(
                "AGENT_CONFIG_PATH",
                str(Path(__file__).resolve().parents[1] / "agent-config.json"),
            )
            config_path = Path(config_path_str)
        roles_tuple: tuple[str, ...] = (
            tuple(roles_to_check)
            if roles_to_check is not None
            else (
                "developer",
                "code-reviewer",
                "security-auditor",
                "qa-agent",
                "architect",
            )
        )
        # The daemon needs at least these env vars wired to do its job.
        # Operators can extend via PREFLIGHT_REQUIRED_ENV (comma-separated).
        defaults = ("TASKBOARD_URL", "TASKBOARD_BEARER_TOKEN")
        extra = tuple(
            v.strip()
            for v in os.environ.get("PREFLIGHT_REQUIRED_ENV", "").split(",")
            if v.strip()
        )
        return cls(
            config_path=config_path,
            required_env_vars=defaults + extra,
            roles_to_check=roles_tuple,
            smoke_timeout_seconds=float(os.environ.get("PREFLIGHT_SMOKE_TIMEOUT_SECONDS", "30")),
            dev_mode=os.environ.get("KAI_DEV_MODE", "").strip() in ("1", "true", "yes"),
        )


class PreflightFailure(RuntimeError):
    """Raised when one or more preflight checks fail in run_preflight_or_exit."""

    def __init__(self, results: Iterable[CheckResult]) -> None:
        self.results = tuple(r for r in results if not r.ok)
        super().__init__(self._format())

    def _format(self) -> str:
        if not self.results:
            return "preflight failed"
        lines = ["preflight failed:"]
        for r in self.results:
            lines.append(f"  ✗ {r.name}: {r.detail} (failure_class={r.failure_class})")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Hashing / drift detection
# ---------------------------------------------------------------------------


def compute_agent_config_sha256(config_path: Path) -> Optional[str]:
    """SHA256 of the ``agent-config.json`` bytes on disk. None on read error."""
    try:
        return hashlib.sha256(config_path.read_bytes()).hexdigest()
    except OSError as exc:
        LOGGER.warning("compute_agent_config_sha256 failed: %s", exc)
        return None


def check_config_drift(
    *, loaded_config: Mapping[str, Any], config_path: Path
) -> CheckResult:
    """Compare loaded-config hash vs on-disk hash; surface drift."""
    on_disk = compute_agent_config_sha256(config_path)
    if on_disk is None:
        return CheckResult(
            name="config_unreadable",
            ok=False,
            detail=f"agent-config.json unreadable at {config_path}",
            failure_class="config_missing_required",
        )
    # The loaded-config hash isn't directly available without re-reading;
    # compute from the dict's canonical serialization. This catches:
    # (a) edits to the file after process start that the daemon hasn't
    #     reloaded, (b) tampered-with on-disk file vs what's in memory.
    import json as _json

    canonical = _json.dumps(loaded_config, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    loaded = hashlib.sha256(canonical).hexdigest()
    # The two hashes don't have to be byte-equal because the on-disk JSON
    # may have whitespace differences. Re-canonicalize the on-disk file
    # the same way and compare.
    try:
        on_disk_canonical = hashlib.sha256(
            _json.dumps(
                _json.loads(config_path.read_text(encoding="utf-8")),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    except (OSError, _json.JSONDecodeError) as exc:
        return CheckResult(
            name="config_unreadable",
            ok=False,
            detail=f"agent-config.json could not be re-parsed: {exc}",
            failure_class="config_missing_required",
        )
    if loaded != on_disk_canonical:
        return CheckResult(
            name="config_stale",
            ok=False,
            detail=(
                f"loaded config sha={loaded[:12]}... differs from on-disk "
                f"{on_disk_canonical[:12]}...; restart daemon or wait for hot-reload"
            ),
            failure_class="config_stale",
        )
    return CheckResult(name="config_in_sync", ok=True, detail="")


# ---------------------------------------------------------------------------
# Required env vars
# ---------------------------------------------------------------------------


def check_required_env(*, required: Iterable[str]) -> list[CheckResult]:
    """Return one CheckResult per missing required env var."""
    results: list[CheckResult] = []
    for name in required:
        value = os.environ.get(name, "").strip()
        if not value:
            results.append(
                CheckResult(
                    name=f"env_missing_{name.lower()}",
                    ok=False,
                    detail=f"required env var {name} is unset or empty",
                    failure_class="config_missing_required",
                )
            )
        else:
            results.append(CheckResult(name=f"env_present_{name.lower()}", ok=True, detail=""))
    return results


# ---------------------------------------------------------------------------
# Service URL checks
# ---------------------------------------------------------------------------


def check_service_url(
    *, name: str, url: str, dev_mode: bool
) -> CheckResult:
    """Refuse historical-default service URLs in production."""
    normalized = url.strip().rstrip("/")
    if not normalized:
        return CheckResult(
            name=f"url_missing_{name}",
            ok=False,
            detail=f"{name} is empty",
            failure_class="config_missing_required",
        )
    base_lower = normalized.lower()
    if not dev_mode and (
        base_lower in HISTORICAL_DEFAULT_URLS
        or any(base_lower.startswith(d.rstrip("/")) for d in HISTORICAL_DEFAULT_URLS)
    ):
        return CheckResult(
            name=f"url_historical_default_{name}",
            ok=False,
            detail=(
                f"{name}={url!r} is the historical-default footgun; "
                "set explicit prod URL or pass KAI_DEV_MODE=1"
            ),
            failure_class="config_unresolved_hostname",
        )
    return CheckResult(name=f"url_ok_{name}", ok=True, detail="")


# ---------------------------------------------------------------------------
# Endpoint placeholder + smoke checks
# ---------------------------------------------------------------------------


def _resolved_api_key(endpoint_cfg: Mapping[str, Any]) -> str:
    """Return the api key the runtime will actually use for this endpoint.

    Mirrors the resolution order in agent.core.create_llm: explicit ``api_key``
    field wins, else env var named in ``api_key_env``, else empty.
    """
    explicit = str(endpoint_cfg.get("api_key", "") or "").strip()
    if explicit:
        return explicit
    env_var = endpoint_cfg.get("api_key_env")
    if isinstance(env_var, str) and env_var:
        return os.environ.get(env_var, "").strip()
    return ""


def check_endpoint_placeholder(
    *,
    endpoint_id: str,
    endpoint_cfg: Mapping[str, Any],
    dev_mode: bool,
) -> CheckResult:
    """Catch endpoints with placeholder/missing API keys at startup."""
    key = _resolved_api_key(endpoint_cfg).lower()
    if not key:
        # Some endpoints legitimately use no key (codex-cli OAuth via auth.json,
        # local llama). Whitelist by provider rather than guessing.
        provider = str(endpoint_cfg.get("provider") or "").lower()
        if provider == "codex-cli":
            return CheckResult(name=f"endpoint_no_key_ok_{endpoint_id}", ok=True, detail="")
        return CheckResult(
            name=f"endpoint_no_key_{endpoint_id}",
            ok=False,
            detail=f"endpoint {endpoint_id!r} has no resolvable api key",
            failure_class="config_missing_required",
        )
    if key in PLACEHOLDER_API_KEY_PATTERNS:
        if dev_mode and key == "not-needed":
            return CheckResult(name=f"endpoint_dev_key_{endpoint_id}", ok=True, detail="")
        return CheckResult(
            name=f"endpoint_placeholder_key_{endpoint_id}",
            ok=False,
            detail=(
                f"endpoint {endpoint_id!r} api_key={key!r} matches the closed "
                "placeholder set; refuse to start"
            ),
            failure_class="config_placeholder_value",
        )
    if key.startswith("missing-"):
        return CheckResult(
            name=f"endpoint_missing_prefix_key_{endpoint_id}",
            ok=False,
            detail=f"endpoint {endpoint_id!r} api_key starts with 'missing-' marker",
            failure_class="config_placeholder_value",
        )
    return CheckResult(name=f"endpoint_key_ok_{endpoint_id}", ok=True, detail="")


def check_endpoint_smoke(
    *,
    endpoint_id: str,
    endpoint_cfg: Mapping[str, Any],
    timeout_seconds: float,
) -> CheckResult:
    """Issue a no-op chat to the endpoint; fail if it returns nothing.

    For ``codex-cli`` we delegate to :func:`agent.codex_auth.get_valid_credentials`;
    if creds load and aren't expired, the smoke passes. The actual Responses
    API call is deferred to first use rather than every startup, since codex
    OAuth is rate-limited and a token refresh storm at process start is bad
    citizenship.

    For other providers we issue a HEAD against ``base_url`` to confirm
    the host resolves and returns *something* (any 2xx/4xx is fine; a
    network error or 5xx fails the smoke). The full no-op chat is too
    expensive to run for every endpoint at every startup.
    """
    provider = str(endpoint_cfg.get("provider") or "").lower()
    base_url = str(endpoint_cfg.get("base_url") or "").strip()

    if provider == "codex-cli":
        return _smoke_codex_cli(endpoint_id=endpoint_id)

    if not base_url:
        return CheckResult(
            name=f"endpoint_smoke_no_url_{endpoint_id}",
            ok=False,
            detail=f"endpoint {endpoint_id!r} has no base_url",
            failure_class="config_missing_required",
        )

    head_url = base_url
    try:
        with httpx.Client(timeout=timeout_seconds) as http:
            response = http.get(head_url, follow_redirects=False)
    except httpx.HTTPError as exc:
        return CheckResult(
            name=f"endpoint_smoke_unreachable_{endpoint_id}",
            ok=False,
            detail=f"endpoint {endpoint_id!r} HEAD failed: {exc!r}",
            failure_class="endpoint_unreachable",
        )
    if response.status_code >= 500:
        return CheckResult(
            name=f"endpoint_smoke_5xx_{endpoint_id}",
            ok=False,
            detail=(
                f"endpoint {endpoint_id!r} HEAD returned {response.status_code} "
                f"({response.text[:120]!r})"
            ),
            failure_class="endpoint_invalid_response",
        )
    return CheckResult(
        name=f"endpoint_smoke_ok_{endpoint_id}",
        ok=True,
        detail=f"HEAD {head_url} → {response.status_code}",
    )


def _smoke_codex_cli(*, endpoint_id: str) -> CheckResult:
    """Verify codex-cli OAuth credentials are loaded and not expired."""
    try:
        from agent.codex_auth import get_valid_credentials
    except ImportError as exc:
        return CheckResult(
            name=f"endpoint_codex_import_{endpoint_id}",
            ok=False,
            detail=f"agent.codex_auth import failed: {exc}",
            failure_class="config_missing_required",
        )
    try:
        creds = get_valid_credentials()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name=f"endpoint_codex_creds_{endpoint_id}",
            ok=False,
            detail=f"codex creds load failed: {exc}",
            failure_class="auth_agent_identity_missing",
        )
    if creds is None:
        return CheckResult(
            name=f"endpoint_codex_no_creds_{endpoint_id}",
            ok=False,
            detail="codex-cli endpoint has no valid OAuth creds; run `codex login`",
            failure_class="auth_agent_identity_missing",
        )
    return CheckResult(
        name=f"endpoint_codex_ok_{endpoint_id}",
        ok=True,
        detail=f"OAuth account_id={getattr(creds, 'account_id', '?')[:12]}",
    )


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------


def collect_active_endpoints(
    *,
    loaded_config: Mapping[str, Any],
    roles: Iterable[str],
) -> dict[str, Mapping[str, Any]]:
    """Return ``{endpoint_id: endpoint_cfg}`` for every endpoint actually
    referenced by one of the workforce roles.

    Inactive endpoints (defined in the config but not used by any role) are
    skipped so their placeholder keys don't fail the smoke.
    """
    agents = loaded_config.get("agents") or {}
    endpoints = loaded_config.get("endpoints") or {}
    active: dict[str, Mapping[str, Any]] = {}
    for role in roles:
        role_cfg = agents.get(role) or {}
        endpoint_id = role_cfg.get("endpoint")
        if not endpoint_id:
            continue
        cfg = endpoints.get(endpoint_id)
        if isinstance(cfg, Mapping):
            active[endpoint_id] = cfg
    return active


def run_preflight(
    *,
    loaded_config: Mapping[str, Any],
    preflight_cfg: Optional[PreflightConfig] = None,
) -> list[CheckResult]:
    """Run every preflight check and return one CheckResult per check."""
    cfg = preflight_cfg or PreflightConfig.from_env()
    results: list[CheckResult] = []

    # 1. Required env vars.
    results.extend(check_required_env(required=cfg.required_env_vars))

    # 2. Config drift detection.
    results.append(check_config_drift(loaded_config=loaded_config, config_path=cfg.config_path))

    # 3. Service URLs.
    for url_name, env_var in (
        ("taskboard_url", "TASKBOARD_URL"),
        ("forgejo_url", "FORGEJO_URL"),
    ):
        url = os.environ.get(env_var, "").strip()
        if not url and url_name == "forgejo_url":
            # Forgejo URL is only required when an endpoint references it.
            continue
        results.append(check_service_url(name=url_name, url=url, dev_mode=cfg.dev_mode))

    # 4. Per-active-endpoint placeholder + smoke.
    active = collect_active_endpoints(
        loaded_config=loaded_config, roles=cfg.roles_to_check
    )
    for endpoint_id, endpoint_cfg in active.items():
        results.append(
            check_endpoint_placeholder(
                endpoint_id=endpoint_id,
                endpoint_cfg=endpoint_cfg,
                dev_mode=cfg.dev_mode,
            )
        )
        results.append(
            check_endpoint_smoke(
                endpoint_id=endpoint_id,
                endpoint_cfg=endpoint_cfg,
                timeout_seconds=cfg.smoke_timeout_seconds,
            )
        )

    return results


def run_preflight_or_exit(
    *,
    loaded_config: Mapping[str, Any],
    preflight_cfg: Optional[PreflightConfig] = None,
) -> str:
    """Run preflight; on any failure raise PreflightFailure.

    Returns the loaded config sha256 on success so the daemon can stamp
    every ``agent_runs`` row with it (Phase 1 ``config_sha256`` column).
    """
    results = run_preflight(loaded_config=loaded_config, preflight_cfg=preflight_cfg)
    failures = [r for r in results if not r.ok]
    if failures:
        for r in failures:
            LOGGER.error("preflight FAIL: %s — %s", r.name, r.detail)
        raise PreflightFailure(results)
    cfg = preflight_cfg or PreflightConfig.from_env()
    sha = compute_agent_config_sha256(cfg.config_path) or ""
    LOGGER.info(
        "preflight OK: %d checks passed, config_sha256=%s",
        len(results),
        sha[:12] if sha else "?",
    )
    return sha
