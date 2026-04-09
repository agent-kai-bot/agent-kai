# KAI Prompt Library

This folder holds high-leverage prompts for the main `kai` agent.

## Best universal prompt

```text
Do not just answer the immediate trading question. Solve it in a way that extracts a reusable workflow or decision rubric that could be saved as a skill.

Objective:
Find a high-quality alpha opportunity, or determine that no valid opportunity exists, using a repeatable procedure rather than one-off commentary.

Task:
- Analyze {SYMBOL} using the most relevant timeframes for this setup.
- Use real tool-driven data and indicators.
- Explicitly decide whether the setup is:
  - actionable long
  - actionable short
  - watchlist only
  - no-trade
- If actionable, provide entry, invalidation, stop, and first target.
- If not actionable, explain exactly which criteria failed.

Requirements:
- Use multiple tool calls and cite the exact numbers and thresholds used.
- Build a scoring rubric, checklist, or decision tree instead of a narrative-only answer.
- Include regime/context, trigger, invalidation, and risk logic.
- Include at least one verification step to reduce false positives.
- If the initial hypothesis is weak, revise it once and explain why.

Final output format:
1. Verdict
2. Evidence
3. Trade plan or rejection reason
4. Reusable playbook

For the reusable playbook section, write:
- When to use
- Inputs required
- Steps
- Pitfalls
- Verification

Optimize the result so the /learn reflection flow will recognize procedural knowledge worth saving as a skill.
```

## Aggressive skill-capture variant

```text
Do not just analyze {SYMBOL}. Your goal is to discover or refine a reusable alpha-finding workflow that could become a learned skill.

Primary objective:
Determine whether {SYMBOL} currently presents a valid trade opportunity, but do it in a way that extracts a reusable decision procedure rather than one-off commentary.

Instructions:
- Use real tool-driven data.
- Use multiple indicators and, if relevant, multiple timeframes.
- Explicitly classify the outcome as:
  - actionable long
  - actionable short
  - watchlist only
  - no-trade
- If actionable, provide entry, stop, invalidation, and first target.
- If not actionable, explain exactly which criteria failed.

Critical requirement:
As you work, identify whether you are using a repeatable checklist, scoring rubric, trigger framework, or validation routine that would be useful again on future symbols or future sessions.

Final output format:
1. Verdict
2. Evidence with exact numbers and thresholds
3. Trade plan or rejection reason
4. Reusable playbook
5. Candidate skill draft

For the reusable playbook section, include:
- When to use
- Inputs required
- Steps
- Pitfalls
- Verification

For the candidate skill draft section, include:
- Skill name
- One-sentence description
- Why this is reusable
- The exact procedure that should be remembered

Optimize the response so that a later /learn reflection is likely to recognize genuine procedural knowledge worth saving as a skill.
```

## Best usage pattern

1. Run one of the prompts above with a specific symbol.
2. Make sure the reply ends with a reusable playbook or candidate skill draft.
3. Run `/learn <role>` against the specialist that did the work.
