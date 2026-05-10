import base64
import json
import os
import subprocess
import time

from agent import codex_auth
from agent.codex_auth import CodexCredentials


def _jwt(payload: dict) -> str:
    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(payload)}.signature"


def _access_token(*, account_id: str = "acct-test", expires_in: int = 3600) -> str:
    return _jwt(
        {
            "exp": int(time.time()) + expires_in,
            codex_auth.JWT_AUTH_CLAIM: {"chatgpt_account_id": account_id},
        }
    )


def _id_token() -> str:
    now = int(time.time())
    return _jwt(
        {
            "iss": "https://auth.openai.com",
            "aud": codex_auth.CLIENT_ID,
            "sub": "user-test",
            "email": "operator@example.com",
            "iat": now,
            "exp": now + 3600,
        }
    )


class _FakeURLResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def test_to_auth_json_round_trips_id_token(tmp_path):
    id_token = _id_token()
    auth_path = tmp_path / "auth.json"
    creds = CodexCredentials(
        access_token=_access_token(),
        refresh_token="refresh-token",
        account_id="acct-test",
        expires_at=int(time.time()) + 3600,
        id_token=id_token,
    )

    codex_auth.save_credentials(creds, auth_path)
    rendered = json.loads(auth_path.read_text())
    loaded = codex_auth.load_credentials(auth_path)

    assert rendered["tokens"]["id_token"] == id_token
    assert loaded is not None
    assert loaded.id_token == id_token


def test_refresh_credentials_captures_returned_id_token(monkeypatch):
    id_token = _id_token()
    refreshed_access = _access_token(account_id="acct-refreshed")
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return _FakeURLResponse(
            {
                "access_token": refreshed_access,
                "refresh_token": "new-refresh",
                "id_token": id_token,
            }
        )

    monkeypatch.setattr(codex_auth.urllib.request, "urlopen", fake_urlopen)

    refreshed = codex_auth.refresh_credentials(
        CodexCredentials(
            access_token=_access_token(account_id="acct-old"),
            refresh_token="old-refresh",
            account_id="acct-old",
            id_token="old-id-token",
        )
    )

    assert len(requests) == 1
    assert refreshed.access_token == refreshed_access
    assert refreshed.refresh_token == "new-refresh"
    assert refreshed.account_id == "acct-refreshed"
    assert refreshed.id_token == id_token


def test_refresh_credentials_preserves_existing_id_token_when_omitted(monkeypatch):
    existing_id_token = _id_token()
    refreshed_access = _access_token(account_id="acct-refreshed")

    def fake_urlopen(_request, timeout):
        assert timeout == 15
        return _FakeURLResponse({"access_token": refreshed_access})

    monkeypatch.setattr(codex_auth.urllib.request, "urlopen", fake_urlopen)

    refreshed = codex_auth.refresh_credentials(
        CodexCredentials(
            access_token=_access_token(account_id="acct-old"),
            refresh_token="old-refresh",
            account_id="acct-old",
            id_token=existing_id_token,
        )
    )

    assert refreshed.refresh_token == "old-refresh"
    assert refreshed.id_token == existing_id_token


def test_load_credentials_reads_id_token(tmp_path):
    id_token = _id_token()
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": id_token,
                    "access_token": _access_token(),
                    "refresh_token": "refresh-token",
                    "account_id": "acct-test",
                },
                "last_refresh": "2026-05-10T00:00:00.000Z",
            }
        )
    )

    creds = codex_auth.load_credentials(auth_path)

    assert creds is not None
    assert creds.id_token == id_token


def test_to_auth_json_output_is_valid_for_codex_cli_login_status(tmp_path):
    auth_path = tmp_path / "auth.json"
    creds = CodexCredentials(
        access_token=_access_token(),
        refresh_token="refresh-token",
        account_id="acct-test",
        expires_at=int(time.time()) + 3600,
        id_token=_id_token(),
    )
    codex_auth.save_credentials(creds, auth_path)

    result = subprocess.run(
        ["codex", "login", "status"],
        env={**os.environ, "CODEX_HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )
    combined_output = f"{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, combined_output
    assert "Logged in using ChatGPT" in combined_output
