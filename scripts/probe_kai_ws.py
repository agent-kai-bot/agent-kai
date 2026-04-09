#!/usr/bin/env python3
"""Standalone WebSocket probe for the cloud agent-k.ai market data feed.

The TUI's chart panel relies on a kai-api WebSocket consumer to
deliver live OHLCV updates. When the chart shows stale data, the
question is "what is the WebSocket actually doing?" — is it
connecting, subscribing, receiving frames, idle, throttled, only
pushing on bar-close, or completely dead? This script answers that
without going through the TUI at all.

Usage::

    .venv/bin/python scripts/probe_kai_ws.py                    # BTC 1m, 60s
    .venv/bin/python scripts/probe_kai_ws.py ETH 5m             # ETH 5m, 60s
    .venv/bin/python scripts/probe_kai_ws.py BTC 1m 300         # BTC 1m, 5 min
    .venv/bin/python scripts/probe_kai_ws.py BTC 1m 0           # BTC 1m, run forever
    .venv/bin/python scripts/probe_kai_ws.py --raw BTC 1m       # dump raw JSON
    .venv/bin/python scripts/probe_kai_ws.py --quiet BTC 1m     # only summary

Reads ``AGENT_KAI_API_KEY`` via the same config loader the agent
uses, so the same env / .env / token-file fallbacks work here.

Output (default mode):

  - One line per WS frame with timestamp, op, channel, key fields
  - Periodic summary every 10s with frame count + frame rate
  - Final summary on exit (Ctrl+C or duration timeout) with
    total frames, frames per op type, frame rate, gap statistics

If you see frames every 1-5 seconds during the run, the WebSocket
is healthy and intra-bar updates are flowing. The TUI consumer
should be receiving them — if the chart is stale despite the
probe seeing live frames, the bug is in the TUI consumer code
(filter, handler, error swallowing).

If you only see one ``snapshot`` frame and then silence, the
backend is only emitting on bar-close (or not at all). On a 1m
chart you should still see an event every minute when the bar
closes; on a 1h chart you'd see one every hour. If you see NO
frames after the snapshot, something is wrong upstream.

If you see no frames at all (not even the snapshot), the WS
isn't connecting — check the API key, network, and the cloud
WS URL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

# Make agent imports work when running from project root or scripts/
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── Output helpers ──────────────────────────────────────────


def _ts() -> str:
    """Local time HH:MM:SS.mmm for log line prefixing."""
    now = time.time()
    return f"{time.strftime('%H:%M:%S', time.localtime(now))}.{int((now % 1) * 1000):03d}"


def _print(msg: str) -> None:
    print(f"{_ts()} {msg}", flush=True)


def _summarize_frame(frame: dict) -> str:
    """Render a one-line summary of a parsed JSON frame."""
    op = frame.get("op", "?")
    if op == "event":
        data = frame.get("data") or {}
        sym = data.get("symbol", "?")
        interval = data.get("interval", "?")
        ts_ms = data.get("ts")
        ts_iso = ""
        if ts_ms:
            try:
                ts_iso = time.strftime(
                    "%H:%M:%S", time.gmtime(int(ts_ms) / 1000)
                )
            except Exception:
                pass
        close = data.get("close")
        is_closed = data.get("is_closed", False)
        closed_marker = "CLOSED" if is_closed else "live"
        return (
            f"event {sym} {interval} bar_ts={ts_iso} "
            f"close={close} [{closed_marker}]"
        )
    if op == "snapshot":
        data = frame.get("data") or frame.get("bars") or []
        n = len(data) if isinstance(data, list) else "?"
        return f"snapshot {n} bars (initial)"
    if op == "subscribed":
        return f"subscribed channels={frame.get('channels')}"
    if op == "ping":
        return f"ping ts={frame.get('ts')}"
    if op == "pong":
        return f"pong ts={frame.get('ts')}"
    if op == "error":
        return f"ERROR code={frame.get('code')} msg={frame.get('message')}"
    if op == "welcome":
        return f"welcome (server hello)"
    return f"{op} {json.dumps(frame, default=str)[:120]}"


# ── Probe ───────────────────────────────────────────────────


async def probe(
    symbol: str,
    interval: str,
    duration: float,
    raw: bool,
    quiet: bool,
) -> int:
    """Connect to the cloud WS and dump frames for `duration` seconds.

    duration <= 0 means "run until Ctrl+C". raw=True dumps full JSON
    bodies instead of one-line summaries. quiet=True suppresses
    per-frame output and shows only periodic + final summaries.
    """
    # Load AGENT_KAI_API_KEY via the same path config.py uses, so the
    # .env / token-file fallbacks work here too. Defer import until
    # after sys.path setup so the script works whether you run it
    # from the project root or from scripts/.
    try:
        from config import AGENT_KAI_API_KEY  # noqa: F401
        # config.py auto-loads the env var into os.environ if it
        # found one in .env or the token file, so just read from env.
    except Exception as exc:
        _print(f"FAIL: could not load config.py — {exc}")
        return 2

    api_key = os.environ.get("AGENT_KAI_API_KEY")
    if not api_key:
        _print("FAIL: AGENT_KAI_API_KEY not set in env / .env / AGENT-KAI-API-KEY.txt")
        return 2

    try:
        import aiohttp
    except ImportError:
        _print("FAIL: aiohttp not installed (pip install aiohttp)")
        return 2

    ws_url = "wss://agent-k.ai/v1/ws"
    channel = f"market.{symbol.upper()}.{interval}"

    _print("=" * 60)
    _print(f"probe target: {ws_url}")
    _print(f"channel:      {channel}")
    _print(f"duration:     {'forever' if duration <= 0 else f'{duration:.0f}s'}")
    _print(f"api key:      {api_key[:12]}…{api_key[-4:]}")
    _print("=" * 60)

    op_counts: Counter = Counter()
    event_count = 0
    closed_event_count = 0
    snapshot_count = 0
    error_count = 0
    last_frame_at: float | None = None
    inter_frame_gaps: list[float] = []
    started_at = time.time()

    async def periodic_summary():
        """Print a one-line stats summary every 10 seconds."""
        try:
            while True:
                await asyncio.sleep(10)
                elapsed = time.time() - started_at
                rate = event_count / elapsed if elapsed > 0 else 0.0
                last_age = (
                    f"{time.time() - last_frame_at:.1f}s ago"
                    if last_frame_at
                    else "(none yet)"
                )
                _print(
                    f"[summary] elapsed={elapsed:.0f}s events={event_count} "
                    f"closed={closed_event_count} snapshots={snapshot_count} "
                    f"errors={error_count} rate={rate:.2f}/s last={last_age}"
                )
        except asyncio.CancelledError:
            return

    summary_task = asyncio.create_task(periodic_summary())

    url_with_key = f"{ws_url}?api_key={api_key}"

    timeout_handle = None

    try:
        async with aiohttp.ClientSession() as session:
            try:
                async with session.ws_connect(
                    url_with_key,
                    heartbeat=None,
                    receive_timeout=120,
                ) as ws:
                    _print(f"WS connected to {ws_url}")
                    await ws.send_json(
                        {"op": "subscribe", "channels": [channel]}
                    )
                    _print(f"sent subscribe: {channel}")

                    # If a duration is set, schedule a stop after N seconds
                    stop_after = None
                    if duration > 0:
                        loop = asyncio.get_running_loop()
                        stop_after = loop.create_future()
                        loop.call_later(duration, stop_after.set_result, None)

                    while True:
                        if stop_after is not None and stop_after.done():
                            _print(f"duration {duration:.0f}s reached, closing")
                            await ws.close()
                            break

                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=15)
                        except asyncio.TimeoutError:
                            _print(
                                "[no frames in 15s] WS idle — backend not pushing "
                                "OR connection wedged"
                            )
                            continue

                        if msg.type == aiohttp.WSMsgType.CLOSED:
                            _print(f"WS closed by server (code={ws.close_code})")
                            break
                        if msg.type == aiohttp.WSMsgType.ERROR:
                            _print(f"WS error: {ws.exception()}")
                            error_count += 1
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue

                        # Parse + tally + render
                        try:
                            frame = json.loads(msg.data)
                        except json.JSONDecodeError:
                            _print(f"[unparseable frame] {msg.data[:120]}")
                            continue

                        op = frame.get("op", "?")
                        op_counts[op] += 1
                        now = time.time()
                        if last_frame_at is not None:
                            inter_frame_gaps.append(now - last_frame_at)
                        last_frame_at = now

                        if op == "event":
                            event_count += 1
                            data = frame.get("data") or {}
                            if data.get("is_closed"):
                                closed_event_count += 1
                        elif op == "snapshot":
                            snapshot_count += 1
                        elif op == "error":
                            error_count += 1
                        elif op == "ping":
                            # Respond to keepalive so the connection stays open
                            try:
                                await ws.send_json(
                                    {
                                        "op": "pong",
                                        "ts": frame.get("ts") or int(time.time() * 1000),
                                    }
                                )
                            except Exception as exc:
                                _print(f"failed to respond to ping: {exc}")

                        if not quiet:
                            if raw:
                                _print(json.dumps(frame, default=str))
                            else:
                                _print(_summarize_frame(frame))
            except aiohttp.ClientConnectorError as exc:
                _print(f"WS connect failed: {exc}")
                return 3
            except asyncio.TimeoutError:
                _print("WS connect timed out")
                return 3
    except KeyboardInterrupt:
        _print("interrupted by user")
    finally:
        summary_task.cancel()
        try:
            await summary_task
        except (asyncio.CancelledError, Exception):
            pass

    # ── Final summary ──
    elapsed = time.time() - started_at
    rate = event_count / elapsed if elapsed > 0 else 0.0
    print()
    _print("=" * 60)
    _print("FINAL SUMMARY")
    _print(f"  duration:        {elapsed:.1f}s")
    _print(f"  total frames:    {sum(op_counts.values())}")
    _print(f"  events:          {event_count} ({rate:.2f}/s)")
    _print(f"    closed bars:   {closed_event_count}")
    _print(f"    live updates:  {event_count - closed_event_count}")
    _print(f"  snapshots:       {snapshot_count}")
    _print(f"  errors:          {error_count}")
    _print(f"  per-op counts:   {dict(op_counts)}")
    if inter_frame_gaps:
        gaps = sorted(inter_frame_gaps)
        median = gaps[len(gaps) // 2]
        max_gap = max(gaps)
        _print(f"  inter-frame gaps: median={median:.2f}s max={max_gap:.2f}s")
    _print("=" * 60)
    print()
    _print("interpretation:")
    if event_count == 0 and snapshot_count == 0:
        _print("  ✗ NO FRAMES at all — WS connect failed or auth rejected")
    elif event_count == 0:
        _print(
            "  ⚠ snapshot received but ZERO event frames — backend isn't pushing updates"
        )
        _print("    on this channel during the probe window. Either the bar didn't close,")
        _print(
            "    OR the backend doesn't emit intra-bar updates and you'll only see"
        )
        _print("    a frame when the bar closes (next event in up to 1× interval)")
    elif event_count - closed_event_count == 0:
        _print("  ⚠ ALL events were closed bars — no intra-bar updates")
        _print(
            "    backend only emits on bar-close. The chart is correct at "
            "bar boundaries"
        )
        _print(
            "    but stale within a bar. The TUI's periodic REST refresh "
            "(20s) covers"
        )
        _print("    the gap. Live tick streaming would require a backend change.")
    else:
        _print(
            f"  ✓ {event_count - closed_event_count} live updates received — "
            "WS is healthy"
        )
        _print(
            "    if the TUI chart is still stale despite this, the bug is in"
        )
        _print(
            "    the TUI consumer (filter, handler, error swallow). Check"
        )
        _print(
            "    tui/terminal.py:_run_kai_api_consumer and the symbol/interval"
        )
        _print("    filter at lines ~529-532")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Standalone WS probe for the kai-api market data stream"
    )
    parser.add_argument("symbol", nargs="?", default="BTC", help="Symbol (default BTC)")
    parser.add_argument("interval", nargs="?", default="1m", help="Interval (default 1m)")
    parser.add_argument(
        "duration",
        nargs="?",
        default="60",
        help="Run duration in seconds (0 = forever, default 60)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Dump full JSON frames instead of summaries",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-frame output, only show summaries",
    )
    args = parser.parse_args()

    try:
        duration = float(args.duration)
    except ValueError:
        print(f"invalid duration: {args.duration!r}", file=sys.stderr)
        return 2

    try:
        return asyncio.run(
            probe(args.symbol, args.interval, duration, args.raw, args.quiet)
        )
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
