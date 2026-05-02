# Agent Workflow Review Approval

This repository uses an **observed in prod / observed in the running system** standard for completion.

## Rule: PM mode includes operational verification
When an operator is acting in project-management mode, the job is not limited to paperwork, coordination, or checking boxes.

PM responsibilities include all of the following before a PR or task is called done:
1. **Live-smoke verification** of the feature being PM'd.
   - Capture the exact command or procedure.
   - Capture the environment or host where it was run.
   - Attach the observed output, screenshot, log excerpt, or equivalent artifact.
2. **Ledger / audit-trail check**.
   - Verify the taskboard, runtime ledger, audit comment stream, or other authoritative trail reflects the expected execution.
   - Record the timestamp used for the ledger check.
3. **Post-deploy observation window**.
   - Observe the running system for a defined number of minutes after the deploy or change.
   - Record both the duration and the explicit end-state observed when the window ends.

## Required PM signoff contents
A PM signoff is incomplete unless it includes all of the following:
- Live-smoke artifact
- Environment / host / target verified
- Ledger-check timestamp
- Observation-window duration in minutes
- Observation-window explicit end-state

## Reviewer policy for orchestrator-managed PRs
For any orchestrator-managed PR, reviewers must reject the PR if the PM signoff section is missing or incomplete.

Minimum rejection conditions:
- no live-smoke artifact
- no ledger-check timestamp
- no observation-window duration
- no explicit end-state
- evidence that the system was not actually observed exhibiting the claimed behavior

## Anti-pattern
**Do not close a PR or ticket as done while the running system has not been observed exhibiting the new behavior.**

The following do **not** qualify as completion by themselves:
- merged PR
- green unit/integration tests only
- deployment message only
- documentation updated only
- verbal assertion that a change "should be live"

## Completion standard
A change is review-complete only when:
- code and docs are updated as needed,
- automated checks relevant to the change are green or any failure is explicitly explained,
- the PM signoff artifact is attached,
- the ledger / audit trail was checked, and
- the observation window ended with a recorded final state.
