# KAI Daemon Shutdown Hang and Dead Restart RCA

Date: 2026-05-21

## Incident

A `scripts/start-kai-daemon.sh --force` restart at about 17:32 sent SIGTERM to the running daemon. The daemon logged uvicorn shutdown messages and did not exit before the wrapper escalated. No replacement daemon was launched because the wrapper then failed required-env validation. Dan restored service manually at about 18:26.

## Root Cause

### Graceful shutdown hang

The shutdown hang was a cancellation correctness bug in active session turns, plus unbounded daemon task draining.

File/line evidence from the incident revision:

- `daemon/server.py:1894-1898` caught `asyncio.CancelledError` in `DaemonServer.run_input()` and did not re-raise it. That converted outer task cancellation into a normal `InputRunResult`.
- `daemon/server.py:4207` and `daemon/server.py:4340` awaited `daemon_server.run_input(...)` inside the websocket request loop. If uvicorn cancelled that request task during shutdown while an agent turn was active, `run_input()` swallowed the cancellation and the websocket loop continued instead of exiting.
- `agent/taskboard_dispatcher.py:801-810` created a background `run_input()` task for in-process taskboard sessions and stored it as `managed.current_input_task`, but `DaemonServer.shutdown()` did not cancel or await active session input tasks.
- `daemon/event_injector.py:151-157` created heartbeat/signal injection tasks without retaining them for shutdown.
- `daemon/server.py:1542-1549`, `daemon/server.py:1153-1170`, and `daemon/heartbeat.py:196-207` awaited cancelled background tasks without a timeout. A task that resisted cancellation could hold shutdown open indefinitely.
- `daemon/scheduler.py:417-419` cancelled startup catch-up tasks but did not await them; `daemon/scheduler.py:929` created async event-callback tasks without tracking them.
- `main.py:151-157` did not configure `uvicorn.Config.timeout_graceful_shutdown`, so uvicorn had no daemon-level hard stop for graceful shutdown.

Fix:

- `DaemonServer.run_input()` now performs its cleanup and re-raises `asyncio.CancelledError`.
- `DaemonServer.shutdown()` now drains taskboard dispatcher, session lifecycle tasks, heartbeat, scheduler, event injector, active input tasks, sub-agent managers, daemon background tasks, and bus disconnect through bounded shutdown steps.
- `daemon/shutdown.py` centralizes bounded cancel-and-await behavior using `asyncio.gather(..., return_exceptions=True)` under `asyncio.wait_for(asyncio.shield(...))`, so stubborn tasks are logged and cannot wedge shutdown.
- Event injector and scheduler background tasks are now tracked and cancelled/awaited during shutdown.
- The taskboard dispatcher's run-outcome reaper now runs via `asyncio.to_thread(...)` so its synchronous sweep does not block the event loop during cancellation.
- uvicorn daemon startup now sets `timeout_graceful_shutdown=20`.

### Restart left daemon dead

The wrapper killed before it validated whether a replacement could start.

File/line evidence from the incident revision:

- `scripts/start-kai-daemon.sh:24-42` detected an existing daemon and, under `--force`, sent SIGTERM and possibly SIGKILL.
- Only after that kill path did `scripts/start-kai-daemon.sh:44-66` check required env and exit with `Missing required env: ...`.

That ordering made `--force` unsafe: if Dan ran it without sourcing the daemon env, the script stopped the healthy daemon and then aborted before launching the replacement.

Fix:

- The script now checks for an existing daemon and still refuses non-force starts idempotently.
- For force starts, required-env validation runs before any stop signal.
- After the force stop path starts, there is no validation exit between SIGTERM/SIGKILL fallback and launching the new daemon.
- The script accepts test-only path overrides for repo/log/pidfile/port while preserving production defaults.
