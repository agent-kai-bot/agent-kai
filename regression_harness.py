#!/usr/bin/env python3
"""Regression harness for the BTC analysis agent flow."""

import argparse
import asyncio
import json
import sys
import time

from agent.core import AgentRunner
from agent.crypto_tools import calculate_indicator, query_ohlcv
from agent.sub_agents import SubAgent
from agent.tools import create_tools

ANALYZE_TASK = (
    "Run a full technical analysis on BTC 1m timeframe. "
    "Include RSI, MACD, Bollinger Bands, and key support/resistance levels."
)


def _status_from_text(text: str) -> bool:
    """Treat empty/error-like text as a regression failure."""
    stripped = text.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    return not (
        lowered.startswith("error:")
        or "traceback" in lowered
        or "agent stopped due to" in lowered
    )


def _result(name: str, ok: bool, elapsed_s: float, **extra) -> dict:
    """Normalize a harness result entry."""
    return {
        "name": name,
        "ok": ok,
        "elapsed_s": round(elapsed_s, 2),
        **extra,
    }


def _analysis_quality_ok(text: str) -> bool:
    """Check that an analysis response covers the core BTC TA topics.

    Args:
        text: Final response text from an agent path.

    Returns:
        True when the response mentions the expected technical-analysis components.
    """
    lowered = text.lower()
    required_terms = ("rsi", "macd", "bollinger")
    level_terms = ("support", "resistance")
    return all(term in lowered for term in required_terms) and any(
        term in lowered for term in level_terms
    )


def run_tool_checks() -> list[dict]:
    """Smoke-test the tool layer used by the analyst flow."""
    results = []

    start = time.time()
    ohlcv = query_ohlcv.invoke({"symbol": "BTC", "interval": "1m", "limit": 20})
    results.append(
        _result(
            "tool.query_ohlcv",
            _status_from_text(ohlcv) and "BTC 1m" in ohlcv,
            time.time() - start,
            preview=ohlcv[:300],
        )
    )

    start = time.time()
    rsi = calculate_indicator.invoke(
        {"symbol": "BTC", "indicator": "RSI", "period": 14, "interval": "1m", "limit": 200}
    )
    results.append(
        _result(
            "tool.calculate_indicator.rsi",
            _status_from_text(rsi) and "RSI(14)" in rsi,
            time.time() - start,
            preview=rsi[:300],
        )
    )

    start = time.time()
    macd = calculate_indicator.invoke(
        {"symbol": "BTC", "indicator": "MACD", "period": 14, "interval": "1m", "limit": 200}
    )
    results.append(
        _result(
            "tool.calculate_indicator.macd",
            _status_from_text(macd) and "MACD" in macd,
            time.time() - start,
            preview=macd[:300],
        )
    )

    return results


async def run_agent_runner(agent_name: str, task: str, result_name: str) -> dict:
    """Run a task through AgentRunner and capture the final output."""
    start = time.time()
    runner = AgentRunner(
        tools=create_tools(bus=None, sub_agent_manager=None),
        bus=None,
        agent_name=agent_name,
    )

    final_text = ""
    token_text = ""
    event_types = []

    async for event in runner.run(task):
        event_types.append(event["type"])
        if event["type"] == "token":
            token_text += event["data"]
        elif event["type"] == "final":
            final_text = event["data"]

    response = final_text or token_text
    return _result(
        result_name,
        _status_from_text(response) and _analysis_quality_ok(response),
        time.time() - start,
        event_types=event_types,
        output_len=len(response),
        preview=response[:600],
    )


async def run_sub_agent(task: str) -> dict:
    """Run a task through the analyst SubAgent directly."""
    start = time.time()
    agent = SubAgent("analyst", bus=None)
    output = await agent.run_once(task)
    return _result(
        "subagent.analyst.run_once",
        _status_from_text(output) and _analysis_quality_ok(output),
        time.time() - start,
        output_len=len(output),
        preview=output[:600],
    )


async def run_harness() -> list[dict]:
    """Execute all regression scenarios."""
    results = []
    results.extend(run_tool_checks())
    results.append(await run_agent_runner("analyst", ANALYZE_TASK, "agent_runner.analyst"))
    results.append(
        await run_agent_runner(
            "nano",
            f"[For analyst]: {ANALYZE_TASK}",
            "agent_runner.nano_for_analyst",
        )
    )
    results.append(await run_sub_agent(ANALYZE_TASK))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Regression harness for /analyze BTC")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a text summary")
    args = parser.parse_args()

    results = asyncio.run(run_harness())
    failed = [r for r in results if not r["ok"]]

    if args.json:
        print(json.dumps({"results": results, "passed": not failed}, indent=2))
    else:
        for item in results:
            status = "PASS" if item["ok"] else "FAIL"
            print(f"[{status}] {item['name']} ({item['elapsed_s']}s)")
            print(f"  preview: {item.get('preview', '')!r}")
            if item.get("event_types"):
                print(f"  events: {item['event_types']}")
        print()
        print(f"overall: {'PASS' if not failed else 'FAIL'}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
