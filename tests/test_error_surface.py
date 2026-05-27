"""Smoke tests for typed operator-facing runtime errors."""

from __future__ import annotations

from agent.error_surface import surface_exception


def _runtime_connection_error_with_cause() -> RuntimeError:
    try:
        raise OSError("DNS lookup failed for chatgpt.com")
    except OSError as exc:
        raise RuntimeError("Connection error.") from exc


def test_connection_error_preserves_underlying_cause() -> None:
    try:
        _runtime_connection_error_with_cause()
    except RuntimeError as exc:
        surfaced = surface_exception(exc)

    message = surfaced.display_message(prefix="Primary endpoint failed")

    assert surfaced.error_class == "codex_transport_connection_error"
    assert "Connection error." in message
    assert "DNS lookup failed for chatgpt.com" in message
    assert message != "Primary endpoint failed: Connection error."


def test_codex_cli_timeout_is_typed_with_real_timeout_message() -> None:
    surfaced = surface_exception(TimeoutError("codex CLI timed out after 10.0 seconds"))

    assert surfaced.error_class == "codex_cli_timeout"
    assert "codex CLI timed out after 10.0 seconds" in surfaced.display_message()


def test_nats_timeout_is_typed_with_real_nats_message() -> None:
    surfaced = surface_exception(TimeoutError("nats: timeout"))

    assert surfaced.error_class == "nats_timeout"
    assert "nats: timeout" in surfaced.display_message()


def test_nats_connection_error_is_typed_with_real_nats_message() -> None:
    surfaced = surface_exception(
        RuntimeError("nats: empty response from server when expecting INFO message")
    )

    assert surfaced.error_class == "nats_connection_error"
    assert "expecting INFO message" in surfaced.display_message()


def test_websocket_drop_is_typed_with_real_websocket_message() -> None:
    surfaced = surface_exception(
        RuntimeError('WebSocket is not connected. Need to call "accept" first.')
    )

    assert surfaced.error_class == "websocket_dropped"
    assert 'Need to call "accept" first' in surfaced.display_message()


def test_websocket_service_restart_is_typed_with_real_close_message() -> None:
    surfaced = surface_exception(
        RuntimeError("sent 1012 (service restart); no close frame received")
    )

    assert surfaced.error_class == "websocket_service_restart"
    assert "service restart" in surfaced.display_message()


def test_endpoint_timeout_is_typed_with_real_timeout_message() -> None:
    surfaced = surface_exception(TimeoutError("model endpoint read timed out"))

    assert surfaced.error_class == "endpoint_timeout"
    assert "model endpoint read timed out" in surfaced.display_message()


def test_agent_crash_is_typed_with_exception_message() -> None:
    surfaced = surface_exception(ValueError("strategy config exploded"))

    assert surfaced.error_class == "agent_runtime_exception"
    assert "ValueError: strategy config exploded" in surfaced.display_message()
