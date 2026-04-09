# Eval Fixtures

Hand-authored portfolio snapshots used by `eval_skill_learning.py` to give
scenarios realistic `get_positions` state instead of asking the agent to
"assume" a portfolio. Each fixture matches the on-disk format used by
`data_api/paper_trading.py` (cash, starting_balance, positions, closed_trades).

The eval harness snapshots `workspaces/trader/portfolio.json` before a
fixtured run, overlays the fixture, runs the scenario, and restores the
original file afterward — your real paper-trading state is never lost.

## Fixtures

| File | Models | Used by |
|---|---|---|
| `portfolio_clean.json` | Empty portfolio: $100k cash, no positions | `leverage-vol-check` |
| `portfolio_btc_winner.json` | Single 1.0 BTC long from $68k, price at $71.5k (up ~5%) | `partial-exit-ladder` |
| `portfolio_drawdown.json` | 3 losing positions + 2 closed losing trades, day at -3.4% | `daily-loss-limit` |
| `portfolio_concentrated.json` | 65% BTC / 25% ETH / 10% SOL allocation drift | `concentration-rebalance` |

## Adding a new fixture

1. Copy an existing file and edit. Keep the structure (top-level keys
   `cash`, `starting_balance`, `positions`, `closed_trades`) and use
   `asdict(Position)` field names for position records.
2. Make the numbers internally consistent — cash should equal
   `starting_balance - sum(entry_price * qty for longs) + sum(proceeds from closed trades)`.
3. Wire it into a scenario by setting `Scenario.fixture = "your_file"`
   (without the `.json` extension).
4. Rewrite the scenario's task prompt to use imperative "check your
   positions" language instead of "assume you have X". The whole point
   of a fixture is the agent reads real state via `get_positions`.
