# Chat input

The chat input box at the bottom of the trading terminal looks like a simple single-line text field but has five non-obvious features stacked on top of Textual's `Input` widget: bash-style command history, multi-line paste capture, a type-ahead queue with click-to-drop UI, runtime-detected clipboard backends, and OSC 52 fallback for SSH. This doc covers all five.

## Bash-style history

Type a few commands, hit `Up` to recall them, edit, hit `Enter` to send. Same UX as `bash`, `zsh`, `fish`, and every other shell since 1989.

### Behavior

- `Up` walks backward through previously submitted lines
- `Down` walks forward; from the most recent past entry it lands back on the in-progress draft (whatever you were typing before you started browsing)
- The cursor parks at end-of-line on every recall (matches bash / readline so you can keep typing or hit `Backspace` to edit)
- Consecutive duplicate submissions are collapsed to one entry (`HISTCONTROL=ignoredups` equivalent)
- Empty / whitespace-only submissions are dropped
- Capped at 200 entries in memory; oldest evicted on overflow

### Persistence

History is saved to `workspaces/terminal/input_history.txt`, one entry per line, in the same format as `~/.bash_history`. The file is loaded on TUI start and trimmed to the most recent 200 entries.

```bash
# Inspect your history file
cat workspaces/terminal/input_history.txt

# Wipe it
> workspaces/terminal/input_history.txt
```

Read failures and write failures are silently swallowed — history is a convenience, not load-bearing, and a corrupt file should never block the TUI from starting.

### What does NOT go in history

- **Multi-line pastes.** They're content from elsewhere, not typed commands worth replaying via Up arrow.
- **Slash commands the parser rejected.** Not yet implemented but on the roadmap.
- **Anything you typed but never submitted.** History only records what you actually pressed Enter on.

### Why a subclass and not an app-level handler

Textual routes key events to the focused widget first via MRO walk. The cleanest way to bind `Up` / `Down` to history navigation specifically when the input is focused is to put the bindings on the input widget itself (`tui/panels/history_input.py`). Bonus: the history state lives encapsulated with the widget instead of sprawling onto the app class.

---

## Multi-line paste

Paste a stack trace, a code block, a multi-line prompt — the input box captures the FULL pasted content into a buffer and shows a one-line summary in place of the actual text:

```
[paste: 18 lines, 873 chars] Analyze BTC with the goal of discovering…
```

### Why we do this

Textual's `Input` widget is single-line by default, and its built-in paste handler does `event.text.splitlines()[0]` — only takes the first line. Without the buffer, pasting a 50-line stack trace would leave you with just `Traceback (most recent call last):` in the input. The agent would get a broken fragment instead of the actual content, with no way to know without comparing what was sent vs what you copied.

### How it works

1. You paste multi-line text into the input
2. `HistoryInput._on_paste` detects the newlines, normalizes line endings (CRLF → LF, CR → LF), and stores the full text in `self._pasted_buffer`
3. The visible value of the input becomes `[paste: N lines, M chars] {first 40 chars}…`
4. The cursor parks at end-of-summary
5. You hit `Enter`
6. `on_input_submitted` calls `take_pasted_buffer()` which returns the full text and clears the buffer
7. The full text is dispatched to the agent (or queued, if the agent is busy)

The summary indicator is never sent to the agent — only the original full content. Internal whitespace and indentation are preserved exactly; only line endings are normalized.

### Editing semantics

A buffered paste is **all or nothing**:

- Hit `Enter` to send the full text as-is
- Hit `Up` / `Down` for history nav, `Ctrl+Y`, `Ctrl+L`, `Ctrl+Shift+C`, `Shift`, `Ctrl`, or `Alt` — the buffer survives
- Hit ANY other key (a printable letter, `Backspace`, `Delete`, `Left`, `Right`) — the buffer AND the visible value are cleared, you get a fresh empty input to type into

This avoids the confusing intermediate state where you backspace through the `[paste: N lines]` placeholder one character at a time. Editing a placeholder makes no sense — it's not the real content. Better to abandon and re-paste.

### Line endings

CRLF (Windows) and CR (old Mac) are normalized to `\n` before being stored in the buffer, so the agent always sees consistent text regardless of where the source content came from.

### `prevent_default()` is the load-bearing part

The fix for the multi-line paste bug isn't `event.stop()` — it's `event.prevent_default()`. Textual's message pump walks the MRO of the receiving widget and calls every `_on_paste` in the chain. `event.stop()` only blocks bubbling to ANCESTOR widgets. `prevent_default()` blocks the within-widget MRO walk so the parent `Input._on_paste` doesn't run after our override and leak the first line into the visible value. See `tui/panels/history_input.py:_on_paste` for the documented fix.

---

## Type-ahead queue

While an agent is busy (running `/analyze`, `/buy`, a long chat, etc.) anything you type lands in a FIFO queue and runs in submission order as soon as the previous task completes. Same UX as typing ahead in bash while a long-running command is in flight.

### Behavior

- Cap of 10 items max — the 11th input is rejected with a `[queue full (10 max)]` message and pointers at `/queue clear` and `/queue drop N`
- Each queued item gets its own clickable row in chat with the format `queued (#N): preview…   [X]`
- Items dispatch in strict FIFO order
- When an item dispatches, the row is removed from chat and the rest renumber so `(#N)` labels stay accurate
- Click the `[X]` button on any row to drop just that item
- `/queue clear` flushes everything at once
- `/queue drop N` removes the item at 1-indexed position N
- Empty / whitespace-only inputs are still rejected at submit time, never queued

### Visual flow

```
> /analyze BTC
Spawning analyst...
> what's the trend on ETH
queued (#1): what's the trend on ETH                          [X]
> /chart SOL 1h
queued (#2): /chart SOL 1h                                    [X]
> /sell DOGE 100
queued (#3): /sell DOGE 100                                   [X]

[analyst finishes its 30-second run]

→ running queued: what's the trend on ETH (2 more queued)
[main agent processes the ETH question]
→ running queued: /chart SOL 1h (1 more queued)
[chart loads]
→ running queued: /sell DOGE 100
[trader sub-agent runs]
```

### How sync slash commands fit

Synchronous slash commands like `/chart`, `/think`, `/model`, `/queue` don't spawn workers — they run inline and return immediately. For the queue to handle these correctly, `_dispatch_input` calls `_drain_input_queue()` at the end whenever a sync command was just routed and `_agent_working` is False. This means a queued `/chart BTC 1h` followed by a queued `/buy ETH 0.5` will see the chart load instantly and then the trader spawn — no stalls.

### Race-safety

`_drain_input_queue` is intentionally synchronous (no `await`). Between the caller's `self._agent_working = False` and the drain's pop+spawn, no other coroutine can run because there's no yield point. This means a user input arriving in the same tick lands in the queue (because we'll have set busy=True via the new worker before the user's submission is processed) instead of racing for the slot.

### Why a cap of 10

Realistic interactive use rarely needs more than 5-6 items stacked up. A cap of 10 is generous. Beyond that, you're better off cancelling and rethinking. The cap is tunable via `MAX_INPUT_QUEUE` in `tui/terminal.py:32`.

See [commands.md#queue](commands.md#queue) for the full `/queue` command reference.

---

## Copy and paste

Three ways to get text out of the chat into your system clipboard:

| How | Where it comes from |
|---|---|
| **Click and drag** any panel | Mouse-released selection — auto-copies on mouse-up, no chat confirmation |
| **`Ctrl+Shift+C`** | Current mouse selection — chat shows confirmation with the backend used |
| **`Ctrl+Y`** | Most recent agent response — no selection needed, single keystroke |

All three routes go through `_set_system_clipboard(text)` which detects the best available backend at first call and caches the result.

### Backend detection order

```
WAYLAND_DISPLAY set → wl-copy first
DISPLAY set        → xclip, then xsel
neither / nothing   → wl-copy as a final native attempt
all CLI tools missing → OSC 52 escape sequence (terminal handles it)
```

The detected backend is shown in the chat confirmation message:

```
Copied 312 chars to clipboard via wl-copy — The market is currently…
```

The backend name in the confirmation is critical for debugging. If you see `via osc52` and your paste in a text editor shows stale content, you have your answer immediately — the OSC 52 fallback isn't reaching your system clipboard. Install one of the CLI tools and try again.

### Why CLI > OSC 52 on Linux

VTE-based terminals (gnome-terminal, Tilix, Terminator, Konsole, MATE Terminal, XFCE Terminal — i.e. basically every default terminal on every Linux distro) **disable OSC 52 clipboard writes by default for security reasons**. The bytes leave the program, the terminal reads them, and then silently throws them on the floor. No error, no logging — the program thinks the copy worked.

The native CLI tools (`wl-copy`, `xclip`, `xsel`) bypass the terminal entirely and talk directly to the Wayland compositor or X11 selection owner. They're the only reliable path on Linux.

### Installing the CLI tools

```bash
# Wayland (default on modern Ubuntu / Fedora / Pop!_OS)
sudo apt install wl-clipboard

# X11
sudo apt install xclip
# or
sudo apt install xsel
```

### macOS / Windows / SSH

- **macOS:** OSC 52 works on iTerm2 (enable in `Preferences → General → Selection → "Applications in terminal may access clipboard"`), Kitty, Alacritty, WezTerm. Apple's stock Terminal.app does NOT honor OSC 52 — use one of the alternatives.
- **Windows Terminal:** OSC 52 works out of the box.
- **SSH:** OSC 52 is the right path — the bytes tunnel through the SSH session and your local terminal handles the actual clipboard write. Same terminal-support caveats apply on the client side. There's no `wl-copy` on the remote box; that's by design.

### Extracting the agent's text

`Ctrl+Y` reads the most recent agent message via `_extract_last_agent_text` which walks the chat panel children newest-first, picks the first widget tagged `agent-msg`, pulls the markup string off `widget.content` (NOT `widget.renderable` — that doesn't exist on Textual `Static`), strips Rich markup tags via `Text.from_markup(content).plain`, and returns plain text the user can paste into anything. If no `agent-msg` exists yet, falls back to any non-user, non-error widget so freshly-started sessions can still copy welcome banners.

---

## What to read next

- [keybindings.md](keybindings.md) — every keyboard shortcut in one table
- [troubleshooting.md#clipboard](troubleshooting.md#clipboard-says-copied-but-paste-shows-old-content) — when copy says "ok" but paste fails
