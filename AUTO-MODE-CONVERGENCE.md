# /auto Mode Convergence

## Verdict

**CONVERGED, but only on the narrowed v1.**

The v2 reframe is correct: the problem is **premature finalization**, not a missing execution loop. `AgentRunner` already delegates to `AgentExecutor` with a real internal multi-step tool loop and a large iteration budget (`agent/core.py:433-444`, `agent/core.py:554-571`, `agent/core.py:573-700`). The right v1 is therefore:

- prompt/executor variant for auto mode
- session/daemon-owned control flow
- one hidden recovery retry at most
- strict tool policy gates
- conservative stop behavior

I would not block implementation on the remaining points below; they are implementation priorities, not design-level misframing.

## 1. Is the v2 reframe correct?

Yes.

The v1 design claimed the loop stops after one thought/action cycle. That is contradicted by the current runner: the inner engine is already `AgentExecutor`, with `max_iterations` support and fallback executors (`agent/core.py:437-460`, `agent/core.py:555-571`, `agent/core.py:669-700`). v2 correctly moves the fix to the **prompt + runner/session layer**.

## 2. Criticism-by-Criticism Status

### 1. Regex continuation checker / wrong layer

**Adequate fix.**

Deleting the regex-driven continuation checker is the right correction. Using model-emitted control state is materially better than trying to infer intent from surface phrases.

### 2. Budget model

**Adequate fix.**

Switching from raw tool count to `max_iterations + wall-clock`, and treating tool count as telemetry only, matches the current architecture (`agent/core.py:555-571`, `daemon/core.py:707-715`).

### 3. Prompt injection / prompt lifecycle

**Adequate fix, with one caveat.**

v2 correctly recognizes that the prompt is currently frozen at runner construction and reused by `reload_llm()` (`agent/core.py:429-445`, `agent/core.py:478-518`). So auto mode needs a real prompt rebuild path.

The caveat: “prompt swap” is acceptable only if it also recreates the primary and fallback executors atomically. A string-level toggle without executor rebuild is not enough.

### 4. Stuck behavior / history pollution / busy scope

**Mostly fixed in design; one must-have implementation guard remains.**

Session/daemon ownership is the right answer, because server-side input locking and idle transitions are session-scoped today (`daemon/server.py:475-506`), and the TUI currently assumes one user input maps to one streamed response (`tui/terminal.py:3218-3306`).

The remaining must-have guard: v2 only mentions suppressing the hidden retry `HumanMessage`. That is not sufficient. `AgentRunner.run()` also appends the `AIMessage` for every turn (`agent/core.py:575`, `agent/core.py:654`), and session save/load persists history (`daemon/core.py:614-680`). If the first auto pass is “hidden,” both sides of that synthetic turn need to stay out of persisted history.

### 5. Approval gates incomplete

**Adequate fix conceptually.**

Moving to a central tool policy table is the right correction. That policy must cover the real tool surface, including host mutation, frontier executors, scheduler mutation, trading, and autonomy-expanding tools, not just a small ad hoc allow/deny set.

### 6. Streaming and TUI interaction

**Adequate architectural fix.**

Moving auto logic out of the TUI and into session/daemon resolves the biggest integration error in v1. The UI can render events; it should not own the auto loop.

### 7. Progress UX

**Adequate fix.**

Status-bar/session-event driven progress is the right direction. Avoiding fake user messages is the correct correction.

### 8. Mode taxonomy

**Adequate fix.**

Dropping `aggressive` and keeping only `/auto` and `/auto readonly` is the right simplification.

## 3. Is the one-shot recovery v1 viable or too narrow?

It is **viable and appropriately narrow**.

That is the right v1 precisely because the underlying executor already does multi-tool work inside one turn. A one-shot hidden retry addresses the specific annoyance without inventing a second autonomous runtime.

It is narrow by design, and that is good. It will not solve every premature-finalization case. It does not need to.

## 4. Prompt-swap vs executor-swap

**Acceptable, with a strict definition.**

If “prompt swap” means:

- rebuild the system prompt for auto mode
- recreate the primary executor and fallback executors against that prompt
- preserve normal chat history

then it is acceptable.

If it means mutating some prompt string while reusing the old executors, it is not acceptable.

So the practical answer is: **prompt-swap is fine, but only via executor rebuild**.

## 5. Is the structured `AUTO_STATE` footer reliable enough?

**Reliable enough for the narrowed v1, not reliable enough as the sole control plane for a broader autonomous mode.**

For v1, the footer is good enough if:

- parsing is strict
- missing/malformed footer means stop
- tool-policy pauses override footer claims
- loop detection and wall-clock limits also override footer claims

That is a big improvement over regexes.

But it is still model-generated text. I would trust it for a conservative one-shot recovery path, not for an open-ended autonomous session without additional state checks.

## 6. New Concerns From v2

### 1. Recovery trigger is underspecified

v2 says “kill the regex checker,” but also says one-shot recovery should happen when the model ends with permission-seeking language. Those are not the same trigger.

This needs one explicit rule:

- either the hidden retry is driven only by `[AUTO_STATE: continue]`
- or there is one narrowly scoped fallback parser for the first-turn recovery case

Right now those two ideas are mixed together.

### 2. “No history pollution” fix is incomplete

Guarding only `chat_history.append(HumanMessage(...))` is not enough (`agent/core.py:575`). The hidden pass also currently appends an `AIMessage` (`agent/core.py:654`), logs request/response, and can publish bus output (`agent/core.py:578-579`, `agent/core.py:656-665`). The hidden retry needs an explicitly ephemeral path, not a single boolean around user-message append.

### 3. Footer should not outrank tool policy

If a tool policy wrapper determines “pause for approval,” that should short-circuit auto regardless of what footer the model emits. The authoritative pause reason should come from runtime policy, not from the model’s self-report.

## 7. Final Call

**CONVERGED.**

The core conceptual disagreements are resolved:

- correct problem diagnosis
- correct integration layer
- correct budget model
- correct narrowing of v1
- correct move away from regexes and TUI-owned looping

The remaining issues are implementation details that should be handled during build, not reasons to reopen the design.

## Top 5 Implementation Priorities

1. Add an `AgentRunner` auto-mode rebuild path that regenerates the system prompt and recreates the primary plus fallback executors, instead of relying on the frozen `self._prompt`.
2. Add an explicitly ephemeral hidden-turn path so the one-shot retry does not append synthetic `HumanMessage` or intermediate `AIMessage` entries, and does not leak synthetic control turns into normal persistence/logging paths.
3. Implement the auto loop at the session/daemon layer under the existing session lock so activity state, persistence timing, and remote/local behavior stay consistent.
4. Introduce a central tool policy registry and enforce it at tool execution time for dangerous, persistent, long-running, and autonomy-expanding tools.
5. Implement strict `AUTO_STATE` parsing plus conservative stop conditions: malformed footer, wall-clock exceeded, iteration exceeded, runtime policy pause, and basic loop detection.
