# Architecture Artifact — Task 10372

## Title
KAI dashboard low-res redesign — move topbar info to side panel + responsive layout

## Context inspected
- Taskboard task `10372`: architecture request, no description beyond title, no blockers/dependencies, one orchestrator fire comment.
- Web dashboard implementation:
  - `web/src/routes/+page.svelte`: monolithic Svelte route containing dashboard state, topbar, chart toolbar, model picker/actions, and three-column content grid.
  - `web/src/app.css`: current global dashboard layout and responsive rules.
  - `web/src/lib/components/*Panel.svelte`: reusable panel frame and dashboard panels.
  - Existing tests under `web/src/routes/page.test.ts`, `web/src/lib/*test.ts`.
- Relevant docs:
  - `docs/architecture.md`: web dashboard is the browser client attached to daemon sessions.
  - `docs/chart-panel.md`, `docs/watchlist-and-positions.md`: terminal/dashboard panel model and chart/watchlist interactions.

## Problem statement
The current connected dashboard spends too much vertical and horizontal space on `dashboard-topbar`. On low-resolution screens (especially laptop-height displays and narrow widths), the topbar contains:

1. Brand/session/status/model/chart/positions/watchlist metadata.
2. Chart controls.
3. Model picker and session actions.
4. Extra status summary lines below the header.

Existing CSS tries to compress or hide topbar fragments at low height, but this creates an inconsistent information model: important data is sometimes hidden, chart controls still compete with metadata, and the central chart/chat area loses usable space. The redesign should move informational topbar content into a side panel and make the core trading workflow responsive without changing daemon protocols or data contracts.

## Goals
1. Recover vertical space for chart + chat on low-resolution displays.
2. Preserve all existing operational information; do not simply hide it.
3. Keep chart controls reachable and predictable.
4. Reuse existing Svelte state and panel conventions where practical.
5. Avoid backend/API changes unless a later implementation discovers missing data.
6. Make responsive behavior deterministic at desktop, low-res laptop, tablet, and phone widths/heights.

## Non-goals
- No redesign of daemon websocket/REST APIs.
- No change to chart state persistence semantics (`chart_symbol`, `chart_timeframe`, `chart_source`, `chart_layout_mode`).
- No trading/order-flow changes.
- No replacement of Lightweight Charts.
- No broad component-library migration.

## Chosen design

### Summary
Replace the current metadata-heavy topbar with a compact command bar plus an `Overview` side panel. Move the topbar's informational content and secondary operational status into the right sidebar. Keep the chart toolbar in the top command area on desktop/tablet, but make it wrap into compact rows and optionally relocate below/above the chart on very narrow mobile screens. The connected dashboard becomes:

```text
Desktop / >= 1100px wide
┌──────────────────────────────────────────────────────────────────────────┐
│ Compact command bar: KAI | chart controls | model/action controls        │
├───────────────┬─┬───────────────────────────────────────┬─┬──────────────┤
│ Watchlist     │ │ Chart                                 │ │ Overview     │
│ Positions     │ │ Chat                                  │ │ Signals      │
│               │ │ Input                                 │ │ Events       │
└───────────────┴─┴───────────────────────────────────────┴─┴──────────────┘
```

```text
Low-res laptop / constrained height
┌──────────────────────────────────────────────────────────────────────────┐
│ Slim command bar: symbol/timeframes/source/star/refresh + essential acts │
├──────────────┬─┬────────────────────────────────────────┬─┬──────────────┤
│ Watch/Pos    │ │ Chart + Chat                            │ │ Overview     │
│              │ │                                        │ │ Signals/Bus  │
└──────────────┴─┴────────────────────────────────────────┴─┴──────────────┘
```

```text
Mobile / <= 700px wide
┌──────────────────────────────────────┐
│ Sticky compact command bar           │
│ Chart controls                       │
├──────────────────────────────────────┤
│ Chart                                │
│ Chat                                 │
│ Overview (collapsible)               │
│ Watchlist / Positions / Signals ...  │
└──────────────────────────────────────┘
```

### Structural changes

#### 1. Introduce `OverviewPanel.svelte`
Add a new component under `web/src/lib/components/OverviewPanel.svelte` using the existing `Panel.svelte` frame.

Props should be plain data from existing route state:

```ts
type OverviewPanelProps = {
  activeSession: string;
  currentStatus: string;
  queueDepth: number;
  selectedAgentLabel: string;
  modelStatus: string;
  snapshotSummary: string;
  chartSymbol: string;
  chartTimeframe: string;
  chartSource: string;
  chartMode: ChartMode;
  chartUpdateLabel: string;
  chartPriceLabel: string;
  chartChangeLabel: string;
  streamLatencyLabel: string;
  streamThroughputLabel: string;
  positionsCount: number;
  watchlistCount: number;
  signalCount: number;
  schedulerEventCount: number;
  natsEventCount: number;
  attachError?: string;
  mobileCollapsible?: boolean;
  initiallyOpen?: boolean;
};
```

The implementation can group values as compact definition-list sections:
- Session: session, status, queue.
- Market: chart symbol/timeframe/source/mode, price, 24h change.
- Runtime: selected model, model status, stream latency/throughput, chart update label.
- Activity: positions, watchlist, signals, NATS, scheduled jobs.
- Errors: attach/chart/model errors where applicable.

Important: `selectedAgentLabel`, `chartUpdateLabel`, `formatPrice`, `formatChange`, `streamLatencyLabel`, and `streamThroughputLabel` are currently functions in `+page.svelte`. Pass their evaluated strings/values to avoid duplicating formatting logic.

#### 2. Shrink `dashboard-topbar` into `dashboard-commandbar`
In `+page.svelte`, replace header responsibilities:

Current header contents:
- `dashboard-heading` with brand and `dashboard-meta` status strip.
- `chart-toolbar`.
- `dashboard-actions` with model picker, Stop, Disconnect, Ctrl+K.

Recommended new header:
- Small brand block: `KAI` + compact connection pill (`currentStatus`, queue optionally).
- Existing `chart-toolbar` as the primary control group.
- Existing `dashboard-actions` as a secondary control group, but hide verbose model selects behind responsive behavior (see breakpoints).

Do not keep `dashboard-meta.status-strip` in the topbar. Its values belong in `OverviewPanel`.

The standalone lines below the header:
- `<p class="model-status">{modelStatus}</p>`
- `<p class="dashboard-summary">{snapshotSummary}</p>`

should be removed from the main shell and represented inside `OverviewPanel`. This lets `.dashboard-shell` use fewer rows:

```css
.dashboard-shell {
  grid-template-rows: auto minmax(0, 1fr);
}
```

If `attachError` remains global, place it directly above the grid or inside Overview. Recommended: show a short global error banner only for blocking attach failures, but duplicate non-blocking details in Overview.

#### 3. Right sidebar composition
Change the right column order from:
1. Signals
2. NATS
3. Scheduled Jobs

to:
1. Overview
2. Signals
3. Activity panel(s)

Recommended default:
```svelte
<div class="dashboard-column right">
  <OverviewPanel ... />
  <SignalPanel ... />
  <EventPanel eyebrow="Bus" ... />
  <EventPanel eyebrow="Scheduler" ... />
</div>
```

On desktop this can be four rows with Overview slightly smaller:
```css
.dashboard-column.right {
  grid-template-rows: minmax(9rem, 0.85fr) minmax(0, 1.15fr) minmax(0, 1fr) minmax(0, 1fr);
}
```

If four rows feel too cramped, combine NATS and scheduler into a single `ActivityPanel` in a later implementation phase. For the first slice, keeping both existing `EventPanel`s minimizes code risk.

#### 4. Responsive grid behavior
Use width and height breakpoints with clear responsibilities.

Recommended breakpoints:

- `>= 1100px`: full three-column grid with resizers.
- `800px-1099px`: two-column grid: left column + center, right column full-width below or right column retained if width allows. Prefer center priority.
- `<= 700px`: single-column stacked layout with mobile-collapsible panels.
- `max-height: 850px`: command bar density only; do not hide metadata because Overview now owns it.
- `max-height: 720px`: make the command bar as thin as possible; avoid `display:none` of essential actions except optional labels.

Desktop CSS sketch:
```css
.dashboard-shell {
  grid-template-rows: auto minmax(0, 1fr);
}

.dashboard-commandbar {
  display: grid;
  grid-template-columns: minmax(7rem, auto) minmax(0, 1fr) minmax(13rem, auto);
  align-items: center;
  gap: 0.65rem;
  padding: 0.5rem 0.65rem;
}

.dashboard-grid {
  grid-template-columns:
    minmax(13rem, var(--left-pane, 20%))
    0.55rem
    minmax(0, 1fr)
    0.55rem
    minmax(15rem, var(--right-pane, 22%));
}
```

Low-height CSS should compress controls, not remove data:
```css
@media (max-height: 850px) {
  .dashboard-shell { gap: 0.4rem; padding: 0.45rem; }
  .dashboard-commandbar { padding: 0.3rem 0.45rem; gap: 0.4rem; }
  .chart-toolbar { padding: 0.25rem 0.35rem; }
  .chart-toolbar-status { display: none; } /* duplicated in Overview */
}
```

Mobile CSS should stop using fixed full-viewport hidden overflow for stacked content. Preserve scroll:
```css
@media (max-width: 700px) {
  body.dashboard-active { overflow: auto; }
  .landing-shell.dashboard-mode { height: auto; min-height: 100dvh; overflow: visible; }
  .dashboard-shell { height: auto; min-height: 100dvh; overflow: visible; }
  .dashboard-commandbar { position: sticky; top: 0; z-index: 10; grid-template-columns: 1fr; }
  .dashboard-grid { grid-template-columns: 1fr; grid-template-areas: "center" "right" "left"; overflow: visible; }
}
```

#### 5. Chart toolbar priority model
The chart toolbar should remain the primary top command affordance because chart switching is core to dashboard usage. Make controls degrade by priority:

1. Always visible: symbol combobox, timeframe group, source, refresh/star.
2. Low-height optional: chart status chips (`Ready`, price, change, stream metrics) because Overview duplicates them.
3. Narrow width: wrap controls to multiple rows; buttons may use shorter labels (`Split`, `Hide`, `↻`).
4. Mobile: toolbar becomes single-column or two-row inside sticky command bar.

Avoid putting chart controls only in a sidebar; that would separate controls from the chart and slow down the main workflow.

#### 6. Model controls priority model
Model picker is important but not needed every minute. Keep it in the command bar on desktop. For low width/height:
- Phase 1: let existing selects wrap; shorten button labels if needed.
- Phase 2 (optional): move model picker into Overview as an expandable `Runtime controls` section, leaving only `Stop`, `Disconnect`, and `Ctrl+K` in the command bar.

Because model switching has stateful controls, do not duplicate interactive controls in both topbar and Overview in the same phase unless events and disabled states are carefully synchronized.

## Data contracts
No daemon API changes are required.

### Existing state consumed by Overview
All values are already present in `+page.svelte`:
- Session/runtime: `activeSession`, `currentStatus`, `queueDepth`, `modelStatus`, `selectedAgentLabel()`.
- Market/chart: `chartSymbol`, `chartTimeframe`, `chartSource`, `chartMode`, `chartQuote`, `chartBars`, `chartUpdateLabel()`.
- Stream: `streamLatencyLabel()`, `streamThroughputLabel()`.
- Activity: `portfolio.positions.length`, `watchlist.length`, `signalAlerts.length`, `natsEvents.length`, `schedulerEvents.length`.
- Errors: `attachError`, `chartUpdateError` if exposed.

### Component contract stability
`OverviewPanel` should be presentation-only:
- No daemon calls.
- No state mutation except local collapse/expand through `Panel.svelte`.
- No dependency on `DaemonClient`.
- All labels formatted by parent, or with simple display fallback.

This keeps data ownership in `+page.svelte` and makes future extraction of dashboard layout easier.

## Rejected alternatives

### A. Only hide current topbar items at low height
Rejected. Existing CSS already does this partially and it causes information loss/inconsistency. The task specifically asks to move topbar info to a side panel.

### B. Move all topbar content, including chart controls, into a side panel
Rejected. Chart controls are high-frequency actions and should remain spatially near the chart. Moving them to the sidebar would increase pointer travel and make mobile layout awkward.

### C. Use a drawer/off-canvas menu for all metadata
Rejected for first implementation. It saves space, but critical runtime information becomes hidden behind an extra interaction. The existing dashboard already has side panels that can own this information visibly.

### D. Full layout rewrite with new route/component architecture
Rejected for this task. It would be cleaner long term, but riskier. A focused extraction of `OverviewPanel` and header/grid CSS changes is enough to satisfy the requirement and easier for implementation agents to test.

### E. Backend-side layout profile
Rejected. Layout is purely client-side. Existing chart layout mode persistence is sufficient.

## Failure modes and mitigations

1. **Right sidebar becomes too crowded with Overview + Signals + two EventPanels.**
   - Mitigation: make Overview concise; set sensible `minmax` rows; in Phase 2 combine NATS/Scheduler into one Activity panel if needed.

2. **Low-height screens still lose too much chart space due to wrapped command bar.**
   - Mitigation: hide chart status chips at `max-height: 850px` because Overview duplicates them; shorten labels; test 1366x768.

3. **Mobile page traps content due to `body.dashboard-active { overflow: hidden; }`.**
   - Mitigation: override to `overflow: auto` for `max-width: 700px`; ensure dashboard shell height is auto.

4. **Duplicate status values get out of sync.**
   - Mitigation: topbar should only show minimal status; full details live in one Overview component driven from the same parent state.

5. **Accessibility regression from reordering controls.**
   - Mitigation: keep semantic `header`, `section aria-label="Chart controls"`, labelled selects/buttons, and test keyboard tab order.

6. **Resizable column percentages create unusable sidebars after breakpoint changes.**
   - Mitigation: clamp CSS grid with `minmax(13rem, ...)` and ignore resizers at `max-width: 1024px` as current code already does.

7. **Chart canvas sizing breaks when mobile/stacked.**
   - Mitigation: keep existing `ResizeObserver` in `ChartPanel`; verify `min-height: 40vh` mobile rule still applies.

## Implementation phases

### Phase 1 — Minimal architecture-compliant redesign
1. Create `web/src/lib/components/OverviewPanel.svelte` using `Panel.svelte`.
2. Import `OverviewPanel` in `web/src/routes/+page.svelte`.
3. Remove `dashboard-meta.status-strip` from the topbar.
4. Rename or restyle header to `dashboard-commandbar` (or keep class name temporarily but change responsibility; preferred: new class for clarity).
5. Move `modelStatus` and `snapshotSummary` display into `OverviewPanel` and remove standalone rows from the shell.
6. Insert `OverviewPanel` at the top of the right sidebar.
7. Update `.dashboard-shell` from four grid rows to two/three rows depending on error-banner handling.
8. Update right sidebar row template for four panels.
9. Replace low-height rules that hide `.dashboard-heading`/metadata with rules that compact commandbar and hide duplicated chart-toolbar status chips only.
10. Run web tests and a production build.

### Phase 2 — Responsive refinement
1. Tune `@media (max-width: 1024px)` to prioritize center content and keep Overview visible before verbose event logs.
2. Tune `@media (max-width: 700px)` stacked order to `center`, `right`, `left` so Overview is near chart/chat.
3. Add short-label classes or CSS-only text hiding for low-res buttons if wrapping remains too tall.
4. Consider combining NATS and Scheduler into one `ActivityPanel` if the right column is too cramped.

### Phase 3 — Optional component extraction
1. Extract `ChartToolbar.svelte` from `+page.svelte` if the route remains hard to maintain.
2. Extract `DashboardCommandBar.svelte` for brand/chart/model controls.
3. Add visual regression/e2e tests with Playwright if the project adopts browser automation.

## Test plan

### Unit/component tests
- Add a test for any pure helper introduced for overview rows (if added).
- Existing `chart-mode` and command palette tests should continue passing.
- If `OverviewPanel` has conditional formatting helpers, test them in isolation rather than relying only on DOM snapshots.

### Build/static checks
Run in `web/`:
```bash
npm test -- --run
npm run build
```
If package scripts differ, use the existing Vite/Vitest commands from `web/package.json`.

### Manual viewport acceptance matrix
Verify connected dashboard at:
- 1920x1080: three columns, compact commandbar, Overview first in right sidebar.
- 1366x768: commandbar is one/two thin rows; no metadata strip; chart/chat gain vertical space; Overview retains session/model/chart details.
- 1024x768: resizers hidden; layout readable; no horizontal overflow.
- 700x900 and 390x844: single-column scroll; sticky commandbar usable; panels collapsible; chat input accessibility acceptable.
- 390x700: no trapped content; body scroll works.

### Interaction checks
- Change chart symbol/timeframe/source from commandbar.
- Hide/show chart mode.
- Add/remove chart symbol from watchlist via star.
- Switch model on desktop width.
- Stop a stream and disconnect.
- Select watchlist row and signal row.
- Open command palette with button and keyboard.
- Verify Overview values update after chart changes, signal arrival, watchlist changes, and stream state changes.

### Accessibility checks
- Keyboard tab order starts at commandbar controls, then main panels.
- Symbol combobox retains `aria-*` behavior.
- Overview uses semantic list/table-like labels, not color alone.
- Buttons retain accessible names after any short-label styling.
- Mobile collapsible panels expose correct `aria-expanded` through existing `Panel.svelte`.

## Rollout guardrails
1. Keep changes client-only and avoid daemon schema changes.
2. Ship behind CSS/markup refactor only; do not change data-fetch cadence or websocket lifecycle.
3. Preserve all existing control IDs used by tests or accessibility where practical, especially `chart-symbol-search` and `symbol-results`.
4. Do not remove existing panel components.
5. Avoid committing generated `web/build`, `.svelte-kit`, or `node_modules` changes.
6. If visual behavior is uncertain, prefer preserving functionality over perfect density.
7. Implementation PR should include screenshots or a written viewport matrix for at least desktop and 1366x768.

## Risks
- The dashboard route is currently large; small markup changes can affect many states. Keep Phase 1 surgical.
- Four panels in the right column may be visually dense. This is acceptable for first pass if panels are scrollable/collapsible; revisit with an ActivityPanel if needed.
- Existing CSS near the mobile block appears to contain a duplicated/incomplete selector fragment around `.dashboard-column.left > *`; implementation should inspect/fix carefully while editing responsive rules.
- Low-res behavior depends on both width and height; testing only browser width is insufficient.

## Acceptance criteria
1. Topbar no longer contains the verbose status/meta strip (`session`, `status`, `queue`, `model`, `chart`, `positions`, `watchlist` as multiple pills).
2. The same information is visible in a right-side `Overview` panel while connected.
3. `modelStatus` and `snapshotSummary` are not standalone vertical rows consuming main shell space; they are represented in Overview.
4. Chart controls remain accessible from the top command area on desktop and low-res laptop screens.
5. At 1366x768, chart/chat have more vertical space than before and the header does not disappear wholesale.
6. At mobile widths, content scrolls and panels remain usable/collapsible; no horizontal overflow.
7. Existing chart, watchlist, positions, signals, event panels, model switch, stop, disconnect, and command palette actions still work.
8. Web tests/build pass.

## Recommended handoff to implementation agent
Implement Phase 1 first in a single PR. Do not attempt a full dashboard component decomposition in the same change. After Phase 1 passes tests and viewport checks, decide whether Phase 2 needs an ActivityPanel or only CSS tuning.
