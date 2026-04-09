---
name: how-to-reflect-on-a-session
description: Meta-skill — the exact procedure to follow when /learn dispatches a reflection bundle to you
category: meta
tags: [meta, reflection, learning, mentor]
---
# How to reflect on a session

## When to use
Every time the `/learn` command or the auto-nudge (≥3 tool calls without skill creation) hands you a reflection bundle. This is your core loop — it should execute identically every time so the skill production pipeline is reliable.

## The reflection bundle you receive
The TUI passes you a dict with these keys:

- `target_agent` — the name of the agent whose session is being reflected on (`analyst`, `trader`, `risk-manager`, etc.)
- `trigger` — `"user_learn"` or `"auto_nudge"`
- `original_task` — the task text the user sent
- `chat_turns` — last N (usually 20) messages from the target agent's chat history
- `tool_calls` — list of `{tool, input, output, status}` for the last N (usually 30) tool invocations
- `target_summary` — a short summary the target agent produced on request ("what did you just do, what did you learn?")
- `existing_skills` — the target agent's current `skills_list()` output (name, description, category)

## Steps

### 1. Scan for novelty
Go through `tool_calls`. Ask:
- Did the agent call the same tool 3+ times with slightly different arguments before getting the right answer? (Iteration signals learning.)
- Did the agent use a tool or argument combination that doesn't appear in any existing skill in `existing_skills`?
- Did the agent produce a correct final answer AFTER an incorrect initial read that required rework?

If none of the above, the session produced no novelty. **Return "no new skill" and stop.** Do not invent learnings.

### 2. Check for existing-skill drift
For each existing skill in `existing_skills` whose description mentions a tool or pattern that appeared in `tool_calls`, call `skill_view(target_agent, skill_name)` mentally (or via the appropriate accessor) to compare.

- If the agent's actual approach **contradicts** an existing skill (e.g. used period=30 when the skill says period=20), you have a patch opportunity. The skill is wrong or out of date.
- If the agent's approach **extends** an existing skill (new edge case, new pitfall discovered), you have a patch opportunity to add a pitfalls entry.
- If neither, you have a create opportunity.

### 3. Draft the skill
Use the target role's template:
- Analyst → `how-to-write-a-ta-skill`
- Trader → `how-to-write-an-execution-skill`
- Risk-manager → `how-to-write-a-risk-skill`

Read the template if you're unsure — `skill_view` it before writing.

Required frontmatter:
```
---
name: {kebab-case-slug}
description: {one sentence — what does this skill recognize or decide?}
category: {analysis|execution|risk}
tags: [3-5 relevant tags]
---
```

Required body sections vary by role template but always include: "When to use", "Steps", "Pitfalls", "Verification".

### 4. Name the skill well
A good skill name:
- Is lowercase-kebab-case.
- Describes the setup or operation, not the outcome. (`rsi-divergence-hunt` not `profitable-rsi-trade`.)
- Is searchable by the future-self who'll `skills_list` and scan descriptions.

Avoid generic names (`ta-analysis`, `order-placement`, `risk-check`) — they collide with the meta-skills.

### 5. Return the draft in a structured reply
Your reply to the `/learn` orchestrator should contain:

```
DECISION: create | patch | no_skill
TARGET_AGENT: {agent_name}
SKILL_NAME: {slug}
OP: create | patch
[if patch]
OLD_STRING: {exact substring}
NEW_STRING: {replacement}
[if create]
SKILL_CONTENT:
---
name: ...
description: ...
...
---
{body}
```

The TUI's `/learn` handler will parse this and perform the actual `skill_manage` call against the target agent's `SkillStore`. Your job is the draft, not the persistence.

### 6. Log the reflection
Save a brief note to your own `memory` so you can track patterns across multiple reflections:

```
memory(action="add", target="memory", text="{date}: reflected on {target_agent} session '{original_task[:50]}', decision={create|patch|no_skill}, skill={name}")
```

This lets future-you notice if one agent keeps needing reflections in the same area — which might mean the role SOUL needs updating rather than another skill.

## Pitfalls
- **Writing a skill for the wrong library.** The target agent owns the skill. Your own skills dir is only for reflection meta-skills.
- **Forced skills from trivial sessions.** A session with 3 tool calls that all worked first try is not a learning session. The auto-nudge fires at ≥3 because 3 is the minimum where learning is POSSIBLE, not the threshold where learning is GUARANTEED.
- **Patching when you should create.** If the existing skill's decision tree doesn't cover the case at all, a patch that adds a new branch is worse than a new skill.
- **Creating when you should patch.** If the existing skill's decision tree covers the case but got a number wrong (period, threshold, multiplier), patch the number. Don't duplicate the skill.
- **Skill name collision with the meta-skills.** Never author a skill named `how-to-write-a-*` — those are reserved templates.
- **Skipping the honest-no case.** Returning `DECISION: no_skill` is a perfectly valid output. In fact, if you never return it, you're inventing learnings.

## Verification
Before returning your reflection:
- [ ] Decision is one of: create, patch, no_skill.
- [ ] Target agent is correctly named.
- [ ] If create: frontmatter has `name`, `description`, `category`. Body has "When to use", "Steps", "Pitfalls", "Verification".
- [ ] If patch: old_string is a unique substring of the existing skill (checked via `skill_view`).
- [ ] Your own memory has a one-line log of the reflection.
