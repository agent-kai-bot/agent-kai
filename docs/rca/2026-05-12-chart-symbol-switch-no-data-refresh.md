# RCA: Chart Symbol Switch Does Not Refresh Candle Data

Date: 2026-05-12

## Scope

This RCA is based on static code analysis of the web chart path. No live daemon
reproduction was run.

Bug report:

> The chart does not update when switching tokens. One time it updated when the
> agent chat responded.

This is distinct from PR #10412. That change handled price-scale auto-refit
after a symbol change. This issue was about stale candle data.

## Data Path Reviewed

The chart data path is:

1. UI changes chart view state.
2. `+page.svelte` updates `chartSymbol`, `chartTimeframe`, and `chartSource`.
3. The web socket should subscribe to the matching chart stream.
4. Incoming `chart_bar` envelopes should update `chartBars`.
5. `ChartPanel.svelte` receives `bars` and calls Lightweight Charts
   `setData`.

`ChartPanel.svelte` was not the primary failure point. It maps every `bars`
entry into candlestick and histogram data and calls `setData` at
`web/src/lib/components/ChartPanel.svelte:66-84`. Its `$effect` also reads the
`bars` prop and calls `applyBars()` at
`web/src/lib/components/ChartPanel.svelte:156-166`.

## Reproduction From Code

The symbol selector eventually calls `requestChartViewUpdate(...)`, which
patches the daemon UI state and applies the returned chart view at
`web/src/routes/+page.svelte:211-225`.

Before this fix, `applyChartViewState(...)` assigned the new symbol, timeframe,
and source, then kicked off `refreshSidebarData()` and `refreshChartData()`.
It did not clear the old `chartBars` buffer before the async refresh completed.

Before this fix, `refreshChartData()` assigned `chartBars` only after
`client.fetchChartHistory(...)` returned. The previous-symbol bars stayed in
the buffer during the request, and there was no request sequence guard to stop a
slow old request from overwriting a newer market selection. Pre-fix evidence:
`web/src/routes/+page.svelte:669-682` in the base revision.

Before this fix, the web app never subscribed to the chart channel on attach.
It subscribed only to `signals` and `nats`. Pre-fix evidence:
`web/src/routes/+page.svelte:861-862` in the base revision.

The daemon only forwards `chart.bar` session events when a chart subscription
exists. It drops them when `subscriptions["chart"]` is empty at
`daemon/server.py:2429-2436`, and it only adds chart subscriptions when the
client sends a chart `subscribe` envelope at `daemon/server.py:3771-3775`.

Before this fix, the web socket client had a `subscribe(...)` method but no
matching `unsubscribe(...)` method. Pre-fix evidence:
`web/src/lib/daemon/client.ts:217-228` in the base revision.

Before this fix, `applyEnvelope(...)` had handlers for `session_attached`,
`status`, `token`, `final`, `chart_view`, `watchlist`, `signal`, `nats_event`,
scheduled events, and `error`, but no `chart_bar` branch. Pre-fix evidence:
`web/src/routes/+page.svelte:734-793` in the base revision, especially the jump
from `chart_view` to `watchlist` at `web/src/routes/+page.svelte:759-764`.

Result: after a token switch, the chart title and price scale could update,
but candle data remained the previous market's `chartBars` until a later REST
history refresh or wider state event replaced the array.

## Hypotheses

### 1. WebSocket receive task is outside Svelte reactivity

Status: refuted.

The existing WebSocket envelope path already updates Svelte state for status,
chat tokens, final chat messages, watchlist, signals, and NATS events in
`applyEnvelope(...)`. Those are normal assignments, not out-of-runtime writes.
The missing piece was not Svelte visibility of assignments. The missing piece
was that chart bar envelopes had no handler before this fix.

### 2. `applyBars` only fires on prop change, not array mutation

Status: mostly refuted for the pre-fix code.

The pre-fix parent did not append live bars with `push(...)`. It replaced the
array from REST history with `chartBars = await client.fetchChartHistory(...)`
in the base revision at `web/src/routes/+page.svelte:676-681`.

`ChartPanel.svelte` reacts to the `bars` prop and calls `setData(...)` at
`web/src/lib/components/ChartPanel.svelte:156-166`.

The fix keeps the live path on the assignment pattern as well:
`chartBars = nextBars` at `web/src/routes/+page.svelte:850`.

### 3. Chart subscription is sticky to the first mounted symbol

Status: verified, but stronger than the hypothesis.

The subscription was not just sticky. The web app did not create a chart
subscription at all before this fix. Attach-time subscriptions were limited to
signals and NATS in the base revision at `web/src/routes/+page.svelte:861-862`.

The daemon requires an explicit chart subscription to forward `chart_bar`
envelopes. Evidence: `daemon/server.py:2429-2436` and
`daemon/server.py:3771-3775`.

The fixed code now watches the active connection, symbol, and timeframe, then
sends `unsubscribe(old)` followed by `subscribe(new)` when the stream key
changes at `web/src/routes/+page.svelte:285-313`.

### 4. Stale bars are left in the buffer

Status: verified.

Before this fix, `chartBars` was not cleared on market change. It remained on
screen until the async REST refresh resolved. A slow response for an older
market could also win the race and overwrite the newer market.

The fixed code resets the bar buffer and increments a request sequence on
market change at `web/src/routes/+page.svelte:276-282`, and calls that reset
before refreshing after a chart view change at
`web/src/routes/+page.svelte:201-208`.

`refreshChartData()` now captures the requested connection, symbol, timeframe,
source, and sequence, then discards stale responses at
`web/src/routes/+page.svelte:727-762`.

### 5. Race on `lastMarketKey` in `ChartPanel`

Status: refuted.

`lastMarketKey` only controls price-scale auto-refit and first-fit behavior in
`ChartPanel.svelte:156-166`. It does not own the bar buffer and does not fetch
or subscribe to candle data. `applyBars()` always maps whatever `bars` prop it
receives and pushes it into Lightweight Charts at `ChartPanel.svelte:66-84`.

## Actual Root Cause

The parent chart orchestration did not own the live chart stream lifecycle.

Specifically:

1. `+page.svelte` did not subscribe to `channel: "chart"` for the selected
   symbol and timeframe.
2. `DaemonConnection` did not expose `unsubscribe(...)`, even though the daemon
   protocol supports it.
3. `+page.svelte` did not handle `chart_bar` envelopes, so any chart bars that
   did arrive would have been ignored.
4. On market changes, `chartBars` was not cleared before the REST history
   refresh completed.
5. REST history refreshes were unsequenced, so stale responses could overwrite
   newer selections.

## Why Chat Could Appear To Fix It

The code does not show the `final` chat envelope directly refreshing chart
bars. Pre-fix `final` handling only appended an AI chat message and cleared the
streaming reply.

The code-backed explanation is incidental timing. Chat or slash-command flows
can cause daemon UI-state events that arrive as `chart_view`; that path calls
`applyChartViewState(...)`, which triggers a REST chart refresh. Separately,
the existing 15 second poll calls `refreshChartData()`. Either broader refresh
could replace stale bars shortly after a chat response, making the chat path
look causal even though the chart stream itself was not wired correctly.

## Fix Summary

The fix keeps the diff scoped to the web chart path:

1. Added `web/src/lib/chart-stream.ts` for chart stream keys, subscribe action
   ordering, stale bar filtering, compact bar normalization, and immutable bar
   application.
2. Added `DaemonConnection.unsubscribe(...)` in
   `web/src/lib/daemon/client.ts:228-237`.
3. Added a Svelte effect that syncs the chart stream subscription on active
   connection, symbol, and timeframe at `web/src/routes/+page.svelte:1120-1122`.
4. Added reset-on-market-change and request sequencing at
   `web/src/routes/+page.svelte:276-282` and
   `web/src/routes/+page.svelte:727-762`.
5. Added a guarded `chart_bar` handler that drops stale old-symbol bars and
   reassigns `chartBars` at `web/src/routes/+page.svelte:841-852`.

## Test Coverage

Added Vitest coverage for:

1. `unsubscribe(old)` then `subscribe(new)` action ordering.
2. No resubscribe when the stream key is unchanged.
3. Dropping stale bars from an old symbol or timeframe.
4. Normalizing compact daemon bar payloads such as `o/h/l/c/v`.
5. Reassigning, ordering, replacing, and limiting live bars.
6. The daemon client emitting chart `subscribe` and `unsubscribe` envelopes.
