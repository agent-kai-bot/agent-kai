# Polygon Chain Intelligence Dashboard — Design

**Port:** 3200
**Stack:** SvelteKit + TailwindCSS + D3.js + lightweight-charts
**Theme:** Deep space dark with electric cyan/magenta accents

---

## Design Philosophy

This isn't a generic block explorer. It's a **chain intelligence
command center** — think Bloomberg Terminal meets Tron Legacy.
Every pixel communicates data. Every animation has purpose.
The dashboard should feel alive with the chain's heartbeat.

---

## Color Palette

```
Background:     #050a12 → #0a1628 (radial gradient, subtle blue shift)
Surface:        rgba(8, 20, 40, 0.85) (glassmorphism with backdrop-blur)
Border:         rgba(0, 220, 255, 0.12) (electric cyan glow)
Primary accent: #00dcff (electric cyan)
Secondary:      #8b5cf6 (vivid purple)
Positive:       #00ff88 (neon green)
Negative:       #ff3366 (hot pink/red)
Warning:        #ffaa00 (amber)
Text primary:   #e8f0ff
Text muted:     #6b82a6
```

### Glow effects
- Panel borders: `box-shadow: 0 0 20px rgba(0, 220, 255, 0.06)`
- Active elements: `box-shadow: 0 0 12px rgba(0, 220, 255, 0.15)`
- Whale alerts: pulsing `box-shadow` animation in hot pink
- New block: brief cyan flash across the header

---

## Layout — 5 Zones

```
┌─────────────────────────────────────────────────────────────────┐
│  HEADER — Chain Pulse Bar                                        │
│  [block ████████] [tps ██] [gas ██] [indexed ██%] [lag 0s]      │
├──────────────────────┬──────────────────────────────────────────┤
│                      │                                          │
│  TOKEN CARDS         │  LIVE TRANSFER FEED                      │
│  (left column)       │  (center — the main show)                │
│                      │                                          │
│  ┌──────────────┐    │  ┌──────────────────────────────────┐    │
│  │ USDC         │    │  │ ▶ 0xab..cd → 0xef..12  $45,230  │    │
│  │ $15.2M xfers │    │  │ ▶ 0x34..56 → 0x78..90  $1,200   │    │
│  │ 42K holders  │    │  │ ▷ 0xbc..de → 0xf0..23  $89      │    │
│  │ ████████ 62% │    │  │ 🐋 0x12..34 → 0x56..78 $890K   │    │
│  └──────────────┘    │  │ ▶ 0xcd..ef → 0x90..12  $3,450   │    │
│  ┌──────────────┐    │  └──────────────────────────────────┘    │
│  │ USDT         │    │                                          │
│  │ $7.8M xfers  │    │  TRANSFER VOLUME CHART                   │
│  │ 38K holders  │    │  (area chart — 24h by hour)              │
│  └──────────────┘    │  ┌──────────────────────────────────┐    │
│  ┌──────────────┐    │  │    ╱╲    ╱╲                      │    │
│  │ WMATIC       │    │  │  ╱  ╲╱╱  ╲  ╱╲                  │    │
│  │ ...          │    │  │╱         ╲╱  ╲___               │    │
│  └──────────────┘    │  └──────────────────────────────────┘    │
│  ┌──────────────┐    │                                          │
│  │ WETH         │    ├──────────────────────────────────────────┤
│  └──────────────┘    │  BOTTOM ROW                              │
│  ┌──────────────┐    │  ┌────────────┬─────────────┬──────────┐│
│  │ WBTC         │    │  │ GAS GAUGE  │ WHALE ALERTS │ INDEXER  ││
│  └──────────────┘    │  │  ◉ 32 gwei │ 🐋 $2.1M    │ STATUS   ││
│                      │  │   ▔▔▔▔▔▔   │ 🐋 $890K    │ ████ 57% ││
│                      │  └────────────┴─────────────┴──────────┘│
└──────────────────────┴──────────────────────────────────────────┘
```

---

## Component Details

### 1. Chain Pulse Bar (header)

A slim, always-visible header that shows the chain's vital signs:

- **Block counter**: animated number that ticks up with each new
  block. Brief cyan flash on increment. Shows block number in
  monospace font.
- **TPS gauge**: transactions per second, computed from recent
  blocks. Animated bar that breathes.
- **Gas indicator**: current base fee in gwei, color-coded
  (green < 30, yellow 30-100, red > 100). Animated needle gauge.
- **Indexer progress**: backfill percentage with animated progress
  bar. Glows when actively indexing.
- **Head lag**: seconds behind chain head. Green = 0, yellow < 5,
  red > 10. Pulses if lagging.
- **Throughput**: transfers/sec being indexed. Small sparkline.

**Animation:** On each new block, a subtle wave of light sweeps
left-to-right across the entire header bar.

### 2. Token Cards (left column)

One card per tracked token. Each card shows:

- **Token symbol + name** (large, bold)
- **Total transfers indexed** (animated counter)
- **Unique holders** (from balance table)
- **Transfer volume 24h** (in USD if available, raw otherwise)
- **Top holder concentration** (horizontal bar: top 10 vs rest)
- **Activity sparkline** (7-day transfer count, tiny line chart)
- **Trend arrow** (up/down vs yesterday)

Cards have a subtle glow border. On hover, the card elevates with
a stronger glow and shows additional details (top 5 holders, 
recent large transfers).

**Animation:** When a new transfer comes in for a token, its card
briefly pulses with a cyan ring.

### 3. Live Transfer Feed (center, top)

The centerpiece. A real-time scrolling feed of token transfers:

- Each row shows: token icon, from → to (truncated addresses),
  amount, USD value (if available), time ago
- **Whale transfers (>$10K)** get a special treatment:
  - Hot pink glow border
  - Whale emoji 🐋
  - Larger row height
  - Animated entrance (slide in from right)
- Normal transfers fade in smoothly
- Clicking a row opens the tx on Polygonscan

Feed auto-scrolls but pauses on hover. Shows last 50 transfers.
Auto-refreshes every 2 seconds from the analytics API.

**Stacking behavior:** When many transfers arrive at once,
they cascade in with a staggered animation (each row 50ms after
the previous).

### 4. Transfer Volume Chart (center, middle)

An area chart showing transfer volume over time:

- X axis: last 24 hours, bucketed by hour
- Y axis: transfer count or USD volume
- Stacked by token (each token a different color with opacity)
- Gradient fill under each line
- Hover tooltip shows exact values per token

Built with lightweight-charts (same as the KAI trading terminal)
or D3.js for the stacked area effect.

### 5. Gas Gauge (bottom left)

A circular gauge showing current Polygon gas price:

- Needle moves smoothly between readings
- Color zones: green (cheap), yellow (normal), red (expensive)
- Center shows exact gwei value
- Below: "avg 24h: X gwei" text
- Small sparkline of gas over last 6 hours

CSS-only animated gauge with `conic-gradient` and `transform: rotate()`.

### 6. Whale Alert Panel (bottom center)

Recent large transfers (>$10K equivalent):

- Shows last 10 whale movements
- Each entry: token, amount, from → to, time ago
- Color-coded by size:
  - $10K-$100K: cyan
  - $100K-$1M: purple
  - >$1M: hot pink with glow animation
- Click to expand and see full addresses + tx link

**Animation:** New whale alerts slide in from the bottom with a
bounce effect and a brief glow pulse.

### 7. Indexer Status Panel (bottom right)

Shows the health of the indexer stack:

- **Backfill progress**: animated progress bar with percentage
- **Services status**: green/red dots for each service
  (gateway, ingest, decoder, analytics)
- **Data stats**: total transfers, blocks, events indexed
- **Lag**: blocks behind chain head
- **Rate**: blocks/sec, transfers/sec being processed

---

## Animations & Micro-interactions

### Page load
- Background gradient slowly shifts color (30s cycle)
- Panels fade in with a staggered cascade (left → center → bottom)
- Numbers count up from 0 to their current value (1.5s ease-out)

### Data updates
- New block: header flashes, block counter ticks
- New transfer: feed row slides in, relevant token card pulses
- Whale alert: dramatic entrance animation + sound-ready (muted by default)
- Gas change: gauge needle rotates smoothly

### Hover effects
- Cards elevate with shadow + glow increase
- Feed rows highlight with subtle cyan underline
- Addresses show full value in tooltip
- Charts show crosshair + value popover

### Glassmorphism
Every panel uses:
```css
background: rgba(8, 20, 40, 0.75);
backdrop-filter: blur(16px);
border: 1px solid rgba(0, 220, 255, 0.08);
border-radius: 16px;
```

---

## Data Sources

All data comes from the analytics API at `http://localhost:8100`:

| Component | Endpoint | Refresh |
|---|---|---|
| Chain pulse | `/health` + `/v1/polygon/status` | 5s |
| Token cards | `/v1/polygon/tokens` | 30s |
| Holder data | `/v1/polygon/tokens/{addr}/holders` | 60s |
| Transfer feed | `/v1/polygon/tokens/{addr}/transfers?since=...&limit=50` | 2s |
| Volume chart | Custom aggregation from transfers | 60s |
| Gas gauge | `/v1/polygon/gas` | 10s |
| Gas history | `/v1/polygon/gas/history?hours=24` | 60s |
| Whale alerts | `/v1/polygon/whale-transfers?min_usd=10000` | 5s |
| Indexer status | `/v1/polygon/status` | 5s |

---

## Tech Stack

- **SvelteKit** with static adapter (same as KAI web UI)
- **TailwindCSS** for utility styling + custom theme
- **D3.js** for the volume chart and gas gauge
- **lightweight-charts** (optional, for any candlestick views)
- **Docker**: nginx serving the built static files
- **Port**: 3200

---

## Docker Setup

```dockerfile
FROM node:20-slim AS builder
WORKDIR /app
COPY package*.json .
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/build /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 3200
```

```nginx
# nginx.conf
server {
    listen 3200;
    root /usr/share/nginx/html;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    # Proxy analytics API to avoid CORS
    location /api/ {
        proxy_pass http://analytics:8000/;
        proxy_set_header Host $host;
    }
}
```

Add to the existing `polygon-indexer/docker-compose.yml`:
```yaml
  dashboard:
    build:
      context: ./dashboard
    ports:
      - "3200:3200"
    depends_on:
      analytics:
        condition: service_healthy
    restart: unless-stopped
```
