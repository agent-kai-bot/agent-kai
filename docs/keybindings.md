# Keybindings

Every keyboard shortcut in the trading terminal. Mouse interactions are also covered here because they're related (drag-to-select, click-to-copy).

## App-level shortcuts

These work anywhere in the TUI regardless of which widget has focus.

| Key | Action |
|---|---|
| `Ctrl+C` | Quit the TUI |
| `Ctrl+L` | Clear the chat panel (does NOT clear chat history — see note below) |
| `Ctrl+T` | Cycle to the next chart timeframe (`1m → 5m → 15m → 1h → 4h → 6h → 1d → 1m`) |
| `Ctrl+S` | Cycle through tracked symbols on the chart |
| `Ctrl+W` | Focus the chat input with `/watch ` prefix (so you can type a symbol and hit Enter to add it to the watchlist) |
| `Ctrl+Y` | Copy the most recent agent response to the system clipboard |
| `Ctrl+Shift+C` | Copy the current mouse selection to the system clipboard |

> `Ctrl+L` clears the visible chat panel widgets but does NOT clear the agent's `chat_history` (which is what the LLM sees as conversation context). To start a fresh conversation context, restart the TUI.

## Chat input (HistoryInput)

These work when the cursor is in the chat input box at the bottom of the screen.

| Key | Action |
|---|---|
| `Up` | Recall the previous submitted line from history |
| `Down` | Move forward through history (or back to the in-progress draft if at the most recent past entry) |
| `Enter` | Submit the current input |
| `Backspace` | Delete one character (or abandon a buffered multi-line paste — see note) |
| `Left` / `Right` | Move the cursor |
| `Home` / `End` | Jump to start / end of input |

History persists across TUI restarts in `workspaces/terminal/input_history.txt`, one entry per line, in the same format as `~/.bash_history`. Capped at 200 entries; consecutive duplicates are collapsed (HISTCONTROL=ignoredups equivalent).

### Multi-line paste behavior

When you paste text containing newlines (a stack trace, a code block, a multi-line prompt), the input box stores the FULL pasted text in a buffer and shows a summary indicator like:

```
[paste: 18 lines, 873 chars] First line preview…
```

- Press `Enter` to send the full multi-line text
- Press any other key (a letter, backspace, delete) to abandon the buffer and start fresh — this is the "all or nothing" rule, half-edited paste summaries make no sense
- `Up`, `Down`, `Ctrl+Y`, `Ctrl+L`, `Ctrl+Shift+C`, `Shift`, `Ctrl`, `Alt` all preserve the buffer

Multi-line pastes are NOT added to the up-arrow history (they're content from elsewhere, not typed commands worth replaying). Single-line pastes still go to history.

See [chat-input.md#multi-line-paste](chat-input.md#multi-line-paste) for the full details.

## Mouse interactions

| Action | Effect |
|---|---|
| Click and drag inside any panel | Select text. Releasing the mouse button auto-copies the selection to the system clipboard (no need to hit `Ctrl+Shift+C`) |
| Click on the `[X]` button on a queued input row | Drop that item from the type-ahead queue |
| Click on a watchlist row | Load that symbol on the chart panel at the current timeframe |

### Auto-copy on mouse-up

Selecting text with the mouse fires Textual's `TextSelected` event, which the TUI hooks into to push the selection through the same clipboard backend chain (`wl-copy` → `xclip` → `xsel` → OSC 52). The chat panel does NOT show a confirmation message for auto-copy because the user knows they highlighted something — the proof is the paste working. There's an INFO log line in `logs/tui_YYYY-MM-DD.log` for post-mortems.

## Backend selection for copy

All three copy paths (`Ctrl+Y`, `Ctrl+Shift+C`, mouse-drag-auto-copy) route through a runtime-detected clipboard backend in this order:

1. `wl-copy` (Wayland) — `sudo apt install wl-clipboard`
2. `xclip` — `sudo apt install xclip`
3. `xsel` — `sudo apt install xsel`
4. OSC 52 escape sequence (terminal-handled) — only reliable on Kitty, Alacritty, WezTerm, iTerm2 (with the security setting on), Windows Terminal, SSH tunneled to one of those

The detected backend is shown in the chat confirmation when you use `Ctrl+Y` or `Ctrl+Shift+C`:

```
Copied 312 chars to clipboard via wl-copy — The market is currently…
```

If you see `via osc52` and pasting in your editor returns stale content, install `wl-clipboard` or `xclip` and the next copy will use it. See [troubleshooting.md#clipboard](troubleshooting.md#clipboard-says-copied-but-paste-shows-old-content).

## Why these shortcuts and not others

- `Ctrl+C` is quit (the Textual default), NOT copy — that's why `Ctrl+Shift+C` is used for copy. They're distinct keys to Textual.
- Up / Down on a single-line `Input` widget aren't used by Textual for cursor movement (only Left / Right are), so we can claim them for history navigation without colliding with anything.
- `Ctrl+Y` is "yank" in many terminal traditions (emacs, readline) which conventionally pastes — but here it COPIES the last reply. Yes, this is backwards from emacs. It was the cleanest free shortcut.
- `Ctrl+T` for timeframe cycle is convenient (`T` for time) and doesn't conflict with terminal defaults the way `Ctrl+P` would.

## Adding new keybindings

Keybindings are declared in `tui/terminal.py` in the `BINDINGS` class attribute on `TradingTerminal`. Each entry is `(key, action_method_name, description)`. The action method is auto-dispatched by Textual via the `action_*` naming convention.

Example: to add `Ctrl+E` to focus the chat input:

```python
BINDINGS = [
    # ... existing ...
    ("ctrl+e", "focus_input", "Focus chat input"),
]

def action_focus_input(self) -> None:
    self.query_one("#input-area", HistoryInput).focus()
```

See [architecture.md#textual-tui](architecture.md#textual-tui) for the full TUI extension guide.
