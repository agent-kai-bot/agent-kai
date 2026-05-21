# RCA: reviewer verdicts did not land on PRs

Date: 2026-05-21

## Summary

KAI had two reviewer-fire paths with the same symptom: review agents completed analysis but the machine-readable verdict was not landed in the system that advances the gate.

Path 1, `forgejo-pr-fire`, was a prompt contract bug. The role prompts told reviewers to write an artifact file and stop. They did not instruct the agent to submit a formal Forgejo PR review.

Path 2, `taskboard-fire`, was a runtime identity bug. The taskboard prompts already required `taskboard_submit_review_verdict`, but reviewer sessions could be spawned with the daemon/generic taskboard bearer instead of a distinct reviewer bearer. That can fetch and mint in daemon contexts but is not the reviewer's taskboard identity for verdict submission.

## Path 1: forgejo-pr-fire artifact-only completion

Pre-fix evidence captured before this change:

- `prompts/forgejo-pr-fire/code-reviewer.md.tmpl:30-31` said:

```text
## Submit
Write the PR review summary to `{output_target}`. Lead with findings by severity, then list open questions, tests checked, and a concise verdict.
```

- `prompts/forgejo-pr-fire/security-auditor.md.tmpl:30-31` said:

```text
## Submit
Write the security audit to `{output_target}`. Lead with high-impact findings, then note tests or checks performed and any required remediation.
```

- `prompts/forgejo-pr-fire/qa-agent.md.tmpl:30-31` said:

```text
## Submit
Write the QA report to `{output_target}`. Include pass/fail status, executed checks, defects found, logs or artifacts produced, and remaining risk.
```

- `prompts/forgejo-pr-fire/default.md.tmpl:28-29` said:

```text
## Submit
Write the review output to `{output_target}`. Include findings, checks performed, open questions, and verdict.
```

The renderer did not compensate for that missing instruction. `agent/prompt_renderer.py:127-165` selects a role template, injects substitutions, and renders it; there is no post-processing that adds a formal review submission step. The same renderer only produced an artifact target when none was supplied, via `_forgejo_output_target` at `agent/prompt_renderer.py:685-695`.

Why one prior formal review could land: the repo already had working formal-review transport, but it was not bound into the forgejo-pr-fire prompt contract. Commit `e80ec59` added `forgejo_submit_review` and per-session Forgejo context to `agent/forgejo_tools.py`; current `agent/forgejo_tools.py:497-531` posts `APPROVED` or `REQUEST_CHANGES` to `/pulls/{pr_number}/reviews`, and `agent/forgejo_tools.py:587-681` exposes it as `forgejo_submit_review`. Commit `79f4fdc` also taught the general KAI system prompt that the external CLI supports `pr-review` and `fg-reviews`, now visible at `agent/prompts.py:46-72`. Those capabilities explain how a review could be manually or opportunistically posted, but the forgejo-pr-fire role templates never made formal submission mandatory.

Fix: each Forgejo PR reviewer template now adds a non-optional `## Submit formal review` section. Current evidence:

- Code Reviewer: `prompts/forgejo-pr-fire/code-reviewer.md.tmpl:33-52`
- Security Auditor: `prompts/forgejo-pr-fire/security-auditor.md.tmpl:33-52`
- QA Agent: `prompts/forgejo-pr-fire/qa-agent.md.tmpl:33-52`
- Default reviewer fallback: `prompts/forgejo-pr-fire/default.md.tmpl:31-50`

The new step writes the artifact, maps `APPROVED` to `approved` and blocking findings to `changes`, runs `agent-ops-forgejo-taskboard-cli.py pr-review`, verifies with `fg-reviews`, requires the expected role identity, and treats post or verification failure as a hard failure.

`agent/prompt_renderer.py:394-404` now derives `forgejo_org` and `forgejo_repo` for those CLI commands, with parsing implemented at `agent/prompt_renderer.py:698-714`.

## Path 2: taskboard-fire generic bearer in reviewer sessions

The taskboard-fire prompts already had the right terminal action:

- `prompts/taskboard-fire/code-reviewer.md.tmpl:88-90`
- `prompts/taskboard-fire/security-auditor.md.tmpl:53-55`
- `prompts/taskboard-fire/qa-agent.md.tmpl:111-113`

The tool also posts a structured verdict, not a comment. `agent/taskboard_tools.py:466-521` resolves the pending review row and posts to `/api/tasks/{task_id}/reviews/{review_id}/verdict` with `gate_type`, `verdict`, and `reviewer_user`. `agent/taskboard_tools.py:780-804` exposes `taskboard_submit_review_verdict` only to Code Reviewer, Security Auditor, and QA Agent contexts.

The failure was in credential selection. The resolver already had the right per-role taskboard token sources:

- Vault paths at `agent/runtime_config_resolver.py:31-35`: `taskboard/agent-code-reviewer`, `taskboard/agent-security-auditor`, and `taskboard/agent-qa`.
- Environment fallbacks at `agent/runtime_config_resolver.py:397-405`: `TASKBOARD_BEARER_TOKEN_<ROLE>` / `TASKBOARD_TOKEN_<ROLE>`, with the `QA` short alias.

But before this fix, `agent/runtime_config_resolver.py:_config_from_parts` silently set `taskboard_bearer_token` to the global daemon bearer when no per-role taskboard token was found. That made `RoleRuntimeConfig.env_overlay()` place the generic bearer into the spawned reviewer session. The dispatcher then carried that into the spawn path through `agent/taskboard_dispatcher.py:1249-1254`, and the daemon spawner built the session taskboard context with fallback to `runtime_config.taskboard_bearer_token`, kwargs, or process env at `agent/taskboard_dispatcher.py:763-779`.

Fix:

- `agent/runtime_config_resolver.py:423-452` now keeps `taskboard_bearer_token` strictly role-scoped and stores the daemon/admin bearer only in `taskboard_mint_bearer_token`.
- `agent/taskboard_dispatcher.py:65-70` defines the reviewer roles and their taskboard Vault paths.
- `agent/taskboard_dispatcher.py:1145-1167` validates reviewer runtime identity before session token mint or spawn.
- `agent/taskboard_dispatcher.py:648-653` repeats the same validation in the daemon spawner before attaching runtime env/context.
- `agent/taskboard_dispatcher.py:2771-2813` fails closed if a reviewer has no per-role bearer or if that bearer equals the generic daemon/admin bearer. The error names the required Vault/env sources without printing token values.

The session-token mint path still uses the daemon/admin bearer by design. `agent/taskboard_dispatcher.py:1183-1195` chooses the mint bearer separately, so reviewer write identity and daemon session-token minting no longer share one field.

## Tests

New and updated coverage:

- `tests/test_forgejo_pr_renderer.py` asserts every forgejo-pr-fire reviewer prompt contains the mandatory `pr-review` and `fg-reviews` formal-review instructions.
- `tests/test_forgejo_pr_renderer.py` asserts renderer substitutions derive the CLI org/repo target.
- `tests/test_runtime_config_resolver.py` asserts the global taskboard bearer is mint-only and does not become reviewer runtime identity.
- `tests/test_session_token_role_casing.py` asserts reviewer sessions use the per-role bearer while minting with the admin bearer, and that missing or generic reviewer bearers fail closed before mint/spawn.

## Deployment note

The operator must restart the daemon after merge/deploy so running dispatcher/spawner processes reload the prompt templates and token validation code.
