# KAI UI Scheduler Panel RCA

Root cause: the web UI scheduler panel was not rendering the persisted scheduler inventory from `workspaces/scheduler/jobs.json`. `web/src/routes/+page.svelte` only appended `scheduled_job_*` websocket lifecycle envelopes into `schedulerEvents`, then rendered those recent events through `EventPanel`. That means active jobs that already existed at page load, or jobs owned by sessions whose events were not visible on the attached websocket, never appeared in the panel.

Backend state also had a brittle visibility filter in `DaemonServer.metrics_snapshot()`: it counted only jobs whose `owner_session` appeared in live `self.sessions` or `list_indexed_sessions()`. If an owner session was not loaded or indexed, those jobs were removed from scheduler metrics even though `Scheduler.list_jobs()` had loaded them from `jobs.json`. That filter could make API/health consumers report zero jobs for valid persisted jobs.

Verification on 2026-05-17:

- `workspaces/scheduler/jobs.json` contains active `polymarket` and `polymarket-main` jobs.
- `workspaces/sessions/index.json` currently includes both `polymarket` and `polymarket-main`, so the metrics filter is not hiding them in the live state checked during this RCA.
- `GET /api/health` on `127.0.0.1:18789` reported `scheduler_job_count: 41`.
- The Svelte panel source does not call `/api/metrics` for job rows; it only reads scheduler websocket events.

Reproduction steps:

1. Start the daemon and UI.
2. Open the UI and attach to any session.
3. Observe the Scheduler panel: it shows recent lifecycle events only, not the current jobs from `jobs.json`.
4. Compare with daemon state: `GET /api/health` or direct `jobs.json` inspection shows scheduled jobs exist.
5. In a test with jobs owned by unindexed sessions, the old metrics filter reports `scheduler.job_count == 0`.

Chosen fix: show all jobs. The backend now counts all `Scheduler.list_jobs()` entries and exposes `GET /api/scheduler/jobs`, returning normalized rows with `id`, `cron`/schedule, `prompt_preview`, `owner_session`, `next_run`, `status`, `last_run`, `run_count`, and `max_runs`. The Svelte panel now fetches that endpoint and renders the full inventory with a session column. This matches the single-user operator preference and avoids tying scheduler visibility to session memory/index state.
