# Project Manager Prompt Starters

Use this folder for planning and delivery prompts.

## PM verification reminder
When you are operating in PM mode, your job includes operational verification, not just paperwork.

Before declaring a task or PR done, require:
- a live-smoke artifact,
- a ledger / audit-trail check timestamp,
- a post-deploy observation window with duration in minutes, and
- an explicit end-state recorded when the window ends.

Anti-pattern: closing a PR or ticket as done while the running system has not been observed exhibiting the new behavior.

## Starter template

```text
Approach this as a reusable project-management workflow.

Requirements:
- define scope
- define dependencies
- define sequencing
- define risk and mitigation
- define the live-smoke verification plan
- define the ledger / audit-trail check
- define the observation window and explicit end-state
- end with a reusable playbook section
```
