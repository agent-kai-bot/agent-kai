import json
import time

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from agent import core as agent_core
from agent import codex_auth
from agent.codex_auth import CodexCredentials
from agent.core import (
    CODEX_OPERATOR_REAUTH_MESSAGE,
    ChatCodex,
    CodexTransportDrop,
    _is_retryable_codex_transport_error,
)


class DroppingSSEStream(httpx.SyncByteStream):
    def __iter__(self):
        payload = _responses_sse("partial")
        yield payload[: payload.index(b"data: [DONE]")]
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body "
            "(incomplete chunked read)"
        )


def _responses_payload(text: str) -> dict:
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": time.time(),
        "status": "completed",
        "model": "gpt-5.5",
        "output": [
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


def _responses_sse(text: str) -> bytes:
    completed = _responses_payload(text)
    events = [
        {
            "type": "response.created",
            "response": {
                "id": "resp_test",
                "object": "response",
                "created_at": time.time(),
                "status": "in_progress",
                "model": "gpt-5.5",
                "output": [],
            },
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_test",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "item_id": "msg_test",
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
        {"type": "response.completed", "response": completed},
    ]
    lines = []
    for event in events:
        lines.append(f"event: {event['type']}\n")
        lines.append(f"data: {json.dumps(event)}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def _chat_codex_with_transport(transport: httpx.MockTransport) -> ChatCodex:
    return ChatCodex(
        base_url="https://chatgpt.test/backend-api/codex",
        api_key="stale-token",
        model="gpt-5.5",
        default_headers={
            "chatgpt-account-id": "acct-old",
            "originator": "kai",
        },
        use_responses_api=True,
        streaming=True,
        max_retries=0,
        http_client=httpx.Client(transport=transport),
    )


def test_retryable_marker_detection():
    err = RuntimeError(
        "peer closed connection without sending complete message body "
        "(incomplete chunked read)"
    )
    assert _is_retryable_codex_transport_error(err)


def test_retryable_exception_types():
    assert _is_retryable_codex_transport_error(httpx.RemoteProtocolError("x"))
    assert _is_retryable_codex_transport_error(httpx.ReadError("x"))


def test_non_retryable():
    assert not _is_retryable_codex_transport_error(ValueError("nope"))


def test_chat_codex_retries_dropped_stream_then_recovers(monkeypatch):
    requests = []
    monkeypatch.setattr(agent_core, "CODEX_TRANSPORT_RETRY_BACKOFF_SECONDS", (0,))

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                stream=DroppingSSEStream(),
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        return httpx.Response(
            200,
            content=_responses_sse("recovered"),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    model = _chat_codex_with_transport(httpx.MockTransport(handler))

    message = model.invoke(
        [SystemMessage(content="system"), HumanMessage(content="hello")]
    )

    assert message.content == "recovered"
    assert len(requests) == 2


def test_chat_codex_exhausts_dropped_stream_retries(monkeypatch):
    requests = []
    monkeypatch.setattr(agent_core, "CODEX_TRANSPORT_RETRY_BACKOFF_SECONDS", (0, 0))

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            stream=DroppingSSEStream(),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    model = _chat_codex_with_transport(httpx.MockTransport(handler))

    with pytest.raises(CodexTransportDrop):
        model.invoke([SystemMessage(content="system"), HumanMessage(content="hello")])

    assert len(requests) == 3


def test_chat_codex_retries_once_with_refreshed_bearer(monkeypatch):
    refresh_calls = []
    requests = []

    def fake_get_valid_credentials(*, force_refresh=False):
        refresh_calls.append(force_refresh)
        return CodexCredentials(
            access_token="fresh-token",
            refresh_token="fresh-refresh",
            account_id="acct-fresh",
            id_token="fresh-id-token",
        )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "authorization": request.headers.get("authorization"),
                "account_id": request.headers.get("chatgpt-account-id"),
                "body": json.loads(request.content.decode()),
            }
        )
        if len(requests) == 1:
            return httpx.Response(
                401,
                json={"detail": {"code": "token_expired"}},
                request=request,
            )
        return httpx.Response(
            200,
            content=_responses_sse("recovered"),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    monkeypatch.setattr(codex_auth, "get_valid_credentials", fake_get_valid_credentials)
    model = _chat_codex_with_transport(httpx.MockTransport(handler))

    message = model.invoke(
        [SystemMessage(content="system"), HumanMessage(content="hello")]
    )

    assert message.content == "recovered"
    assert refresh_calls == [True]
    assert [request["authorization"] for request in requests] == [
        "Bearer stale-token",
        "Bearer fresh-token",
    ]
    assert [request["account_id"] for request in requests] == [
        "acct-old",
        "acct-fresh",
    ]
    assert requests[1]["body"]["stream"] is True
    assert model.root_client.api_key == "fresh-token"
    assert model.root_client.auth_headers["Authorization"] == "Bearer fresh-token"


def test_chat_codex_consecutive_401s_surface_operator_action(monkeypatch):
    refresh_calls = []
    requests = []

    def fake_get_valid_credentials(*, force_refresh=False):
        refresh_calls.append(force_refresh)
        return CodexCredentials(
            access_token="fresh-token",
            refresh_token="fresh-refresh",
            account_id="acct-fresh",
            id_token="fresh-id-token",
        )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.headers.get("authorization"))
        return httpx.Response(
            401,
            json={"error": {"message": "unauthorized"}},
            request=request,
        )

    monkeypatch.setattr(codex_auth, "get_valid_credentials", fake_get_valid_credentials)
    model = _chat_codex_with_transport(httpx.MockTransport(handler))

    with pytest.raises(RuntimeError) as exc_info:
        model.invoke([SystemMessage(content="system"), HumanMessage(content="hello")])

    assert str(exc_info.value) == CODEX_OPERATOR_REAUTH_MESSAGE
    assert refresh_calls == [True]
    assert requests == ["Bearer stale-token", "Bearer fresh-token"]
