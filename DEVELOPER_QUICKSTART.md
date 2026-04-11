# agent-k.ai — Developer Quickstart

Get from zero to your first billed AI response in five minutes.

The full API reference is in `agent-k-ai-openapi.yaml`. This doc is the
hand-held tour.

- **Base URL:** `https://agent-k.ai`
- **All public endpoints live under:** `/v1/*`
- **Success responses wrap payloads in:** `{ "v": 1, "data": { ... } }`

---

## 1. Register an account

```bash
curl -sS -X POST https://agent-k.ai/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{
    "email": "you@example.com",
    "password": "a-reasonably-long-password",
    "display_name": "Your Name"
  }'
```

**Response:**

```json
{
  "v": 1,
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIs...",
    "account_id": "acct_01H...",
    "email": "you@example.com"
  }
}
```

- `token` is a short-lived JWT you can use immediately as a Bearer token.
- Prefer creating a long-lived **API key** (next step) for scripts.
- If you already have an account, use `POST /v1/auth/login` with the same body shape minus `display_name`.

---

## 2. Mint a long-lived API key

Use the JWT from step 1 to mint a key you'll hang onto:

```bash
TOKEN='eyJhbGciOiJIUzI1NiIs...'    # from step 1

curl -sS -X POST https://agent-k.ai/v1/auth/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name": "my-first-bot"}'
```

**Response:**

```json
{
  "v": 1,
  "data": {
    "id": 42,
    "raw_key": "kai-9f3b...64-hex-chars-total...",
    "key_prefix": "kai-9f3b0c12",
    "name": "my-first-bot",
    "tier": "standard",
    "rate_limit_rpm": 60,
    "rate_limit_tpm": 100000
  }
}
```

> **⚠️ Copy `raw_key` now — it is only returned once.**
> The server stores a hash; nobody (including us) can recover the secret if you lose it.
> If you do lose it, just `DELETE /v1/auth/api-keys/{id}` and mint another.

From here on, use the API key as the bearer token:

```bash
KEY='kai-9f3b...'
```

API keys can be sent either way — pick one:

```
Authorization: Bearer kai-9f3b...
X-API-Key: kai-9f3b...
```

---

## 3. Check your balance

New accounts start with zero KAI. Verify:

```bash
curl -sS https://agent-k.ai/v1/billing/balance \
  -H "Authorization: Bearer $KEY"
```

**Response:**

```json
{
  "v": 1,
  "data": {
    "account_id": "acct_01H...",
    "balance_micros": 0,
    "balance_kai": 0.0
  }
}
```

**1 KAI = 1,000,000 micros.** The API keeps both so you can pick whichever
precision your UI wants.

### Top up

Two options:

- **Stripe Checkout:** `POST /v1/billing/stripe/checkout` returns a hosted
  payment URL. Redirect your user there; credits land automatically after
  Stripe confirms the charge.
- **On-chain deposit:** sign in to the web dashboard at
  `https://agent-k.ai/account` and send SOL or USDC to the deposit
  address shown on your Account page. Credits appear within a block or two.

Re-run the balance call until `balance_kai > 0`.

---

## 4. Pick a model (optional)

```bash
curl -sS https://agent-k.ai/v1/models \
  -H "Authorization: Bearer $KEY"
```

Returns an array of available models with per-1k-token input/output costs.
Omit `model` in the next step to use the server default.

---

## 5. Run your first AI chat

```bash
curl -sS -X POST https://agent-k.ai/v1/ai/chat \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "In one sentence, what is the efficient-markets hypothesis?",
    "max_tokens": 200
  }'
```

**Response:**

```json
{
  "v": 1,
  "data": {
    "answer": "The efficient-markets hypothesis holds that asset prices fully reflect all available information, so consistently earning above-market returns through stock picking or market timing is impossible.",
    "tokens_in": 21,
    "tokens_out": 38,
    "latency_ms": 842,
    "cost": {
      "llm_usd": 0.000118,
      "fee_multiplier": 1.2,
      "total_usd": 0.000142,
      "total_micros": 6280,
      "kai_price_usd": 0.0000226
    }
  }
}
```

**Fields:**

| Field | Meaning |
|---|---|
| `answer` | The LLM's completion text. |
| `tokens_in` / `tokens_out` | Prompt + completion token counts from the upstream LLM. |
| `latency_ms` | End-to-end wall time on our side. |
| `cost.llm_usd` | Raw model cost, before fees. |
| `cost.fee_multiplier` | Service fee multiplier applied (e.g. `1.2` = +20%). |
| `cost.total_usd` | What you actually paid in USD equivalent. |
| `cost.total_micros` | What was debited from your custodial balance. |
| `cost.kai_price_usd` | KAI/USD rate used for the USD→micros conversion. |

Re-run `GET /v1/billing/balance` — `balance_micros` should have dropped by exactly `cost.total_micros`.

---

## 6. OpenAI-compatible endpoint (drop-in for the OpenAI SDK)

If you already have code written against OpenAI's Chat Completions API,
you can point it at agent-k.ai with **a single base-URL change** — no
code changes.

### Python (`openai` SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://agent-k.ai/v1",
    api_key="kai-...",
)

resp = client.chat.completions.create(
    model="qwen35-gptq",
    messages=[
        {"role": "system", "content": "You are a terse assistant."},
        {"role": "user", "content": "In one sentence: what is EMH?"},
    ],
    max_tokens=200,
)
print(resp.choices[0].message.content)
print(f"Used {resp.usage.total_tokens} tokens")
```

### Streaming

```python
stream = client.chat.completions.create(
    model="qwen35-gptq",
    messages=[{"role": "user", "content": "Count to 3."}],
    stream=True,
    max_tokens=500,
)
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
```

### Raw curl

```bash
curl -sS -N -X POST https://agent-k.ai/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "messages": [
      {"role": "system", "content": "You are a terse assistant."},
      {"role": "user",   "content": "In one sentence: what is EMH?"}
    ],
    "max_tokens": 200
  }'
```

**Response** (matches the OpenAI schema exactly):

```json
{
  "id": "chatcmpl-a7490141691...",
  "object": "chat.completion",
  "created": 1775681861,
  "model": "qwen35-gptq",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "The efficient-markets hypothesis..."},
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 21,
    "completion_tokens": 38,
    "total_tokens": 59
  }
}
```

Streaming response is SSE with the OpenAI chunked format, terminated by
`data: [DONE]`. The final chunk before `[DONE]` carries a `usage` object
with the token counts we used for billing.

### Pick a model: `kai-fast` vs `kai-smart`

The public API exposes two model aliases, both backed by the same
reasoning-capable underlying LLM but with different default behavior:

| Alias | Reasoning | Best for | Cost |
|---|---|---|---|
| **`kai-fast`** | off | Classification, routing, structured output, simple Q&A, template filling, agent sub-steps | Cheap — you only pay for visible output tokens |
| **`kai-smart`** | on (default) | Market structure reads, multi-step analysis, code generation, portfolio reviews, anything needing deep thought | Billed for all generated tokens including hidden reasoning |

**Both aliases are billed at the same per-1k-token rate.** The cost
savings on `kai-fast` are organic — it simply generates 10–100× fewer
tokens for the same prompt because the model skips its hidden
chain-of-thought stage entirely.

Real numbers from the same `"Say hi in one word."` prompt:

| Model | Visible answer | `completion_tokens` | Cost ratio |
|---|---|---|---|
| `kai-fast` | `"Hello"` | **2** | **1×** |
| `kai-smart` | `"\n\nHi"` | **~200** | **~100×** |

```bash
# Classification — use kai-fast
curl -sS -X POST https://agent-k.ai/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "kai-fast",
    "messages": [{"role": "user", "content": "Classify: bull or bear?"}],
    "max_tokens": 20
  }'

# Market analysis — use kai-smart
curl -sS -X POST https://agent-k.ai/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "kai-smart",
    "messages": [{"role": "user", "content": "Analyze BTC Wyckoff structure from the last 48h."}],
    "max_tokens": 1000
  }'
```

Python SDK:

```python
client = OpenAI(base_url="https://agent-k.ai/v1", api_key="kai-...")

# Fast path
fast = client.chat.completions.create(
    model="kai-fast",
    messages=[{"role": "user", "content": "Classify: bull or bear?"}],
    max_tokens=20,
)

# Smart path
smart = client.chat.completions.create(
    model="kai-smart",
    messages=[{"role": "user", "content": "Write a Wyckoff analysis of BTC."}],
    max_tokens=1000,
)
```

Call `GET /v1/models` at any time to list the current aliases with
their descriptions and default reasoning settings.

### Reasoning override (power users)

The `reasoning: bool` request field overrides the alias default
per-call:

- `model: kai-smart, reasoning: false` → kai-fast behavior without
  switching model ids. Useful when you're piping the same request
  builder through different call sites and want the default to be
  kai-smart but one specific call to save tokens.
- `model: kai-fast, reasoning: true` → forces thinking on even on the
  fast alias. Rare but occasionally useful for adaptive flows.
- Omit the field → use the alias default.

```python
# Mostly smart, but a cheap classification sub-step
resp = client.chat.completions.create(
    model="kai-smart",
    messages=[{"role": "user", "content": "Is this bullish? BTC 70k→72k"}],
    max_tokens=20,
    extra_body={"reasoning": False},  # override for this one call
)
```

### What this gives you vs `/v1/ai/chat`

| | `/v1/chat/completions` | `/v1/ai/chat` |
|---|---|---|
| Shape | OpenAI (messages array) | Simple `{message}` |
| System prompts | Yes | No |
| Multi-turn | Yes | No (one prompt per call) |
| Streaming | Yes (`stream: true`) | No |
| OpenAI SDK compat | Yes | No (agent-k.ai envelope) |
| Billing | Debits KAI balance | Debits KAI balance |
| Usage recorded | `/v1/chat/completions` | `/v1/ai/chat` |

Pick `/v1/chat/completions` if you want OpenAI SDK compatibility or
streaming. Pick `/v1/ai/chat` if you want the thin envelope with an
explicit cost breakdown inline in the response.

---

## 7. Fetch market data

Historical OHLCV candles from our TimescaleDB — free to query, rate-limited
by your API key tier, **not billed per-call**:

```bash
curl -sS "https://agent-k.ai/v1/market/ohlcv/BTCUSDT?interval=1h&limit=10" \
  -H "Authorization: Bearer $KEY"
```

**Response:**

```json
{
  "v": 1,
  "data": [
    [1775671200000, 71849.5, 71852.9, 71048.9, 71068.2, 864.7525],
    [1775674800000, 71068.0, 71346.6, 70980.1, 71294.5, 535.2607],
    ...
  ]
}
```

Each row is `[timestamp_ms, open, high, low, close, volume]`. Query
parameters:

| Param | Meaning | Default |
|---|---|---|
| `interval` | `1m`, `5m`, `15m`, `1h`, `4h`, `12h`, `1d` | `1h` |
| `limit` | 1–1000 rows | `100` |
| `from` | Start timestamp in ms (optional) | — |
| `to` | End timestamp in ms (optional) | — |

Feed the result straight into your AI chat call:

```python
candles = requests.get(
    f"{BASE}/v1/market/ohlcv/BTCUSDT",
    headers=HEADERS,
    params={"interval": "1h", "limit": 48},
).json()["data"]

prompt = f"Here are the last 48 hourly BTC candles. What structure do you see?\n{candles}"
answer = ask(prompt, max_tokens=800)
```

---

## 8. Live streaming via WebSocket

`GET /v1/market/ohlcv/{symbol}` is great for history and cold-start
warmup. For a live chart / TUI / trading bot you want updates as they
happen — that's what `wss://agent-k.ai/v1/ws` is for.

**Free.** Not metered per message. Rate-limited per API key (max 50
concurrent channels per connection).

### Connect + subscribe

```python
import asyncio
import json
import os
import websockets

KEY = os.environ["KAI_API_KEY"]  # "kai-..."

async def stream():
    url = f"wss://agent-k.ai/v1/ws?api_key={KEY}"
    async with websockets.connect(url) as ws:
        # 1) Welcome frame arrives first
        welcome = json.loads(await ws.recv())
        print("connected:", welcome)

        # 2) Subscribe to channels
        await ws.send(json.dumps({
            "op": "subscribe",
            "channels": ["market.BTC.1m", "market.ETH.1m", "market.SOL.1m"],
        }))

        # 3) Consume events
        async for raw in ws:
            msg = json.loads(raw)
            op = msg["op"]

            if op == "snapshot":
                # One snapshot per subscribed channel, delivered before
                # the first live update. Up to 50 recent candles as
                # [ts, open, high, low, close, volume] tuples.
                print(f"snapshot {msg['channel']}: {len(msg['data'])} bars")

            elif op == "event":
                # Per-tick update to the current candle. is_closed=True
                # marks a candle that has finalized (its time window
                # passed); is_closed=False is an in-progress update.
                d = msg["data"]
                print(
                    f"tick {msg['channel']} "
                    f"o={d['open']} c={d['close']} "
                    f"closed={d['is_closed']}"
                )

            elif op == "ping":
                # Respond with pong; server closes the connection if
                # you miss ~3 consecutive heartbeats.
                await ws.send(json.dumps({"op": "pong", "ts": msg["ts"]}))

asyncio.run(stream())
```

### Channel naming

| Channel | Meaning | Payload |
|---|---|---|
| `market.{SYMBOL}.{INTERVAL}` | OHLCV candle updates | `{symbol, interval, ts, open, high, low, close, volume, is_closed, source}` |

- **Symbol** is the base ticker: `BTC`, `ETH`, `SOL`, etc. You can also
  send `BTCUSDT` or `BTC-USD` — the server normalizes.
- **Interval** is one of `1m`, `5m`, `15m`, `1h`, `4h`, `12h`, `1d`
  (matching the REST endpoint). Live 1m updates arrive 3–4× per second
  per active symbol straight from BingX.
- **`is_closed: false`** means the candle is still forming and its
  open/high/low/close/volume may update again before its time window
  rolls over. **`is_closed: true`** means the candle window has passed
  and this is a finalized update.
- **`ts`** is the candle's **open time** in unix milliseconds. Two
  ticks for the same (symbol, interval, ts) are the same candle being
  updated; a new ts means a new candle has started.

### Client protocol reference

**Outbound (client → server):**

```json
{"op": "subscribe",   "channels": ["market.BTC.1m", "market.ETH.1m"]}
{"op": "unsubscribe", "channels": ["market.ETH.1m"]}
{"op": "ping",        "ts": 1775681800000}
{"op": "pong",        "ts": 1775681800000}
```

**Inbound (server → client):**

```json
{"op": "welcome",      "version": 1, "heartbeat_interval_ms": 30000, "ts": ...}
{"op": "subscribed",   "channels": ["market.BTC.1m"], "ts": ...}
{"op": "unsubscribed", "channels": ["market.BTC.1m"], "ts": ...}
{"op": "snapshot",     "channel": "market.BTC.1m", "data": [[ts,o,h,l,c,v], ...]}
{"op": "event",        "channel": "market.BTC.1m", "data": {...}, "ts": ...}
{"op": "ping",         "ts": ...}
{"op": "error",        "code": "unknown_op" | "too_many_channels" | "bad_json", "message": "...", "ts": ...}
```

### Reconnect guidance

Server state is not preserved across reconnects — if the connection
drops, re-connect and re-send your subscribe list. You'll get fresh
snapshots automatically, so you can safely drop any in-flight live
deltas during the gap. Use exponential backoff: 1s → 2s → 4s → 8s,
cap at 30s.

### Auth + limits

- **Auth:** API key via `?api_key=kai-...` query string on the connect
  URL. Server returns close code `4401` if the key is missing or
  invalid. (Browsers can't set custom WebSocket headers reliably, so
  the query string is the portable option.)
- **Concurrent channels per connection:** 50 (error `too_many_channels`
  if you exceed it).
- **Heartbeat:** the server sends `{"op": "ping"}` every 30s. Respond
  with `{"op": "pong", "ts": <echo>}`. If you miss ~3 in a row, the
  server closes with code `1011`.

---

## 9. Inspect your usage history

Every billable call is recorded:

```bash
curl -sS 'https://agent-k.ai/v1/billing/usage?limit=20' \
  -H "Authorization: Bearer $KEY"
```

Returns a paginated list of `{endpoint, tokens_in, tokens_out, cost_micros, latency_ms, created_at}` rows — feed it into your own cost-attribution dashboard.

---

## Error handling

Errors return standard HTTP status codes with a JSON body:

```json
{ "detail": "Insufficient custodial balance" }
```

| Status | Meaning | What to do |
|---|---|---|
| `401` | Missing or invalid auth header | Send `Authorization: Bearer kai-...` or `X-API-Key: kai-...` |
| `402` | Zero / negative KAI balance | Top up via Stripe or on-chain deposit |
| `400` | Bad request body | Check field names + types against the OpenAPI spec |
| `404` | Resource not found (e.g. token, symbol) | Verify the identifier |
| `429` | Rate limited | Your key's `rate_limit_rpm` / `rate_limit_tpm` was exceeded |
| `5xx` | Server error | Retry with backoff; if it persists, contact support |

---

## Idiomatic Python

Using `requests`:

```python
import os
import requests

BASE = "https://agent-k.ai"
KEY = os.environ["KAI_API_KEY"]  # "kai-..."
HEADERS = {"Authorization": f"Bearer {KEY}"}

def ask(message: str, model: str | None = None, max_tokens: int = 500) -> dict:
    body = {"message": message, "max_tokens": max_tokens}
    if model:
        body["model"] = model
    resp = requests.post(f"{BASE}/v1/ai/chat", headers=HEADERS, json=body, timeout=60)
    resp.raise_for_status()
    return resp.json()["data"]

result = ask("Summarize the last CPI print in 2 sentences.")
print(result["answer"])
print(f"Cost: {result['cost']['total_micros']} micros "
      f"(${result['cost']['total_usd']:.6f})")
```

---

## Other useful endpoints

Beyond `/v1/ai/chat` there's a small read-only data surface you can call
with the same key — rate-limited but **not billed per-call** unless
otherwise noted:

| Endpoint | What it returns |
|---|---|
| `GET /v1/market/ohlcv/{symbol}` | OHLCV candles for a CEX symbol |
| `GET /v1/news` | AI-enriched market news feed |
| `GET /v1/tokens/trending?network_id=...` | Top trending tokens on a given chain |
| `GET /v1/tokens/{network_id}/{address}` | Full metadata for a specific token |
| `GET /v1/pumpfun/top` | Top pump.fun tokens |
| `GET /v1/models` | Available AI models + their per-1k-token rates |
| `GET /v1/health` | Liveness check — no auth required |

Full schemas + response examples are in `agent-k-ai-openapi.yaml`. Drop
it into [editor.swagger.io](https://editor.swagger.io) for a live
Swagger UI with try-it-out, or into
[redocly.com](https://redocly.github.io/redoc/) for polished HTML docs.

---

## Getting help

- **API reference:** `agent-k-ai-openapi.yaml` in this bundle
- **Dashboard:** `https://agent-k.ai/account`
- **Health check:** `curl https://agent-k.ai/v1/health`
