# Broadcast Report Subscription Service — Design Proposal

Status: **DRAFT — awaiting answers to 6 open questions**

---

## 1. Core concept

The backend generates **ONE report per token per period** (e.g.
daily BTC report). N users consume the same report. Cost is fixed
per token regardless of subscriber count — revenue scales with
subscribers, generation cost does not.

The agent is a **consumer**, not a producer. It subscribes to
report events, fetches from the backend API, displays in the
session, and debits the user's custodial balance for the read.

The existing BTC Discord bot becomes just another consumer of the
same backend-generated report.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    BACKEND (kai-api)                     │
│                                                         │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────┐  │
│  │ Report Cron  │───▶│ Data Assembly│───▶│ LLM Write │  │
│  │ (per token,  │    │ (OHLCV, TA,  │    │ (one call │  │
│  │  per period) │    │  on-chain)   │    │  per rpt) │  │
│  └──────────────┘    └──────────────┘    └─────┬─────┘  │
│                                                │        │
│                                          ┌─────▼─────┐  │
│                                          │  Report   │  │
│                                          │  Store    │  │
│                                          │ (DB/S3)   │  │
│                                          └─────┬─────┘  │
│                                                │        │
│  ┌──────────────────────────────────────────────┤        │
│  │                                              │        │
│  ▼                                              ▼        │
│  REST API                                WS broadcast    │
│  GET /api/reports/:token/latest          event:          │
│  GET /api/reports/:token/:date           report_published│
│  GET /api/reports/catalog                                │
│                                                         │
└─────────────┬───────────────────────────┬───────────────┘
              │                           │
    ┌─────────▼────────┐        ┌─────────▼────────┐
    │   Agent/TUI      │        │   Discord bot    │
    │   (subscriber)   │        │   (subscriber)   │
    │                  │        │                  │
    │ User A: BTC,ETH  │        │ #btc-daily chan  │
    │ User B: SOL,ETH  │        │ #eth-daily chan  │
    │ User C: BTC      │        │                  │
    └──────────────────┘        └──────────────────┘
```

### Responsibility split

| Layer | Responsibility |
|---|---|
| **Report Cron** (backend) | Runs on schedule (e.g. daily 7am UTC). One job per token in the catalog. |
| **Data Assembly** (backend) | Pulls OHLCV, indicators, on-chain metrics, market context. Same data pipeline already used for the AI copilot endpoint. |
| **LLM Writer** (backend) | One LLM call per token per period. Generates a markdown narrative. Cost = fixed per token regardless of subscriber count. Uses the existing vLLM / kai-smart inference. |
| **Report Store** (backend) | Persists the report (DB row or S3 object). Immutable once written — same report served to every consumer. |
| **REST API** (backend) | Endpoints for fetching reports (see §4). |
| **WS Event** (backend → daemon) | `report_published` event notifies connected daemons that a new report is available. Fire-and-forget notification; the consumer fetches the full report via REST. |
| **Agent** (consumer) | Receives event, checks if user is subscribed, fetches report via REST, displays in session, debits custodial balance for the read. |
| **Discord bot** (consumer) | Same event → format as Discord embed → post to configured channel. The existing BTC bot simplifies to this path. |

---

## 3. Why broadcast, not per-user generation

| Aspect | Per-user generation | Broadcast (this proposal) |
|---|---|---|
| Who generates | Agent per user | Backend once |
| LLM cost for 1000 subscribers | 1000x | 1x |
| Report content | Potentially personalized | Same for everyone |
| Where logic lives | Agent tools + sub-agent | Backend cron + API |
| Agent's role | Producer | Consumer + displayer |
| Billing model | Per-generation | Per-read |
| Existing BTC Discord bot | Separate or migrated | Just another consumer |
| Complexity | High (each agent orchestrates data assembly + LLM) | Low on agent side; moderate on backend |

The broadcast model means inference cost is amortized across all
subscribers. Since inference runs on local GPUs (100% margin per
the business model), the per-token generation cost is essentially
electricity — fixed and predictable.

---

## 4. Backend REST API

### Endpoints

```
GET  /api/reports/catalog
     → list of tokens with active report generation + frequencies

GET  /api/reports/:token/latest
     → the most recent report for this token

GET  /api/reports/:token/:date
     → report for a specific date (YYYY-MM-DD)

GET  /api/reports/:token/history?limit=7
     → last N reports for this token (metadata + IDs, not full content)
```

All endpoints require authentication (same auth as the existing
`/api/ai` endpoint — wallet session or bearer token).

### Report payload

What the REST API returns:

```jsonc
{
  "report_id": "rpt_2026_04_10_btc_daily",
  "token": "BTC",
  "period": "daily",
  "generated_at": "2026-04-10T07:00:12Z",
  "data": {
    "price": 68420.50,
    "change_24h_pct": 1.8,
    "change_7d_pct": -2.3,
    "volume_24h": 28400000000,
    "rsi_14": 55.2,
    "macd_signal": "bullish_cross",
    "key_levels": {
      "support": [66800, 65200],
      "resistance": [69500, 71000]
    }
  },
  "narrative": "## BTC Daily Report — April 10, 2026\n\nBitcoin reclaimed $68k overnight...",
  "tier": "standard"
}
```

- `data` — structured block for rendering cards/charts in the UI
- `narrative` — LLM-written markdown for the chat panel
- Both travel in the same payload so the agent can render either way

### Catalog payload

```jsonc
{
  "tokens": [
    {"token": "BTC", "frequencies": ["daily"], "tier": "standard"},
    {"token": "ETH", "frequencies": ["daily"], "tier": "standard"},
    {"token": "SOL", "frequencies": ["daily"], "tier": "standard"}
  ],
  "next_generation_at": "2026-04-11T07:00:00Z"
}
```

---

## 5. WebSocket event

When the backend finishes generating a report, it broadcasts to
all connected daemons:

```jsonc
{
  "type": "report_published",
  "token": "BTC",
  "report_id": "rpt_2026_04_10_btc_daily",
  "period": "daily",
  "generated_at": "2026-04-10T07:00:12Z"
}
```

This is a lightweight notification — it does NOT contain the full
report. The consumer (agent, Discord bot) fetches the full report
from the REST API using the `report_id` or the
`/api/reports/:token/latest` endpoint. This avoids sending large
payloads over WS to consumers who may not even be subscribed.

---

## 6. Agent-side implementation (consumer)

The agent side is thin. It subscribes, receives events, fetches,
displays, and bills. No report generation logic.

### Slash commands

```
/report subscribe BTC,ETH,SOL        # subscribe to these tokens
/report unsubscribe ETH               # drop one token
/report list                          # show active subscriptions + next delivery
/report latest BTC                    # fetch + display the latest BTC report now
/report history BTC 7                 # list last 7 BTC report dates + summaries
/report catalog                       # show all available tokens with reports
/report pause                         # pause all subscriptions (stop receiving)
/report resume                        # resume
```

### Agent tools (for natural language)

For users who prefer "subscribe me to the daily BTC and ETH
report" over slash commands:

- `subscribe_report(tokens: list[str])` — add tokens to the
  user's subscription list
- `unsubscribe_report(token: str)` — remove a token
- `get_latest_report(token: str)` — fetch from backend, display
  inline in the session
- `list_report_subscriptions()` — show what the user is subscribed
  to, when the next reports arrive
- `get_report_catalog()` — show available tokens + frequencies

### WS event handler

When the daemon receives `report_published`:

1. Check which sessions have subscribed to that token
2. For each subscribed session:
   a. Fetch the full report from the backend REST API
   b. Display in the session chat with a `[report: TOKEN daily]`
      marker and the narrative rendered as markdown
   c. Show the `data` block as an insight card (same rendering as
      the existing AI copilot cards)
   d. Debit the user's custodial balance (per-read fee)
3. If no sessions are subscribed, ignore the event (no cost)

### Persistence (agent-side)

Per-session subscription list:

```
workspaces/sessions/{name}/report_subscriptions.json
```

```jsonc
{
  "subscriptions": [
    {"token": "BTC", "subscribed_at": "2026-04-10T..."},
    {"token": "ETH", "subscribed_at": "2026-04-10T..."}
  ],
  "paused": false
}
```

No report storage in the agent — the backend is the source of
truth. The agent fetches on demand via the REST API. Past reports
are available via `/api/reports/:token/:date` and
`/report history`.

---

## 7. Discord bot integration

The existing BTC Discord bot simplifies to the same consumer
pattern:

1. Subscribe to `report_published` events for configured tokens
2. Fetch the full report from the REST API
3. Format the `narrative` as a Discord embed (markdown → embed
   fields)
4. Post to the configured channel via webhook

Benefits:
- Same report content as the agent (no drift between channels)
- Bot becomes ~50 lines of code (event listener + fetch + format +
  post)
- Adding a new token to Discord = adding one line to the bot config
- The bot no longer needs its own data pipeline or LLM access

---

## 8. Backend report generation pipeline

This runs in the `kai-api` backend (`~/git/kai-new-v2` on
homedevbox).

### Cron schedule

One job per token in the catalog. All fire at the same time
(e.g. 07:00 UTC daily) with a small stagger (30s between tokens)
to avoid slamming the LLM endpoint.

### Data assembly per token

| Data source | What it provides | Existing? |
|---|---|---|
| Coinbase / kai-api OHLCV | Price, volume, candles | Yes |
| Indicator computation | RSI, MACD, MAs, Bollinger | Yes (existing `_build_cards` logic) |
| Key level detection | Support/resistance from candle structure | Yes (can reuse copilot prompt logic) |
| Market context | BTC correlation, sector performance | Partially (multi-token OHLCV exists) |
| On-chain (Alchemy/Helius) | Whale moves, holder changes, DEX flows | Future (web3 track) |
| News/social signal | Sentiment, trending topics | Future |

v1 uses what already exists (OHLCV + indicators + key levels +
market context). On-chain and social layers are additive — they
make reports richer but aren't required for launch.

### LLM prompt template

```
You are a crypto market analyst writing a daily report for {TOKEN}.

## Data
{structured_data_block}

## Instructions
Write a concise daily report (300-500 words) covering:
1. Price action summary (what happened in the last 24h)
2. Key technical levels (support/resistance, trend)
3. Indicator signals (RSI, MACD, MA crossovers)
4. Market context (correlation with BTC, sector trends)
5. Outlook (bullish/bearish/neutral, what to watch)

Use markdown formatting. Be direct, no fluff. Write for an
audience that understands crypto but wants the TL;DR.
```

### Storage

Reports are stored in the existing database (PostgreSQL) or as
flat files (S3/local):

```sql
CREATE TABLE reports (
    id          TEXT PRIMARY KEY,
    token       TEXT NOT NULL,
    period      TEXT NOT NULL DEFAULT 'daily',
    generated_at TIMESTAMPTZ NOT NULL,
    data_json   JSONB NOT NULL,
    narrative   TEXT NOT NULL,
    tier        TEXT NOT NULL DEFAULT 'standard',
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_reports_token_date ON reports (token, generated_at DESC);
```

### Publish event

After the report is written to the store, the backend publishes
the `report_published` event to all connected WebSocket clients
(same WS infrastructure used for market data streaming).

---

## 9. Pricing model

### Per-read micro-fee (recommended for v1)

Each time a report is delivered to a user's session, debit:

| Tier | Per-report fee | Monthly cost (1 token, daily) |
|---|---|---|
| Standard | $0.02 | ~$0.60/month |

At 100% margin (local GPU inference), the LLM generation cost is
~$0.001–$0.01 per report regardless of subscriber count. Even at
$0.02/read, 50 subscribers paying $0.02 = $1.00 revenue on
$0.01 cost per token per day.

### Future: subscription bundles

- "Top 10" bundle: all top-10 tokens daily for $X/month
- "Custom portfolio" bundle: reports for all tokens in user's
  watchlist for $Y/month
- These can be priced at a discount vs per-read to incentivize
  commitment

---

## 10. Rollout plan

### Sprint 1 — Backend report generator

- Add report cron job to `kai-api`
- Data assembly using existing OHLCV + indicator pipeline
- LLM prompt template for standard tier
- Report storage (DB)
- REST API: `/api/reports/catalog`, `/:token/latest`, `/:token/:date`
- WS broadcast: `report_published` event
- Generate for 3 tokens to start: BTC, ETH, SOL

### Sprint 2 — Agent consumer

- Add `/report` slash commands to the daemon
- Agent tools: `subscribe_report`, `unsubscribe_report`,
  `get_latest_report`, `list_report_subscriptions`
- WS event handler: receive `report_published`, fetch, display,
  debit
- Per-session subscription persistence
- Wire protocol: add `report_published` + `report_delivered`
  envelope types

### Sprint 3 — Discord bot migration

- Refactor existing BTC Discord bot to consume from the REST API
  instead of generating its own report
- Add support for multiple tokens (ETH, SOL channels)
- Single config file: token → channel mapping

### Sprint 4 — Expand + polish

- Expand token catalog (top 10, then on-demand based on subscriber
  count)
- Add report history browsing in TUI and web UI
- Add on-chain data layer when web3 tools land
- Subscription bundles + pricing tiers
- Report quality iteration based on user feedback

---

## 11. What the existing BTC Discord bot becomes

The current standalone BTC report bot retires its own data
pipeline + LLM logic and becomes a thin consumer:

```
report_published event
    │
    ▼
Fetch report from /api/reports/BTC/latest
    │
    ▼
Format narrative as Discord embed
    │
    ▼
POST to Discord webhook
```

~50 lines of Python. Same report content as the agent. No drift
between channels. Adding a new token = one config line.

**Transition plan:** keep the old bot running in parallel during
Sprint 1-2. Once the backend report generator is producing reports
that match or exceed the old bot's quality, flip the Discord bot
to the new consumer path and retire the old generation logic.

---

## 12. Open questions

1. **Where does report generation run?** The `kai-api` backend at
   `agent-k.ai`, or a new microservice?
   *(Recommended: kai-api — it already has the LLM endpoint, data
   tools, and auth infrastructure.)*

2. **Which tokens get reports?** Fixed list (top N) or dynamic
   based on subscriber demand?
   *(Recommended: start with BTC, ETH, SOL. Add tokens when N+
   users subscribe to one that isn't covered yet — threshold
   configurable.)*

3. **Tiers from day one?** One standard tier, or basic/standard/
   detailed?
   *(Recommended: one "standard" tier. Tiers add complexity for
   unclear value before you have user feedback on what they
   actually read.)*

4. **Per-read or per-subscription pricing?**
   *(Recommended: per-read for v1 (simpler). Subscription bundles
   in Sprint 4 once you know usage patterns.)*

5. **Report frequency** — daily only, or also weekly/4h?
   *(Recommended: daily only for launch. Add frequencies based on
   demand.)*

6. **Build the backend report generator in this conversation, or
   separately?** The backend lives in `~/git/kai-new-v2` (the
   kai-api gateway repo), not in this agent repo.
   *(This depends on whether you want to context-switch now or
   keep the agent-side and backend-side as separate work streams.)*
