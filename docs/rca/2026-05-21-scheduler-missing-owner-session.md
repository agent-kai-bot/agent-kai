# RCA: scheduled per-game jobs fired but did not write briefs

Date: 2026-05-21
Impact: MLB lineup-watch / lineup-eval one-shot jobs for fresh per-game sessions fired and then failed before prompt execution. Expected files such as `/home/atc/git/OPS/vpn-stack/docs/daily_brief/2026-05-21_atl-mia_lineup-eval.md` were not created.

## Summary

Two scheduler issues were observed across 2026-05-20/21:

1. 2026-05-20 morning cron misfires: APScheduler missed near-term jobs by a few seconds under load. This was addressed separately in `daemon/scheduler.py` by adding misfire catch-up and grace handling.
2. 2026-05-20/21 per-game job failures: absolute jobs did fire, but failed immediately because the daemon tried to attach to the requested owner session with `create_if_missing=False`. Fresh per-game sessions like `lineup-watch-2026-05-21-atl-mia` did not pre-exist, so no prompt ran and no brief was written.

This RCA covers #2.

## Evidence

`workspaces/scheduler/jobs.json` showed failed jobs with `last_run` set and error previews like:

```text
job_2026_05_21_195623_6b7141 failed lineup-eval-2026-05-21-atl-mia-1559 2026-05-21T19:59:01.582695+00:00 "session 'lineup-eval-2026-05-21-atl-mia-1559' does not exist"
job_2026_05_21_133119_82f79b failed lineup-watch-2026-05-21-atl-mia 2026-05-21T19:55:00.310035+00:00 "session 'lineup-watch-2026-05-21-atl-mia' does not exist"
job_2026_05_21_133119_f296fc failed lineup-watch-2026-05-21-cle-det 2026-05-21T14:25:01.129441+00:00 "session 'lineup-watch-2026-05-21-cle-det' does not exist"
```

The same pattern appeared for the 2026-05-20 15-game fresh-session dispatch:

```text
job_2026_05_20_213208_a95bb2 failed lineup-eval-2026-05-20-atl-mia ... "session 'lineup-eval-2026-05-20-atl-mia' does not exist"
...
```

Because `last_run` is populated, these were not scheduler timing misses. The jobs fired and failed during session attach.

## Root cause

In `daemon/server.py`, scheduled-job dispatch used:

```python
managed = await self.get_or_create_session(
    job.owner_session,
    create_if_missing=False,
)
```

This only works when the scheduled job targets an already-existing session such as `polymarket-main`. It fails for the operator's newer pattern of one fresh session per game: `lineup-watch-*`, `lineup-eval-*`, etc.

There is special lifecycle handling for sessions prefixed `agent:kai:cron-wake:`, but the actual per-game session names do not use that prefix. Therefore they were neither pre-created nor auto-created at fire time.

## Fix implemented

Changed scheduled-job dispatch to create the owner session on demand:

```python
managed = await self.get_or_create_session(
    job.owner_session,
    create_if_missing=True,
)
```

File changed:

- `daemon/server.py`

Regression coverage added:

- `tests/test_session_lifecycle.py::SessionLifecycleTests::test_scheduled_job_fire_creates_missing_non_prefixed_owner_session`

## Verification

Focused tests passed:

```bash
cd /home/atc/git/claude-local-ai-agent
.venv/bin/python -m pytest \
  tests/test_session_lifecycle.py::SessionLifecycleTests::test_scheduled_job_fire_creates_missing_non_prefixed_owner_session \
  tests/test_session_lifecycle.py::SessionLifecycleTests::test_scheduled_job_fire_closes_cron_wake_session_on_success \
  -q
# 2 passed

.venv/bin/python -m pytest tests/test_daemon_server.py -k scheduled_job_dispatch_runs_in_target_session -q
# 1 passed
```

A broader scheduler/session run produced one unrelated Codex CLI timeout in an auto-loop-brain startup probe; that was not caused by this patch.

## Deployment note

The live daemon must be restarted after this patch so future scheduled jobs use the fixed code.

## Remaining gaps

- Today's final `2026-05-21_*_lineup-eval.md` files were still missing at investigation time; only morning `*_deep.md` and manual `*_lineup-confirmed.md` files existed.
- The local LLM fallback at `192.168.222.222:8002` was connection-refused during the RCA, so sub-agent request/reply can still fail if cloud LLM has a transient issue.
- Add a durable health alert for any failed scheduled job whose preview contains `session ... does not exist` or whose expected output file is missing after deadline.
