# RCA: scheduler missed 2026-05-20 gameday lineup-watch kicker

Date: 2026-05-20
Impact: The 8:55 AM ET MLB lineup-watch kicker (`job_2026_05_20_125336_64e31d`) did not dispatch, so the expected morning gameday brief/watch jobs were not created until manual intervention.

## What happened

- Operator requested moving the daily MLB lineup-watch kicker from 9:00 AM ET to 8:55 AM ET.
- A new cron job was created at ~08:53:36 ET with `cron="55 12 * * *"` and `next_run=2026-05-20T12:55:00Z`.
- At ~09:04 ET, `list_scheduled_jobs` still showed the same job active with `last_run=null`, `run_count=0`, and `next_run=2026-05-20T12:55:00Z` even though that timestamp was in the past.
- No expected `2026-05-20_*` daily-brief output files existed and no per-game lineup-watch jobs were visible.

## Evidence

- `workspaces/scheduler/jobs.json` showed `job_2026_05_20_125336_64e31d` with `last_run=null`, `run_count=0`, and later cancelled manually.
- `/tmp/kai-daemon.log` contains many APScheduler messages like:
  - `Run time of job "Scheduler._fire_scheduled_job (...)" was missed by 0:00:04...`
  - `Run time of job ... hour='12', minute='55' ... was missed by 0:00:01.221641`
  - `Run time of job ... date[2026-05-20 15:04:00 UTC] ... was missed by 0:00:04.581089`
- These messages are APScheduler misfires. The old scheduler wrapper did not listen for misfire events and did not configure job-level `misfire_grace_time`.

## Root cause

APScheduler default misfire handling was too strict for this workload. When the daemon event loop was busy or delayed by a few seconds at the scheduled fire time, APScheduler classified the run as missed and did not execute it. The daemon scheduler wrapper did not subscribe to `EVENT_JOB_MISSED`, so missed jobs were not catch-up dispatched. Persisted `next_run` could remain stale until another scheduler state update, which made the job look active but wedged.

Contributing factors:

1. The new 8:55 job was created very close to its first fire time (~84 seconds before), leaving little margin.
2. The daemon was under heavy operator/gameday load and had other long-running scheduled jobs/codex sessions.
3. Old missed one-shot jobs remained active with stale `next_run` in the scheduler store, making operational visibility noisy.

## Fix implemented

Code changed in `daemon/scheduler.py`:

1. Add an APScheduler listener for `EVENT_JOB_MISSED`.
2. On recent misfire, schedule an immediate internal catch-up dispatch through the same `_fire_scheduled_job` path.
3. Add `misfire_grace_time=self.catch_up_window_seconds` and `coalesce=True` to date and cron jobs.
4. Persist recalculated `next_run` during startup recovery so `jobs.json` and UI do not retain stale next-run timestamps after daemon restarts.
5. Guard against very old misfires by skipping catch-up when age exceeds the bounded catch-up window.

Tests added in `tests/test_daemon_scheduler.py`:

- Recent cron misfire triggers catch-up dispatch.
- Old cron misfire is not caught up.
- Scheduled jobs are registered with expected `misfire_grace_time` and `coalesce=True`.

Verification:

```bash
cd /home/atc/git/claude-local-ai-agent
.venv/bin/python -m pytest tests/test_daemon_scheduler.py -q
# 11 passed
```

## Operational remediation performed

- Cancelled stuck/missed 8:55 job.
- Created replacement recurring 8:55 AM ET job for tomorrow onward: `job_2026_05_20_150240_44fbb1`.
- Created a one-shot catch-up job for today: `job_2026_05_20_150303_117e77`.
- Manually started the daily brief dispatcher for 2026-05-20 because today's slate needed immediate coverage.

## Follow-ups

- Restart the KAI daemon after deploying this code so the live scheduler picks up the misfire listener and job misfire grace.
- Prune or cancel stale active absolute jobs with `next_run` in the past and `run_count=0`.
- Add scheduler health alerting: active job with `next_run < now - 2m` should be P1/Discord alert.
- Consider moving production gameday brief dispatch from LLM-scheduled prompts into a deterministic repo cron wrapper for the critical 8:55/lineup-watch path.
