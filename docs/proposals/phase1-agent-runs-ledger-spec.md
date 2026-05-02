# Phase 1: Agent Runs Ledger Spec

## Summary
Phase 1 moves agent run lifecycle visibility into the taskboard `agent_runs` ledger so operators can audit, query, and clean in-flight operational state before and after deploys.

## Goals
- Record run lifecycle transitions in a queryable ledger.
- Make active capacity derive from taskboard-visible state.
- Give cutover operators a deterministic way to audit and clean zombie rows.

## State-prereq
Any cutover that changes the interpretation of persisted run state assumes these conditions are true before deploy:

- `agent_runs.status IN ('queued','dispatching','spawning','running')` has no zombie rows older than `KAI_STUCK_AFTER_SECONDS`.
- Any stale in-flight rows identified by preflight were patched to a terminal status before deploy.
- Capacity gates were re-checked after cleanup and returned `0` active stale rows blocking the release.
- The cutover evidence includes `snapshot → audit → clean → deploy → smoke` in that order.

## Canonical active statuses
The dispatcher treats these ledger states as active in-flight capacity:

- `queued`
- `dispatching`
- `spawning`
- `running`

## Canonical preflight cleanup
Before deploy:

1. snapshot the current ledger / DB
2. audit active rows with `scripts/preflight-agent-runs-state.sh`
3. clean stale rows older than `KAI_STUCK_AFTER_SECONDS`
4. deploy the new code
5. run live smoke and capture observed output

## Terminal cleanup mapping
Default cutover cleanup maps stale active rows to:

- `status=stuck_aborted`
- `failure_class=session_stuck_no_progress`
- `failure_detail=preflight cleanup: <old-status> row older than <threshold>s (age=<age>s) before cutover`

This preserves auditability while clearing capacity gates safely.
