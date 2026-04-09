# Mentor Prompt Library

This folder is for prompts that improve learning, reflection, and skill capture quality.

## Reflection helper prompt

Use this when you want to prime the target run before calling `/learn`.

```text
Do not just solve the task. As you work, notice whether you are discovering a reusable checklist, scoring rubric, validation routine, or tool choreography that would be valuable in future sessions.

When you finish:
- briefly solve the immediate task
- summarize the reusable method you used
- name the decision criteria and thresholds
- state whether this looks like a candidate skill or not

Focus on procedural learning, not commentary.
```

## Why /learn returns no_skill

The current learning pipeline prefers:
- non-trivial workflows
- repeated tool use
- validation or verification
- exact thresholds and formulas
- reusable playbooks

It rejects or deprioritizes:
- plain market commentary
- simple one-off asks
- outputs with no clear reusable procedure

## Best user pattern

1. Ask a specialist to solve a task as a reusable workflow.
2. Make sure the reply includes a playbook or decision rubric.
3. Then run `/learn <agent>`.

## Related files

- [learn trigger patterns](/home/atc/git/claude-local-ai-agent/docs/mentor/prompts/learn-trigger-patterns.md)
- [no_skill versus create](/home/atc/git/claude-local-ai-agent/docs/mentor/prompts/no-skill-vs-create.md)
