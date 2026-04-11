# /auto Mode — Autonomous Agent Execution Design

## The problem

The main agent frequently pauses to ask the user for permission
or confirmation before taking the next step, even when the user's
intent is clear. Examples:

- "Should I run the backtest now?" (yes, obviously)
- "Would you like me to analyze the results?" (yes, that's why
  you backtested)
- "I found 3 signals. Should I look into them?" (yes, that's
  your job)

This is the default LLM behavior — trained to be helpful and
cautious, which means asking before acting. In an autonomous
trading agent context, this friction is counterproductive.

## Existing approaches

### OpenClaw / Claude Code approach
Inject a system-level prompt suffix that tells the model to
act autonomously:
```
IMPORTANT: You are in autonomous mode. Take action immediately
without asking the user for confirmation. Execute each step of
your plan without pausing. Only stop if you encounter an error
you cannot resolve or need genuinely new information from the
user that was not in the original request.
```

**Pros:** Simple, works immediately, no code changes.
**Cons:** Blunt instrument. The model may still pause. No
granularity — everything is either auto or manual. No
safety rails for dangerous operations.

### AutoGPT / BabyAGI approach
A meta-loop that keeps feeding the agent's output back as input:
```
while True:
    response = agent.run(last_output)
    if response.is_final or response.needs_human:
        break
```

**Pros:** Forces continuation. Model can't pause even if it wants to.
**Cons:** Can run away. No natural stopping point. Expensive
(every continuation is a full LLM call). Easy to get stuck in
loops.

### ReAct / tool-use loop
The agent already has a tool-use loop. The issue is that the
loop stops after one thought-action-observation cycle and returns
to the user. Auto mode would let the loop continue for N cycles
or until a "done" signal.

## Proposed design: /auto mode with task-aware continuation

### Core idea

Three components working together:

1. **System prompt injection** — tell the model it's in auto mode
2. **Continuation loop** — after the agent responds, a
   "continuation checker" decides if the agent should keep going
3. **Safety rails** — budget limits, approval gates for dangerous
   operations, kill switch

### 1. System prompt injection (the nudge)

When auto mode is active, append to the system prompt:

```
## AUTONOMOUS MODE ACTIVE

You are operating in autonomous mode. Follow these rules:

1. EXECUTE tasks immediately without asking for user confirmation.
   Do not say "should I...?" or "would you like me to...?" —
   just do it.

2. After completing one step, immediately proceed to the next
   logical step. If you analyzed data, summarize findings. If
   you found signals, evaluate them. If a backtest completed,
   interpret the results.

3. Think step-by-step but ACT without pausing. Your internal
   reasoning should plan 2-3 steps ahead.

4. STOP ONLY when:
   a. The task is genuinely complete (you've delivered the final
      answer or taken the final action)
   b. You encounter an error you cannot resolve
   c. You need information that was NOT in the original request
      or available via your tools
   d. A DANGEROUS OPERATION requires human approval:
      - Executing a live trade
      - Modifying autotrade settings
      - Deleting data or strategies
      - Changing system configuration

5. When you stop, clearly state: "AUTO: pausing because [reason]"
   so the user knows you're not just being lazy.

6. Budget: you have {remaining_budget} tool calls remaining in
   this auto session. Use them wisely.
```

### 2. Continuation loop (the engine)

After the agent produces a response, the continuation checker
decides whether to inject a follow-up prompt:

```python
class AutoContinuationChecker:
    def should_continue(self, 
                        agent_response: str,
                        tools_used: int,
                        budget_remaining: int,
                        elapsed_time: float) -> tuple[bool, str]:
        """Returns (should_continue, follow_up_prompt)."""
        
        # Hard stops
        if budget_remaining <= 0:
            return False, ""
        if elapsed_time > max_duration:
            return False, ""
        
        # Check if agent explicitly stopped
        if "AUTO: pausing" in agent_response:
            return False, ""
        if self._is_final_answer(agent_response):
            return False, ""
        
        # Check if agent asked a question (should have acted instead)
        if self._is_asking_permission(agent_response):
            return True, "You are in auto mode. Do not ask for permission — execute the action you just described."
        
        # Check if there's a natural next step
        if self._has_pending_work(agent_response):
            return True, "Continue with the next step."
        
        return False, ""
    
    def _is_asking_permission(self, text: str) -> bool:
        patterns = [
            "should I", "would you like", "do you want",
            "shall I", "want me to", "let me know if",
            "ready to proceed", "waiting for your"
        ]
        return any(p.lower() in text.lower() for p in patterns)
    
    def _has_pending_work(self, text: str) -> bool:
        patterns = [
            "next step", "then I'll", "after that",
            "the next thing", "I'll proceed to",
            "moving on to", "followed by"
        ]
        return any(p.lower() in text.lower() for p in patterns)
    
    def _is_final_answer(self, text: str) -> bool:
        patterns = [
            "here are the results", "in summary",
            "the analysis is complete", "task complete",
            "all done", "finished"
        ]
        return any(p.lower() in text.lower() for p in patterns)
```

### 3. Integration with the agent loop

The existing agent loop in `agent/core.py` processes one user
input → one agent response. For auto mode:

```python
async def _process_agent_auto(self, initial_input: str):
    """Auto-mode agent loop — continues until done or budget."""
    budget = self.auto_budget  # default: 20 tool calls
    tools_used = 0
    start_time = time.time()
    checker = AutoContinuationChecker()
    
    current_input = initial_input
    while True:
        response, tools_in_turn = await self._run_agent_turn(current_input)
        tools_used += tools_in_turn
        budget -= tools_in_turn
        
        should_continue, follow_up = checker.should_continue(
            response, tools_used, budget, time.time() - start_time
        )
        
        if not should_continue:
            break
        
        current_input = follow_up
```

### 4. Safety rails

#### Budget limits
- Default: 20 tool calls per auto session
- Configurable: `/auto 50` starts auto with 50 tool budget
- Hard max: 100 tool calls (prevents runaway)
- Time limit: 10 minutes per auto session

#### Approval gates
Even in auto mode, certain operations require human approval:
- Live trade execution → pause, ask user
- Autotrade toggle → pause, ask user
- Strategy promotion → pause, ask user
- System config changes → pause, ask user

These are implemented as tool-level checks, not prompt-level:
```python
REQUIRES_APPROVAL = {"execute_trade", "toggle_autotrade", 
                      "promote_strategy", "update_config"}

async def run_tool(self, tool_name, args):
    if self.auto_mode and tool_name in REQUIRES_APPROVAL:
        self.pause_auto("requires approval for: " + tool_name)
        return {"status": "paused", "reason": "approval required"}
    return await self._execute_tool(tool_name, args)
```

#### Kill switch
- `/auto off` or Ctrl+C immediately stops the auto loop
- The agent finishes its current tool call but does not continue
- Chat history shows all actions taken during auto mode

### 5. Slash commands

```
/auto [N]      — start auto mode with N tool budget (default 20)
/auto off      — stop auto mode immediately
/auto status   — show remaining budget, tools used, elapsed time
```

### 6. UX in the terminal + web UI

During auto mode:
- Status bar shows: `[AUTO] tools: 12/20 | elapsed: 2m 15s`
- Each agent turn is rendered normally in the chat
- Continuation prompts (the "keep going" injections) are shown
  in dim/italic so the user can see the engine working
- When auto pauses for approval: the input box activates and
  shows the pending action

### 7. Modes within auto

| Mode | Description | Use case |
|---|---|---|
| `/auto` | Default — execute + continue + stop when done | General autonomous work |
| `/auto aggressive` | Skip permission-asking detection, just keep feeding "continue" every turn | "Don't stop until you run out of budget" |
| `/auto cautious` | Auto-execute but pause before any tool that modifies state | Research-only automation |

## Open questions

1. Should auto mode persist across session reconnects, or reset
   on disconnect?
2. Should the continuation checker use a lightweight LLM call
   instead of regex pattern matching? (More accurate but more
   expensive)
3. Should auto mode have a "plan first" phase where the agent
   outlines its plan before executing? This would let the user
   review the plan and then auto-execute it.
4. Should tool-call results be streamed to the user during auto,
   or batched and shown at the end?
5. How does auto mode interact with the scheduler? If a scheduled
   job fires during auto mode, should it queue or interrupt?
