#!/usr/bin/env python3
"""Entry point for the local AI agent system."""

import argparse
import asyncio
import sys

from agent.core import AgentRunner
from agent.sub_agents import SubAgentManager
from agent.tools import create_tools
from config import DEFAULT_AGENT, NATS_URL
from nats_bus.bus import NatsBus
from tui.app import AgentTUI


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


async def main():
    parser = argparse.ArgumentParser(description="Local AI Agent")
    parser.add_argument("--no-tui", action="store_true", help="Run headless (NATS only)")
    parser.add_argument("--terminal", action="store_true", help="Launch KAI trading terminal")
    parser.add_argument("--name", default=DEFAULT_AGENT, help=f"Agent name (default: {DEFAULT_AGENT})")
    parser.add_argument("--nats-url", default=NATS_URL, help=f"NATS URL (default: {NATS_URL})")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Override log level from config")
    args = parser.parse_args()

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

    # Connect to NATS
    bus = NatsBus(url=args.nats_url, agent_name=args.name)
    try:
        await bus.connect()
    except Exception as e:
        print(f"Warning: Could not connect to NATS at {args.nats_url}: {e}")
        print("Running without NATS.\n")
        bus = None

    # Create sub-agent manager and tools
    sub_agent_manager = SubAgentManager(bus) if bus else None
    tools = create_tools(bus, sub_agent_manager)
    agent_runner = AgentRunner(tools=tools, bus=bus, agent_name=args.name)

    try:
        if args.terminal:
            # Start data API server as subprocess
            import subprocess as _sp
            api_proc = _sp.Popen(
                [sys.executable, "-m", "data_api.server"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
            await asyncio.sleep(2)  # Let API start
            try:
                from tui.terminal import TradingTerminal
                terminal = TradingTerminal(agent_runner=agent_runner, bus=bus)
                terminal._sub_agent_manager = sub_agent_manager
                await terminal.run_async()
            finally:
                api_proc.terminate()
                api_proc.wait(timeout=5)
        elif args.no_tui:
            if not bus:
                print("Error: headless mode requires NATS connection.")
                sys.exit(1)
            await run_headless(bus, agent_runner)
        else:
            await run_tui(bus, agent_runner)
    finally:
        if sub_agent_manager:
            await sub_agent_manager.stop_all()
        if bus:
            await bus.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
