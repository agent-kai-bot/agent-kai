# /auto Mode Review

High-level verdict: this design is aimed at a real annoyance, but it misdiagnoses the failure and therefore puts complexity in the wrong place. The current runner already has an internal multi-tool loop. The real problem is that the model sometimes chooses to terminate a turn early with a permission-seeking final answer. The proposed regex-driven outer loop is a brittle recovery hack, not a solid execution model.

## 1. Continuation checker

The design says the current loop "stops after one thought-action-observation cycle" and that `/auto` should let it continue (`design:51-55`, `design:172-197`). That is not what the current code does. `AgentRunner` already uses `AgentExecutor`, which keeps running tool steps until completion or `max_iterations` (`agent/core.py:437-444`, `agent/core.py:669-700`). The main agent is configured for `max_iterations: 2000` (`agent-config.json:98-105`). So the proposed continuation checker is solving the wrong layer of the problem.

The checker in `design:114-167` will fail in both directions:

- False negatives: `_is_asking_permission()` only catches a few phrases. It misses variants like "I can run the backtest next if helpful", "need your go-ahead", "confirm before I modify this", "awaiting approval", "I recommend X unless you want Y instead", and anything phrased indirectly.
- False positives: it will trigger on quoted text, examples, tool output, or rhetorical follow-ups after a valid answer.
- `_is_final_answer()` is especially weak. "in summary", "here are the results", or "finished" can appear in an intermediate auto turn and cause an early stop.
- `_has_pending_work()` is just as weak. "then I'll" or "after that" can appear in a plan that was already executed or in a quoted block.
- Exact-match detection on `"AUTO: pausing"` is brittle. Any paraphrase, lowercase variant, punctuation change, or model drift breaks it.
- Surface-text regexes are blind to state. They cannot tell whether the next action is safe, already done, impossible, or blocked on a tool result.

The follow-up prompt `"execute the action you just described"` is also ambiguous. If the model described multiple actions, or described a dangerous one, you have no guarantee it executes the intended step.

## 2. Budget model

`20 tool calls` (`design:201-205`) is both too small and too large because it is measuring the wrong thing.

- Too small: a normal analysis can easily cross 20 tool calls if it does indicator checks, signal lookups, backtests, and sub-agent delegation.
- Too large: one tool call can be huge. `codex_exec` and `claude_exec` have 8-hour timeouts (`agent/tools.py:225-240`, `agent/tools.py:280-299`). `nats_request` also defaults to 8 hours (`agent/tools.py:571-582`, `agent/tools.py:595-655`). So "1 tool call" is not a meaningful unit of cost or risk.
- Misaligned with the current system: the runner already has an iteration budget via `max_iterations` and an override hook (`agent/core.py:554-571`, `daemon/core.py:699-707`).
- Dangerous bug: the proposed budget only decrements on tool usage (`design:183-190`). If the model gets stuck in text-only permission loops, `tools_in_turn == 0`, so budget never drops.

If you want a v1 numeric default, use executor iterations plus wall-clock, not raw tool count. A more honest starting point is something like `max_iterations=40`, `max_duration=3m`, and `0` frontier escalations / scheduler mutations unless explicitly approved.

## 3. Prompt injection reliability

The auto suffix in `design:73-107` will help somewhat, but it will not reliably suppress permission-seeking behavior. Models still ask permission under stronger prompts than this, especially after a long chat history has established an interactive pattern.

There is also a concrete implementation problem: the current runner does not have a per-turn system prompt injection hook. The prompt is built once in `AgentRunner.__init__` and stored on `self._prompt` (`agent/core.py:433-444`). `reload_llm()` explicitly reuses that frozen prompt rather than rebuilding it (`agent/core.py:492-495`). So "append to the system prompt when auto mode is active" is not a trivial toggle. You need either:

- a second executor/prompt for auto mode, or
- a prompt rebuild path when auto mode changes.

On the Codex path, system content is moved into the Responses API `instructions` field (`agent/core.py:238-273`), which is good. But it only matters if you actually rebuild or swap the prompt for auto sessions.

Bottom line: prompt injection is a useful nudge, not a control plane. The system should not depend on the model obeying `"AUTO: pausing because ..."` exactly.

## 4. What happens when auto gets stuck

The proposed outer loop in `design:175-197` can fail badly:

- Repeated text-only loops: the model keeps asking a question or rewriting the same summary; budget does not decrease.
- Semantic loops: "Continue with the next step" can cause the model to restate the same plan because there is no new state, only more chat history.
- Tool loops: the model can rerun the same read-only tool sequence after each injected continuation.

There is a more serious integration problem: every continuation will be recorded as a synthetic `HumanMessage` because `AgentRunner.run()` appends the input to `chat_history` (`agent/core.py:575`) and the final response back into history (`agent/core.py:654`). Session save/load persists that history (`daemon/core.py:650-653`). So your hidden control loop becomes part of the permanent conversation state.

The TUI makes this worse. `_process_agent()` resets `_agent_working`, sets status to idle, saves history, and drains queued user input after every turn (`tui/terminal.py:3178-3187`). If auto is implemented as repeated normal turns, a real user message can slip in between internal auto steps unless you widen the busy scope.

The kill switch is also underspecified:

- `Ctrl+C` currently quits the TUI, not "stop auto" (`tui/terminal.py:130-143`).
- Long-running tools are not interruptible in the design. If auto enters `codex_exec`, `claude_exec`, or a long `nats_request`, your "stop immediately" promise is false.

You need explicit loop detection. At minimum:

- stop if the same final text repeats twice,
- stop after two consecutive no-tool continuations,
- stop if the same `(tool_name, tool_input)` pair repeats N times without new artifacts,
- surface `"AUTO: pausing because loop detected"` to the user.

## 5. Approval gates are incomplete

The proposed approval list in `design:207-224` does not match the real tool surface and misses the most dangerous operations.

The obvious missing tools are:

- `file_write`, `file_edit`, `shell_exec`, `python_exec` (`agent/tools.py:59-173`). These can alter the host machine immediately.
- `codex_exec`, `claude_exec` (`agent/tools.py:225-329`). These are external autonomous executors; `claude_exec` is invoked with `--dangerously-skip-permissions` (`agent/tools.py:285`).
- Scheduler mutation tools: `schedule_at`, `schedule_recurring`, `schedule_when`, `cancel_scheduled_job`, `pause_scheduled_job`, `resume_scheduled_job` (`agent/tools.py:779-938`). These create persistent automation.
- `optimizer_start`, `optimizer_pause` (`agent/strategy_agent_tools.py:201-209`, `agent/strategy_agent_tools.py:348-360`). These start and stop a background optimizer loop.
- `place_order` (`agent/crypto_tools.py:326-355`). It is "paper" trading, but it still mutates portfolio state.
- `spawn_agent`, `nats_request`, `nats_publish` (`agent/tools.py:539-709`). These expand the autonomy boundary and bypass top-level budgets.

The proposed tool names also do not line up with reality:

- There is no `execute_trade` tool; the current tool is `place_order`.
- There is no `toggle_autotrade` tool; autotrade is a TUI slash command (`tui/terminal.py:2232-2283`).
- There is no single `update_config` tool; config changes happen through generic file/shell tools.
- `promote_strategy` exists in storage code (`agent/strategy_store.py:475-515`) but is not even exposed in the current `create_strategy_tools()` list (`agent/strategy_agent_tools.py:171-216`).

This needs a central tool policy table, not an ad hoc string set.

## 6. Streaming and TUI interaction

The design hand-waves over the hardest UI problem.

Current local streaming is built around one input producing one response widget (`tui/terminal.py:3099-3177`, `tui/panels/agent_chat.py:53-80`). If auto mode chains multiple internal turns:

- you either create a fresh widget for every continuation, which clutters chat,
- or you need a separate streaming path that keeps one auto-session widget alive across turns.

The design says continuation prompts should be shown dim/italic (`design:243-245`), but the normal input path renders submitted text as a user message (`tui/terminal.py:1426-1466`). If you reuse that path, the transcript becomes fake: the user appears to have typed "Continue with the next step."

Remote mode has the same issue. The daemon wraps one input turn under `input_lock` and sets session status to `idle` when it finishes (`daemon/server.py:439-458`). The remote client finalizes the response widget on `final` or `idle` (`tui/terminal.py:3228-3288`). If auto is implemented client-side, the UI will appear to finish between internal steps. If auto is implemented server-side, you need new event types or a wider lock/status window for the whole auto session.

Also, local `_process_agent()` does not currently consume generic `status` events except tool-driven state changes. Your proposed `[AUTO] tools ... elapsed ...` bar needs explicit auto events or direct TUI state updates.

## 7. Right UX for progress

The right UX is not "show internal control prompts in the chat transcript."

For v1:

- Show a persistent status pill: `AUTO | step 3 | tools 9 | 01:42 | running run_backtest`.
- Reuse the NATS/tool log area for compact auto control events: `AUTO continue`, `AUTO paused: approval needed for shell_exec`, `AUTO stopped: loop detected`.
- Keep actual model output in the normal chat stream.
- When approval is required, show the blocked tool name and arguments explicitly. "Pending action" in the input box is too vague.
- On reconnect, reset auto to off. Persisting unattended automation across reconnects is a bad default, and the current persisted UI state does not model it anyway (`daemon/core.py:307-320`).

The design's instinct to show remaining budget and elapsed time is good. Preserve that. Just do it in the status bar or a dedicated auto log, not as fake user messages.

## 8. Modes: default / aggressive / cautious

Three modes is over-engineered for v1.

- `aggressive` is an anti-feature. "Keep feeding continue every turn" is basically "please build a runaway loop on purpose" (`design:253-255`).
- `cautious` only makes sense if you already have accurate tool metadata for "modifies state." The design does not.
- The base `auto` mode semantics are not even stable yet, so adding a mini mode taxonomy now is premature.

For v1, ship:

- `/auto`
- optionally `/auto readonly`

That is enough. Anything beyond that can wait for telemetry.

## 9. What I would change

I would narrow v1 and move it to the right layer.

1. Keep auto state in `Session` / daemon, not in the TUI.
   Local and remote need the same lock, same status lifecycle, and same event stream. Client-side auto loops will interleave badly and diverge across frontends.

2. Do not make regexes the primary continuation decision.
   Add an explicit machine-readable footer or structured field to the final answer, for example:

   ```text
   AUTO_STATE: done | pause | continue
   AUTO_REASON: <one short reason>
   ```

   If the parser is missing or malformed, stop conservatively or allow a single retry.

3. Use the current executor loop as the real engine.
   The runner already does multi-tool reasoning. Auto mode should first be a prompt/executor variant plus stronger safety policies, not a second orchestration loop pretending the inner one does not exist.

4. Rebuild or swap the prompt for auto sessions.
   Do not mutate chat history with synthetic follow-ups. Create an auto executor or a prompt swap path.

5. Use existing `override_max_iterations()` / `tool_budget` for the first budget control.
   Add wall-clock separately. Track tool count for telemetry, not as the sole safety limit.

6. Introduce tool metadata and central policy.
   Every tool should declare something like `read_only`, `persistent`, `external_side_effects`, `long_running`, `requires_approval`. Approval gating and readonly mode should key off this metadata, not hand-maintained string names.

7. Add real loop detection.
   Repeated final text, repeated tool/input tuples, consecutive no-tool turns, or no state delta should force a pause.

8. If you want a tiny v1, make the outer continuation loop a one-shot recovery path only.
   If the model ends with a permission-seeking final turn, inject one hidden auto nudge and rerun once. That solves the actual annoyance without inventing a full autonomous meta-runtime.

## 10. What is good and should be preserved

There are parts worth keeping:

- Tool-level safety gates instead of relying only on prompt text (`design:207-224`). That instinct is correct.
- A hard stop reason exposed to the user (`design:102-103`). Keep the human-readable `"AUTO: pausing because ..."` convention, just do not use it as the machine control plane.
- A visible progress indicator in the UI (`design:241-247`). Users need to see that auto is running and why it stopped.
- Hard caps and a kill switch (`design:201-229`). The concept is right even though the implementation details here are weak.
- Starting simple. The correct simple version is narrower and stricter than this proposal, but the desire not to build full AutoGPT is right.

If I had to summarize the main objection in one sentence: the design treats premature finalization as if it were a missing execution loop, and that mistake cascades into brittle regex heuristics, bad history pollution, and the wrong integration point.
