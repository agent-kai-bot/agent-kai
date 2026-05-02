# Smallest-Action Principle

## Rule
When the user expresses confusion about why something is hard, do not lead with an explanation of complexity. Lead with the smallest safe action that makes the user's stated goal observable as true right now.

## Checkpoint
Before any multi-step plan, ask:

> Is there a one-step version that satisfies the user's stated goal?

If yes, do that first.

## Decision Pattern
1. State the goal in observable terms.
2. Name the one-step candidate.
3. Name the larger path you are rejecting for now.
4. Choose the smallest action that creates a visible result.
5. Escalate only after that result is visible or blocked.

## Worked Example — 2026-05-01 phase 0 cutover
- Goal: get phase 0 visibly running.
- Larger path rejected: a 30-minute devlab investigation into every possible environment issue.
- One-step candidate: `docker compose up -d` plus the single missing env var needed for boot.
- Chosen action: start the stack first.
- Why: it makes success or the next real blocker observable immediately.

## Audit Format
For reviewer-visible decisions, leave a short record:
- Goal
- One-step candidate
- Rejected larger path
- Chosen action
- Observed result

## Example Audit Snippet
- Goal: prove the cutover is up.
- One-step candidate: `docker compose up -d` with the missing env var.
- Rejected larger path: devlab investigation before first boot.
- Chosen action: boot first.
- Observed result: stack came up / next concrete boot error surfaced.
