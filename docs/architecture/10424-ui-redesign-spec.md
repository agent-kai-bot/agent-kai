# Architecture Spec - #10424 KAI Web UI Redesign
## 0. Preamble
This is a spec-only artifact for task `#10424`. No `.svelte`, `.ts`, `.js`, or `.css` source should be changed by this ticket. The requested implementation
target is a chat-first KAI web dashboard that works at `1920x1024`, has mobile behavior, exposes view toggles, and makes agent tool use visible.
### 0.1 Operator Pain, Restated Verbatim
From `/tmp/file_ui_redesign.json`, the authoritative pain points are:
1. **Chat area unusable on 1920x1024.** Top row (commandbar + chart toolbar) + side panels (chart, watchlist, positions, signal, events, overview) consume too much vertical real estate; chat is squeezed. Current `web/src/routes/+page.svelte` is 1667 lines, single mega-shell.
2. **No quick view switch.** Sometimes operator doesn't need crypto chart at all but still wants alerts, NATS feeds, signals, scheduler visible.
3. **Agent tool use invisible.** When the operator sends a chat message, they can't tell if the agent is working, or what it's doing (which tool/subprocess is running, progress, chain of operations).
4. **Mobile not supported.** Layout breaks below desktop width.
### 0.2 Acceptance Items, Restated
1. Audit `+page.svelte`, including what each section costs in screen real estate.
2. Explain why the current layout fails at `1920x1024`.
3. Specify a new CSS Grid/Flex layout for desktop and mobile breakpoints.
4. Include wireframes for `chat-focus`, `chart`, and `mobile`.
5. Specify view-toggle state, keyboard shortcut, URL state, localStorage, and transitions.
6. Specify agent tool-use UX: streaming envelopes, inline indicator, collapsible groups, error states, duration, and history.
7. Decompose the 1667-line route into focused components.
8. Analyze backend protocol gaps against `chat-activity.ts`.
9. Provide phased delivery, risk register, and test plan.
### 0.3 Source Inventory Completed
The route imports chart-mode, chart-stream, chat-activity, command-palette, daemon client/types, market helpers, and ten Svelte panel components in one file
(`web/src/routes/+page.svelte:1` through `web/src/routes/+page.svelte:65`). The route holds daemon, chart, chat, palette, model, stream metrics, and layout
split state in one script block (`web/src/routes/+page.svelte:67` through `web/src/routes/+page.svelte:137`). The global app stylesheet is loaded once from the
root layout (`web/src/routes/+layout.svelte:1` through `web/src/routes/+layout.svelte:5`). The document viewport is configured in `app.html`
(`web/src/app.html:1` through `web/src/app.html:11`). The frontend stack is Svelte 5, SvelteKit 2, Vite 8, vitest, Playwright, `lightweight-charts`, and
`marked` (`web/package.json:13` through `web/package.json:27`). The app is built as a static SvelteKit bundle with `$lib` aliasing (`web/svelte.config.js:1`
through `web/svelte.config.js:19`). Vitest is configured for jsdom and Testing Library Svelte (`web/vite.config.ts:1` through `web/vite.config.ts:13`).
## 1. Audit Of Current Layout
### 1.1 Mega-Shell Structure
`+page.svelte` is the connected dashboard shell and is 1667 lines. The script block owns:
- Chart layout mode and persisted chart mode state
 (`web/src/routes/+page.svelte:89` through `web/src/routes/+page.svelte:90`).
- Chart symbol, timeframe, source, and symbol-search state
 (`web/src/routes/+page.svelte:91` through `web/src/routes/+page.svelte:96`).
- Chat history, streaming reply, and chat activity state
 (`web/src/routes/+page.svelte:97` through `web/src/routes/+page.svelte:99`).
- Watchlist, portfolio, chart bars, signal, NATS, and scheduler arrays
 (`web/src/routes/+page.svelte:100` through `web/src/routes/+page.svelte:108`).
- Palette state and daemon connection handles
 (`web/src/routes/+page.svelte:109` through `web/src/routes/+page.svelte:114`).
- Model picker and stream-control state
 (`web/src/routes/+page.svelte:115` through `web/src/routes/+page.svelte:130`).
- Manual left, right, and chart split percentages
 (`web/src/routes/+page.svelte:131` through `web/src/routes/+page.svelte:133`).
The markup block owns:
- Connected-mode shell and command bar
 (`web/src/routes/+page.svelte:1174` through `web/src/routes/+page.svelte:1376`).
- Dashboard grid
 (`web/src/routes/+page.svelte:1382` through `web/src/routes/+page.svelte:1538`).
- Disconnected landing state
 (`web/src/routes/+page.svelte:1540` through `web/src/routes/+page.svelte:1653`).
- Command palette mount
 (`web/src/routes/+page.svelte:1656` through `web/src/routes/+page.svelte:1667`).
This is a real smell because layout, data transport, chart commands, keyboard shortcuts, model switching, and component composition all change together.
### 1.2 Current Top Region
The connected shell starts with `.dashboard-shell`, whose rows are `auto` plus `minmax(0, 1fr)` (`web/src/app.css:69` through `web/src/app.css:78`). The first
row is `.dashboard-commandbar`, a three-column CSS grid: brand, chart toolbar, and actions (`web/src/app.css:279` through `web/src/app.css:288`). The commandbar
content includes:
- Brand, status, and queue pill (`web/src/routes/+page.svelte:1177` through
 `web/src/routes/+page.svelte:1182`).
- Full chart toolbar (`web/src/routes/+page.svelte:1184` through
 `web/src/routes/+page.svelte:1331`).
- Router link, auto-loop-brain toggle, model picker, stop, disconnect, and
 palette button (`web/src/routes/+page.svelte:1333` through
`web/src/routes/+page.svelte:1375`). The chart toolbar is itself a grid with controls and status rows (`web/src/app.css:377` through `web/src/app.css:390`). The
toolbar controls include symbol search, seven timeframe buttons, source select, reset, hide chart, watchlist star, and refresh
(`web/src/routes/+page.svelte:1184` through `web/src/routes/+page.svelte:1313`). The toolbar status row reports chart update state, price, 24h change,
first-token latency, and stream throughput (`web/src/routes/+page.svelte:1315` through `web/src/routes/+page.svelte:1330`). The actions region wraps flex items
and a model picker with three selects (`web/src/app.css:492` through `web/src/app.css:516`).
### 1.3 Current Content Grid
The main dashboard grid has five tracks:
```css
left | left-resizer | center | right-resizer | right
```
That is encoded in `.dashboard-grid` (`web/src/app.css:525` through `web/src/app.css:538`). The default route state sets `leftPanePct = 20`, `rightPanePct =
20`, and `chartPanePct = 56` (`web/src/routes/+page.svelte:131` through `web/src/routes/+page.svelte:133`). Those percentages are written into CSS custom
properties by `dashboardGridStyle()` and `centerColumnStyle()` (`web/src/routes/+page.svelte:437` through `web/src/routes/+page.svelte:443`). The left column
contains watchlist and positions (`web/src/routes/+page.svelte:1382` through `web/src/routes/+page.svelte:1395`). The center column contains chart/status, row
resizer, chat panel, and chat input (`web/src/routes/+page.svelte:1404` through `web/src/routes/+page.svelte:1476`). The right column contains overview,
signals, NATS, and scheduled jobs (`web/src/routes/+page.svelte:1485` through `web/src/routes/+page.svelte:1537`). The CSS rows for the center column make chart
the first-class element: `minmax(8rem, var(--chart-pane, 56%))`, row resizer, chat, input (`web/src/app.css:552` through `web/src/app.css:559`). The right
column statically divides overview, signals, NATS, and scheduler into four tracks (`web/src/app.css:561` through `web/src/app.css:564`).
### 1.4 Current Resize Model
The route has pointer handlers for horizontal side-rail resizing (`web/src/routes/+page.svelte:445` through `web/src/routes/+page.svelte:473`). The route has a
pointer handler for vertical chart/chat resizing (`web/src/routes/+page.svelte:475` through `web/src/routes/+page.svelte:495`). The horizontal handler clamps
side panes between 12% and 38% (`web/src/routes/+page.svelte:453` through `web/src/routes/+page.svelte:463`). The vertical chart handler clamps chart height
between 24% and 74% (`web/src/routes/+page.svelte:483` through `web/src/routes/+page.svelte:486`). These are chart-centric controls. They do not model a
top-level `chat-focus` view, and they do not remove side-panel cost.
### 1.5 Current Chart Mode Support
The code already has chart-layout modes and chart persistence. `chart-mode.ts` defines `full`, `half`, `mini`, and `hide` (`web/src/lib/chart-mode.ts:1` through
`web/src/lib/chart-mode.ts:7`). It accepts aliases including `chat -> mini` and `focus -> hide` (`web/src/lib/chart-mode.ts:19` through
`web/src/lib/chart-mode.ts:36`). It persists mode per session under `kai.chart.mode.<session>` (`web/src/lib/chart-mode.ts:121` through
`web/src/lib/chart-mode.ts:153`). `+page.svelte` applies chart mode, updates the daemon snapshot, and writes local storage (`web/src/routes/+page.svelte:178`
through `web/src/routes/+page.svelte:190`). `+page.svelte` restores persisted chart mode before falling back to daemon state (`web/src/routes/+page.svelte:242`
through `web/src/routes/+page.svelte:251`). The existing chart-mode work should be reused. The redesign should not replace it with a second chart-specific state
machine.
### 1.6 Current Chat Panel
`ChatPanel` already accepts an optional `activity` prop with chat activity state (`web/src/lib/components/ChatPanel.svelte:16` through
`web/src/lib/components/ChatPanel.svelte:28`). It auto-scrolls on messages, streaming text, tool count, and auto iteration changes
(`web/src/lib/components/ChatPanel.svelte:34` through `web/src/lib/components/ChatPanel.svelte:46`). It treats a reply as active when either streaming text or
`activity.active` exists (`web/src/lib/components/ChatPanel.svelte:58` through `web/src/lib/components/ChatPanel.svelte:64`). It can show auto-mode badges and
status text in the streaming message header (`web/src/lib/components/ChatPanel.svelte:109` through `web/src/lib/components/ChatPanel.svelte:142`). It renders
active tool rows when `activity.tools.length` is non-zero (`web/src/lib/components/ChatPanel.svelte:143` through `web/src/lib/components/ChatPanel.svelte:171`).
The current tool rows are inline and active-turn-only. They are not grouped per completed turn after finalization because `applyChatActivityEnvelope()` clears
activity on `final` (`web/src/lib/chat-activity.ts:127` through `web/src/lib/chat-activity.ts:129`). On mobile, `ChatPanel` caps the chat log at `36vh`
(`web/src/lib/components/ChatPanel.svelte:438` through `web/src/lib/components/ChatPanel.svelte:443`). That `36vh` rule is incompatible with the new requirement
that chat is always primary on mobile.
### 1.7 Current Panel Frame
All dashboard panels reuse `Panel.svelte`. `Panel` has `mobileCollapsible` and `initiallyOpen` props (`web/src/lib/components/Panel.svelte:2` through
`web/src/lib/components/Panel.svelte:18`). It renders a header and optional mobile toggle button (`web/src/lib/components/Panel.svelte:27` through
`web/src/lib/components/Panel.svelte:62`). The frame is a grid with header and body, full height, and overflow hidden (`web/src/lib/components/Panel.svelte:64`
through `web/src/lib/components/Panel.svelte:76`). At `max-width: 700px`, `Panel` becomes block layout and collapsed bodies are hidden
(`web/src/lib/components/Panel.svelte:131` through `web/src/lib/components/Panel.svelte:170`). The new design should keep `Panel` for repeated content, but a
bottom-sheet ops drawer should not be faked with many independent panel toggles.
### 1.8 Why Current 1920x1024 Layout Fails
At `1920x1024`, `.landing-shell.dashboard-mode` consumes `1rem` padding on every side (`web/src/app.css:47` through `web/src/app.css:53`). `.dashboard-shell`
consumes another `1rem` padding and a `1rem` row gap (`web/src/app.css:69` through `web/src/app.css:78`). The top commandbar is an auto-height row and contains
the chart toolbar plus actions (`web/src/app.css:279` through `web/src/app.css:288`). Because the chart toolbar includes two internal rows and `0.65rem` padding
(`web/src/app.css:377` through `web/src/app.css:390`), it commonly lands around 100-125px tall before shell padding and grid gap. The chat input is always in
the center column as an auto row (`web/src/routes/+page.svelte:1456` through `web/src/routes/+page.svelte:1475`). The chat input CSS adds a bordered panel,
`1rem` padding, prompt chips, a textarea with `4.75rem` minimum height, and a submit button (`web/src/app.css:635` through `web/src/app.css:689`). The center
column gives the chart 56% before the chat row gets the remainder (`web/src/app.css:552` through `web/src/app.css:559`). Approximate vertical math at
`1920x1024`:
- Viewport height: 1024px.
- Outer shell padding: 32px from `.landing-shell.dashboard-mode`.
- Inner shell padding: 32px from `.dashboard-shell`.
- Dashboard shell row gap: 16px.
- Commandbar/chart-toolbar row: about 110px.
- Center column gaps: about 48px across four rows.
- Row resizer: about 9px.
- Chat input: about 175-210px.
- Chart track at 56% of the remaining grid: about 430-470px.
- Residual chat panel: often about 120-210px before panel header/body padding.
This is why a nominally large `1920x1024` monitor still produces a cramped chat. The horizontal math is also against chat:
- Left rail default: 20% (`web/src/routes/+page.svelte:131`).
- Right rail default: 20% (`web/src/routes/+page.svelte:132`).
- Resizer tracks: two `0.55rem` columns (`web/src/app.css:528` through
 `web/src/app.css:533`).
- Grid gaps: `0.65rem` between tracks (`web/src/app.css:525` through
 `web/src/app.css:527`).
- Center column gets the remainder, so chat is roughly 55-57% of content width,
 not the requested 60-70% dominant region.
The "hide chart" state helps vertical space inside the center column, but it does not remove left/right rail cost (`web/src/app.css:557` through
`web/src/app.css:559`). The "hide chart" state is chart layout state, not a whole-app view mode (`web/src/routes/+page.svelte:1279` through
`web/src/routes/+page.svelte:1287`).
### 1.9 Current Mobile Behavior
The current responsive split starts at `max-width: 1099px` (`web/src/app.css:719` through `web/src/app.css:762`). At that width, the grid becomes left plus
center, then right full-width below (`web/src/app.css:737` through `web/src/app.css:744`). At `max-width: 700px`, the grid becomes single column in the order
center, right, left (`web/src/app.css:845` through `web/src/app.css:853`). At `max-width: 700px`, all dashboard columns become block layout
(`web/src/app.css:855` through `web/src/app.css:872`). This is a stacked page, not a chat-primary mobile app. The operator must scroll through
chart/status/panels, and chat itself is capped at `36vh` (`web/src/lib/components/ChatPanel.svelte:438` through `web/src/lib/components/ChatPanel.svelte:443`).
## 2. New Layout Grid
### 2.1 Design Principle
Separate app view mode from chart layout mode. `viewMode` answers: "what is the primary workspace?" `chartMode` answers: "if chart is visible, how tall is it?"
The existing chart mode is preserved for chart users. The new default dashboard surface is `chat-focus`.
### 2.2 Breakpoints
Use these named breakpoints:
| Breakpoint | Width | Effective behavior |
| --- | ---: | --- |
| mobile | `<= 768px` | Single column, chat primary, ops bottom sheet, chart opt-in |
| narrow desktop | `769px - 1366px` | Chat plus compact ops rail; chart mode can use a stacked or two-column variant |
| standard desktop | `1367px - 1919px` | Chat-focus uses chat plus ops rail; chart mode preserves current chart-centric shell |
| wide desktop | `>= 1920px` | Chat-focus target: chat about 65-70%, ops rail about 28-30% |
The existing CSS uses `700px`, `1099px`, and height-only media queries (`web/src/app.css:719` through `web/src/app.css:944`). Implementation should migrate
those numbers to named variables or colocated layout helpers so tests can assert the same breakpoints.
### 2.3 `chat-focus` Wide Desktop
Target for `>= 1920px`:
- Outer shell padding: 12px.
- Command row: 56-64px, no full chart toolbar.
- Main gap: 16px.
- Chat column: `minmax(0, 1fr)`.
- Ops rail: `clamp(30rem, 28vw, 34rem)`.
- At 1920px, ops rail is about 520-540px and chat is about 1280-1320px.
- Chat share is about 68-69% after gaps.
Wireframe:
```text
1920x1024 chat-focus
+------------------------------------------------------------------------------+
| KAI | session/status | View: Chat | symbol chip | model | stop | palette     |
+--------------------------------------------------------------+---------------+
| Chat stream                                                  | Ops rail      |
| - human turn                                                 | Auto brain    |
| - agent working indicator                                    | Alerts        |
| - collapsible tool group                                     | Signals       |
| - agent answer                                               | NATS feed     |
| Composer                                                     | Scheduler     |
|                                                              | Watch/Pos     |
+--------------------------------------------------------------+---------------+
```
Recommended grid:
```css
.dashboard-main[data-view="chat-focus"] {
  display: grid;
  grid-template-columns: minmax(0, 1fr) clamp(30rem, 28vw, 34rem);
  grid-template-areas: "chat ops";
  gap: 1rem;
}
```
The chart toolbar is not visible in `chat-focus`. The chart status remains as a compact symbol/price/source chip in the command row, reusing values currently
rendered by the toolbar status (`web/src/routes/+page.svelte:1315` through `web/src/routes/+page.svelte:1330`).
### 2.4 `chat-focus` Standard Desktop
Target for `1367px - 1919px`:
- Command row: 56-72px.
- Ops rail: `clamp(24rem, 30vw, 30rem)`.
- Chat remainder: usually 62-68%.
- Ops rail sections are accordion-like when height is constrained.
At 1440px content width, a 416-432px ops rail leaves about 930-980px for chat.
### 2.5 `chat-focus` Narrow Desktop
Target for `769px - 1366px`:
- Command row may wrap to two short rows.
- Ops rail: `20rem - 22rem`.
- Chat should remain at least 60% when possible.
- If width is below about 980px, ops rail becomes a drawer even before mobile.
Wireframe:
```text
1024x768 chat-focus
+------------------------------------------------------+
| KAI | View | session | status | actions              |
+-------------------------------------+----------------+
| Chat stream + composer              | Ops rail       |
|                                     | Alerts/NATS    |
|                                     | Signals/Jobs   |
+-------------------------------------+----------------+
```
### 2.6 `chart` Mode
`chart` mode preserves current chart-centric behavior. It keeps:
- Chart toolbar with symbol, timeframe, source, reset, hide chart, star, refresh
 (`web/src/routes/+page.svelte:1184` through `web/src/routes/+page.svelte:1313`).
- Chart panel backed by `lightweight-charts`
 (`web/src/lib/components/ChartPanel.svelte:91` through
`web/src/lib/components/ChartPanel.svelte:178`).
- Watchlist and positions on the left
 (`web/src/routes/+page.svelte:1382` through `web/src/routes/+page.svelte:1395`).
- Overview, signals, NATS, and scheduler on the right
 (`web/src/routes/+page.svelte:1485` through `web/src/routes/+page.svelte:1537`).
- Existing chart split controls and persisted chart layout
 (`web/src/routes/+page.svelte:178` through `web/src/routes/+page.svelte:190`).
Wireframe:
```text
chart mode
+------------------------------------------------------------------------------+
| KAI | full chart toolbar                                      | actions       |
+--------------+-+-------------------------------------+-+--------------------+
| Watchlist    | | Chart                               | | Overview           |
| Positions    | | resize                              | | Signals            |
|              | | Chat                                | | NATS               |
|              | | Composer                            | | Scheduler          |
+--------------+-+-------------------------------------+-+--------------------+
```
The implementation can keep the current CSS grid initially, then move it behind `ChartWorkspace.svelte` or `ChartModeLayout.svelte` during decomposition.
### 2.7 `mobile` Mode
Mobile is an effective mode at `<= 768px`. It is automatically selected based on viewport, while preserving the requested desktop view in storage. Mobile
requirements:
- Single column.
- Chat is first and primary.
- Composer is sticky to the bottom if safe on the target browser.
- Ops rail is a bottom-sheet drawer.
- Chart is hidden by default.
- Chart opens through a command-row button or bottom-sheet tab.
Wireframe:
```text
mobile <= 768px
+-----------------------------+
| KAI | Chat | Ops | Chart     |
+-----------------------------+
| Chat stream                  |
| Agent working + tools        |
| Messages                     |
| Composer                     |
+-----------------------------+

Bottom sheet when Ops is open:
+-----------------------------+
| drag handle | Ops            |
| Auto brain                   |
| Alerts                       |
| Signals                      |
| NATS                         |
| Scheduler                    |
| Watchlist / Positions        |
+-----------------------------+
```
Mobile chart opt-in:
```text
+-----------------------------+
| Chart | close                |
+-----------------------------+
| Lightweight chart canvas     |
| Symbol/timeframe controls    |
+-----------------------------+
```
### 2.8 Ops Rail Composition
The ops rail should include, in order:
1. Session/auto-loop-brain status.
2. Alerts/signals.
3. NATS feed.
4. Scheduler.
5. Watchlist/positions compact market section.
The right-column data already exists in route state:
- Signal alerts are appended from `signal` envelopes
 (`web/src/routes/+page.svelte:883` through `web/src/routes/+page.svelte:886`).
- NATS rows are appended from `nats_event`
 (`web/src/routes/+page.svelte:889` through `web/src/routes/+page.svelte:894`).
- Scheduler rows are appended from scheduled-job envelopes
 (`web/src/routes/+page.svelte:897` through `web/src/routes/+page.svelte:899`).
- Watchlist and portfolio are refreshed by REST polling
 (`web/src/routes/+page.svelte:773` through `web/src/routes/+page.svelte:795`).
Do not remove watchlist or positions. In `chat-focus`, compress them into the ops rail rather than dedicating a full left column.
## 3. View-Toggle Mechanism
### 3.1 State Model
Add a small view-mode helper, not a chart-mode replacement. Suggested type:
```ts
export type DashboardViewMode = "chat-focus" | "chart";
export type EffectiveDashboardView = DashboardViewMode | "mobile";
```
Keep `mobile` as derived effective state, not a normal persisted desktop choice. Derived state:
```ts
requestedView: DashboardViewMode;
effectiveView: EffectiveDashboardView;
mobileOpsOpen: boolean;
mobileChartOpen: boolean;
```
`effectiveView` is `mobile` when `matchMedia("(max-width: 768px)")` is true. On mobile, `requestedView` still controls where the app returns when widened.
### 3.2 Persistence Priority
Resolve initial requested view in this order:
1. URL query param `?view=chat-focus` or `?view=chart`.
2. Session-scoped localStorage key.
3. Global localStorage key for pre-attach state.
4. Default `chat-focus`.
Use a storage pattern parallel to chart mode. `chart-mode.ts` already uses a prefix and session-normalized key (`web/src/lib/chart-mode.ts:121` through
`web/src/lib/chart-mode.ts:153`). Suggested keys:
- `kai.dashboard.view.global`
- `kai.dashboard.view.<session>`
### 3.3 URL State
URL must round-trip refresh. When the user selects a view:
- Update in-memory requested view.
- Update localStorage.
- Update `?view=...` using `history.replaceState` or SvelteKit navigation.
- Avoid adding browser history entries for every toggle.
On load:
- Parse `view`.
- Ignore unknown values.
- If `view=mobile` appears, treat it as `chat-focus` requested view and let
 viewport derive mobile.
### 3.4 Keyboard Shortcut
Existing keyboard handling reserves `Ctrl/Cmd+K` for the command palette (`web/src/routes/+page.svelte:1127` through `web/src/routes/+page.svelte:1137`).
Recommended default:
- `Ctrl/Cmd+Shift+V` toggles between `chat-focus` and `chart`.
- Ignore the shortcut while focus is inside `input`, `textarea`, `select`, or
 contenteditable.
- Do nothing while the command palette is open, except let Escape/Enter remain
 palette-owned.
Also add command palette items for view changes. The palette helper already contains chart commands (`web/src/lib/command-palette.ts:8` through
`web/src/lib/command-palette.ts:53`). Add non-daemon UI commands in a later implementation as local palette actions, not slash commands sent to the daemon.
### 3.5 Toggle UI
Add `ViewToggle.svelte`. It should render a segmented control:
- Chat
- Chart
On mobile, it should render:
- Chat
- Ops
- Chart
Props:
```ts
type ViewToggleProps = {
  requestedView: DashboardViewMode;
  effectiveView: EffectiveDashboardView;
  mobileOpsOpen: boolean;
  mobileChartOpen: boolean;
  onViewChange: (view: DashboardViewMode) => void;
  onMobileOpsToggle: () => void;
  onMobileChartToggle: () => void;
};
```
### 3.6 Transition Behavior
Use instant layout switching for Phase 1. Reasons:
- It avoids chart resize race conditions with `ResizeObserver`.
- It makes Playwright assertions deterministic.
- The current chart component already responds to container resize
 (`web/src/lib/components/ChartPanel.svelte:138` through
`web/src/lib/components/ChartPanel.svelte:144`). After stabilization, small opacity transitions are acceptable for bottom-sheet open/close only. Do not animate
grid track sizes in Phase 1.
### 3.7 Default Logic
Default first visit to `chat-focus`. If URL or storage says `chart`, respect it. If viewport is mobile, effective view is `mobile`, chat is shown first, and
chart is closed until explicitly opened. This satisfies the operator's stated pain without deleting chart-centric behavior.
## 4. Agent Tool-Use UX
### 4.1 Existing Infrastructure To Reuse
`chat-activity.ts` already defines `ToolActivity` with id, tool, args preview, state, elapsed milliseconds, and ok flag (`web/src/lib/chat-activity.ts:12`
through `web/src/lib/chat-activity.ts:19`). It defines `ChatActivityState` with active flag, started timestamp, status text, tool array, auto state, and next
tool id (`web/src/lib/chat-activity.ts:31` through `web/src/lib/chat-activity.ts:38`). It accepts status, token, final, tool start/end, and auto activity
envelopes (`web/src/lib/chat-activity.ts:40` through `web/src/lib/chat-activity.ts:48`). It creates compact argument previews and truncates them
(`web/src/lib/chat-activity.ts:101` through `web/src/lib/chat-activity.ts:120`). It starts tool rows on `tool_start` (`web/src/lib/chat-activity.ts:145` through
`web/src/lib/chat-activity.ts:161`). It completes tool rows on `tool_end` (`web/src/lib/chat-activity.ts:164` through `web/src/lib/chat-activity.ts:199`). This
should be the foundation. Do not introduce a competing envelope mapper.
### 4.2 Current UI Partial Answer
`ChatPanel` already shows active status and tool rows inside the streaming assistant message (`web/src/lib/components/ChatPanel.svelte:109` through
`web/src/lib/components/ChatPanel.svelte:176`). This partially addresses "agent working". It does not fully satisfy #10424 because:
- It is not collapsible.
- It disappears when `final` clears activity.
- It only has `running` and `complete`.
- It relies on frontend-assigned numeric ids, not backend call ids.
- It cannot show failed tool details beyond `ok === false`, and current backend
 never sends `ok: false` for `tool_end`.
### 4.3 UX Requirements
Every active agent turn should show:
- Inline "Agent is working" status.
- Tool stream inside the assistant turn.
- Tool name.
- Status: pending, running, done, error, timeout, retrying.
- Redacted short args summary, max 80 visible characters.
- Duration in milliseconds or compact seconds.
- Collapsed summary after the turn finishes.
The tool stream belongs inside chat, not in a separate global panel.
### 4.4 Turn Group Behavior
Add `ChatTurnGroup.svelte`. For each assistant turn:
- Render message body.
- Render `AgentActivityStream` above streaming text while active.
- After final, keep a collapsed activity summary for the current browser session.
- Default collapsed after completion if all tools succeeded.
- Default expanded if any tool failed or timed out.
State should be bounded. Keep activity sidecars for the last 50 assistant turns in browser memory. Do not try to persist historical tool activity in Phase 1
unless the backend adds turn history later.
### 4.5 Tool Status Semantics
Map envelopes to UI states:
| Wire event | UI state |
| --- | --- |
| `tool_start` received | running |
| `tool_end` with `ok: true` | done |
| `tool_end` with `ok: false` | error |
| no end after timeout threshold | timeout |
| future retry envelope | retrying |
Pending is useful if the backend later emits planned tool calls before start. Do not invent pending rows on the client unless a daemon envelope supports them.
### 4.6 Redaction
Current args previews stringify arbitrary args (`web/src/lib/chat-activity.ts:101` through `web/src/lib/chat-activity.ts:120`). The UI requirement is stricter:
visible preview must be at most 80 characters and redacted. Redaction should replace likely secrets in keys or values:
- token
- password
- secret
- api_key
- authorization
- bearer
- key
The frontend should still treat backend-provided summaries as untrusted display text.
### 4.7 Worked Example
```text
User
  Check BTC risk and pause noisy jobs if needed.
Agent                                          14:32:05 EDT
  Working... analyzing request
  Tools (3)  [expanded]
  - running  fetch_chart_history  args: {"symbol":"BTC","tf":"1h"}       0ms
  - done     list_scheduled_jobs  args: {"scope":"session"}              184ms
  - error    pause_scheduled_job  args: {"job_id":"sched_12"}            420ms
             scheduler rejected: job belongs to another session
  I checked the BTC context and found no need to pause jobs owned by this session...
```
After completion:
```text
Agent                                          14:32:05 EDT
  Tools (3, 1 failed) [Show details]
  I checked the BTC context and found no need to pause jobs owned by this session...
```
### 4.8 Accessibility
Use `aria-live="polite"` for the active working line. Do not announce every token. Tool rows should be a list with status text, not icon-only status. Collapsed
tool summary should be a button with `aria-expanded`.
## 5. Component Decomposition
### 5.1 Extraction Goal
`+page.svelte` should become orchestration, not a mega-shell. It should own:
- daemon connection lifecycle;
- top-level stores;
- URL/storage synchronization;
- handlers that call `DaemonClient`.
It should not own:
- full commandbar markup;
- full chart toolbar markup;
- grid layout markup;
- ops rail composition;
- activity stream presentation;
- mobile drawer behavior.
### 5.2 Proposed Components
#### `DashboardCommandBar.svelte`
Owns brand/status, `ViewToggle`, compact chart chip, model controls, stop, disconnect, palette entry, and auto brain placement for chart mode. Props:
```ts
type DashboardCommandBarProps = {
  activeSession: string;
  currentStatus: string;
  queueDepth: number;
  requestedView: DashboardViewMode;
  effectiveView: EffectiveDashboardView;
  chartSummary: ChartSummary;
  modelState: ModelPickerState;
  isStoppingStream: boolean;
  onViewChange: (view: DashboardViewMode) => void;
  onStop: () => void;
  onDisconnect: () => void;
  onOpenPalette: () => void;
};
```
Test:
- renders chat/chart toggle;
- calls view change;
- does not render full chart toolbar in chat-focus.
#### `ChartToolbar.svelte`
Extracts the current toolbar from `+page.svelte`. Current source is `web/src/routes/+page.svelte:1184` through `web/src/routes/+page.svelte:1331`. Props:
```ts
type ChartToolbarProps = {
  symbolSearch: string;
  chartSymbol: string;
  chartTimeframe: string;
  chartSource: string;
  chartMode: ChartMode;
  isUpdatingChart: boolean;
  chartUpdateLabel: string;
  priceLabel: string;
  changeLabel: string;
  streamLatencyLabel: string;
  streamThroughputLabel: string;
  suggestions: SymbolSuggestion[];
  onPatch: (patch: ChartViewPatch) => void;
};
```
Test:
- timeframe buttons dispatch patch;
- hide chart dispatches `{ mode: "hide" }`;
- symbol combobox remains keyboard-accessible.
#### `DashboardWorkspace.svelte`
Owns top-level view layout. Props:
```ts
type DashboardWorkspaceProps = {
  requestedView: DashboardViewMode;
  effectiveView: EffectiveDashboardView;
  mobileOpsOpen: boolean;
  mobileChartOpen: boolean;
};
```
Slots:
- `chat`
- `chart`
- `marketRail`
- `opsRail`
- `composer`
Test:
- at desktop chat-focus, chart slot is not in layout;
- at chart mode, chart slot is present;
- at mobile, ops slot is in bottom sheet.
#### `OpsRail.svelte`
Composes overview, auto brain, signals, NATS, scheduler, watchlist, and positions into a compact right rail. It should reuse existing panels:
- `OverviewPanel` already groups status and activity counts
 (`web/src/lib/components/OverviewPanel.svelte:111` through
`web/src/lib/components/OverviewPanel.svelte:171`).
- `SignalPanel` already filters and renders alert cards
 (`web/src/lib/components/SignalPanel.svelte:61` through
`web/src/lib/components/SignalPanel.svelte:129`).
- `EventPanel` already renders generic event rows
 (`web/src/lib/components/EventPanel.svelte:42` through
`web/src/lib/components/EventPanel.svelte:84`).
- `WatchlistPanel` already filters, sorts, adds, removes, and selects symbols
 (`web/src/lib/components/WatchlistPanel.svelte:87` through
`web/src/lib/components/WatchlistPanel.svelte:160`).
- `PositionsPanel` already renders responsive positions
 (`web/src/lib/components/PositionsPanel.svelte:25` through
`web/src/lib/components/PositionsPanel.svelte:68`). Test:
- all required sections render with counts;
- NATS and scheduler empty states remain visible;
- auto-loop toggle remains reachable.
#### `AgentActivityStream.svelte`
Extracts the tool list from `ChatPanel`. Current inline source is `web/src/lib/components/ChatPanel.svelte:143` through
`web/src/lib/components/ChatPanel.svelte:171`. Props:
```ts
type AgentActivityStreamProps = {
  activity: ChatActivityState | CompletedTurnActivity;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
};
```
Test:
- running row displays tool name and args summary;
- done row displays duration;
- failed row defaults expanded;
- collapsed summary is accessible.
#### `ChatTurnGroup.svelte`
Wraps human, system, and assistant messages. It should call `renderMarkdown()` as `ChatPanel` does now (`web/src/lib/components/ChatPanel.svelte:104` through
`web/src/lib/components/ChatPanel.svelte:106`). It should receive optional activity sidecars. Test:
- active turn shows streaming pre text;
- completed turn keeps collapsed tool summary;
- timestamps still use `formatChatTimestamp`.
#### `MobileOpsSheet.svelte`
Owns bottom-sheet behavior. Props:
```ts
type MobileOpsSheetProps = {
  open: boolean;
  title?: string;
  onClose: () => void;
};
```
Slot:
- `children`
Test:
- Escape closes;
- backdrop click closes;
- focus returns to opener.
### 5.3 What Existing Components Should Do After Redesign
| Component | Today | After redesign |
| --- | --- | --- |
| `ChatPanel` | Renders history, active streaming reply, inline active tools | Owns chat scroll/composer area or delegates turn rendering to `ChatTurnGroup` |
| `ChartPanel` | Owns chart canvas and meta | Remains chart canvas; only mounted in chart mode or mobile opt-in |
| `WatchlistPanel` | Full left-rail market panel | Compact rail section in chat-focus; full panel in chart mode |
| `PositionsPanel` | Full left-rail portfolio panel | Compact rail section in chat-focus; full panel in chart mode |
| `SignalPanel` | Right-rail alert list | Primary ops rail section |
| `EventPanel` | Generic NATS/scheduler cards | Reused for NATS and scheduler |
| `OverviewPanel` | Right-rail status panel | Top ops rail summary, possibly denser |
| `AutoLoopBrainToggle` | Commandbar action | Commandbar in chart mode; ops rail top in chat-focus/mobile |
| `CommandPalette` | Global modal | Stays global; gains local view commands |
| `Panel` | Generic frame and mobile collapse | Stays frame; not the bottom-sheet controller |
## 6. Backend Protocol Gap Analysis
### 6.1 Current Frontend Envelope Types
The frontend daemon types define:
- `ToolStartEnvelope` as `type`, `tool`, and optional `args`
 (`web/src/lib/daemon/types.ts:253` through `web/src/lib/daemon/types.ts:257`).
- `ToolEndEnvelope` as `type`, `tool`, optional `elapsed_ms`, and `ok`
 (`web/src/lib/daemon/types.ts:259` through `web/src/lib/daemon/types.ts:264`).
- `ServerEnvelope` includes tool start/end, auto activity, signal, chart, NATS,
 scheduled-job, and error envelopes
(`web/src/lib/daemon/types.ts:330` through `web/src/lib/daemon/types.ts:346`).
### 6.2 Current Backend Protocol Types
The backend protocol uses strict Pydantic models with extra fields forbidden (`daemon/protocol.py:21` through `daemon/protocol.py:25`). It defines
`ToolStartEnvelope` with `tool` and `args` (`daemon/protocol.py:130` through `daemon/protocol.py:134`). It defines `ToolEndEnvelope` with `tool`, `elapsed_ms`,
and `ok` (`daemon/protocol.py:136` through `daemon/protocol.py:140`). It includes these envelopes in the server union (`daemon/protocol.py:267` through
`daemon/protocol.py:290`). Because backend models forbid extra fields, any new wire fields must be added to `daemon/protocol.py` before they can be emitted.
### 6.3 Current Backend Emission
The agent runner yields `tool_start` with tool name and input (`agent/core.py:1269` through `agent/core.py:1274`). The agent runner yields `tool_end` with tool
name and output string (`agent/core.py:1275` through `agent/core.py:1279`). The session republishes agent events to the session event bus (`daemon/core.py:1191`
through `daemon/core.py:1209`). The websocket forwarder maps `agent.tool_start` to `ToolStartEnvelope` (`daemon/server.py:2348` through
`daemon/server.py:2356`). The websocket forwarder maps `agent.tool_end` to `ToolEndEnvelope` (`daemon/server.py:2358` through `daemon/server.py:2369`). The
current daemon tracks elapsed time by tool name (`daemon/server.py:2279` through `daemon/server.py:2281` and `daemon/server.py:2350` through
`daemon/server.py:2363`). The current daemon always emits `ok=True` on tool end (`daemon/server.py:2364` through `daemon/server.py:2369`). The forwarder flushes
token buffers before sending non-token envelopes, which helps preserve visible ordering across token/tool boundaries (`daemon/server.py:2316` through
`daemon/server.py:2330`).
### 6.4 Current Attach And Subscription Behavior
The websocket endpoint requires an attach envelope first (`daemon/server.py:3470` through `daemon/server.py:3503`). It sends `session_attached` and initial
`status` (`daemon/server.py:3517` through `daemon/server.py:3532`). It handles input and slash envelopes by either intercepting known slash commands or
forwarding to `run_input` (`daemon/server.py:3542` through `daemon/server.py:3772`). It handles subscriptions for signals, chart, and NATS
(`daemon/server.py:3784` through `daemon/server.py:3799`). The browser client sends `input`, `slash`, `interrupt`, `subscribe`, and `unsubscribe` envelopes
through `DaemonConnection` (`web/src/lib/daemon/client.ts:194` through `web/src/lib/daemon/client.ts:237`). The browser client expects attach to resolve only
after `session_attached` and initial `status` (`web/src/lib/daemon/client.ts:575` through `web/src/lib/daemon/client.ts:664`).
### 6.5 Ops REST Endpoints Already Available
The frontend already has REST methods for:
- sessions (`web/src/lib/daemon/client.ts:333` through
 `web/src/lib/daemon/client.ts:338`);
- model registry and switch (`web/src/lib/daemon/client.ts:340` through
 `web/src/lib/daemon/client.ts:452`);
- auto-loop-brain health/config (`web/src/lib/daemon/client.ts:348` through
 `web/src/lib/daemon/client.ts:374`);
- chart view (`web/src/lib/daemon/client.ts:461` through
 `web/src/lib/daemon/client.ts:499`);
- watchlist (`web/src/lib/daemon/client.ts:501` through
 `web/src/lib/daemon/client.ts:529`);
- quotes, portfolio, and chart history (`web/src/lib/daemon/client.ts:531`
 through `web/src/lib/daemon/client.ts:573`).
The backend exposes matching endpoints for market quotes, OHLCV, portfolio, chart state, watchlist state, and stop (`daemon/server.py:3264` through
`daemon/server.py:3458`). No new REST endpoint is required for Phase 1 layout.
### 6.6 Minimum Additive Protocol Extension
The backend gap is additive, not breaking. Minimum fields:
```ts
type ToolStartEnvelope = {
  type: "tool_start";
  call_id?: string;
  turn_id?: string;
  tool: string;
  args?: unknown;
  args_summary?: string;
  status?: "running";
  ts?: string;
};
type ToolEndEnvelope = {
  type: "tool_end";
  call_id?: string;
  turn_id?: string;
  tool: string;
  elapsed_ms?: number | null;
  ok: boolean;
  status?: "done" | "error" | "timeout";
  error?: string;
  ts?: string;
};
```
Optional `call_id` solves overlapping calls to the same tool. Optional `turn_id` solves grouping when chart/NATS/scheduler envelopes interleave with agent
activity on the same websocket. Optional `args_summary` lets the daemon redact before the browser sees raw args. Optional `status` and `error` let the UI show
failed tool states. Existing clients can continue using `tool` and `ok`.
### 6.7 Back-Pressure And Ordering
Do not emit progress spam for tool calls. Emit at most:
- one start;
- one terminal end;
- optional retry transition if the runtime genuinely retries.
WebSocket ordering is reliable per connection, but unrelated session events can interleave because the forwarder maps all session-bus events into one stream
(`daemon/server.py:2271` through `daemon/server.py:2330`). Use `turn_id` to group UI rows. Use a monotonically increasing optional `seq` only if Playwright or
production logs show reordering bugs. Do not send raw tool output by default.
## 7. Phased Delivery
### Phase 1 - View Toggle And Chat-Dominant Layout
Scope:
- Add view-mode helper/store.
- Add `ViewToggle.svelte`.
- Add `DashboardWorkspace.svelte`.
- Implement `chat-focus` desktop layout.
- Keep existing chart mode intact as `chart`.
- Add URL and localStorage round-trip.
Likely files:
- `web/src/routes/+page.svelte`
- `web/src/app.css`
- `web/src/lib/view-mode.ts`
- `web/src/lib/view-mode.test.ts`
- `web/src/lib/components/ViewToggle.svelte`
- `web/src/lib/components/__tests__/ViewToggle.test.ts`
Tests:
- vitest for parsing/persistence helper.
- component test for toggle events.
- Playwright for `?view=chat-focus` refresh.
- Playwright computed-width assertion at `1920x1024`.
Deploy risk:
- Medium CSS risk due to current global stylesheet.
- Low backend risk.
Dependency:
- None.
### Phase 2 - Ops Rail Extraction
Scope:
- Add `OpsRail.svelte`.
- Move overview, signals, NATS, scheduler, watchlist, positions into rail.
- Keep current panels as child components.
- Make auto-loop-brain reachable in rail for chat-focus/mobile.
Likely files:
- `web/src/lib/components/OpsRail.svelte`
- `web/src/routes/+page.svelte`
- `web/src/app.css`
- panel tests as needed.
Tests:
- component test for all required sections and counts.
- Playwright smoke test that alerts, NATS, signals, scheduler remain visible in
 chat-focus.
Deploy risk:
- Medium due to panel ordering and overflow.
Dependency:
- Phase 1.
### Phase 3 - Agent Activity Stream UI
Scope:
- Add `AgentActivityStream.svelte`.
- Add `ChatTurnGroup.svelte`.
- Keep active and completed-turn activity sidecars.
- Add redaction and 80-character visible summary.
- Add collapsible per-turn group.
Likely files:
- `web/src/lib/chat-activity.ts`
- `web/src/lib/chat-activity.test.ts`
- `web/src/lib/components/AgentActivityStream.svelte`
- `web/src/lib/components/ChatTurnGroup.svelte`
- `web/src/lib/components/ChatPanel.svelte`
Tests:
- vitest for redaction/truncation.
- component tests for running/done/error/collapsed.
- Playwright for streaming turn with three tool envelopes.
Deploy risk:
- Low-to-medium frontend risk.
- Higher if protocol extension is included in same PR.
Dependency:
- Phase 0 additive protocol fields if failed tool states must be fully accurate.
### Phase 4 - Mobile Bottom Sheet
Scope:
- Raise mobile breakpoint to `<= 768px`.
- Remove chat `36vh` cap.
- Add `MobileOpsSheet.svelte`.
- Add mobile chart opt-in.
- Add focus management and Escape/backdrop handling.
Likely files:
- `web/src/lib/components/MobileOpsSheet.svelte`
- `web/src/lib/components/ViewToggle.svelte`
- `web/src/lib/components/ChatPanel.svelte`
- `web/src/app.css`
Tests:
- component tests for sheet open/close.
- Playwright at 390x844 for chat primary layout.
- Playwright for chart opt-in.
Deploy risk:
- Medium mobile interaction risk.
Dependency:
- Phases 1 and 2.
### Phase 5 - Chart Mode Polish And Dead CSS Removal
Scope:
- Extract `ChartToolbar.svelte`.
- Extract chart-mode layout wrapper.
- Remove obsolete global CSS selectors after parity.
- Keep `/chart` slash behavior and chart mode persistence.
Likely files:
- `web/src/lib/components/ChartToolbar.svelte`
- `web/src/lib/components/ChartWorkspace.svelte`
- `web/src/routes/+page.svelte`
- `web/src/app.css`
Tests:
- existing `chart-mode.test.ts`.
- component test for chart toolbar.
- Playwright chart-mode smoke test.
Deploy risk:
- Medium for chart-only users.
Dependency:
- Phase 1.
## 8. Risk Register
### Risk 1 - CSS Regression From Global Layout
The dashboard CSS is global and centralizes shell, commandbar, grid, inputs, and responsive behavior in one file (`web/src/app.css:40` through
`web/src/app.css:944`). Mitigation:
- Start with additive `data-view` selectors.
- Avoid deleting old chart-mode CSS in Phase 1.
- Add computed layout assertions before visual polish.
### Risk 2 - `+page.svelte` Coupling
The route owns transport, data mapping, state, layout, and markup (`web/src/routes/+page.svelte:1` through `web/src/routes/+page.svelte:1667`). Mitigation:
- Extract UI components one at a time.
- Keep handlers in the route until component contracts stabilize.
- Do not refactor daemon client calls during layout PRs.
### Risk 3 - WebSocket Back-Pressure
The forwarder sends tokens, tool envelopes, status, chart, signals, NATS, and scheduler events through one websocket (`daemon/server.py:2271` through
`daemon/server.py:2538`). Mitigation:
- Emit only start/end tool envelopes.
- Keep token buffering.
- Add `turn_id` instead of relying on adjacent events.
### Risk 4 - Mobile WS Reliability
Mobile browsers suspend tabs and network connections aggressively. Current client has close handling but no mobile-specific reconnect UX
(`web/src/routes/+page.svelte:969` through `web/src/routes/+page.svelte:983`). Mitigation:
- Show a sticky reconnect/status banner on mobile.
- Keep unsent composer text in memory during reconnect.
- Avoid auto-resubscribing chart unless chart is open.
### Risk 5 - Chart-Only Users
Existing users may expect the chart-centric layout and current chart toolbar. Mitigation:
- Preserve `chart` mode.
- Persist `chart` selection in URL/localStorage.
- Add command palette entries for `View: Chart` and `View: Chat`.
### Risk 6 - Stale localStorage
Chart mode already persists per session (`web/src/lib/chart-mode.ts:121` through `web/src/lib/chart-mode.ts:153`). New view-mode storage can conflict with old
chart expectations. Mitigation:
- Use new keys.
- Ignore unknown values.
- Prefer URL over storage.
- Provide fallback to `chat-focus`.
### Risk 7 - Tool Privacy
Current frontend can stringify raw tool args (`web/src/lib/chat-activity.ts:101` through `web/src/lib/chat-activity.ts:120`). Mitigation:
- Redact before display.
- Prefer backend-provided `args_summary`.
- Cap visible text to 80 characters.
## 9. Test Plan
### 9.1 Unit Tests
Add vitest coverage for:
- `view-mode.ts` parsing.
- URL query normalization.
- localStorage read/write failure handling.
- shortcut guard for editable elements.
- `chat-activity.ts` redaction and 80-character summary.
- activity status mapping from old and new tool envelopes.
The repo already uses vitest/jsdom (`web/vite.config.ts:5` through `web/vite.config.ts:13`). Existing helper tests cover chart mode, chart stream, command
palette, and market helpers (`web/src/lib/chart-mode.test.ts:1` through `web/src/lib/chart-mode.test.ts:61`, `web/src/lib/chart-stream.test.ts:1` through
`web/src/lib/chart-stream.test.ts:82`, `web/src/lib/command-palette.test.ts:1` through `web/src/lib/command-palette.test.ts:34`).
### 9.2 Component Tests
Add Testing Library Svelte tests for:
- `ViewToggle.svelte`.
- `OpsRail.svelte`.
- `AgentActivityStream.svelte`.
- `ChatTurnGroup.svelte`.
- `MobileOpsSheet.svelte`.
- `ChartToolbar.svelte` when extracted.
The repo already has a component-test pattern for `AutoLoopBrainToggle` (`web/src/lib/components/__tests__/AutoLoopBrainToggle.test.ts:1` through
`web/src/lib/components/__tests__/AutoLoopBrainToggle.test.ts:124`).
### 9.3 Playwright Tests
Add e2e coverage for:
- `?view=chat-focus` loads chat-focus.
- Toggle to chart updates URL.
- Refresh preserves chart.
- Toggle back to chat-focus updates URL and localStorage.
- `Ctrl/Cmd+Shift+V` toggles when focus is not editable.
- `Ctrl/Cmd+K` still opens command palette.
- Mobile width shows chat first and ops as a bottom sheet.
- Mobile chart is hidden until opt-in.
- Tool-use rendering shows three tools with one failure.
### 9.4 Objective 1920x1024 Dominance Assertion
In Playwright:
1. Set viewport to `1920x1024`.
2. Load with `?view=chat-focus`.
3. Locate dashboard main, chat region, and ops rail.
4. Assert:
 - chart panel is absent or hidden;
- `chatWidth / mainWidth >= 0.60`; - `chatWidth / mainWidth <= 0.72`; - `opsWidth / mainWidth >= 0.24`; - `opsWidth / mainWidth <= 0.32`; - chat body height
exceeds 60% of main content height, excluding composer. Prefer computed bounding boxes over screenshot pixel heuristics for the pass/fail gate.
### 9.5 Visual Regression Strategy
Use both:
- targeted screenshots for chat-focus, chart, and mobile;
- manual review checklist for live data states.
Manual checklist:
- long model names do not overflow commandbar;
- empty watchlist/positions/signals/NATS/scheduler states render cleanly;
- long tool args are truncated and redacted;
- failed tool groups are expanded by default;
- chart mode still shows symbol selector and timeframe controls;
- mobile bottom sheet does not trap the composer behind the keyboard.
### 9.6 Backend Protocol Tests
If Phase 0 protocol extension is implemented:
- unit-test `daemon/protocol.py` decode/encode for optional `call_id`,
 `turn_id`, `status`, `args_summary`, and `error`.
- unit-test `_event_to_message` for `tool_end ok=False`.
- unit-test overlapping same-name tools if the agent runtime can surface ids.
- browser test old envelopes to preserve compatibility.
## 10. Phase 1 Implementation Ticket Draft
Title: `Implement KAI dashboard view toggle and chat-focus layout (#10424 phase 1)` Scope:
- Add `view-mode.ts` helper.
- Add `ViewToggle.svelte`.
- Add `DashboardWorkspace.svelte`.
- Add `chat-focus` CSS grid.
- Preserve current chart mode under `view=chart`.
- Add URL/localStorage persistence.
Files:
- `web/src/routes/+page.svelte`
- `web/src/app.css`
- `web/src/lib/view-mode.ts`
- `web/src/lib/view-mode.test.ts`
- `web/src/lib/components/ViewToggle.svelte`
- `web/src/lib/components/__tests__/ViewToggle.test.ts`
Acceptance:
- `1920x1024 ?view=chat-focus` has chat between 60% and 72% of main width.
- Chart panel is absent from chat-focus.
- Ops rail remains visible with overview/signals/NATS/scheduler.
- `?view=chart` preserves current chart-centric layout.
- Toggle state survives refresh.
- `Ctrl/Cmd+K` still opens palette.
## 11. Open Operator Questions
1. Should first-time desktop users always start in `chat-focus`, or should an
 existing daemon `chart_layout_mode` of `full` imply `chart` for legacy sessions?
2. Should watchlist and positions be always visible in `chat-focus`, or acceptable
 as compact rail sections below alerts/NATS/scheduler?
3. Is browser-session-only tool history enough for Phase 3, or should completed
 tool groups survive refresh through daemon-persisted turn metadata?
4. Should mobile chart opt-in open as a full-screen panel or as a tab inside the
 bottom sheet?
