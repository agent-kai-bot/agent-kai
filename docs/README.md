# Agent KAI documentation

Everything you need to operate and extend Agent KAI. Pick a doc by what you want to do.

## For users

| Doc | Read this when |
|---|---|
| [getting-started.md](getting-started.md) | First-time install, API key setup, your first chat with kai |
| [commands.md](commands.md) | You want the syntax for any slash command (`/buy`, `/analyze`, `/learn`, `/think`, `/queue`, …) |
| [keybindings.md](keybindings.md) | You forgot a keyboard shortcut |
| [chat-input.md](chat-input.md) | History recall, multi-line paste, type-ahead queue, copy/paste |
| [chart-panel.md](chart-panel.md) | Chart symbols, timeframes, color schemes, switching data source |
| [watchlist-and-positions.md](watchlist-and-positions.md) | The two side panels — what they show and how to use them |
| [agents.md](agents.md) | Sub-agent runtime, the 14 built-in specialists, multi-agent orchestration prompts |
| [models-and-thinking.md](models-and-thinking.md) | Switching LLM endpoints at runtime, reasoning effort levels, Codex OAuth |
| [learning-and-skills.md](learning-and-skills.md) | The `/learn` reflection loop, memory, skills — how the agent gets better over time |

## For developers

| Doc | Read this when |
|---|---|
| [configuration.md](configuration.md) | Editing `agent-config.json`, env vars, secret loading, adding a new endpoint or agent |
| [data-sources.md](data-sources.md) | How `kai-api` and Coinbase clients work, the signal consumer, backtesting internals |
| [architecture.md](architecture.md) | Process model, NATS topics, workspaces layout, how to add a new tool / panel / sub-agent, test harness |
| [troubleshooting.md](troubleshooting.md) | Clipboard not working, Codex auth expired, NATS down, queue full, model fallback chain failures |

## Internal

`docs/proposals/` (gitignored) holds in-progress design docs and is not part of the published documentation.

## How the docs are organized

- **getting-started** is the only doc you have to read in order. The rest are reference — read them when you need them.
- Every doc starts with a one-paragraph "what this covers" summary so you can skim.
- Code examples are copy-pasteable and expected to work as-shown against a fresh checkout.
- Commands are documented with `[required] {placeholder} <optional>` syntax. Square brackets are literal.
- Cross-references use relative links: `[/think](models-and-thinking.md#think)`.
- File:line references in `architecture.md` point at the source so contributors can navigate from doc to code.

## Quick reference

- **First chat:** `python main.py --terminal`, then type `analyze the BTC trend` and hit Enter.
- **First sub-agent:** `/analyze BTC` — spawns the analyst, runs technical analysis, returns a structured report.
- **Make the agent learn:** after a complex sub-agent task, type `/learn` — the mentor will turn the session into a reusable skill.
- **Switch to a smarter model:** `/model kai codex-cli/gpt-5.4` then `/think kai high`.
- **Get unstuck:** see [troubleshooting.md](troubleshooting.md).
