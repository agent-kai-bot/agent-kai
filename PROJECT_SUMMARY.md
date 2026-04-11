# KAI Project Summary

## What is KAI?

KAI (agent-k.ai) is an AI-powered crypto trading terminal that combines local LLM inference with a multi-agent architecture. It's designed for traders who want AI assistance without sending their data to the cloud.

## Core Value Proposition

- **Local-first**: Primary LLM runs on your hardware — fast, private, no API costs for routine tasks
- **Agent escalation**: Complex analysis automatically routes to cloud frontier models (Claude, Codex)
- **Multi-agent**: 15 specialized agents that collaborate via NATS message bus
- **Real-time data**: Live market data from 93 crypto symbols across 6 timeframes
- **Paper trading**: Risk-free simulation with full position management

## Technical Stack

| Component | Technology |
|-----------|-----------|
| Local LLM | NVIDIA Nemotron 3 Nano (~31.5B params, GGUF via llama.cpp) |
| Agent Framework | LangChain AgentExecutor |
| Message Bus | NATS |
| Data Store | TimescaleDB (PostgreSQL 16) |
| Data API | FastAPI (port 8877) |
| TUI | Textual (Python) |
| Cloud Fallback | Claude Code CLI, OpenAI Codex CLI |

## Agent Roster (15 agents)

### Crypto Agents (5)
1. **Trader** — Order execution, position management, P&L tracking
2. **Analyst** — Technical analysis (7 indicators), multi-timeframe, signal generation
3. **Risk Manager** — Position sizing (1% risk/trade), exposure limits (20% max), drawdown monitoring
4. **Scanner** — pump.fun token scanning (new/trending/graduated), breakout detection
5. **On-chain** — Contract analysis, honeypot detection, whale tracking, liquidity checks

### Organization Agents (9)
6. CEO, 7. CTO, 8. Architect, 9. Developer, 10. QA, 11. UX Manager, 12. Project Manager, 13. SEO, 14. Sales & Marketing

### Primary Agent (1)
15. **Nano** — User-facing agent that coordinates all others

## Tool Inventory (19 tools)

| Category | Tools | Count |
|----------|-------|-------|
| Crypto | query_ohlcv, get_latest_price, list_symbols, calculate_indicator, place_order, get_positions, scan_tokens | 7 |
| System | file_read, file_write, file_edit, shell_exec, python_exec, web_fetch | 6 |
| Escalation | codex_exec, claude_exec | 2 |
| Agent Coordination | spawn_agent, nats_request, nats_publish, list_agents | 4 |

## Data Pipeline

```
BingX Exchange (WebSocket)
    ↓
TimescaleDB (93 symbols × 6 timeframes)
    ↓
FastAPI Data API (:8877)
    ├── REST: /api/v1/ohlcv, /symbols, /price
    ├── WebSocket: /ws/{symbol}/{interval}
    └── NATS Bridge: market.{symbol}.{tf}
            ↓
    Terminal Panels + AI Agents
```

## NATS Subject Hierarchy

```
market.{symbol}.1m        Live 1-minute candles
market.{symbol}.price     Latest price ticks
market.{symbol}.signal    Trading signals from analyst
portfolio.positions       Position updates from trader
portfolio.pnl             P&L updates
portfolio.orders          Order events
alert.pump                New token alerts from scanner
alert.breakout            Price breakout alerts
alert.pattern             Pattern detection alerts
agent.{name}.request      Task requests to specific agent
agent.{name}.response     Agent task responses
agent.{name}.status       Agent status (thinking/idle/error)
agent.broadcast           Broadcast to all agents
system.registry           Agent online/offline announcements
system.log                System-wide logging
```

## Slash Commands (10)

```
/buy BTC 0.1 [limit 67000]    Place paper trade
/sell ETH 0.5                  Close/reduce position
/analyze BTC [15m]             Full technical analysis
/scan trending|new|graduated   pump.fun token scanner
/risk                          Portfolio risk assessment
/chart SOL [15m]               Switch chart symbol/timeframe
/watch DOGE                    Add/remove watchlist symbol
/positions                     Refresh positions panel
```

## File Structure

```
kai-terminal/
├── main.py                    Entry point (--terminal, --no-tui)
├── config.py                  Config loader from agent-config.json
├── agent-config.json          All endpoints, agents, settings
├── agent_logger.py            Structured logging (DEBUG: full prompts/responses)
├── requirements.txt           Python dependencies
│
├── agent/                     Agent framework
│   ├── core.py                AgentRunner (LangChain + fallback)
│   ├── prompts.py             System prompts (KAI-tuned)
│   ├── tools.py               Tool registry (19 tools)
│   ├── crypto_tools.py        7 crypto-specific tools
│   └── sub_agents.py          SubAgent spawner + manager
│
├── data_api/                  Market data service
│   ├── server.py              FastAPI app (:8877)
│   ├── db.py                  asyncpg connection pool
│   ├── routes.py              REST endpoints
│   ├── websocket.py           WebSocket streaming
│   ├── nats_bridge.py         TimescaleDB → NATS bridge
│   ├── paper_trading.py       Paper trading engine
│   ├── models.py              Pydantic models
│   └── config.py              Data layer config
│
├── nats_bus/                  Message bus
│   └── bus.py                 NatsBus (pub/sub/request-reply)
│
├── tui/                       Terminal UI
│   ├── terminal.py            Trading terminal (6-panel layout)
│   ├── terminal_styles.tcss   Terminal CSS
│   ├── app.py                 Simple chat TUI (original)
│   ├── styles.tcss            Simple TUI CSS
│   └── panels/                Panel widgets
│       ├── watchlist.py       Live price table
│       ├── chart.py           ASCII candlestick chart
│       ├── positions.py       Position/P&L table
│       ├── alerts.py          Signal/alert log
│       └── agent_chat.py      Chat panel widget
│
├── workspaces/                Agent workspaces (each has SOUL.md)
│   ├── trader/
│   ├── analyst/
│   ├── risk-manager/
│   ├── scanner/
│   ├── onchain/
│   ├── ceo/, cto/, architect/, developer/, qa/
│   ├── ux-manager/, project-manager/, seo/, sales-marketing/
│   └── nano/
│
├── eval_harness.py            LLM evaluation framework
└── logs/                      Agent logs (per-day, per-agent)
```

## Key Metrics

- **LLM Speed**: ~155 tok/s generation, ~512 tok/s prompt processing
- **Eval Score**: 28/28 (100%) on logic, coding, tool use, support
- **Symbols**: 93 crypto futures
- **Timeframes**: 1m, 5m, 15m, 1h, 6h, 1d
- **Agents**: 15
- **Tools**: 19
- **Paper Portfolio**: $100,000 starting balance
- **Risk Limits**: 5% max per position, 20% total exposure, 10% max drawdown

## Development History

1. **Eval phase**: Benchmarked Nemotron 3 Nano — 100% after tuning (temperatures, max_tokens, system prompt)
2. **Agent framework**: LangChain AgentExecutor + NATS + Textual TUI + configurable endpoints with fallback
3. **Sub-agents**: NATS-based spawning, SOUL.md personas, workspace isolation
4. **Escalation**: codex_exec + claude_exec tools for frontier model access
5. **KAI fork**: Data API, crypto tools, paper trading, 5 crypto agents, trading terminal TUI

## KAI Token

The KAI Solana token is used for API payments to the crypto data backend, providing access to:
- Real-time WebSocket streaming (1-min TF, 200+ crypto futures)
- 15+ Web3 chain data
- pump.fun token data
- Historical OHLCV across all timeframes
