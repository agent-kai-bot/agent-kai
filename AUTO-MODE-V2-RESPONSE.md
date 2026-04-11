# /auto Mode v2 — Response to Critic Review

## Core reframe: accepted

Codex is right. The problem is NOT a missing execution loop.
The AgentExecutor already runs multi-tool chains up to 2000
iterations. The problem is **premature finalization** — the
model chooses to end its turn with a permission-seeking answer
instead of continuing to use tools.

The fix belongs at the prompt + executor layer, not as a brittle
outer continuation loop.

## Accepted changes

### 1. Kill the regex continuation checker
**Fully agree.** Surface-text regexes for "should I", "in summary"
etc. are fragile, will have false positives on quoted text, and
are blind to state. Deleting the entire `AutoContinuationChecker`
class. Instead:

### 2. Fix premature finalization at the prompt layer
Two mechanisms, layered:

**A) Auto-mode system prompt variant**
Create a second prompt template (not a suffix — a full variant)
that is swapped in when auto mode is active. The variant:
- Removes all "ask the user" conditioning
- Explicitly instructs multi-step execution
- Includes the `AUTO_STATE` structured footer convention

**B) Structured footer for machine parsing**
Instead of regex-matching the response text, require the model
to emit a structured footer:
```
[AUTO_STATE: done]
[AUTO_STATE: continue]
[AUTO_STATE: pause | reason: requires approval for place_order]
```
Parse ONLY this footer for control decisions. If missing or
malformed: stop conservatively (don't guess).

### 3. Auto lives in Session/daemon, not TUI
**Agree.** Auto state is session-level:
```python
# In Session / daemon core
self.auto_mode: bool = False
self.auto_budget_remaining: int = 0
self.auto_start_time: float = 0
```
The daemon manages the auto loop. Both TUI and web UI just
render the events. No client-side auto logic.

### 4. One-shot recovery as the minimal v1
**Agree with the "tiny v1" suggestion.** Ship this first:
- If the model ends a turn with permission-seeking language
  AND auto mode is on, inject ONE hidden nudge and re-run.
- If the second attempt also permission-seeks: stop.
- This solves 80% of the annoyance with minimal complexity.

Full multi-step auto can come in v2 after we see how the
one-shot recovery works in practice.

### 5. Tool metadata + central policy
**Agree.** Every tool gets metadata:
```python
@dataclass
class ToolPolicy:
    name: str
    read_only: bool = True
    persistent: bool = False        # writes to disk/DB
    external_side_effects: bool = False  # network, trades
    long_running: bool = False      # >30s typical
    requires_approval_in_auto: bool = False
```

Tool policy table replaces the ad-hoc string set. Auto mode
consults this table, not hardcoded names. `/auto readonly`
only allows `read_only=True` tools.

### 6. Use existing max_iterations + wall-clock for budget
**Agree.** Don't invent a new budget unit.
- `max_iterations` already exists (default 2000, way too high
  for auto — reduce to 40 in auto mode)
- Add `max_auto_duration_seconds = 180` (3 min wall-clock)
- Tool count is telemetry, not the budget gate

### 7. Real loop detection
**Agree.** Add:
- Stop if the same final text repeats twice
- Stop after 2 consecutive no-tool turns
- Stop if the same `(tool_name, input_hash)` pair repeats 3x
- Surface the reason to the user via auto event

### 8. No chat history pollution
**Agree this is critical.** The auto nudge must NOT be persisted
as a `HumanMessage` in chat history. Options:
- Use a separate `SystemMessage` that is excluded from save/load
- Or use a transient input that the runner processes but doesn't
  persist

The runner's `chat_history.append(HumanMessage(...))` call
needs a guard: `if not self._is_auto_continuation`.

### 9. Drop aggressive mode, keep only /auto and /auto readonly
**Agree.** Two modes, not three.

### 10. Reset auto on disconnect
**Agree.** Auto is transient. Reconnecting starts fresh.

## Disagreements

### Codex says "don't rebuild the prompt, swap the executor"
I partially disagree. Swapping the full executor means maintaining
two executor configurations. A prompt swap is simpler AND the
existing `reload_llm()` path already exists for model swaps.
Adding a `rebuild_prompt(auto=True)` method to `AgentRunner` is
cleaner than maintaining two executor instances.

**Compromise:** add `AgentRunner.set_auto_mode(enabled: bool)`
that rebuilds the prompt with the auto variant and adjusts
`max_iterations`. One method call, no executor duplication.

## Revised v1 design summary

1. `/auto [N]` → sets `session.auto_mode = True`, rebuilds prompt
   with auto variant, sets `max_iterations = N or 40`,
   starts wall-clock timer (3 min default)
2. Agent runs normally through the existing executor loop
3. If the model ends a turn with `[AUTO_STATE: continue]`: the
   daemon injects one hidden continuation (NOT persisted to
   chat history) and re-runs
4. If `[AUTO_STATE: done]` or `[AUTO_STATE: pause]`: stop
5. If footer is missing: stop (conservative)
6. If budget or wall-clock exceeded: stop
7. If loop detected (same text 2x, no-tool 2x, same tool 3x): stop
8. Tool calls check the policy table; `requires_approval_in_auto`
   tools force a pause
9. `/auto off` or disconnect: stop immediately
10. Status bar shows: `[AUTO] step 3 | iter 12/40 | 01:42`
