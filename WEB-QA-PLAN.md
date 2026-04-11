# KAI Web Terminal — Comprehensive QA Plan

## What was missed and why

The first QA suite checked structural presence (element exists,
viewport fills, no JS errors) and basic interactions (click
button, fill input). It completely missed:

- **Streaming behavior** — chat scroll during live token output
- **WebSocket message flow** — do tokens actually stream in?
- **Error recovery** — what happens when daemon disconnects mid-stream?
- **State persistence** — does reconnecting restore chat history?
- **Slash command execution** — does typing `/chart` actually work?
- **Real user workflows** — send a message, wait for response, verify it renders
- **Edge cases** — rapid messages, long responses, network interruption
- **Cross-panel interaction** — does asking about BTC update the chart?
- **Performance** — does the UI stay responsive with 100+ messages?

This plan covers everything a real user would encounter.

---

## Test categories

### 1. Connection lifecycle

| # | Test | How to verify |
|---|---|---|
| 1.1 | Fresh load shows landing page | Page contains session selector and Attach button |
| 1.2 | Attach to existing session loads dashboard | Dashboard grid visible, panels populated |
| 1.3 | Attach with invalid session name creates new session | Dashboard loads with empty chat |
| 1.4 | Disconnect returns to landing page | Landing card visible, dashboard gone |
| 1.5 | Reconnect to same session restores chat history | Messages from before disconnect still visible |
| 1.6 | Reconnect to same session restores watchlist | Same tokens showing with prices |
| 1.7 | Bearer token auth works for non-localhost | Set token, attach, verify no 401 |
| 1.8 | Wrong bearer token shows error | Attach attempt fails with auth error message |
| 1.9 | Daemon goes down mid-session | Error shown, no crash, reconnect option |
| 1.10 | Rapid disconnect/reconnect doesn't crash | 5x disconnect+reconnect in 10s |

### 2. Chat — input

| # | Test | How to verify |
|---|---|---|
| 2.1 | Enter submits message | Textarea clears, message appears in chat |
| 2.2 | Ctrl+Enter submits message | Same as above |
| 2.3 | Shift+Enter inserts newline | Textarea grows, no submit |
| 2.4 | Send button submits | Click Send, textarea clears |
| 2.5 | Empty input does nothing on Enter | No empty message in chat |
| 2.6 | Empty input does nothing on Send click | No empty message |
| 2.7 | Very long message (>2000 chars) submits | No truncation, no crash |
| 2.8 | Special chars submit correctly | `<script>`, `"quotes"`, backticks |
| 2.9 | Rapid multiple submits don't duplicate | Send 3 fast, get 3 user messages |
| 2.10 | Input retains focus after submit | Can type next message immediately |

### 3. Chat — streaming response

| # | Test | How to verify |
|---|---|---|
| 3.1 | Agent response streams token-by-token | Text grows progressively, not all at once |
| 3.2 | Chat auto-scrolls during streaming | Scroll position stays at bottom as text grows |
| 3.3 | Streaming indicator visible (dashed border) | `.streaming` class on message |
| 3.4 | Final message renders as markdown | Code blocks, bold, lists format after stream ends |
| 3.5 | User can scroll up during stream | Scrolling up stays put (doesn't force-jump back) |
| 3.6 | Scrolling back to bottom resumes auto-scroll | After user scrolls up then back down |
| 3.7 | Multiple streamed responses don't overlap | Send two messages, each streams independently |
| 3.8 | Stream error shows error message | Kill daemon mid-stream, verify error in chat |

### 4. Chat — rendering

| # | Test | How to verify |
|---|---|---|
| 4.1 | Markdown bold/italic renders | `**bold**` shows as bold |
| 4.2 | Code blocks render with syntax highlighting | ```python ... ``` has background |
| 4.3 | Inline code renders | `code` has monospace background |
| 4.4 | Lists render (ordered and unordered) | Bullets and numbers display |
| 4.5 | Tables render | Columns and rows with borders |
| 4.6 | Links are clickable | `[text](url)` renders as `<a>` |
| 4.7 | Headers render (h1-h4) | Larger font sizes |
| 4.8 | Long code blocks don't overflow panel | Horizontal scroll within code block |
| 4.9 | User messages show "User" label | Green-tinted with correct label |
| 4.10 | Agent messages show "Agent" label | Dark tinted with correct label |

### 5. Slash commands

| # | Test | How to verify |
|---|---|---|
| 5.1 | `/status` returns agent status | Response contains status info |
| 5.2 | `/chart BTC 4h` changes chart | Chart updates symbol/timeframe |
| 5.3 | `/sessions` lists sessions | Response contains session names |
| 5.4 | `/schedule list` shows scheduler | Response about scheduled jobs |
| 5.5 | `/help` shows available commands | Response lists commands |
| 5.6 | Unknown slash command handled | Error message, no crash |
| 5.7 | Slash command via Ctrl+K palette | Open palette, select command, executes |

### 6. Command palette (Ctrl+K)

| # | Test | How to verify |
|---|---|---|
| 6.1 | Ctrl+K opens palette | Modal/overlay appears with command list |
| 6.2 | Typing filters commands | Fewer items shown as you type |
| 6.3 | Enter executes selected command | Palette closes, command runs |
| 6.4 | Escape closes palette | Palette disappears |
| 6.5 | Palette doesn't interfere with chat input | After closing, chat textarea works normally |
| 6.6 | Ctrl+K works from any focused element | Works even when textarea is focused |

### 7. Watchlist panel

| # | Test | How to verify |
|---|---|---|
| 7.1 | Shows tracked tokens (BTC, ETH, SOL) | 3 rows with symbols |
| 7.2 | Prices update live | Values change over time (poll comparison) |
| 7.3 | 24h change shows with color | Green for positive, red for negative |
| 7.4 | Volume displays | Non-zero volume numbers |

### 8. Chart panel

| # | Test | How to verify |
|---|---|---|
| 8.1 | TradingView chart renders candles | Canvas element with non-zero dimensions |
| 8.2 | Chart shows correct symbol | Header/label matches expected token |
| 8.3 | Chart is prominently sized (>25% viewport) | Bounding box check |
| 8.4 | Chart data loads (not empty) | Canvas has drawn content (non-blank pixel check) |

### 9. Positions panel

| # | Test | How to verify |
|---|---|---|
| 9.1 | Shows open positions if any | Position rows with symbol/side/qty |
| 9.2 | Shows P&L data | Dollar or percent values |
| 9.3 | Empty state handled | "No positions" or similar if none |

### 10. Event panels (Alerts, NATS, Scheduler)

| # | Test | How to verify |
|---|---|---|
| 10.1 | Alerts panel exists and renders | Panel visible with header |
| 10.2 | NATS panel exists | Panel visible |
| 10.3 | Scheduler panel exists | Panel visible |
| 10.4 | Empty states show placeholder text | "No alerts yet" etc. |

### 11. Status bar

| # | Test | How to verify |
|---|---|---|
| 11.1 | Shows session name | Contains "terminal" or active session |
| 11.2 | Shows status (idle/thinking) | Contains "idle" or activity |
| 11.3 | Shows queue depth | Contains "queue" with a number |
| 11.4 | Shows watchlist count | Contains "watchlist" with count |
| 11.5 | Updates during agent activity | Status changes from idle to thinking |

### 12. Responsive layout

| # | Test | How to verify (per viewport) |
|---|---|---|
| 12.1 | No horizontal overflow | scrollWidth <= viewportWidth |
| 12.2 | No page-level vertical scroll (desktop) | scrollHeight <= viewportHeight + 50 |
| 12.3 | Dashboard fills viewport width (desktop) | bodyWidth >= 90% viewport |
| 12.4 | 3-column layout (desktop >1024px) | 3 dashboard columns visible |
| 12.5 | Stacked layout (mobile <700px) | Single column, collapsible panels |
| 12.6 | All tap targets >= 44px (mobile) | Min-height check |
| 12.7 | Text readable >= 12px (mobile) | Font-size check |

### 13. Performance

| # | Test | How to verify |
|---|---|---|
| 13.1 | 50+ messages don't lag the UI | Scroll smoothly, no jank |
| 13.2 | Rapid typing doesn't drop keystrokes | Type fast, all chars appear |
| 13.3 | Long streaming response stays smooth | 500+ word response, scroll stays responsive |
| 13.4 | Memory doesn't grow unbounded | Heap snapshot before/after 100 messages |

### 14. Error handling

| # | Test | How to verify |
|---|---|---|
| 14.1 | Network disconnect shows error | Banner or message, no white screen |
| 14.2 | Invalid session data handled | No crash on malformed WS message |
| 14.3 | API timeout handled | Loading indicator, then error message |
| 14.4 | Zero JS console errors in normal flow | No errors in console |

---

## Automation coverage

### Currently automated (Playwright)

- S1 (page load, API health)
- S2 (connection flow — attach/disconnect/reconnect)
- S3 partial (input methods: Enter, Ctrl+Enter, Shift+Enter, Send button)
- S4 partial (chat area exists, has content, has HTML)
- S6 (command palette: open, filter, close)
- S7 (watchlist: rows exist, prices present)
- S8 partial (chart: canvas exists, dimensions)
- S9 (positions: panel exists)
- S10 (events: panels exist)
- S11 (status bar: visible, content)
- S12 (responsive: 9 viewports, overflow, sizing, tap targets)

### NOT yet automated (needs new tests)

- **S3.1–3.8**: Streaming behavior (requires sending a real message
  and observing the WS token stream in the browser)
- **S4.1–4.8**: Markdown rendering detail (need to inject known
  markdown and check rendered HTML)
- **S5**: Slash command execution (need to send `/status` and
  verify the response contains expected data)
- **S11.5**: Status bar updates during activity
- **S13**: Performance (need long-running session with many messages)
- **S14**: Error handling (need to simulate network failures)

### What can't be fully automated

- **3.5–3.6**: Scroll-up-during-stream behavior (requires precise
  timing coordination with a real streaming response)
- **8.4**: Chart visual correctness (canvas pixel analysis is
  fragile; visual review is more reliable)
- **13.1**: Perceived smoothness (need human eyes for jank)

---

## Execution plan

1. Fix remaining gaps by writing new Playwright tests for S3
   (streaming), S5 (slash commands), S13 (performance)
2. Run full suite against the live daemon
3. Human spot-check of screenshots for visual quality
4. Fix-and-rerun loop until 100%

## Definition of done

- Zero automated test failures
- Zero JS console errors
- Human reviewer confirms: chat scrolls during stream, chart
  renders candles, markdown formats correctly, mobile is usable
- All automated tests committed to `web/` as part of the project
  (not throwaway scripts in /tmp)
