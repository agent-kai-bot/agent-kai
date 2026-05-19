from agent import claude_auth
from agent.claude_auth import ClaudeCredentials
from agent.core import ChatClaudeOAuth, create_llm


def test_create_llm_routes_claude_oauth_provider(monkeypatch):
    def fake_get_valid_credentials(*args, **kwargs):
        return ClaudeCredentials(
            access_token="access-token-test",
            refresh_token="refresh-token-test",
            expires_at=9999999999,
        )

    monkeypatch.setattr(claude_auth, "get_valid_credentials", fake_get_valid_credentials)

    llm = create_llm(
        {
            "provider": "claude-oauth",
            "base_url": "https://api.anthropic.com",
            "model": "claude-opus-4-7",
            "effort": "low",
            "thinking": "adaptive",
            "max_tokens": 128,
        }
    )

    assert isinstance(llm, ChatClaudeOAuth)
    assert llm.model == "claude-opus-4-7"
    assert llm.model_kwargs["output_config"] == {"effort": "low"}


def test_create_llm_routes_legacy_claude_cli_provider(monkeypatch):
    def fake_get_valid_credentials(*args, **kwargs):
        return ClaudeCredentials(
            access_token="access-token-test",
            refresh_token="refresh-token-test",
            expires_at=9999999999,
        )

    monkeypatch.setattr(claude_auth, "get_valid_credentials", fake_get_valid_credentials)

    llm = create_llm(
        {
            "provider": "claude-cli",
            "base_url": "https://api.anthropic.com",
            "model": "claude-opus-4-7",
        }
    )

    assert isinstance(llm, ChatClaudeOAuth)
