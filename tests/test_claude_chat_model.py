import json

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import SecretStr

from agent import claude_auth
from agent.claude_auth import ClaudeCredentials
from agent.core import (
    CLAUDE_CODE_BETAS,
    CLAUDE_OPERATOR_REAUTH_MESSAGE,
    ChatClaudeOAuth,
)


def _message_payload(text: str) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _message_sse(text: str) -> bytes:
    events = [
        {
            "event": "message_start",
            "data": {
                "type": "message_start",
                "message": {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-opus-4-7",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            },
        },
        {
            "event": "content_block_start",
            "data": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        },
        {
            "event": "content_block_delta",
            "data": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        },
        {
            "event": "content_block_stop",
            "data": {"type": "content_block_stop", "index": 0},
        },
        {
            "event": "message_delta",
            "data": {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        },
        {"event": "message_stop", "data": {"type": "message_stop"}},
    ]
    lines = []
    for event in events:
        lines.append(f"event: {event['event']}\n")
        lines.append(f"data: {json.dumps(event['data'])}\n\n")
    return "".join(lines).encode()


class _BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield _message_sse("partial")
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body "
            "(incomplete chunked read)"
        )


def _chat_claude_with_transport(transport: httpx.MockTransport) -> ChatClaudeOAuth:
    return ChatClaudeOAuth(
        base_url="https://api.anthropic.test",
        api_key="not-used",
        oauth_access_token=SecretStr("access-token-old"),
        model_name="claude-opus-4-7",
        max_tokens=256,
        thinking={"type": "adaptive"},
        model_kwargs={"output_config": {"effort": "low"}},
        default_headers={
            "User-Agent": "kai-agent (linux)",
            "anthropic-beta": ",".join(CLAUDE_CODE_BETAS),
        },
        betas=list(CLAUDE_CODE_BETAS),
        streaming=True,
        max_retries=0,
        http_client=httpx.Client(transport=transport),
    )


def test_chat_claude_sends_oauth_headers_and_opus_payload():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "authorization": request.headers.get("authorization"),
                "anthropic_version": request.headers.get("anthropic-version"),
                "anthropic_beta": request.headers.get("anthropic-beta"),
                "x_api_key": request.headers.get("x-api-key"),
                "body": json.loads(request.content.decode()),
            }
        )
        return httpx.Response(
            200,
            content=_message_sse("ok"),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    model = _chat_claude_with_transport(httpx.MockTransport(handler))

    message = model.invoke([SystemMessage(content="system"), HumanMessage(content="hello")])

    assert message.content == "ok"
    assert requests[0]["authorization"] == "Bearer access-token-old"
    assert requests[0]["x_api_key"] is None
    assert requests[0]["anthropic_version"] == "2023-06-01"
    assert "claude-code-20250219" in requests[0]["anthropic_beta"]
    assert "oauth-2025-04-20" in requests[0]["anthropic_beta"]
    body = requests[0]["body"]
    assert body["model"] == "claude-opus-4-7"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "low"}
    assert "temperature" not in body
    assert "top_p" not in body
    assert "top_k" not in body
    assert "budget_tokens" not in body


def test_chat_claude_retries_once_with_refreshed_bearer(monkeypatch):
    refresh_calls = []
    requests = []

    def fake_get_valid_credentials(*, force_refresh=False):
        refresh_calls.append(force_refresh)
        return ClaudeCredentials(
            access_token="access-token-new",
            refresh_token="refresh-token-new",
            expires_at=9999999999,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.headers.get("authorization"))
        if len(requests) == 1:
            return httpx.Response(
                401,
                json={"error": {"message": "expired"}},
                request=request,
            )
        return httpx.Response(
            200,
            content=_message_sse("recovered"),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    monkeypatch.setattr(claude_auth, "get_valid_credentials", fake_get_valid_credentials)
    model = _chat_claude_with_transport(httpx.MockTransport(handler))

    message = model.invoke([HumanMessage(content="hello")])

    assert message.content == "recovered"
    assert refresh_calls == [True]
    assert requests == ["Bearer access-token-old", "Bearer access-token-new"]
    assert model._client.auth_headers["Authorization"] == "Bearer access-token-new"


def test_chat_claude_consecutive_401s_surface_operator_action(monkeypatch):
    refresh_calls = []
    requests = []

    def fake_get_valid_credentials(*, force_refresh=False):
        refresh_calls.append(force_refresh)
        return ClaudeCredentials(
            access_token="access-token-new",
            refresh_token="refresh-token-new",
            expires_at=9999999999,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.headers.get("authorization"))
        return httpx.Response(
            401,
            json={"error": {"message": "unauthorized"}},
            request=request,
        )

    monkeypatch.setattr(claude_auth, "get_valid_credentials", fake_get_valid_credentials)
    model = _chat_claude_with_transport(httpx.MockTransport(handler))

    with pytest.raises(RuntimeError) as exc_info:
        model.invoke([HumanMessage(content="hello")])

    assert str(exc_info.value) == CLAUDE_OPERATOR_REAUTH_MESSAGE
    assert refresh_calls == [True]
    assert requests == ["Bearer access-token-old", "Bearer access-token-new"]


def test_chat_claude_stream_returns_expected_chunks():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_message_sse("streamed"),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    model = _chat_claude_with_transport(httpx.MockTransport(handler))

    chunks = list(model.stream("hello"))

    assert "".join(chunk.content for chunk in chunks if chunk.content) == "streamed"


def test_chat_claude_transport_drop_retries_with_backoff(monkeypatch):
    monkeypatch.setattr(
        "agent.core.CLAUDE_TRANSPORT_RETRY_BACKOFF_SECONDS",
        (0, 0, 0),
    )
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                stream=_BrokenStream(),
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        return httpx.Response(
            200,
            content=_message_sse("recovered"),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    model = _chat_claude_with_transport(httpx.MockTransport(handler))

    message = model.invoke([HumanMessage(content="hello")])

    assert message.content == "recovered"
    assert len(requests) == 2
