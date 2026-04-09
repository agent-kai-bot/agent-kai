# Nginx / NPM deployment configs

Reference Nginx configuration for the cloud `agent-k.ai` proxy. These
files document what should be in the production reverse proxy so a
fresh deployment (or a recovery after an NPM database wipe) can be
reproduced exactly.

## Files

- **`agent-k.ai.conf`** — Custom location blocks for the `agent-k.ai`
  proxy host. Paste into NPM → proxy host → Advanced tab → "Custom
  Nginx Configuration".

## What's in here

Two location blocks for the `/v1/` path, in this order:

1. **`location /v1/ws`** — long-prefix match for the WebSocket
   endpoint. Has the `Upgrade` / `Connection` headers and a 1h
   timeout.
2. **`location /v1/`** — catch-all for all REST endpoints. Has the
   request-buffering-off setup for Stripe webhooks and a 120s
   timeout for LLM chat completions.

Order matters: nginx matches the longest prefix first, so the WS
block catches `/v1/ws?...` before the catch-all sees it.

## Why the WebSocket toggle in NPM isn't enough

NPM's "Websockets Support" toggle injects the `Upgrade` /
`Connection` headers into NPM's **default** proxy block. As soon as
you put anything in "Custom Nginx Configuration", the custom block
overrides the default — and unless you copy the upgrade headers
into the custom block too, WebSocket upgrades stop working.

This is the bug we hit on 2026-04-09: `wss://agent-k.ai/v1/ws` was
returning HTTP 404 even though the FastAPI route was registered,
because the upgrade headers were being stripped at the proxy.

## How to verify the fix

After saving the new config in NPM, this one-liner should print a
`{"op": "welcome", ...}` frame:

```bash
.venv/bin/python -c "
import asyncio, json, websockets
async def t():
    key = open('AGENT-KAI-API-KEY.txt').read().strip()
    async with websockets.connect(f'wss://agent-k.ai/v1/ws?api_key={key}') as ws:
        print(await ws.recv())
asyncio.run(t())
"
```

Then a real subscribe should give you a `snapshot` frame followed
by `event` frames as new candles tick:

```bash
.venv/bin/python -c "
import asyncio, json, websockets
async def t():
    key = open('AGENT-KAI-API-KEY.txt').read().strip()
    async with websockets.connect(f'wss://agent-k.ai/v1/ws?api_key={key}') as ws:
        await ws.send(json.dumps({'op': 'subscribe', 'channels': ['market.BTC.1m']}))
        for _ in range(4):
            print(await ws.recv())
asyncio.run(t())
"
```
