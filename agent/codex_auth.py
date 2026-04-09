"""OpenAI Codex (ChatGPT subscription) OAuth authentication.

Lets the agent talk to ``https://chatgpt.com/backend-api/codex/responses``
using the same OAuth credentials the official ``codex`` CLI stores at
``~/.codex/auth.json``. Users on a paid ChatGPT plan (Plus / Pro / Team)
can run their agents against their subscription quota with no API key.

Two paths into a usable token set:

1. **Reuse existing codex CLI login** — if the user already ran
   ``codex login`` once, ``~/.codex/auth.json`` exists and we just
   load it. This is the common case. Tokens are refreshed
   automatically when within ``REFRESH_GRACE_SECONDS`` of expiry.

2. **Run our own OAuth flow** — ``login()`` opens a browser to
   ``https://auth.openai.com/oauth/authorize``, spins up a temporary
   loopback server on ``localhost:1455`` to receive the redirect, and
   writes the resulting tokens to ``~/.codex/auth.json``. Useful if
   the user doesn't have the codex CLI installed but does have a
   ChatGPT subscription.

The OAuth client_id and endpoints come from
``packages/ai/src/utils/oauth/openai-codex.ts`` in
``github.com/badlogic/pi-mono`` (the ``@mariozechner/pi-ai`` package),
which is the same flow the openclaw project uses.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import http.server
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── OAuth constants ─────────────────────────────────────────
#
# These match the official codex CLI exactly. Changing any of them
# will break the auth flow.

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
REDIRECT_PORT = 1455
SCOPE = "openid profile email offline_access"
JWT_AUTH_CLAIM = "https://api.openai.com/auth"

# Refresh tokens that are within this many seconds of expiring.
# 5 minutes gives enough headroom for a long agent run after refresh.
REFRESH_GRACE_SECONDS = 5 * 60

# Default location for the credentials file. Matches the codex CLI so
# the two share the same login state.
DEFAULT_AUTH_PATH = Path.home() / ".codex" / "auth.json"


@dataclasses.dataclass
class CodexCredentials:
    """A loaded set of Codex OAuth credentials."""

    access_token: str
    refresh_token: str
    account_id: str
    # Unix epoch seconds at which the access token expires. Decoded
    # from the JWT's ``exp`` claim — the auth.json file itself does
    # NOT store an expiry, only the access_token JWT does.
    expires_at: int = 0

    def is_expired(self, grace_seconds: int = REFRESH_GRACE_SECONDS) -> bool:
        if self.expires_at <= 0:
            # Conservative: if we couldn't decode the JWT, assume
            # we should refresh. The refresh endpoint is idempotent.
            return True
        return time.time() + grace_seconds >= self.expires_at

    def to_auth_json(self) -> dict:
        """Render the credentials in the auth.json on-disk format."""
        return {
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": "",
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "account_id": self.account_id,
            },
            "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        }


# ── JWT helpers (no signature verification — we trust the issuer) ──

def _decode_jwt_payload(token: str) -> dict:
    """Decode the payload of a JWT into a dict. Returns {} on failure."""
    try:
        payload_b64 = token.split(".")[1]
        # Pad base64 to a multiple of 4
        padding = "=" * ((4 - len(payload_b64) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload_b64 + padding)
        return json.loads(decoded)
    except Exception as exc:  # noqa: BLE001
        logger.warning("JWT decode failed: %s", exc)
        return {}


def _extract_account_id(access_token: str) -> Optional[str]:
    """Pull chatgpt_account_id out of the access_token JWT."""
    payload = _decode_jwt_payload(access_token)
    auth = payload.get(JWT_AUTH_CLAIM, {})
    if isinstance(auth, dict):
        aid = auth.get("chatgpt_account_id")
        if isinstance(aid, str) and aid:
            return aid
    return None


def _extract_expiry(access_token: str) -> int:
    """Pull the exp claim from a JWT. Returns 0 if missing."""
    payload = _decode_jwt_payload(access_token)
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        return int(exp)
    return 0


# ── Load / save / refresh ───────────────────────────────────

def load_credentials(path: Path = DEFAULT_AUTH_PATH) -> Optional[CodexCredentials]:
    """Read ``~/.codex/auth.json`` and return parsed credentials.

    Returns None if the file doesn't exist or is malformed.
    """
    if not path.is_file():
        logger.debug("Codex auth file not found at %s", path)
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read Codex auth.json: %s", exc)
        return None

    tokens = data.get("tokens", {})
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    account_id = tokens.get("account_id")
    if not (access and refresh):
        return None

    # Account ID is usually present in the file, but fall back to the JWT.
    if not account_id:
        account_id = _extract_account_id(access)
        if not account_id:
            logger.warning("auth.json has no account_id and JWT decode failed")
            return None

    return CodexCredentials(
        access_token=access,
        refresh_token=refresh,
        account_id=account_id,
        expires_at=_extract_expiry(access),
    )


def save_credentials(creds: CodexCredentials, path: Path = DEFAULT_AUTH_PATH) -> None:
    """Atomically write credentials to ``~/.codex/auth.json``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(creds.to_auth_json(), f, indent=2)
    os.chmod(tmp, 0o600)  # Same permission the codex CLI uses
    os.replace(tmp, path)


def refresh_credentials(creds: CodexCredentials) -> CodexCredentials:
    """Exchange a refresh_token for a fresh access_token.

    Returns a new CodexCredentials. Raises RuntimeError if the
    refresh fails (token revoked, network error, etc.) — callers
    should catch and prompt the user to re-login.
    """
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": creds.refresh_token,
        "client_id": CLIENT_ID,
    }).encode()

    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Codex token refresh failed: {exc}") from exc

    new_access = payload.get("access_token")
    new_refresh = payload.get("refresh_token") or creds.refresh_token
    if not new_access:
        raise RuntimeError(f"Refresh response missing access_token: {payload}")

    new_account_id = _extract_account_id(new_access) or creds.account_id
    return CodexCredentials(
        access_token=new_access,
        refresh_token=new_refresh,
        account_id=new_account_id,
        expires_at=_extract_expiry(new_access),
    )


def get_valid_credentials(path: Path = DEFAULT_AUTH_PATH) -> Optional[CodexCredentials]:
    """Load credentials and refresh them if expired. Persists the refresh.

    Returns None if no credentials exist on disk. Raises if a refresh
    is needed but fails — callers should catch that and tell the user
    to run ``codex login`` (or our ``login()``) again.
    """
    creds = load_credentials(path)
    if creds is None:
        return None
    if not creds.is_expired():
        return creds

    logger.info("Codex access token expired or near expiry — refreshing")
    refreshed = refresh_credentials(creds)
    try:
        save_credentials(refreshed, path)
    except OSError as exc:
        logger.warning("Failed to persist refreshed Codex credentials: %s", exc)
    return refreshed


# ── PKCE helpers ────────────────────────────────────────────

def _generate_pkce() -> tuple[str, str]:
    """Return (verifier, challenge) for an S256 PKCE flow."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


# ── Full OAuth login flow (for users without ~/.codex/auth.json) ──

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """One-shot HTTP handler that captures the OAuth ``code`` parameter."""

    captured_code: Optional[str] = None
    expected_state: Optional[str] = None
    captured_state: Optional[str] = None

    # Silence the default logging
    def log_message(self, format, *args):
        return

    def do_GET(self):  # noqa: N802 — required by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/auth/callback":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"<h1>Not Found</h1>")
            return

        params = urllib.parse.parse_qs(parsed.query)
        state = (params.get("state") or [""])[0]
        code = (params.get("code") or [""])[0]

        if self.expected_state and state != self.expected_state:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h1>State mismatch</h1>")
            return

        if not code:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h1>Missing authorization code</h1>")
            return

        type(self).captured_code = code
        type(self).captured_state = state
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif;text-align:center;padding:3em'>"
            b"<h1>You can close this window</h1>"
            b"<p>Authentication complete. Return to your terminal.</p>"
            b"</body></html>"
        )


def _build_authorize_url(state: str, challenge: str, originator: str = "kai") -> str:
    qs = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": originator,
    })
    return f"{AUTHORIZE_URL}?{qs}"


def _exchange_code_for_tokens(code: str, verifier: str) -> dict:
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def login(
    *,
    open_browser: bool = True,
    timeout_seconds: int = 300,
    originator: str = "kai",
    path: Path = DEFAULT_AUTH_PATH,
) -> CodexCredentials:
    """Run the full OAuth flow and persist credentials to ``path``.

    Spins up a loopback HTTP server on ``localhost:1455``, opens the
    OpenAI authorize URL in the default browser (unless
    ``open_browser=False``), waits up to ``timeout_seconds`` for the
    callback, exchanges the code for tokens, and writes the result.

    Returns the new ``CodexCredentials``. Raises ``RuntimeError`` on
    timeout, state mismatch, or any HTTP failure.
    """
    verifier, challenge = _generate_pkce()
    state = secrets.token_hex(16)
    auth_url = _build_authorize_url(state, challenge, originator=originator)

    handler = type("CallbackHandler", (_CallbackHandler,), {
        "captured_code": None,
        "captured_state": None,
        "expected_state": state,
    })

    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        if open_browser:
            try:
                webbrowser.open(auth_url)
            except Exception:
                pass
        print(f"\nOpen this URL in your browser to log in to ChatGPT:\n  {auth_url}\n")

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if handler.captured_code:
                break
            time.sleep(0.5)

        if not handler.captured_code:
            raise RuntimeError("Codex login timed out waiting for browser callback")

        token_response = _exchange_code_for_tokens(handler.captured_code, verifier)
    finally:
        server.shutdown()
        server.server_close()

    access = token_response.get("access_token")
    refresh = token_response.get("refresh_token")
    if not access or not refresh:
        raise RuntimeError(f"Token exchange returned no tokens: {token_response}")

    account_id = _extract_account_id(access)
    if not account_id:
        raise RuntimeError("Could not extract chatgpt_account_id from access token")

    creds = CodexCredentials(
        access_token=access,
        refresh_token=refresh,
        account_id=account_id,
        expires_at=_extract_expiry(access),
    )
    save_credentials(creds, path)
    return creds


# ── CLI entry for manual login (python -m agent.codex_auth login) ──

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2 or sys.argv[1] != "login":
        print("Usage: python -m agent.codex_auth login")
        sys.exit(1)
    creds = login()
    print(f"\nLogged in! account_id={creds.account_id}")
    print(f"Saved to {DEFAULT_AUTH_PATH}")
