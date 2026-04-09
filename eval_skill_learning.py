#!/usr/bin/env python3
"""End-to-end eval harness for the skill-learning loop.

This harness drives real sub-agents against the configured
``kai-smart`` endpoint (see agent-config.json) through a scripted
scenario that should exercise every piece of the self-improvement
pipeline:

1. A target sub-agent (default: analyst) receives a hard analysis
   task that should need 3+ tool calls to answer.
2. A ``ToolCallRecorder`` on the sub-agent captures every tool
   invocation, and the ``SessionRecord`` is saved on the agent.
3. The mentor sub-agent receives a reflection bundle built from
   that session and is asked to decide create / patch / no_skill.
4. The mentor's reply is parsed, and if it drafts a create or
   patch, the harness applies it directly to the target agent's
   ``SkillStore``.
5. The full transcript (task, tool calls, mentor reply, applied
   outcome, pre/post skill catalog) is saved as a single JSON file
   under ``eval_results/skill_learning/`` with a timestamp so both
   the user and Claude can review later.

Running the harness:

    .venv/bin/python eval_skill_learning.py                    # default scenario
    .venv/bin/python eval_skill_learning.py --scenario bb      # specific scenario
    .venv/bin/python eval_skill_learning.py --skip-mentor      # just the target run
    .venv/bin/python eval_skill_learning.py --list-scenarios   # see what's available

No NATS server is required — the harness uses ``SubAgent.run_once``
which bypasses the bus and runs the executor directly. LLM endpoints
are the same ones the TUI uses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Ensure project root is on path so `agent.*` imports resolve even
# when the harness is invoked from a different cwd.
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.learning import parse_mentor_reply  # noqa: E402
from agent.skills_store import SkillStore  # noqa: E402
from agent.sub_agents import SubAgent  # noqa: E402
from config import get_skills_dir  # noqa: E402


RESULTS_DIR = PROJECT_ROOT / "eval_results" / "skill_learning"
FIXTURES_DIR = PROJECT_ROOT / "eval_fixtures"


# ── Fixture snapshot / restore ──────────────────────────────
#
# The paper trading engine persists its state to
# ``workspaces/trader/portfolio.json`` and holds a module-level
# singleton at ``data_api.paper_trading.portfolio``. To give a
# scenario deterministic portfolio state WITHOUT corrupting the
# user's real paper trades we:
#
# 1. Move the real portfolio.json aside to ``portfolio.json.eval-bak``
# 2. Copy the fixture into portfolio.json
# 3. Re-instantiate the singleton so the next ``get_positions`` call
#    reads the fixture state
# 4. (After the run, in a finally block) restore the backup and
#    re-instantiate the singleton one more time so everything is
#    back to the user's real state.
#
# If anything goes wrong mid-run the backup is ALWAYS restored —
# the try/finally around the scenario guarantees it.


def _portfolio_path() -> Path:
    from data_api.paper_trading import PERSIST_PATH
    return Path(PERSIST_PATH)


def _reload_portfolio_singleton() -> None:
    """Force the paper_trading singleton to re-read portfolio.json."""
    import data_api.paper_trading as pt
    pt.portfolio = pt.PaperPortfolio()


def _apply_fixture(fixture_name: str) -> Path | None:
    """Snapshot portfolio.json and overlay the fixture.

    Returns the backup path (or None if there was nothing to back up).
    Raises FileNotFoundError if the fixture file doesn't exist.
    """
    fixture_path = FIXTURES_DIR / f"{fixture_name}.json"
    if not fixture_path.is_file():
        raise FileNotFoundError(f"Fixture not found: {fixture_path}")

    portfolio_path = _portfolio_path()
    backup: Path | None = None
    if portfolio_path.exists():
        backup = portfolio_path.with_suffix(".json.eval-bak")
        shutil.copy2(portfolio_path, backup)

    portfolio_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fixture_path, portfolio_path)
    _reload_portfolio_singleton()
    return backup


def _restore_fixture(backup: Path | None) -> None:
    """Restore the pre-fixture portfolio.json and reload the singleton.

    Swallows errors so a restore failure never masks a scenario error —
    but the user's real state is high-value, so any failure is logged
    to stderr so they can recover manually from the .eval-bak file.
    """
    portfolio_path = _portfolio_path()
    try:
        if backup and backup.exists():
            shutil.move(str(backup), str(portfolio_path))
        elif portfolio_path.exists():
            # No backup meant no real state existed before — remove the
            # fixture so we leave the filesystem as we found it.
            portfolio_path.unlink()
    except Exception as exc:
        print(
            f"[eval] WARNING: fixture restore failed: {exc}. "
            f"Your original portfolio.json may be at {backup}.",
            file=sys.stderr,
        )
    _reload_portfolio_singleton()


# ── Scenarios ───────────────────────────────────────────────
#
# Each scenario is a (target_agent, task_prompt, expected_min_tool_calls)
# tuple. The task prompts are deliberately chosen so that answering
# honestly requires the agent to use multiple crypto/TA tools. A
# non-cheating agent should exceed NUDGE_THRESHOLD (3) on each.


@dataclass
class Scenario:
    key: str
    target_agent: str
    task: str
    expected_min_tool_calls: int
    description: str
    # Optional name of a fixture under ``eval_fixtures/`` (without the
    # ``.json`` extension). When set, the eval harness snapshots the
    # trader's portfolio.json, overlays the fixture so ``get_positions``
    # returns deterministic state for the run, then restores the
    # snapshot in a ``finally`` block.
    fixture: str | None = None


SCENARIOS: list[Scenario] = [
    # ── Baseline: sanity checks that the mentor correctly returns
    # ``no_skill`` when the session is already covered by a seed
    # skill. These should almost always NOT create new skills. If
    # any of them creates a skill, that's a sign the mentor is over-
    # eager or the seed skill is mis-named.
    Scenario(
        key="bb",
        target_agent="analyst",
        task=(
            "Run a full Bollinger-Band squeeze analysis on BTC. "
            "Fetch 1h and 4h OHLCV, compute BBANDS on 1h, compute the "
            "50 EMA on 4h for regime, and tell me whether a squeeze is "
            "present and in which direction it's likely to break. "
            "Show the actual BB width number you computed and the 4h "
            "EMA context. If you identify a valid setup, state the "
            "entry, stop, and first target levels."
        ),
        expected_min_tool_calls=4,
        description="[baseline] BB squeeze analysis — should match bb-squeeze-breakout skill",
    ),
    Scenario(
        key="divergence",
        target_agent="analyst",
        task=(
            "Check SOL for an RSI divergence on the 1h chart. Pull the "
            "last 60 1h bars, compute RSI(14), identify the last two "
            "swing highs OR lows, and report whether a regular bearish "
            "or bullish divergence is present. Cross-check with the 4h "
            "trend via the 50 EMA. Be specific about the exact swing "
            "points you compared and the RSI values at each."
        ),
        expected_min_tool_calls=4,
        description="[baseline] RSI divergence on SOL — should match rsi-divergence-hunt skill",
    ),
    Scenario(
        key="regime",
        target_agent="analyst",
        task=(
            "Classify the current 1h regime for ETH using a 5/10/20/50 "
            "EMA ribbon. Fetch each EMA, compute the ATR for the "
            "compression check, and tell me whether the regime is "
            "bullish stacked, bearish stacked, compressed, or tangled. "
            "State which skill categories apply at this regime."
        ),
        expected_min_tool_calls=5,
        description="[baseline] Regime classification on ETH — should match moving-average-ribbon-stack skill",
    ),
    Scenario(
        key="risk",
        target_agent="risk-manager",
        task=(
            "A trader wants to open a 0.5 BTC long position on BTC at "
            "the current market price. Compute the correct position "
            "size per the 1% rule using ATR(14) on 1h for the stop "
            "distance (1.5x ATR), check the single-position cap, and "
            "the total heat cap. Decide approve / approve_modified / "
            "reject with the exact numbers and reasoning."
        ),
        expected_min_tool_calls=3,
        description="[baseline] Risk sizing for BTC trade — should match atr-stop-sizing skill",
    ),

    # ── Learning scenarios: these are deliberately NOT covered by
    # any seed skill, so the mentor should return ``create`` on a
    # well-executed session. Running ``--all`` through these is the
    # intended way to build a foundation library for a fresh install.

    # ── Analyst (6) ─────────────────────────────────────────
    Scenario(
        key="momentum-leaders",
        target_agent="analyst",
        task=(
            "Among BTC, ETH, SOL, ADA, LINK, and AVAX, determine "
            "which has the strongest 24-hour momentum right now and "
            "whether the move is still accelerating or decelerating. "
            "For each symbol: fetch at least 30 bars of 1h OHLCV, "
            "compute the percent change over the last 24 bars, and "
            "compute RSI(14) on the most recent close. Then compare "
            "the last 6-bar return to the prior 6-bar return to "
            "measure acceleration. Rank all six symbols by momentum "
            "and give a final pick with a one-sentence rationale."
        ),
        expected_min_tool_calls=6,
        description="Relative strength leaderboard across 6 large caps — multi-symbol reasoning",
    ),
    Scenario(
        key="mtf-confluence",
        target_agent="analyst",
        task=(
            "Find a trade on BTC where the 15m, 1h, and 4h timeframes "
            "all agree on direction. For each timeframe, compute the "
            "20 EMA and RSI(14), then determine whether price is "
            "above or below the 20 EMA and whether RSI is >50 or "
            "<50. A timeframe 'votes bullish' if price > 20 EMA AND "
            "RSI > 50 (mirror for bearish). Report the confluence "
            "score (0-3) and the agreed direction. If all three "
            "agree, recommend entry, stop, and first target based "
            "on the 1h ATR(14)."
        ),
        expected_min_tool_calls=7,
        description="Multi-timeframe confluence scoring for BTC — timeframe-stacking logic",
    ),
    Scenario(
        key="breakout-validation",
        target_agent="analyst",
        task=(
            "BTC just traded through its 24-hour high. Decide "
            "whether this is a genuine breakout or a fakeout using "
            "four criteria: (1) the breakout bar's volume vs the "
            "20-bar volume average, (2) the breakout bar's range "
            "relative to the current 1h ATR, (3) whether price "
            "closed above the prior high or just wicked through, "
            "and (4) the follow-through direction in the next 1-2 "
            "bars. Report the specific numbers for each criterion "
            "and give a verdict: enter, wait for pullback, or skip."
        ),
        expected_min_tool_calls=5,
        description="Breakout vs fakeout decision on BTC — the 'should I chase' gate",
    ),
    Scenario(
        key="dead-cat-bounce",
        target_agent="analyst",
        task=(
            "SOL has had a significant selloff recently. Decide "
            "whether the current bounce is a dead-cat bounce or a "
            "legitimate reversal. Fetch 1h and 4h OHLCV, compute "
            "RSI(14) on both, compute volume on the bounce vs the "
            "pre-bounce average, check whether price has broken "
            "above the consolidation range formed during the "
            "selloff, and verify the bounce is not already "
            "extended (pullback from bounce >38% is weak). "
            "Conclude with 'dead cat' vs 'legit reversal' and cite "
            "the specific indicators that drove the call."
        ),
        expected_min_tool_calls=5,
        description="Dead cat vs reversal decision on SOL — bounce-validity logic",
    ),
    Scenario(
        key="pullback-entry",
        target_agent="analyst",
        task=(
            "ETH appears to be in an uptrend. Find the most recent "
            "tradeable pullback with these criteria: (1) retrace "
            "depth no more than 38% of the last impulse, (2) no "
            "more than 10 bars of pullback, (3) price holding above "
            "the 1h 20 EMA, (4) volume declining during the "
            "pullback (a good pullback is low-volume). Compute the "
            "actual retrace depth, bar count, EMA check, and "
            "volume comparison. If all four criteria pass, "
            "recommend entry, stop, and first target with specific "
            "prices."
        ),
        expected_min_tool_calls=5,
        description="Healthy pullback entry in ETH uptrend — shallow-pullback criteria",
    ),
    Scenario(
        key="range-vs-trend",
        target_agent="analyst",
        task=(
            "Classify BTC's current 4h state as 'ranging' or "
            "'trending'. Use three independent indicators: (1) "
            "ATR(14) as a percentage of price (high = trending), "
            "(2) BB width (upper-lower)/middle (wide = trending), "
            "and (3) the slope of the 50 EMA measured over the "
            "last 20 bars (steep = trending). Give the specific "
            "numeric values for each and a final classification. "
            "If trending, state the direction; if ranging, state "
            "the range high and low."
        ),
        expected_min_tool_calls=4,
        description="Range vs trend classification on BTC — ATR+BB+EMA slope regime detector",
    ),

    # ── Trader (3) ──────────────────────────────────────────
    Scenario(
        key="partial-exit-ladder",
        target_agent="trader",
        task=(
            "First call get_positions to see what I currently hold. "
            "I am long BTC and it is showing a gain. Using the "
            "actual entry price and quantity from get_positions, "
            "plus the current BTC price and 1h ATR(14) from the "
            "crypto tools, design a three-rung partial-exit ladder "
            "with specific prices, quantities, and a corresponding "
            "stop-loss adjustment after each rung fills. First rung "
            "takes 25% off at 1R profit (R = distance from entry to "
            "initial stop), second rung 35% at 2R, third rung 40% "
            "at 3R. After the first fill the stop moves to "
            "break-even; after the second fill the stop trails "
            "1.5 ATR behind the high. Show all the numbers."
        ),
        expected_min_tool_calls=4,
        description="Multi-rung partial profit plan for a winning BTC long",
        fixture="portfolio_btc_winner",
    ),
    Scenario(
        key="chase-vs-wait",
        target_agent="trader",
        task=(
            "BTC just pumped approximately 3% in the last 30 "
            "minutes while I was away from the screen. Decide: "
            "chase at market, place a limit order for a pullback, "
            "or skip entirely. Check: (1) 15m and 1h ATR — is a "
            "3% move more than 2 ATR (= extended)? (2) 1h RSI(14) "
            "— is it above 70? (3) the last 10 1h bars — where did "
            "the last similar-sized pump retrace to? (4) price "
            "distance from the 1h 20 EMA in ATR units. Return a "
            "specific decision with reasoning and, if 'wait', the "
            "exact pullback limit price."
        ),
        expected_min_tool_calls=5,
        description="Chase vs wait vs skip decision for a BTC pump — FOMO-killer skill",
    ),
    Scenario(
        key="conviction-allocation",
        target_agent="trader",
        task=(
            "I have $5,000 USD to deploy across three positions "
            "with varying conviction: BTC (HIGH), ETH (MEDIUM), "
            "SOL (LOW). Size each position so that: (a) the "
            "dollar allocation roughly reflects conviction "
            "(HIGH:MEDIUM:LOW = 3:2:1 is a reasonable default), "
            "AND (b) each position's dollar RISK (computed from a "
            "1.5x 1h ATR stop) is roughly equal across all three. "
            "Fetch current prices and 1h ATR(14) for each, then "
            "compute each position's quantity, stop level, and "
            "verify the equal-risk constraint holds."
        ),
        expected_min_tool_calls=6,
        description="Conviction-weighted three-asset allocation with equal dollar risk",
    ),

    # ── Risk-manager (3) ────────────────────────────────────
    Scenario(
        key="daily-loss-limit",
        target_agent="risk-manager",
        task=(
            "First call get_positions to see current portfolio "
            "state including open positions, cash, closed trades, "
            "and daily P&L. The daily loss limit is 2% of starting "
            "balance (warn + halve size) and the hard halt limit "
            "is 5% (close everything). From what you see, decide: "
            "are we breaching either limit right now? Identify the "
            "largest dollar losers, compute how much dollar loss "
            "needs to be eliminated to bring the day back under "
            "the 2% limit, and recommend specific exit actions "
            "(which symbol, what quantity, what order type) in "
            "loss-reduction priority order. Use get_latest_price "
            "on any symbol you need to size an exit against."
        ),
        expected_min_tool_calls=3,
        description="Daily loss limit enforcement procedure — staged exits to restore headroom",
        fixture="portfolio_drawdown",
    ),
    Scenario(
        key="concentration-rebalance",
        target_agent="risk-manager",
        task=(
            "First call get_positions to see my current portfolio "
            "composition. The target allocation is 50% BTC, 30% "
            "ETH, 20% SOL by USD value. Compute the actual current "
            "allocation percentages from the position values, "
            "check each leg's drift from target, and decide "
            "whether to rebalance — trigger is any leg more than "
            "10 percentage points off target. If rebalancing, "
            "compute the exact USD amount to sell in the "
            "over-allocated symbol(s) and the exact USD amounts to "
            "buy in the under-allocated symbol(s). Use "
            "get_latest_price to convert between USD and base "
            "quantities and report the final target position "
            "quantities after the rebalance."
        ),
        expected_min_tool_calls=4,
        description="Portfolio concentration drift + rebalance amounts",
        fixture="portfolio_concentrated",
    ),
    Scenario(
        key="leverage-vol-check",
        target_agent="risk-manager",
        task=(
            "First call get_positions to see the current portfolio "
            "state (cash available, existing exposure). A trader "
            "wants to open a 3x leveraged BTC long with a 0.5% "
            "stop loss using $10,000 of notional. Determine "
            "whether this is safe given current BTC volatility. "
            "Fetch 14-day daily OHLCV for BTC and compute the "
            "average daily range as a percent of price (daily "
            "realized volatility proxy). Also fetch 1h ATR(14) as "
            "a percent of price (short-horizon noise floor). "
            "Compare the proposed 0.5% stop to the 1h ATR %: if "
            "the stop is inside 1 hour of normal price movement "
            "at 3x leverage, REJECT the trade and counter-"
            "recommend either a wider stop or a lower leverage "
            "level that survives normal noise. Also confirm there "
            "is sufficient cash for the proposed notional."
        ),
        expected_min_tool_calls=4,
        description="Leverage safety check given realized volatility — vol-adjusted stop rule",
        fixture="portfolio_clean",
    ),
]


def scenario_by_key(key: str) -> Scenario:
    for s in SCENARIOS:
        if s.key == key:
            return s
    raise SystemExit(f"Unknown scenario '{key}'. Available: {[s.key for s in SCENARIOS]}")


# ── The harness itself ──────────────────────────────────────


async def run_scenario(
    scenario: Scenario,
    skip_mentor: bool = False,
    skip_apply: bool = False,
) -> dict[str, Any]:
    """Run one scenario end-to-end and return the full transcript dict."""
    ts_start = time.time()
    transcript: dict[str, Any] = {
        "scenario": asdict(scenario),
        "started_at": ts_start,
        "target_agent": scenario.target_agent,
    }

    # ---- Apply portfolio fixture (if the scenario declares one) ----
    fixture_backup: Path | None = None
    if scenario.fixture:
        try:
            fixture_backup = _apply_fixture(scenario.fixture)
            print(f"[eval] fixture applied: {scenario.fixture}")
            transcript["fixture"] = scenario.fixture
        except FileNotFoundError as exc:
            transcript["fixture_error"] = str(exc)
            print(f"[eval] ERROR: {exc}")
            return transcript

    try:
        # ---- Setup: spin up the target sub-agent (no NATS bus) ----
        print(f"[eval] spinning up {scenario.target_agent} sub-agent...")
        target = SubAgent(scenario.target_agent, bus=None)

        # Snapshot the skill catalog BEFORE the run so we can diff at the end.
        pre_skills = target.list_existing_skills()
        transcript["pre_skills"] = pre_skills
        print(f"[eval] {scenario.target_agent} has {len(pre_skills)} skills before the run")

        # ---- Run the hard task against the target ----
        print(f"[eval] dispatching task to {scenario.target_agent}...")
        print(f"[eval] TASK: {scenario.task[:120]}...")
        task_start = time.time()
        try:
            response = await target.run_once(scenario.task)
        except Exception as exc:
            transcript["target_error"] = str(exc)
            transcript["finished_at"] = time.time()
            print(f"[eval] ERROR: target run failed: {exc}")
            return transcript

        return await _finish_scenario(
            scenario=scenario,
            transcript=transcript,
            target=target,
            response=response,
            task_start=task_start,
            ts_start=ts_start,
            pre_skills=pre_skills,
            skip_mentor=skip_mentor,
            skip_apply=skip_apply,
        )
    finally:
        # Always restore the user's real portfolio state, even if
        # the scenario errored mid-run.
        if scenario.fixture:
            _restore_fixture(fixture_backup)
            print(f"[eval] fixture restored")


async def _finish_scenario(
    *,
    scenario: Scenario,
    transcript: dict[str, Any],
    target: SubAgent,
    response: str,
    task_start: float,
    ts_start: float,
    pre_skills: list[dict[str, str]],
    skip_mentor: bool,
    skip_apply: bool,
) -> dict[str, Any]:
    """Post-task processing: target bookkeeping + optional mentor reflection.

    Extracted so the fixture snapshot/restore in ``run_scenario`` can
    wrap the entire target → mentor → apply flow in a single try/
    finally. Everything below is unchanged from the original inline
    version, just lifted into its own function.
    """

    task_wall = time.time() - task_start
    session = target.get_last_session()
    if session is None:
        transcript["target_error"] = "no session recorded"
        return transcript

    transcript["target_response"] = response
    transcript["task_wall_seconds"] = task_wall
    transcript["tool_count"] = session.tool_count
    transcript["tool_calls"] = [tc.to_dict() for tc in session.tool_calls]
    transcript["skill_was_created_during_run"] = session.skill_was_created()

    print(f"[eval] target done in {task_wall:.1f}s — {session.tool_count} tool calls")
    print(f"[eval] response length: {len(response)} chars")
    if session.tool_count >= scenario.expected_min_tool_calls:
        print(f"[eval] PASS: tool count {session.tool_count} >= expected {scenario.expected_min_tool_calls}")
    else:
        print(f"[eval] WARN: tool count {session.tool_count} < expected {scenario.expected_min_tool_calls}")

    # ---- Optional: run the mentor reflection ----
    if skip_mentor:
        transcript["mentor_skipped"] = True
        _save_transcript(scenario, transcript)
        return transcript

    # Guard: a session with zero tool calls is a failure (usually the
    # target endpoint is unreachable or the task was rejected). There
    # is LITERALLY nothing to reflect on — no tool outputs, no
    # iteration, no error recovery — so asking the mentor to produce
    # a skill from it causes hallucinations. Skip the mentor stage
    # and record the reason so batch runs don't generate junk skills.
    if session.tool_count == 0:
        print("[eval] SKIP mentor: target session had 0 tool calls (likely endpoint or task failure)")
        transcript["mentor_skipped"] = True
        transcript["mentor_skip_reason"] = "zero_tool_calls"
        transcript["finished_at"] = time.time()
        _save_transcript(scenario, transcript)
        return transcript

    print(f"[eval] spinning up mentor sub-agent for reflection...")
    mentor = SubAgent("mentor", bus=None)

    bundle = session.to_bundle(chat_turns=[], existing_skills=pre_skills)
    transcript["reflection_bundle"] = bundle

    reflection_prompt = _build_reflection_prompt(bundle, scenario.target_agent)

    print(f"[eval] sending reflection bundle to mentor ({len(reflection_prompt):,} chars)...")
    mentor_start = time.time()
    try:
        mentor_reply = await mentor.run_once(reflection_prompt)
    except Exception as exc:
        transcript["mentor_error"] = str(exc)
        print(f"[eval] mentor run failed: {exc}")
        _save_transcript(scenario, transcript)
        return transcript

    mentor_wall = time.time() - mentor_start
    transcript["mentor_response"] = mentor_reply
    transcript["mentor_wall_seconds"] = mentor_wall

    # Capture the mentor's own tool calls too — essential for
    # debugging when the mentor returns empty or fails to produce
    # a structured reply.
    mentor_session = mentor.get_last_session()
    if mentor_session:
        transcript["mentor_tool_calls"] = [tc.to_dict() for tc in mentor_session.tool_calls]
        transcript["mentor_tool_count"] = mentor_session.tool_count

    parsed = parse_mentor_reply(mentor_reply)
    transcript["mentor_parsed"] = parsed

    print(f"[eval] mentor done in {mentor_wall:.1f}s — decision: {parsed.get('decision')}")

    # ---- Optional: apply the mentor's decision ----
    if skip_apply:
        transcript["apply_skipped"] = True
        _save_transcript(scenario, transcript)
        return transcript

    outcome = _apply_decision(parsed, scenario.target_agent)
    transcript["apply_outcome"] = outcome

    # Post-run skill catalog for diffing
    post_skills = SkillStore(Path(get_skills_dir(scenario.target_agent))).list_skills()
    transcript["post_skills"] = post_skills
    transcript["new_skills"] = [
        s for s in post_skills if s["name"] not in {p["name"] for p in pre_skills}
    ]
    print(f"[eval] post-run skill count: {len(post_skills)} (was {len(pre_skills)})")
    if transcript["new_skills"]:
        for s in transcript["new_skills"]:
            print(f"[eval]   + NEW: {s['name']}: {s['description'][:70]}")

    transcript["finished_at"] = time.time()
    transcript["total_wall_seconds"] = transcript["finished_at"] - ts_start

    _save_transcript(scenario, transcript)
    return transcript


def _build_reflection_prompt(bundle: dict[str, Any], target_agent: str) -> str:
    """Render the reflection bundle as a human-readable prompt.

    Deliberately avoids dumping raw JSON at the model — smaller models
    get confused by nested braces and often go silent. This version is
    plain markdown with a clearly spelled-out output contract at the end,
    plus explicit prompts to look for error-recovery and retry patterns
    (the most common source of capturable learning).
    """
    # Count how many tool calls errored — a strong signal that there's
    # a skill worth capturing ("don't do X, do Y instead").
    tool_calls = bundle.get("tool_calls", [])
    errored = [tc for tc in tool_calls if tc.get("error")]
    retries: list[tuple[int, int]] = []
    for i in range(1, len(tool_calls)):
        if tool_calls[i].get("tool") == tool_calls[i - 1].get("tool"):
            retries.append((i, i + 1))

    lines: list[str] = []
    lines.append(
        f"You are reflecting on a session the {target_agent} agent just "
        "ran. Your job is to decide whether the agent learned anything "
        "worth saving as a skill so future sessions don't repeat the "
        "same trial and error."
    )
    lines.append("")
    lines.append("## Original task")
    lines.append(bundle.get("original_task", "(unknown)"))
    lines.append("")
    lines.append(f"## Final answer from {target_agent}")
    lines.append((bundle.get("target_summary") or "(empty)")[:1500])
    lines.append("")
    lines.append(f"## Tool calls ({bundle.get('tool_count', 0)} total, {len(errored)} errored)")
    for i, tc in enumerate(tool_calls, 1):
        tool = tc.get("tool", "?")
        inp = tc.get("input")
        inp_str = json.dumps(inp, default=str) if not isinstance(inp, str) else inp
        out = str(tc.get("output", ""))[:200]
        err = " [ERROR]" if tc.get("error") else ""
        lines.append(f"{i}. {tool}({inp_str[:150]}){err}")
        lines.append(f"   → {out}")
    lines.append("")
    if errored:
        lines.append("## Signals that may warrant a new skill")
        lines.append(
            f"- {len(errored)} tool call(s) errored and the agent had to retry. "
            "Error + recovery is the #1 source of capturable learning. "
            "Ask: 'what's the rule the agent should have followed to avoid "
            "this error the first time?' That rule is the skill."
        )
    if retries:
        lines.append(
            f"- The agent called the same tool back-to-back {len(retries)} "
            "time(s). That's often iteration on arguments — look for the "
            "specific argument the agent had to correct."
        )
    if errored or retries:
        lines.append("")
    lines.append(f"## Existing skills in {target_agent}'s library")
    for sk in bundle.get("existing_skills", []):
        lines.append(f"- {sk.get('name')}: {sk.get('description')}")
    lines.append("")
    lines.append("## Your decision")
    lines.append(
        "Decide ONE of: `create`, `patch`, or `no_skill`."
    )
    lines.append("")
    lines.append("**Create** when ANY of these is true:")
    lines.append(
        "- The workflow in this session is NOT covered by any existing "
        "skill above. If you scan the existing skills list and none of "
        "them describe what the agent just did, CREATE a new skill "
        "capturing the workflow. A clean session with no errors is "
        "still worth capturing if it represents a reusable procedure "
        "that future sessions will benefit from."
    )
    lines.append(
        "- The agent hit an error and recovered. Capture the rule that "
        "would have avoided the error."
    )
    lines.append(
        "- The agent deviated from the task's explicit requirements "
        "(wrong period, wrong timeframe, wrong symbol). Capture the "
        "correct parameters."
    )
    lines.append("")
    lines.append("**Patch** when:")
    lines.append(
        "- An existing skill covers the workflow but its steps / "
        "numbers / pitfalls are incomplete or wrong based on what you "
        "just observed."
    )
    lines.append("")
    lines.append("**no_skill** when:")
    lines.append(
        "- An existing skill already covers this workflow correctly "
        "AND the session didn't surface anything new. You must be "
        "able to name the specific existing skill that fits."
    )
    lines.append(
        "- The session has fewer than 3 tool calls. A session with "
        "no tool interaction is not a learning session — return "
        "`no_skill` regardless of what the task asked for."
    )
    lines.append(
        "- The final answer is empty, an error string, or a refusal. "
        "A broken session is not a learning opportunity."
    )
    lines.append("")
    lines.append("## Required output format — copy this shape EXACTLY")
    lines.append("")
    lines.append("For `create`, reply with:")
    lines.append("")
    lines.append(f"DECISION: create")
    lines.append(f"TARGET_AGENT: {target_agent}")
    lines.append(f"SKILL_NAME: kebab-case-slug")
    lines.append(f"OP: create")
    lines.append(f"SKILL_CONTENT:")
    lines.append("---")
    lines.append("name: kebab-case-slug")
    lines.append("description: One sentence about what this skill does")
    lines.append("category: analysis")
    lines.append("---")
    lines.append("# Skill title")
    lines.append("## When to use")
    lines.append("Specific precondition.")
    lines.append("## Steps")
    lines.append("1. Exact tool calls with arguments.")
    lines.append("## Pitfalls")
    lines.append("- The specific mistake seen in this session.")
    lines.append("## Verification")
    lines.append("- How to know it worked.")
    lines.append("")
    lines.append("For `patch`, reply with:")
    lines.append("")
    lines.append("DECISION: patch")
    lines.append(f"TARGET_AGENT: {target_agent}")
    lines.append("SKILL_NAME: name-of-existing-skill")
    lines.append("OP: patch")
    lines.append("OLD_STRING:")
    lines.append("the exact substring to replace")
    lines.append("NEW_STRING:")
    lines.append("the replacement text")
    lines.append("")
    lines.append("For `no_skill`, reply with ONLY:")
    lines.append("")
    lines.append("DECISION: no_skill")
    lines.append("")
    lines.append(
        "Do not wrap the reply in code fences. Do not add commentary "
        "before or after the fields. Just emit the fields."
    )
    return "\n".join(lines)


def _apply_decision(parsed: dict[str, Any], target_agent: str) -> dict[str, Any]:
    """Apply the mentor's decision to the target agent's SkillStore."""
    decision = parsed.get("decision", "unknown")
    store = SkillStore(Path(get_skills_dir(target_agent)))

    if decision == "no_skill":
        return {"decision": "no_skill"}

    skill_name = parsed.get("skill_name")
    if not skill_name:
        return {"decision": decision, "error": "missing_skill_name"}

    if decision == "create":
        content = parsed.get("content", "")
        if not content:
            return {"decision": "create", "error": "missing_content"}
        return {"decision": "create", "skill_name": skill_name, "result": store.create(skill_name, content)}

    if decision == "patch":
        old_s = parsed.get("old_string", "")
        new_s = parsed.get("new_string", "")
        if not old_s:
            return {"decision": "patch", "error": "missing_old_string"}
        return {"decision": "patch", "skill_name": skill_name, "result": store.patch(skill_name, old_s, new_s)}

    return {"decision": decision, "error": "unknown_decision"}


def _save_transcript(scenario: Scenario, transcript: dict[str, Any]) -> Path:
    """Persist the full transcript to a timestamped JSON file."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = RESULTS_DIR / f"{ts}-{scenario.key}-{scenario.target_agent}.json"
    path.write_text(json.dumps(transcript, indent=2, default=str), encoding="utf-8")
    print(f"[eval] transcript saved: {path}")
    return path


# ── CLI ─────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Skill-learning eval harness")
    p.add_argument(
        "--scenario",
        default="bb",
        help="Scenario key to run (default: bb)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Run every scenario sequentially",
    )
    p.add_argument(
        "--list-scenarios",
        action="store_true",
        help="List available scenarios and exit",
    )
    p.add_argument(
        "--skip-mentor",
        action="store_true",
        help="Only run the target agent, do not run the mentor reflection",
    )
    p.add_argument(
        "--skip-apply",
        action="store_true",
        help="Run the mentor reflection but do not persist any skills",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    if args.list_scenarios:
        print("Available scenarios:")
        for s in SCENARIOS:
            print(f"  {s.key:12} [{s.target_agent}] {s.description}")
        return 0

    scenarios_to_run: list[Scenario]
    if args.all:
        scenarios_to_run = SCENARIOS
    else:
        scenarios_to_run = [scenario_by_key(args.scenario)]

    summary: list[dict[str, Any]] = []
    for scenario in scenarios_to_run:
        print(f"\n{'=' * 60}\n  SCENARIO: {scenario.key} — {scenario.description}\n{'=' * 60}")
        transcript = asyncio.run(
            run_scenario(
                scenario,
                skip_mentor=args.skip_mentor,
                skip_apply=args.skip_apply,
            )
        )
        summary.append({
            "scenario": scenario.key,
            "tool_count": transcript.get("tool_count"),
            "mentor_decision": (transcript.get("mentor_parsed") or {}).get("decision"),
            "new_skills": [s["name"] for s in transcript.get("new_skills", [])],
            "errors": {k: v for k, v in transcript.items() if k.endswith("_error")},
        })

    print(f"\n{'=' * 60}\n  SUMMARY\n{'=' * 60}")
    for entry in summary:
        print(json.dumps(entry, default=str))

    # Persist the top-level summary too
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = RESULTS_DIR / f"summary-{time.strftime('%Y%m%d-%H%M%S')}.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"[eval] summary saved: {summary_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
