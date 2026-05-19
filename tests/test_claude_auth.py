import json
import time
from urllib.parse import parse_qs

import httpx

from agent import claude_auth
from agent.claude_auth import ClaudeCredentials


def _creds(*, expires_in: int = 3600) -> ClaudeCredentials:
    return ClaudeCredentials(
        access_token="access-token-test",
        refresh_token="refresh-token-test",
        expires_at=int(time.time()) + expires_in,
        subscription_type="max",
        rate_limit_tier="default_claude_max_20x",
        scopes=["user:inference", "user:profile"],
    )


def test_to_auth_json_round_trips_claude_code_shape(tmp_path):
    auth_path = tmp_path / ".credentials.json"
    claude_auth.save_credentials(_creds(), auth_path)

    rendered = json.loads(auth_path.read_text())
    loaded = claude_auth.load_credentials(auth_path)

    assert sorted(rendered) == ["claudeAiOauth"]
    assert rendered["claudeAiOauth"]["accessToken"] == "access-token-test"
    assert rendered["claudeAiOauth"]["refreshToken"] == "refresh-token-test"
    assert rendered["claudeAiOauth"]["expiresAt"] > 10_000_000_000
    assert loaded is not None
    assert loaded.access_token == "access-token-test"
    assert loaded.refresh_token == "refresh-token-test"
    assert loaded.subscription_type == "max"
    assert loaded.rate_limit_tier == "default_claude_max_20x"
    assert loaded.scopes == ["user:inference", "user:profile"]


def test_load_credentials_handles_missing_or_malformed_files(tmp_path):
    assert claude_auth.load_credentials(tmp_path / "missing.json") is None

    malformed = tmp_path / ".credentials.json"
    malformed.write_text("{not-json")
    assert claude_auth.load_credentials(malformed) is None

    malformed.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x"}}))
    assert claude_auth.load_credentials(malformed) is None


def test_is_expired_uses_grace_window():
    assert _creds(expires_in=3600).is_expired() is False
    assert _creds(expires_in=60).is_expired() is True
    assert _creds(expires_in=-60).is_expired() is True


def test_refresh_access_token_posts_oauth_refresh_request():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        form = parse_qs(request.content.decode())
        assert request.url == claude_auth.TOKEN_URL
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert request.headers["anthropic-beta"] == claude_auth.OAUTH_BETA
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["refresh-token-test"]
        assert form["client_id"] == [claude_auth.CLIENT_ID]
        return httpx.Response(
            200,
            json={
                "access_token": "access-token-new",
                "refresh_token": "refresh-token-new",
                "expires_in": 3600,
                "scope": "user:inference user:profile",
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        payload = claude_auth.refresh_access_token(
            "refresh-token-test",
            http_client=client,
        )

    assert len(requests) == 1
    assert payload["access_token"] == "access-token-new"


def test_get_valid_credentials_refreshes_expired_and_saves(monkeypatch, tmp_path):
    auth_path = tmp_path / ".credentials.json"
    claude_auth.save_credentials(_creds(expires_in=-1), auth_path)

    def fake_refresh_access_token(refresh_token: str):
        assert refresh_token == "refresh-token-test"
        return {
            "access_token": "access-token-new",
            "refresh_token": "refresh-token-new",
            "expires_in": 3600,
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_20x",
            "scopes": ["user:inference"],
        }

    monkeypatch.setattr(claude_auth, "refresh_access_token", fake_refresh_access_token)

    refreshed = claude_auth.get_valid_credentials(auth_path)
    loaded = claude_auth.load_credentials(auth_path)

    assert refreshed is not None
    assert refreshed.access_token == "access-token-new"
    assert refreshed.refresh_token == "refresh-token-new"
    assert loaded is not None
    assert loaded.access_token == "access-token-new"


def test_get_valid_credentials_returns_existing_when_fresh(monkeypatch, tmp_path):
    auth_path = tmp_path / ".credentials.json"
    claude_auth.save_credentials(_creds(), auth_path)

    def fail_refresh(_refresh_token: str):
        raise AssertionError("refresh should not be called")

    monkeypatch.setattr(claude_auth, "refresh_access_token", fail_refresh)

    creds = claude_auth.get_valid_credentials(auth_path)

    assert creds is not None
    assert creds.access_token == "access-token-test"
