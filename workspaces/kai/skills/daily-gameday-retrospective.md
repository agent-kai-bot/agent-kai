---
name: daily-gameday-retrospective
description: Build the daily MLB gameday retrospective comparing saved pregame briefs with final results and operator Polymarket activity.
category: analysis
tags: [mlb, polymarket, retrospective, pnl, daily-brief]
---
# Daily Gameday Retrospective

## When to use
Use after an MLB slate settles, or in the scheduled 5 AM ET daily retro job, to compare `docs/daily_brief/{DATE}_*.md` pregame briefs against actual results and operator trading activity.

## Steps
1. Work from `/home/atc/git/OPS/vpn-stack`.
2. Resolve dates in ET if possible:
   ```bash
   TODAY=$(TZ=America/New_York date +%Y-%m-%d)
   YESTERDAY=$(TZ=America/New_York date -d 'yesterday' +%Y-%m-%d)
   ```
3. List brief files. Check both repo and data-dir locations before declaring DATA_GAP because some generated briefs may be saved only under `/home/atc/vpn-stack-data/daily_brief` while `docs/daily_brief` is not a symlink:
   ```bash
   find docs/daily_brief /home/atc/vpn-stack-data/daily_brief -maxdepth 1 -type f -name "${YESTERDAY}_*.md" -printf '%p\n' 2>/dev/null | sort -u
   ```
4. Pull MLB final results:
   ```bash
   curl -sS "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=${YESTERDAY}&hydrate=lineups,probablePitcher,linescore"
   curl -sS "https://statsapi.mlb.com/api/v1.1/game/${GAMEPK}/feed/live"
   ```
   Capture final score, total, hits, HRs, and starter lines.
5. Pull operator activity/P&L:
   - Primary: Polymarket Data API activity for wallet `0x0011AC56cC1AF0412f569E50eAE15f6F48f64CbA`.
   - Open positions: `python3 scripts/manual/positions.py --pretty --min-size 0.5` from host (canonical source).
6. For each briefed game, extract/record:
   - Center band / working total.
   - FIRE CANDIDATES and size guidance.
   - HOLD/SKIP advice.
   - Live triggers.
7. Cross-reference against finals and activity:
   - Did final total land within ±2 of the center midpoint?
   - Which recommended lines landed ITM?
   - Was operator sizing aligned with brief conviction?
   - What went RIGHT / WRONG.
8. Save report:
   ```text
   docs/retro/${YESTERDAY}_gameday_retro.md
   ```

## Pitfalls
- `urllib` may get HTTP 403 from `data-api.polymarket.com`; use `requests` with a normal User-Agent.
- Host `positions.py` only shows open/unredeemed positions; use Data API activity for settled same-day buys/redeems/sells.
- Polymarket uses `OAK` in some slugs while MLB/team APIs may use `ATH`; alias `mlb-stl-oak-{DATE}` to `STL@ATH` results when computing settlements.
- Briefs may have `lineups_confirmed=false` in v3 diagnostics despite MLB lineups being confirmed; flag this as a model/data caveat.

## Verification
- Confirm the output file exists and includes sections: one-line summary, per-game table, final-result details, pattern findings, accuracy score, sizing review, recommended adjustments.
- Confirm scheduled job exists with `list_scheduled_jobs` if creating/updating the daily recurring job.
- Totals/P&L should reconcile per token as: `sell_usd + redeem_usd - buy_usd`.
