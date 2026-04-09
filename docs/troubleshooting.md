# Troubleshooting

Common problems and how to fix them. Each entry is a symptom you'd observe, the root cause, and the actual command/edit that resolves it.

## Clipboard says "Copied" but paste shows old content

**Symptom:** You hit `Ctrl+Y` or `Ctrl+Shift+C`, the chat shows `Copied 312 chars to clipboard via osc52 — …`, but pasting in your text editor returns whatever you previously copied from a browser.

**Cause:** Your terminal doesn't honor OSC 52 clipboard writes. Most VTE-based terminals on Linux (gnome-terminal, Tilix, Terminator, Konsole, MATE Terminal, XFCE Terminal) disable OSC 52 by default for security reasons. The bytes leave the program, the terminal reads them, and silently drops them.

**Fix:** Install a native CLI clipboard tool. The TUI auto-detects and uses it on next copy.

```bash
# Wayland (default on modern Ubuntu / Fedora / Pop!_OS)
sudo apt install wl-clipboard

# X11
sudo apt install xclip
# or
sudo apt install xsel
```

After installing, the next copy in chat should show `via wl-copy` (or `xclip` / `xsel`) instead of `via osc52`. Pasting will then work in any application.

**Verification:** look at the chat confirmation line — the backend name in `via {backend}` is the load-bearing part. Anything other than `osc52` means the system clipboard is actually being written to.

If you're on macOS or Windows the OSC 52 path IS the right one. See [chat-input.md#macos-windows-ssh](chat-input.md#macos--windows--ssh) for terminal compatibility.

---

<a id="chart-load-error"></a>
## Chart load error: `Could not load BTC 1m: ...`

**Symptom:** The chart panel shows a red error message instead of candles.

**Possible causes and fixes:**

### Cause 1 — `AGENT_KAI_API_KEY` is missing or wrong

Check the env:

```bash
echo $AGENT_KAI_API_KEY
```

If empty, export it (or write a `.env` file or `AGENT-KAI-API-KEY.txt`):

```bash
export AGENT_KAI_API_KEY="kai-..."
```

Restart the TUI.

### Cause 2 — Cloud endpoint is unreachable

Test directly:

```bash
curl -H "Authorization: Bearer $AGENT_KAI_API_KEY" https://agent-k.ai/v1/market/ohlcv?symbol=BTC&interval=1m&limit=1
```

If you get a connection error, check your network. If you get `401`, your API key is invalid — generate a new one at `https://agent-k.ai/`.

### Cause 3 — You're on the `coinbase` source and the symbol doesn't exist there

Check the chart source:

```
/chart source                   # (no args) — actually this isn't supported, see /chart usage
```

Or check `workspaces/terminal/state.json`:

```bash
cat workspaces/terminal/state.json
```

Switch back to the cloud:

```
/chart source kai-api
```

### Cause 4 — Stale `state.json` from an older version

Older versions of the agent had a `local` chart source that hit `localhost:8877`. That source was removed. If your `state.json` still has `"chart_source": "local"`, the chart will fail.

```bash
rm workspaces/terminal/state.json
```

The TUI will recreate it with defaults on next launch.

---

## NATS not connecting

**Symptom:** On startup you see:

```
Warning: Could not connect to NATS at nats://localhost:4222: ...
Running without NATS.
```

The TUI starts but `/analyze`, `/buy`, `/sell`, `/scan`, `/risk` all fail with "sub-agent manager not available."

**Cause:** The local NATS container isn't running.

**Fix:**

```bash
docker compose up -d
docker compose ps     # nats container should be "running"
```

If the container is running but you still can't connect, check the URL. Default is `nats://localhost:4222`. Override with `--nats-url` or `KAI_NATS_URL` env var.

If port 4222 is in use by something else:

```bash
sudo ss -tnlp | grep 4222    # find what's holding it
docker compose down
docker compose up -d         # rebind
```

---

## Codex auth expired

**Symptom:** Agents using the `codex-cli` endpoint fail with `401 Unauthorized` or `Invalid token`.

**Cause:** Your ChatGPT OAuth tokens expired and the refresh failed (or the refresh token itself is no longer valid).

**Fix:** Re-run the OAuth flow:

```
/login codex
```

A browser tab opens. Sign in. Wait for the callback. New tokens are written to `~/.codex/auth.json`.

If `/login codex` itself fails:

- Check that port `1455` is free (the OAuth callback listens there): `sudo ss -tnlp | grep 1455`
- Check that you can reach `auth.openai.com` from your machine
- Try the official codex CLI's `codex login` instead — it shares the same auth file

**Verification:**

```bash
cat ~/.codex/auth.json | jq .tokens.account_id
```

Should print a non-null account ID. If empty or missing, the file is corrupt — delete it and re-run `/login codex`.

---

## `'list' object has no attribute 'strip'` error in agent log

**Symptom:** An agent's per-day log file shows:

```
INVOKE_ERROR error='list' object has no attribute 'strip'
```

And the agent silently falls through to the next endpoint in its fallback chain.

**Cause:** This happens when `ChatOpenAI(use_responses_api=True)` returns `AIMessage.content` as a list of structured blocks instead of a plain string. LangChain's `OpenAIToolsAgentOutputParser` calls `.strip()` on the content during `AgentFinish` construction and crashes.

**Fix:** This is fixed in `ChatCodex` (the subclass for the `codex-cli` endpoint). All four content-producing methods (`_astream`, `_stream`, `_generate`, `_agenerate`) flatten the list into a string before they leave the chat model. The fix is at `agent/core.py:_flatten_chat_message` and the `ChatCodex` subclass.

If you're seeing this error against `codex-cli` despite the fix, your agent install is out of date — pull the latest from the `kai/self-learning-platform` branch.

If you're seeing it against a different endpoint, you've added a new `ChatOpenAI(use_responses_api=True)` subclass and forgot the override. Search for `_flatten_chat_message` in the codebase for the fix template.

---

## Queue full — dropped: ...

**Symptom:** You typed more than 10 things while an agent was busy. The 11th input shows:

```
queue full (10 max) — dropped: <preview>
Use /queue clear to flush, or /queue drop N to remove a specific item.
```

**Cause:** The type-ahead queue is capped at `MAX_INPUT_QUEUE = 10` items.

**Fix:** Drop something to make room:

```
/queue                  # see what's queued
/queue clear            # nuke everything
/queue drop 3           # drop just position 3
```

Or click the `[X]` button on any queued row in the chat panel.

If you legitimately need a larger queue, raise `MAX_INPUT_QUEUE` in `tui/terminal.py:32`. But 10 is generous for realistic interactive use — beyond that you're better off cancelling and rethinking.

---

## `/learn` says "no prior session"

**Symptom:** You run `/learn` (or `/learn analyst`) and the TUI shows:

```
analyst has no prior session to reflect on.
```

**Causes and fixes:**

### Cause 1 — The agent never ran a task

`/learn` only works after the target sub-agent has actually processed a task. If you spawned the analyst with `spawn_agent` but never sent it a request, `last_session` is `None`.

Fix: send a real task first.

```
/analyze BTC 1h
```

Then `/learn`.

### Cause 2 — You restarted the TUI

`last_session` lives in process memory. Restarting the TUI loses it. Re-run a task before reflecting.

### Cause 3 — You're trying to /learn the main kai agent

Currently `/learn` only works for sub-agents, not the main kai agent. `AgentRunner` doesn't track `last_session` yet — that's on the roadmap (see `docs/proposals/learn-on-main-kai-agent.md` if you have access to internal proposals).

For now, use `/learn` after sub-agent dispatches (`/analyze`, `/buy`, `/sell`, `/scan`, `/risk`) and the auto-nudge tip will tell you when there's something worth reflecting on.

---

## `/learn` error: "/learn requires the sub-agent manager — not available in this mode"

**Symptom:** You hit `/learn` and get this exact error.

**Cause:** Either you're running with `--no-tui` (which doesn't construct a sub-agent manager), or NATS failed to connect at startup (which skips the sub-agent manager).

**Fix:** Run with `--terminal` and a working NATS:

```bash
docker compose up -d
python main.py --terminal
```

The sub-agent manager only exists when both NATS and the trading terminal are active.

---

## Mentor returns garbage / wrong format

**Symptom:** You run `/learn`, the mentor responds, but the TUI says:

```
[mentor] DECISION: …unparseable text…
Reflection saved: eval_results/reflection-...
[no skill created]
```

**Cause:** The mentor's reply doesn't match the expected `DECISION:` / `TARGET_AGENT:` / `SKILL_NAME:` / `OP:` / `SKILL_CONTENT:` markers. `parse_mentor_reply` couldn't extract a decision.

**Fix:**

1. Read the saved reflection JSON to see exactly what the mentor returned: `cat eval_results/reflection-{ts}-mentor.json | jq .mentor_reply`
2. If the mentor wrote a sensible analysis but not in the structured format, its skill catalog is missing the meta-skill `how-to-reflect-on-a-session`. Check that `workspaces/mentor/skills/how-to-reflect-on-a-session.md` exists and has the right body. If missing, restore it from git: `git checkout workspaces/mentor/skills/how-to-reflect-on-a-session.md`
3. If the mentor is on a weak model that can't follow the format reliably, swap it to something stronger:

```
/model mentor codex-cli/gpt-5.4
/think mentor high
```

Then re-run `/learn`.

4. If the format is correct but the parse still fails, look at the regex set in `agent/learning.py:DECISION_RE` and friends — the parser is intentionally lenient (case-insensitive markers, code-fence stripping) but might miss an edge case.

---

## Sub-agent fallback chain failing

**Symptom:** You see in the agent's log:

```
PRIMARY_FAILED agent=analyst attempt=1/2 trying fallback
PRIMARY_FAILED agent=analyst attempt=2/2 trying fallback
RESPONSE length=0
```

The agent returns an empty response or an error.

**Cause:** Every endpoint in the fallback chain failed. Either all are down, all are misconfigured, or all are rejecting the request for the same reason.

**Fix:**

1. Check each endpoint individually:

```bash
# kai-smart (cloud)
curl -H "Authorization: Bearer $AGENT_KAI_API_KEY" https://agent-k.ai/v1/chat/completions -d '{"model":"kai-smart","messages":[{"role":"user","content":"hi"}],"max_tokens":10}'

# kai-local (your vLLM at 192.168.222.45 by default)
curl http://192.168.222.45:8000/v1/chat/completions -d '{"model":"qwen35-gptq","messages":[{"role":"user","content":"hi"}]}'

# codex-cli (requires a valid ~/.codex/auth.json)
cat ~/.codex/auth.json | jq .tokens.access_token
```

2. If `kai-local` is down, edit `agent-config.json` to remove it from the fallback chain temporarily:

```json
"fallback_endpoints": [
  {"endpoint": "codex-cli", "model": "gpt-5.4"}
]
```

Restart the TUI.

3. If everything is rejecting the request, the issue is the request itself — likely a tool the LLM is trying to call doesn't exist on that endpoint, or `max_iterations` is too low. Check the agent's log for the actual rejection reason.

---

## Auto-copy on mouse-up doesn't work

**Symptom:** Selecting text with click+drag doesn't put anything in the clipboard, even though the chat panel says nothing went wrong.

**Cause:** Probably the same as "Clipboard says 'Copied' but paste shows old content" above — your terminal doesn't honor OSC 52. The auto-copy path doesn't print a confirmation, but it uses the same backend chain as the explicit `Ctrl+Shift+C` and `Ctrl+Y` paths.

**Fix:** Install `wl-clipboard` or `xclip` (see [first entry](#clipboard-says-copied-but-paste-shows-old-content)).

**Verification:** trigger an explicit copy with `Ctrl+Shift+C` and check the chat confirmation. If it says `via osc52`, mouse auto-copy is also using OSC 52 and silently failing the same way.

---

## Multi-line paste only sends the first line

**Symptom:** You paste a multi-line stack trace into the chat input, hit Enter, and the agent only receives the first line.

**Cause:** Either you're on an old version of the agent (pre-`66ce084`), or the bracketed paste mode of your terminal isn't sending Paste events.

**Fix:**

1. Update to the latest version. The fix landed in `tui/panels/history_input.py:_on_paste` and in `tui/terminal.py:on_input_submitted` (which calls `take_pasted_buffer()`).

2. Verify your terminal supports bracketed paste:

```bash
printf '\e[?2004h'    # enable bracketed paste manually
# Now paste multi-line text — you should see weird control sequences around it
```

If you see `\e[200~` and `\e[201~` wrappers, bracketed paste is on. If you don't, your terminal doesn't support it. Try a different terminal (any modern one — kitty, alacritty, wezterm, gnome-terminal, Konsole — supports bracketed paste).

3. Check the input value before submitting. If it shows `[paste: N lines, M chars] preview…`, the buffer is set correctly and Enter will dispatch the full text. If it shows the literal first line of the paste, the parent `Input._on_paste` ran and the override didn't fire.

---

## Sub-agent isn't picking up a config change

**Symptom:** You edited `agent-config.json` and restarted the TUI but the sub-agent is still using the old endpoint or model.

**Cause:** Sub-agents are constructed lazily — they're built the first time they're spawned, and reused thereafter. If a sub-agent was already running when you `/model`-swapped it, the override might not have taken effect on disk (only in memory).

**Fix:**

1. Restart the TUI fully: `Ctrl+C`, `python main.py --terminal`
2. Don't spawn the sub-agent until after the TUI is running with the new config
3. Verify the config loaded correctly:

```
/model analyst
# Should show the new endpoint
```

If it still shows the old config, your `agent-config.json` has a JSON syntax error and `config.py` silently failed to reload. Run `python -c 'import json; json.load(open("agent-config.json"))'` — any error message tells you where the JSON is broken.

---

## Tool call timed out

**Symptom:** Agent's log shows:

```
shell_exec timed out after 30s
```

**Cause:** A shell command exceeded `tool_safety.shell_timeout_seconds` (30 by default).

**Fix:**

1. Identify the command from the log
2. If it's legitimate work that needs more time, raise the timeout in `agent-config.json`:

```json
"tool_safety": {
  "shell_timeout_seconds": 120
}
```

3. If it's a runaway, the timeout is doing its job. Leave it.

For long-running sandboxed jobs, use `docker_sandbox` with an explicit `timeout` arg (capped at `tool_safety.docker_sandbox.max_timeout_seconds = 600` by default).

---

## Memory file is empty even though I told the agent to save things

**Symptom:** You said "remember that I prefer 1h analyses" and the agent confirmed, but `workspaces/{agent}/memories/MEMORY.md` is empty.

**Cause:** Memory writes go through a security scan that rejects content matching prompt injection / exfiltration patterns. The scan log line is in the agent's daily log file. Common false positives:

- Strings containing "ignore previous instructions"
- Strings mentioning `~/.ssh` or `authorized_keys`
- Strings with curl/wget + an env var matching `*KEY` / `*TOKEN` / `*SECRET`
- Invisible Unicode characters (zero-width spaces, BiDi overrides)

**Fix:**

1. Check `logs/{agent_name}_$(date +%F).log` for `Blocked: content matches threat pattern`
2. Rewrite the memory entry without the trigger phrase
3. If you genuinely need to persist a phrase that looks like a threat, edit `MEMORY.md` directly — the security scan only runs on the `memory` tool path, not on direct file writes

The threat patterns are in `agent/memory_store.py:_MEMORY_THREAT_PATTERNS`. Adjust if you must, but understand the trade-off — memory content is injected into the system prompt verbatim, so anything in there runs with the agent's full authority next session.

---

## TUI looks broken / panels are misaligned

**Symptom:** After running `/chart off` (or some other action), the chat panel jumped left or the watchlist disappeared.

**Cause:** Probably old code. The bug where `widget.display = False` collapsed the grid was fixed in `70a62ed` (the chart panel now uses `widget.visible` instead which keeps the slot reserved).

**Fix:** Update to the latest. Or fully restart the TUI — reflowed grids are stateless on relaunch.

If the layout is broken on a fresh launch, your terminal is using a font that's not monospace or the grid `grid-rows` heights don't fit. Try:

- Resize the terminal window (Textual recomputes the grid on resize)
- Switch to a monospace font (the chart panel needs box-drawing characters and uniform column widths to render correctly)
- Check `tui/terminal_styles.tcss` for any local edits you made

---

## Where to find more info

- [getting-started.md](getting-started.md) — install + first run
- [configuration.md](configuration.md) — env vars, secret loading, config schema
- [models-and-thinking.md](models-and-thinking.md) — endpoint and Codex auth details
- [learning-and-skills.md](learning-and-skills.md) — `/learn` flow
- [architecture.md](architecture.md) — process model, NATS topics, tools system

If your problem isn't listed here, check the per-agent logs at `logs/{agent}_$(date +%F).log` and the TUI log at `logs/tui_$(date +%F).log`. The error message + the relevant log line is usually enough to find the cause via grep through the codebase.
