# 2026-05-26 Connection Error Opaque Surface

## Summary

Users saw `Error: Connection error.` / `Agent Error: Connection error.` in the web UI while agent-kai had real upstream failures in the daemon log. The app did not contain a hard-coded user-facing `Connection error` string; the opaque message came from upstream exceptions whose string representation was only `Connection error.`. The agent runtime formatted that directly and the daemon/frontend displayed it unchanged.

## Current Path

1. `AgentRunner.run()` catches primary/fallback endpoint exceptions in `agent/core.py` and emitted `{"type": "error", "data": f"Primary endpoint failed: {e}"}`.
2. `Session.stream_agent_events()` in `daemon/core.py` publishes that as `agent.error`.
3. `DaemonServer._event_to_message()` in `daemon/server.py` converts `agent.error` to a websocket `ErrorEnvelope`.
4. `DaemonClient` / Svelte pages render `envelope.message`.
5. If the exception was an OpenAI/Codex `APIConnectionError` with `str(exc) == "Connection error."`, the UI had no class, cause chain, traceback, or hint.

## RCA Table

| Underlying exception | Caught at file:line | Currently surfaced as | Should surface as |
| --- | --- | --- | --- |
| OpenAI/Codex transport exception whose string is `Connection error.` | `agent/core.py:1247` / `agent/core.py:1267` primary catch in `AgentRunner.run()` | `Primary endpoint failed: Connection error.` and often final `Error: Connection error.` | `codex_transport_connection_error` with exception class, cause chain, traceback, and connectivity hint |
| Fallback endpoint transport exception whose string is `Connection error.` | `agent/core.py:1317` / `agent/core.py:1338` fallback catch in `AgentRunner.run()` | `Endpoint #N failed: Connection error.` and final `Error: Connection error.` | `codex_transport_connection_error` with endpoint/fallback prefix, real cause, and hint |
| Sub-agent model exception whose string is `Connection error.` | `agent/sub_agents.py:225` in `_invoke()` | NATS response body `Error: Connection error.` | `codex_transport_connection_error` in the sub-agent response text, including cause and hint |
| `TimeoutError: codex CLI timed out after 10.0 seconds` | Raised by `agent/auto_loop_brain.py:198`; startup probe invoked at `daemon/server.py:1013` | Daemon startup failure traceback; web clients see websocket/HTTP disruption | `codex_cli_timeout`; startup probe timeout raised to 60s to avoid cold-start false negatives |
| `websockets.exceptions.ConnectionClosedError: no close frame received or sent` | `daemon/server.py:4339` / `daemon/server.py:4638` cleanup awaits the websocket forward task | ASGI traceback; browser reconnect shows generic websocket close | `websocket_dropped` WARN log; client sees typed error only when still connected |
| `websockets.exceptions.ConnectionClosedError: sent 1012 (service restart); no close frame received` | `daemon/server.py:4339` / `daemon/server.py:4638` cleanup awaits the websocket forward task during daemon restart | ASGI traceback; browser reconnect shows close code | `websocket_service_restart` WARN log with reconnect hint |
| `RuntimeError: WebSocket is not connected. Need to call "accept" first.` | `daemon/server.py:4265` attach receive, `daemon/server.py:4326` attach send, `daemon/server.py:4353` websocket receive | ASGI traceback | `websocket_dropped` WARN log; suppress noisy ASGI exception |
| `TimeoutError: nats: timeout` from `nats_request` | `agent/tools.py:637` sync tool catch / `agent/tools.py:676` async tool catch | Tool output included exception class, but no typed class/hint | `nats_timeout` with target-agent hint |
| `nats.errors.Error: nats: empty response from server when expecting INFO message` | `nats_bus/bus.py:33` reconnect error callback / `nats_bus/bus.py:127` request reconnect path | nats-py stderr/log traceback | `nats_connection_error` WARN log; reconnect attempts changed from 10 to unlimited |

## Fixes Applied

- Added `agent/error_surface.py` to classify exceptions into stable operator-facing error classes.
- Threaded `error_class`, `error_message`, `underlying_traceback`, and `actionable_hint` through agent events and daemon websocket `ErrorEnvelope`.
- Updated the web display and daemon client to prefer typed error details and hints when present.
- Added structured WARN logs with `*_TYPED_ERROR` records for agent, daemon websocket, NATS, and sub-agent failures.
- Raised auto-loop-brain Codex CLI startup probe timeout from a hard 10s cap to 60s.
- Changed NATS reconnect attempts from 10 to unlimited and added NATS lifecycle/error callbacks.
- Hardened websocket disconnect handling so dropped clients do not leave noisy ASGI tracebacks.

## Verification

- Python smoke test: `tests/test_error_surface.py`.
- Frontend smoke test: `web/src/lib/daemon/error.test.ts`.
- Focused daemon websocket regression tests cover attach, bad first message, and unauthorized websocket rejection.
- Operator verification after restart: trigger a known upstream failure and confirm the UI includes the bracketed error class plus the underlying exception message/hint, not only `Connection error.`

## Deployment Note

Do not restart the active gameday daemon without operator approval. The currently observed process was manual uvicorn:

`/home/atc/git/claude-local-ai-agent/.venv/bin/python3 .venv/bin/uvicorn daemon.server:app --host 0.0.0.0 --port 56789`
