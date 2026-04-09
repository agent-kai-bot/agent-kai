#!/usr/bin/env python3
"""End-to-end verification of the signal pipeline.

Tests the full path without needing the signal scanner running:

1. SignalConsumer offline tests (buffer, query, filters)
2. NATS publish → SignalConsumer subscribe (live if NATS is reachable)
3. get_signals tool returns the buffered signals
4. Multiple signals + filtering
5. Ring buffer overflow (oldest evicted)

If NATS is not running on localhost:4222, the live tests are skipped
with a warning (offline tests still run).

Run: .venv/bin/python test_signal_pipeline.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.signal_consumer import Signal, SignalConsumer
from agent.crypto_tools import create_get_signals_tool


def _h(label: str) -> None:
    print("\n" + "=" * 60)
    print(f"  {label}")
    print("=" * 60)


failures: list[str] = []


def fail(msg: str) -> None:
    print(f"  FAIL: {msg}")
    failures.append(msg)


def test_offline() -> None:
    """Tests that don't require NATS."""

    _h("1. Empty consumer")
    c = SignalConsumer(max_signals=10)
    assert c.count == 0
    assert c.query() == []
    print("  empty OK")

    _h("2. Manual add + query")
    sig = c.add_manual("clucmay02", "BTC", "BUY", price=70900.0, bb_percent=0.032)
    assert c.count == 1
    results = c.query()
    assert len(results) == 1
    assert results[0]["symbol"] == "BTC"
    assert results[0]["signal_type"] == "BUY"
    assert results[0]["strategy"] == "clucmay02"
    assert results[0]["price"] == 70900.0
    assert results[0]["bb_percent"] == 0.032  # details flattened
    print(f"  add+query OK: {results[0]['strategy']} {results[0]['signal_type']} {results[0]['symbol']}")

    _h("3. Multiple signals + filter by symbol")
    c.add_manual("double_top", "ETH", "SELL", price=2450.0)
    c.add_manual("clucmay02", "SOL", "BUY", price=145.0)
    c.add_manual("ewo", "BTC", "BUY", price=71000.0)
    assert c.count == 4

    btc = c.query(symbol="BTC")
    assert len(btc) == 2
    assert all(r["symbol"] == "BTC" for r in btc)
    print(f"  symbol filter OK: {len(btc)} BTC signals")

    eth = c.query(symbol="ETH")
    assert len(eth) == 1
    print(f"  ETH filter OK: {len(eth)}")

    _h("4. Filter by strategy")
    cluc = c.query(strategy="clucmay02")
    assert len(cluc) == 2  # BTC + SOL
    print(f"  strategy filter OK: {len(cluc)} clucmay02 signals")

    _h("5. Filter by signal_type")
    sells = c.query(signal_type="SELL")
    assert len(sells) == 1
    assert sells[0]["symbol"] == "ETH"
    print(f"  signal_type filter OK: {len(sells)} SELL signals")

    _h("6. Combined filters")
    combined = c.query(symbol="BTC", strategy="clucmay02")
    assert len(combined) == 1
    assert combined[0]["price"] == 70900.0
    print(f"  combined filter OK: {combined[0]['strategy']} {combined[0]['symbol']}")

    _h("7. Limit")
    limited = c.query(limit=2)
    assert len(limited) == 2
    # Newest first
    assert limited[0]["price"] == 71000.0  # ewo BTC was last added
    print(f"  limit OK: returned {len(limited)} of {c.count}")

    _h("8. Ring buffer overflow")
    small = SignalConsumer(max_signals=3)
    small.add_manual("a", "X", "BUY", price=1)
    small.add_manual("b", "Y", "BUY", price=2)
    small.add_manual("c", "Z", "BUY", price=3)
    small.add_manual("d", "W", "BUY", price=4)  # evicts 'a'/'X'
    assert small.count == 3
    all_sigs = small.query(limit=10)
    symbols = [s["symbol"] for s in all_sigs]
    if "X" in symbols:
        fail("oldest signal should have been evicted")
    assert "W" in symbols
    print(f"  overflow OK: buffer has {symbols}")

    _h("9. get_signals tool")
    tool = create_get_signals_tool(c)
    raw = tool.invoke({"symbol": "BTC", "limit": 5})
    parsed = json.loads(raw)
    assert parsed["success"] is True
    assert parsed["count"] == 2
    assert parsed["buffer_total"] == 4
    print(f"  tool OK: {parsed['count']} results, buffer_total={parsed['buffer_total']}")

    _h("10. get_signals tool with no consumer")
    disabled_tool = create_get_signals_tool(None)
    raw2 = disabled_tool.invoke({})
    parsed2 = json.loads(raw2)
    assert parsed2["success"] is False
    print(f"  disabled tool OK: {parsed2['error']}")

    _h("11. on_signal callback fires")
    callback_log = []
    c2 = SignalConsumer(max_signals=5)
    c2.on_signal = lambda sig: callback_log.append(sig.symbol)
    c2.add_manual("test", "DOGE", "BUY", price=0.15)
    assert callback_log == ["DOGE"]
    print(f"  callback OK: {callback_log}")

    _h("12. Signal.summary()")
    sig = Signal(source="test", strategy="clucmay02", symbol="BTC", signal_type="BUY", price=70900.0)
    summary = sig.summary()
    assert "clucmay02" in summary
    assert "BUY" in summary
    assert "BTC" in summary
    print(f"  summary OK: {summary}")


async def test_nats_live() -> None:
    """Publish through real NATS and verify the consumer picks it up.

    CRITICAL: this test must NEVER publish to the production
    ``signals.>`` or ``ai.analysis.completed`` subjects on the
    shared NATS broker. Doing so floods every other consumer on
    the same broker with fake signals — most importantly, the
    running TUI's ``SignalConsumer`` will treat the test prices
    as real scanner output and surface them as alerts. We learned
    this the hard way: the user saw "BUY BTC $70,950" and "SELL
    SOL $145" in their alerts panel during a session — those are
    the EXACT prices hardcoded below, leaked from this test
    every time it ran.

    Fix: publish to a unique throwaway prefix
    (``test_pipeline_signals.>`` and ``test_pipeline_ai.analysis.completed``)
    that the production consumer does NOT subscribe to. The test's
    own local subscription matches the same prefix so the round-
    trip still works for verification — but no other subscriber
    on the broker (the TUI included) sees the test traffic.
    """
    _h("13. NATS live publish → subscribe")

    try:
        import nats as nats_mod
    except ImportError:
        print("  SKIP: nats-py not installed")
        return

    nats_url = "nats://localhost:4222"
    try:
        nc = await nats_mod.connect(nats_url)
    except Exception as e:
        print(f"  SKIP: NATS not reachable at {nats_url}: {e}")
        return

    consumer = SignalConsumer(max_signals=50)

    # We need to simulate what the bus.subscribe does. The actual NatsBus
    # wraps subscriptions, but for this test we subscribe directly and
    # route to the consumer's handler.
    received = asyncio.Event()
    original_ingest = consumer._ingest

    def _tracking_ingest(sig):
        original_ingest(sig)
        received.set()

    consumer._ingest = _tracking_ingest

    async def nats_handler(msg):
        subject = msg.subject
        payload = json.loads(msg.data.decode())
        await consumer._handle_signal(subject, payload)

    # Subscribe to the THROWAWAY test prefix so we don't pollute
    # any other subscribers on this broker (notably a running TUI).
    sub = await nc.subscribe("test_pipeline_signals.>", cb=nats_handler)

    # Publish a test signal — to the throwaway prefix. The
    # consumer's _handle_signal parses parts[1]/parts[2] for
    # strategy/symbol so any subject prefix works as long as the
    # remaining segments are still {strategy}.{symbol}.
    test_payload = {
        "source": "signal-scanner",
        "strategy": "clucmay02",
        "symbol": "BTC",
        "signal_type": "BUY",
        "price": 70950.0,
        "timestamp": "2026-04-09T01:00:00Z",
        "bb_price_percent": 0.028,
    }
    await nc.publish("test_pipeline_signals.clucmay02.BTC", json.dumps(test_payload).encode())
    await nc.flush()

    # Wait for delivery
    try:
        await asyncio.wait_for(received.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        fail("NATS signal not received within 5s")
        await nc.close()
        return

    # Verify
    assert consumer.count == 1
    result = consumer.query(symbol="BTC")
    assert len(result) == 1
    assert result[0]["price"] == 70950.0
    assert result[0]["strategy"] == "clucmay02"
    assert result[0]["bb_price_percent"] == 0.028
    print(f"  NATS live OK: received {result[0]['strategy']} {result[0]['signal_type']} {result[0]['symbol']} @ ${result[0]['price']}")

    _h("14. NATS multiple signals + tool query")
    received.clear()
    for sym in ["ETH", "SOL", "LINK"]:
        payload = {
            "source": "signal-scanner",
            "strategy": "double_top",
            "symbol": sym,
            "signal_type": "SELL",
            "price": {"ETH": 2450.0, "SOL": 145.0, "LINK": 14.5}[sym],
            "timestamp": "2026-04-09T01:05:00Z",
        }
        # Throwaway test prefix — see the comment in test_nats_live
        # for why this isn't `signals.double_top.{sym}`.
        await nc.publish(f"test_pipeline_signals.double_top.{sym}", json.dumps(payload).encode())
    await nc.flush()
    await asyncio.sleep(0.5)

    assert consumer.count == 4  # 1 BTC + 3 new
    tool = create_get_signals_tool(consumer)
    raw = tool.invoke({"strategy": "double_top", "limit": 10})
    parsed = json.loads(raw)
    assert parsed["count"] == 3
    print(f"  multi-signal OK: {parsed['count']} double_top signals in buffer of {parsed['buffer_total']}")

    _h("15. AI analysis event")
    received.clear()

    async def ai_handler(msg):
        subject = msg.subject
        payload = json.loads(msg.data.decode())
        await consumer._handle_ai_analysis(subject, payload)

    # Throwaway test subject — same isolation reason as the
    # signals.> publishes above. The production consumer subscribes
    # to ai.analysis.completed, NOT test_pipeline_ai.analysis.completed,
    # so the running TUI won't see this test ANALYSIS event.
    sub2 = await nc.subscribe("test_pipeline_ai.analysis.completed", cb=ai_handler)
    ai_payload = {
        "event": "analysis_completed",
        "result_id": "abc123",
        "symbol": "BTC",
        "use_case": "daily_analysis",
        "timestamp": "2026-04-09T02:00:00Z",
    }
    await nc.publish("test_pipeline_ai.analysis.completed", json.dumps(ai_payload).encode())
    await nc.flush()
    await asyncio.sleep(0.5)

    analysis = consumer.query(signal_type="ANALYSIS")
    assert len(analysis) == 1
    assert analysis[0]["symbol"] == "BTC"
    assert analysis[0]["source"] == "ai-token-analyzer"
    print(f"  AI analysis event OK: {analysis[0]['strategy']} {analysis[0]['symbol']}")

    await sub.unsubscribe()
    await sub2.unsubscribe()
    await nc.close()
    print(f"\n  Total signals in buffer: {consumer.count}")


def main() -> int:
    test_offline()

    asyncio.run(test_nats_live())

    print("\n" + "=" * 60)
    if failures:
        print(f"  {len(failures)} FAILURE(S)")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("  ALL SIGNAL PIPELINE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
