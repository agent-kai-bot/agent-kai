#!/usr/bin/env python3
"""Entry point for the local AI agent system."""

import argparse
import asyncio
import sys

from config import DEFAULT_AGENT, NATS_URL
from daemon.control import ensure_local_daemon_started
from daemon.core import Session
from nats_bus.bus import NatsBus
from tui.app import AgentTUI
from tui.client_adapter import RemoteSession


async def run_tui(bus, agent_runner):
    """Run the Textual TUI."""
    app = AgentTUI(agent_runner=agent_runner, bus=bus)
    await app.run_async()


async def run_headless(bus, agent_runner):
    """Run in headless mode — NATS-driven only, no TUI."""
    print(f"Agent '{bus.agent_name}' running headless. Listening on NATS...")
    print(f"Send messages to: agent.{bus.agent_name}.request")
    print("Press Ctrl+C to stop.\n")

    async def handle_request(subject, payload):
        task = payload.get("task") or payload.get("message", "")
        if not task:
            return
        print(f"[request] {payload.get('from', '?')}: {task}")
        final = ""
        async for event in agent_runner.run(task):
            if event["type"] == "token":
                print(event["data"], end="", flush=True)
            elif event["type"] == "tool_start":
                print(f"\n[tool] {event['data']['tool']}...", flush=True)
            elif event["type"] == "tool_end":
                print(f"[tool done] {event['data']['tool']}", flush=True)
            elif event["type"] == "final":
                final = event["data"]
            elif event["type"] == "error":
                print(f"\n[error] {event['data']}", flush=True)
        print(f"\n[done]\n")

    await bus.subscribe(f"agent.{bus.agent_name}.request", handle_request)
    await bus.subscribe("agent.broadcast", handle_request)

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for local, remote, and daemon modes."""
    parser = argparse.ArgumentParser(description="Local AI Agent")
    parser.add_argument("--no-tui", action="store_true", help="Run headless (NATS only)")
    parser.add_argument("--terminal", action="store_true", help="Launch KAI trading terminal")
    parser.add_argument("--daemon", action="store_true", help="Run the daemon foreground server")
    parser.add_argument(
        "--remote",
        metavar="WS_URL",
        help="Connect the terminal to a daemon websocket instead of the in-process runtime",
    )
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="Force the in-process runtime path for terminal mode",
    )
    parser.add_argument(
        "--session",
        metavar="NAME",
        help="Attach the terminal to the named daemon/session context",
    )
    parser.add_argument("--name", default=DEFAULT_AGENT, help=f"Agent name (default: {DEFAULT_AGENT})")
    parser.add_argument("--nats-url", default=NATS_URL, help=f"NATS URL (default: {NATS_URL})")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Override log level from config")
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject unsupported flag combinations before runtime setup starts."""
    if args.daemon and args.remote:
        parser.error("--daemon cannot be combined with --remote")
    if args.daemon and args.standalone:
        parser.error("--daemon cannot be combined with --standalone")
    if args.daemon and args.terminal:
        parser.error("--daemon cannot launch the terminal in the same process")
    if args.remote and not args.terminal:
        parser.error("--remote requires --terminal")
    if args.remote and args.standalone:
        parser.error("--remote and --standalone are mutually exclusive")
    if args.session and not args.terminal:
        parser.error("--session requires --terminal")


def _resolve_terminal_session_name(args: argparse.Namespace) -> str:
    """Return the target session name for terminal mode."""
    return args.session or "terminal"


async def _run_daemon(args: argparse.Namespace) -> None:
    """Serve the websocket daemon in the foreground."""
    import uvicorn

    from daemon.server import DEFAULT_DAEMON_HOST, DEFAULT_DAEMON_PORT, create_app

    app = create_app(agent_name=args.name, nats_url=args.nats_url)
    config = uvicorn.Config(
        app=app,
        host=DEFAULT_DAEMON_HOST,
        port=DEFAULT_DAEMON_PORT,
        log_level=(args.log_level or "INFO").lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()


async def _connect_bus(args: argparse.Namespace):
    """Connect to NATS when the selected runtime mode needs it."""
    bus = NatsBus(url=args.nats_url, agent_name=args.name)
    try:
        await bus.connect()
    except Exception as e:
        print(f"Warning: Could not connect to NATS at {args.nats_url}: {e}")
        print("Running without NATS.\n")
        return None
    return bus


async def _run_remote_terminal(args: argparse.Namespace) -> None:
    """Launch the trading terminal against a remote daemon."""
    from tui.terminal import TradingTerminal

    session_name = _resolve_terminal_session_name(args)
    while True:
        session = RemoteSession(
            args.remote,
            session_name=session_name,
        )
        await session.connect()
        try:
            terminal = TradingTerminal(session=session, bus=None)
            result = await terminal.run_async()
        finally:
            await session.close()

        if not isinstance(result, dict) or result.get("action") != "switch_session":
            return
        next_session = result.get("session")
        if not isinstance(next_session, str) or not next_session.strip():
            return
        session_name = next_session.strip()


async def _ensure_local_daemon(args: argparse.Namespace) -> str | None:
    """Start the local daemon on demand and return its websocket URL."""
    try:
        return await asyncio.to_thread(
            ensure_local_daemon_started,
            agent_name=args.name,
            nats_url=args.nats_url,
            log_level=args.log_level,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: Could not auto-start the local daemon: {exc}")
        print("Falling back to --standalone.\n")
        return None


async def _run_local_terminal(args: argparse.Namespace, bus) -> None:
    """Launch the in-process terminal and recreate it when switching sessions."""
    from tui.terminal import TradingTerminal

    session_name = _resolve_terminal_session_name(args)
    while True:
        session = Session(session_name)
        session.touch_index()
        session.attach_runtime(bus=bus, agent_name=args.name)
        sub_agent_manager = session.sub_agent_manager if bus else None
        try:
            terminal = TradingTerminal(session=session, bus=bus)
            result = await terminal.run_async()
        finally:
            if sub_agent_manager:
                await sub_agent_manager.stop_all()

        if not isinstance(result, dict) or result.get("action") != "switch_session":
            return
        next_session = result.get("session")
        if not isinstance(next_session, str) or not next_session.strip():
            return
        session_name = next_session.strip()


async def _run_terminal_mode(args: argparse.Namespace) -> None:
    """Run terminal mode through the daemon by default, with standalone fallback."""
    if args.remote:
        await _run_remote_terminal(args)
        return

    if not args.standalone:
        remote_url = await _ensure_local_daemon(args)
        if remote_url:
            remote_args = argparse.Namespace(**vars(args))
            remote_args.remote = remote_url
            await _run_remote_terminal(remote_args)
            return

    bus = await _connect_bus(args)
    try:
        await _run_local_terminal(args, bus)
    finally:
        if bus:
            await bus.disconnect()


async def main(argv: list[str] | None = None):
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)

    # Apply log level override if specified
    if args.log_level:
        import logging
        import agent_logger
        level = getattr(logging, args.log_level)
        agent_logger.LOG_LEVEL = level
        # Update any existing loggers
        for name in logging.Logger.manager.loggerDict:
            if name.startswith("agent."):
                logging.getLogger(name).setLevel(level)

    if args.daemon:
        await _run_daemon(args)
        return

    if args.terminal:
        await _run_terminal_mode(args)
        return

    # Connect to NATS
    bus = await _connect_bus(args)

    try:
        if args.no_tui:
            session = Session(args.name)
            agent_runner = session.attach_runtime(bus=bus, agent_name=args.name)
            if not bus:
                print("Error: headless mode requires NATS connection.")
                sys.exit(1)
            await run_headless(bus, agent_runner)
        else:
            session = Session(args.name)
            agent_runner = session.attach_runtime(bus=bus, agent_name=args.name)
            await run_tui(bus, agent_runner)
    finally:
        if bus:
            await bus.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
