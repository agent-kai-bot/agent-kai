# Architecture Artifact — Task 10380

## Title
cuOpt research stack on gpubox — phasing + runner architecture for parent epic #10379

## Context inspected
- Taskboard task `10380`: architecture request for a read-only design and phased ticket plan; no blockers or dependencies are registered.
- Prior architecture docs under `docs/architecture/`: no existing cuOpt/gpubox architecture decision found.
- Reference repo `/home/atc/git/cuopt-examples`:
  - `portfolio_optimization/README.md`: portfolio examples cover CVaR, QP, and advanced portfolio optimization references.
  - `portfolio_optimization/cvar_portfolio_optimization.ipynb`: uses `cuopt.linear_programming.problem.Problem`, `VType`, `sense`, `LinearExpression`, and `SolverSettings` to formulate CVaR as an LP with portfolio weights, VaR variable, per-scenario auxiliary variables, budget constraint, and per-scenario CVaR constraints.
  - `portfolio_optimization/QP_portfolio_optimization.ipynb`: confirms the QP path should stay in the Python formulation family rather than notebook-as-runner.
  - `AI-accelerated-routing/README.md`: EARLI pattern uses RL to produce initial VRP solutions and injects them into cuOpt GA solver; useful later for arbitrage-route CVRP-style exploration, but too heavy for Phase 1.
- Hardware facts from task: `gpubox` / `dev01`, 2x RTX 3090 24GB, NVLink P2P around 56 GB/s aggregate, Driver 590.48.01, CUDA 12.3, nvidia-container-runtime, Docker 29.2.1, image `nvidia/cuopt:25.12.0a-cuda12.9-py3.13`.

## Problem statement
KAI needs a GPU-backed research capability for portfolio optimization and later routing-style arbitrage research without giving the main KAI daemon broad SSH, Docker, filesystem, or production-data write privileges. The first useful target is CVaR portfolio optimization over a strategy basket; later targets include productionizable runner APIs, multi-leg arbitrage routing, parameter-sweep MIP work, and an observability/cost dashboard.

The architecture should make early experiments cheap, reproducible, and safe while keeping a clear path from notebook-derived formulation code to a managed runner.

## Goals
1. Provide a practical cuOpt runner topology on `gpubox` that KAI can orchestrate safely.
2. Keep cuOpt execution isolated behind a narrow job contract.
3. Support LP/QP/CVaR portfolio jobs first, without blocking routing work later.
4. Use both RTX 3090 GPUs safely without assuming NVLink creates a unified 48GB solve target.
5. Produce structured artifacts and metrics that can be reviewed by KAI agents, taskboard comments, and later dashboards.
6. Preserve read-only access to production inputs and enforce scoped credentials.
7. Define implementable phases, gates, risks, acceptance criteria, and realistic ETA.

## Non-goals
- No code changes in this architecture ticket.
- No tickets filed by this task.
- No SSH writes, Docker pulls, or remote mutations on `gpubox`.
- No production trading automation from cuOpt output in early phases.
- No attempt to productionize EARLI/RL routing in the CVaR spike.
- No direct Docker or SSH privilege from the KAI daemon to `gpubox`.

---

# Proposed document for `docs/architecture/10380-cuopt-research-stack.md`

## 1. Recommended architecture summary

Build a small cuOpt research runner on `gpubox` and integrate KAI with it through a narrow, authenticated job API. KAI should submit declarative optimization jobs, poll/receive results, and summarize artifacts. KAI should not run notebooks, shell commands, Docker, or arbitrary Python on `gpubox`.

High-level topology:

```text
┌──────────────────────────┐
│ KAI orchestrator / agents│
│ - analyst / optimizer    │
│ - scheduler              │
│ - taskboard summarizer   │
└────────────┬─────────────┘
             │ HTTPS or NATS-to-HTTP bridge
             │ scoped cuOpt runner token
             ▼
┌───────────────────────────────────────────────┐
│ gpubox cuOpt runner API                        │
│ - submit/status/result/cancel/health           │
│ - validates job schema + input hashes          │
│ - owns queue and GPU placement                 │
│ - writes structured artifacts                  │
└────────────┬──────────────────────┬───────────┘
             │                      │
             ▼                      ▼
┌───────────────────────┐   ┌───────────────────────┐
│ worker gpu0 container │   │ worker gpu1 container │
│ CUDA_VISIBLE_DEVICES=0│   │ CUDA_VISIBLE_DEVICES=1│
│ cuOpt Python/REST     │   │ cuOpt Python/REST     │
└───────────┬───────────┘   └───────────┬───────────┘
            │                           │
            └──────────────┬────────────┘
                           ▼
┌───────────────────────────────────────────────┐
│ gpubox artifact store                         │
│ /srv/kai-cuopt/jobs/<job_id>/                 │
│ - input manifest + normalized problem         │
│ - solver.log / cuopt.log                      │
│ - solution.json / portfolio.csv               │
│ - metrics.json / gpu_samples.jsonl            │
│ - summary.md                                  │
└───────────────────────────────────────────────┘
```

Recommended execution modes behind the same runner contract:

1. `lp_qp_python`: direct Python formulation using `cuopt.linear_programming.problem.Problem` for LP/QP/CVaR portfolio jobs. This is the Phase 1 primary path because the portfolio notebooks already demonstrate this API.
2. `routing_service`: self-hosted cuOpt routing service mode for VRP/CVRP-style jobs. This should be introduced only when Phase 3 starts.
3. `routing_ai_initialized`: optional later EARLI-style RL initialization plus cuOpt GA solve. Do not include in Phase 1/2 production runner unless Phase 3 proves value.

## 2. Options considered

### Option A — Notebook execution over SSH
KAI SSHes to `gpubox` and runs modified notebooks or `jupyter nbconvert`.

- Pros: fastest manual experiment path.
- Cons: hard to secure, stateful, poor idempotency, difficult artifact schema, notebooks hide dependencies, KAI would need remote execution privileges.
- Decision: reject for anything beyond manual human exploration.

### Option B — Embed cuOpt client directly in KAI daemon
Install cuOpt dependencies in the main KAI runtime and call GPU code locally/remotely.

- Pros: fewer moving parts in client code.
- Cons: couples KAI daemon to NVIDIA stack, incompatible with local non-GPU environments, makes failures/OOMs affect KAI, encourages broad host access.
- Decision: reject.

### Option C — Managed gpubox runner API with per-GPU workers
Run a small service on `gpubox` that accepts validated jobs and dispatches one containerized solve per GPU or persistent per-GPU workers.

- Pros: narrow security boundary, reproducible artifacts, clear queueing, supports both scheduled and on-demand jobs, can evolve from research to production.
- Cons: requires initial runner implementation and ops work.
- Decision: recommend.

## 3. Container strategy

Pinned base image:

```text
nvidia/cuopt:25.12.0a-cuda12.9-py3.13
```

Implementation guidance:
- Record the pulled image digest in Phase 0 after bootstrap; use tag + digest in production manifests.
- Use `nvidia-container-runtime` with explicit GPU exposure.
- Prefer a managed `docker compose` or `systemd` service on `gpubox` for the runner API and workers.
- Do not mount the Docker socket into KAI or into untrusted runner job contexts.
- Keep reference/example repo mounts read-only.
- Separate persistent state from ephemeral scratch:

```text
/srv/kai-cuopt/
  config/                 # runner config, root-owned, no secrets in git
  inputs/                 # optional uploaded/normalized input manifests
  jobs/<job_id>/           # persistent artifacts with retention policy
  cache/                   # ephemeral/rebuildable dependency or data cache
  tmp/<job_id>/            # per-job scratch, deleted/retained by policy
```

Recommended volumes:
- Read-only:
  - curated production/research input exports, e.g. strategy basket snapshots.
  - `/home/atc/git/cuopt-examples` only for reference during bootstrap/spikes, not as a mutable runtime dependency.
- Writable:
  - per-job artifact directory.
  - per-job scratch directory with quotas.
- Never mount:
  - production trading state with write permissions.
  - SSH agent sockets.
  - Docker socket.
  - broad home directories.

Persistent vs ephemeral:
- Persistent: input manifest, normalized problem JSON/Parquet/NPZ, solver log, result JSON, portfolio weights, metrics, summary markdown.
- Ephemeral: generated matrices, temporary CSVs, intermediate warm starts, debug notebooks, cache files.

Network:
- Bind runner API to localhost/VPN/private interface only.
- If cuOpt routing service requires host networking in Phase 3, isolate it to `gpubox` and expose only through the runner API, not directly to KAI.
- Prefer bridge networking with explicit ports for Phase 0-2 unless cuOpt service behavior forces host networking.

## 4. Dual-GPU and NVLink policy

Treat the RTX 3090s as two independent 24GB solve workers. NVLink improves peer-to-peer transfer for workloads designed to use it, but it does not automatically turn two 24GB consumer GPUs into one safe 48GB/combined-memory cuOpt solve target. RTX 3090 also has no MIG, so isolation is process-level only.

Policy:
- Run one active solve per GPU by default.
- Set `CUDA_VISIBLE_DEVICES=0` for `gpu0` worker and `CUDA_VISIBLE_DEVICES=1` for `gpu1` worker.
- The runner accepts `gpu: auto | 0 | 1`; `auto` picks the least busy healthy GPU.
- Do not oversubscribe a GPU until Phase 5 metrics prove headroom for a specific job class.
- Scenario sharding is allowed for embarrassingly parallel research sweeps, not for a single monolithic solve unless cuOpt mode explicitly supports distributed/multi-GPU solve semantics.
- For CVaR parameter sweeps, shard across GPUs by `(alpha, lambda_risk, universe, scenario_seed)` combinations.
- For large scenario sets, size each shard so peak memory stays below a configured threshold, initially 20GB per 24GB GPU to leave driver/framework headroom.
- Reserve manual override capability to pin routing/RL experiments to GPU 1 while portfolio jobs use GPU 0.

Example placement:

```text
Job A: CVaR alpha=0.95 lambda=2.0 -> GPU 0
Job B: CVaR alpha=0.99 lambda=4.0 -> GPU 1
Job C: waits until either GPU frees
```

## 5. Runner interface contract

The runner should expose a small API. HTTP is easiest to implement and debug; NATS can be added as a KAI-side bridge if desired. Required endpoints/tools:

- `POST /v1/jobs` — submit idempotent job.
- `GET /v1/jobs/{job_id}` — status and metadata.
- `GET /v1/jobs/{job_id}/result` — final result pointer and summary.
- `POST /v1/jobs/{job_id}/cancel` — best-effort cancel.
- `GET /healthz` — runner health.
- `GET /readyz` — runner + GPU + image readiness.

### Submit request

```json
{
  "job_id": "kai-10379-20260507-000001",
  "job_type": "portfolio_cvar",
  "mode": "lp_qp_python",
  "problem_ref": "inputs/strategy-basket/2026-05-07/manifest.json",
  "problem_hash": "sha256:...",
  "solver_settings": {
    "time_limit_seconds": 300,
    "log_to_console": true,
    "method": "default",
    "optimality_tolerance": null
  },
  "gpu_policy": {
    "gpu": "auto",
    "exclusive": true,
    "memory_limit_mb": 20480
  },
  "output_policy": {
    "artifact_ttl_days": 30,
    "include_solver_log": true,
    "include_normalized_problem": true
  },
  "metadata": {
    "task_id": 10379,
    "requested_by": "kai",
    "purpose": "research",
    "source_snapshot_id": "strategy-basket-2026-05-07"
  }
}
```

Idempotency rule: if `job_id` already exists with the same `problem_hash` and settings hash, return the existing job. If the `job_id` exists with different hashes/settings, return `409 conflict`.

### Portfolio CVaR input manifest

The Phase 1 spike should normalize strategy-basket data into a manifest like:

```json
{
  "schema_version": "kai.cuopt.portfolio_cvar.v1",
  "universe": ["strategy_a", "strategy_b"],
  "return_matrix_ref": "returns.parquet",
  "expected_return_ref": "mu.npy",
  "covariance_ref": "sigma.npy",
  "scenario_matrix_ref": "scenarios.npy",
  "scenario_probability_ref": "scenario_probs.npy",
  "constraints": {
    "budget_eq": 1.0,
    "long_only": true,
    "min_weight": 0.0,
    "max_weight": 0.25,
    "turnover_limit": null,
    "group_limits": []
  },
  "objective": {
    "alpha": 0.95,
    "lambda_risk": 2.0,
    "maximize_expected_return_minus_cvar": true
  },
  "data_window": {
    "start": "2025-01-01T00:00:00Z",
    "end": "2026-05-07T00:00:00Z",
    "bar_interval": "1d"
  }
}
```

### Result object

```json
{
  "job_id": "kai-10379-20260507-000001",
  "status": "succeeded",
  "solver_status": "Optimal",
  "objective_value": 0.012345,
  "solve_time_seconds": 18.42,
  "queue_wait_seconds": 2.1,
  "wall_time_seconds": 22.0,
  "gpu_id": 0,
  "image": "nvidia/cuopt:25.12.0a-cuda12.9-py3.13@sha256:...",
  "runner_version": "0.1.0",
  "input_hash": "sha256:...",
  "settings_hash": "sha256:...",
  "artifacts": {
    "root": "/srv/kai-cuopt/jobs/kai-10379-20260507-000001",
    "summary": "summary.md",
    "solution": "solution.json",
    "portfolio_weights": "portfolio_weights.csv",
    "solver_log": "solver.log",
    "metrics": "metrics.json",
    "gpu_samples": "gpu_samples.jsonl"
  },
  "metrics": {
    "num_assets": 100,
    "num_scenarios": 2500,
    "num_variables": 2601,
    "num_constraints": 2501,
    "num_nonzeros": 250000,
    "gpu_memory_peak_mb": 8420,
    "gpu_utilization_peak_pct": 91
  },
  "portfolio": {
    "expected_return": 0.014,
    "cvar": 0.023,
    "top_weights": [
      {"asset": "strategy_a", "weight": 0.12},
      {"asset": "strategy_b", "weight": 0.08}
    ]
  },
  "error": null
}
```

Statuses:
- `queued`
- `running`
- `succeeded`
- `failed`
- `timed_out`
- `cancelled`
- `infeasible`
- `unbounded`
- `oom`
- `lost`

## 6. KAI integration

Recommended first integration: KAI client tools plus an optional `optimizer` or `research-optimizer` agent role.

### KAI tools to add in implementation phases
- `cuopt_submit_job`
- `cuopt_get_job_status`
- `cuopt_get_result`
- `cuopt_cancel_job`
- `cuopt_health`

### Agent role
Add a dedicated optimizer/research agent rather than overloading trader:
- Formulates optimization requests.
- Validates data snapshot references and constraints.
- Submits jobs to the runner.
- Summarizes results and caveats.
- Never places trades directly from cuOpt outputs.

### Webhook vs scheduled job vs agent request
Recommended sequence:
1. Phase 1: manual taskboard-triggered or CLI-triggered KAI agent request.
2. Phase 2: scheduled nightly research job for fixed strategy baskets.
3. Phase 2/5: optional webhook callback from runner to KAI/NATS when jobs complete.

The runner should not need taskboard credentials. KAI owns taskboard comments and summaries after polling or receiving completion events.

## 7. Result flow

```text
1. KAI optimizer identifies a strategy basket snapshot.
2. KAI submits cuOpt job with snapshot ref + hash + constraints.
3. Runner validates schema and permissions.
4. Runner enqueues job and assigns GPU.
5. Worker executes cuOpt solve and records logs/metrics.
6. Runner writes artifacts and final result object.
7. KAI fetches result, produces human-readable summary, and links artifact root.
8. Optional: KAI comments on taskboard or updates dashboard.
```

Important separation: cuOpt outputs are research recommendations. Any downstream trade execution requires a separate risk-manager review and trading workflow.

## 8. Failure modes and mitigations

| Failure mode | Detection | Mitigation / runner behavior |
|---|---|---|
| Image tag unavailable or digest drift | Phase 0 smoke and recorded digest mismatch | Fail readiness; require operator to repin digest. |
| CUDA/driver mismatch | Container import/health test fails | Mark runner not ready; no jobs accepted except health diagnostics. |
| GPU OOM | Process exit, cuOpt error, NVML memory sample reaches threshold | Mark `oom`, preserve logs, recommend smaller scenario count/sharding; do not auto-retry same size on other GPU unless configured. |
| Driver reset/crash | NVML unavailable, worker deaths, kernel logs | Stop scheduling, mark running jobs `lost`, require operator health intervention. |
| Container restart mid-job | heartbeat missing, process exit | Mark job `lost` or `failed`; artifacts include last logs and checkpoint if available. |
| KAI timeout while solve continues | KAI tool times out but runner job still alive | KAI should later poll by `job_id`; runner stays source of truth. |
| Infeasible/unbounded model | cuOpt status | Return `infeasible`/`unbounded` with model metadata and constraints summary. |
| Numerical instability/bad scaling | solver warnings, abnormal objective/iterations | Return warning category; summary flags non-actionable result. |
| Malformed matrix dimensions | schema validation or formulation error | Reject before GPU if possible with `invalid_input`. |
| Concurrent jobs collide on same non-MIG GPU | queue policy violation | Default one active job per GPU; lock GPU allocation. |
| Artifact disk full | write failure / quota monitor | Fail gracefully; pause queue; expose `/readyz` degraded. |
| Production input write attempt | mount permissions | Read-only mounts; job fails if write attempted. |
| Token leak in logs | log scanner / review | Redact Authorization headers and configured secret patterns; never persist raw request headers. |

## 9. Observability

Minimum per-job metrics:
- queue wait seconds.
- solve seconds.
- total wall seconds.
- GPU id and `CUDA_VISIBLE_DEVICES`.
- GPU memory high-water mark.
- GPU utilization samples.
- optional temperature and power samples.
- cuOpt solver status.
- objective value.
- variable/constraint/nonzero counts.
- input hash and settings hash.
- image tag/digest and runner version.
- structured error category.

Artifacts:

```text
/srv/kai-cuopt/jobs/<job_id>/
  manifest.json             # request minus secrets
  normalized_problem.*       # optional; controlled by output policy
  solver.log                 # cuOpt/stdout/stderr redacted
  solution.json              # machine-readable final result
  portfolio_weights.csv      # human-inspectable weights for portfolio jobs
  metrics.json               # final metrics
  gpu_samples.jsonl          # periodic NVML samples
  summary.md                 # KAI/operator summary
```

Dashboard candidates for Phase 5:
- queue depth by job type.
- active GPU per job.
- memory high-water mark per job.
- solve time distribution.
- success/fail/oom rate.
- artifact disk usage and retention.
- estimated power/cost proxy if power samples are reliable.

## 10. Security

Principles:
- KAI gets a scoped runner token, not SSH or Docker privileges.
- Runner has read-only access to production/research snapshots unless explicitly writing its own artifacts.
- Runner API is private-network only.
- No bearer tokens, session tokens, HMAC secrets, Authorization headers, or raw signed webhook bodies in artifacts/logs.
- Job manifests must be declarative; no arbitrary Python/code execution from KAI.
- Validate all file references against allowed roots; prevent `../` traversal and symlink escape.
- Redact environment variables from logs.
- Use per-service credentials with rotation plan.
- Keep examples repo as reference; do not run mutable production jobs from a developer clone.

Recommended scoped permissions:

```text
KAI -> runner: submit/read/cancel own jobs, health
runner -> inputs: read-only curated snapshots
runner -> artifacts: write within /srv/kai-cuopt/jobs
runner -> taskboard: none
worker -> network: minimal; ideally no outbound except internal services required for declared inputs
```

## 11. Testing and rollout guardrails

Phase 0/1 tests:
- container import smoke: `from cuopt.linear_programming.problem import Problem`.
- single-GPU dummy LP solve on GPU 0.
- single-GPU dummy LP solve on GPU 1.
- concurrent two-job smoke, one per GPU, with `CUDA_VISIBLE_DEVICES` verified.
- OOM guard dry run using configured memory threshold, not an actual destructive OOM.
- artifact write/read verification.
- schema validation rejection for malformed manifests.

Phase 2 tests:
- API idempotency.
- cancel semantics.
- worker crash recovery.
- runner restart with queued/running job reconciliation.
- redaction tests.
- read-only mount enforcement.
- KAI tool contract tests.
- taskboard summary generated without exposing secrets.

Rollout guardrails:
- Start with research-only labels in all results.
- Require explicit risk-manager and trader workflow before any allocation change.
- Keep max scenario count and max runtime conservative until metrics show headroom.
- Do not schedule recurring jobs until manual Phase 1 outputs are trusted.

---

# Phased ticket plan, gates, and acceptance criteria

These are recommended implementation tickets only; this architecture task does not file them.

## Phase 0 — Bootstrap + dual-GPU smoke on gpubox

Recommended ticket title:
`CUOPT Phase 0: bootstrap pinned cuOpt container and dual-GPU smoke tests on gpubox`

Scope:
- Confirm image pull/run using `nvidia/cuopt:25.12.0a-cuda12.9-py3.13` and record digest.
- Create a read-only smoke script or notebook-derived script that imports `Problem` and solves a tiny LP.
- Run on GPU 0 with `CUDA_VISIBLE_DEVICES=0`.
- Run on GPU 1 with `CUDA_VISIBLE_DEVICES=1`.
- Run two concurrent tiny solves, one per GPU.
- Capture `nvidia-smi`/NVML memory high-water mark and basic timing.
- Document exact run commands and outputs; no production data.

Acceptance criteria:
- cuOpt import and tiny solve succeed on both GPUs.
- Concurrent per-GPU smoke does not cross-allocate or oversubscribe.
- Image digest, driver, CUDA, Docker, and runtime versions are recorded.
- Artifact/log directory convention is proposed and manually validated.
- No secrets or production writes involved.

Gate to Phase 1:
- Both GPUs can run isolated cuOpt solves reproducibly.
- Memory/timing sampling works well enough for spike metrics.
- No unresolved driver/container mismatch.

Decision if gate fails:
- Fix NVIDIA runtime/image compatibility before any portfolio spike.

ETA: 2 dev days + 1 code review day + 1 security audit day + 1 QA day = ~5 business days serial, no fix loops.

## Phase 1 — CVaR portfolio spike against strategy basket

Recommended ticket title:
`CUOPT Phase 1: CVaR portfolio optimization spike for KAI strategy basket`

Scope:
- Convert the CVaR notebook formulation into a deterministic research script/module.
- Ingest a curated strategy basket return matrix snapshot.
- Generate historical and bounded Monte Carlo scenarios.
- Solve long-only budget-constrained CVaR using `Problem` API.
- Sweep a small grid of `alpha` and `lambda_risk` across the two GPUs.
- Emit standardized artifacts: manifest, solution, weights CSV, solver log, metrics, summary.
- Compare against a baseline such as equal weight, inverse volatility, or existing allocation.

Acceptance criteria:
- Produces stable portfolio weights and CVaR/expected-return metrics for a real strategy basket snapshot.
- Demonstrates at least one useful comparison against baseline.
- Captures solve times and GPU memory high-water marks.
- Handles infeasible/malformed input cleanly.
- Research summary is understandable to a KAI analyst/risk-manager.

Gate to Phase 2:
- Spike shows material research value: e.g. better tail-risk-adjusted objective than baseline, plausible allocations, no obvious numerical/pathological behavior.
- Solve time and memory are inside operational bounds, initially <5 minutes and <20GB per GPU for the target basket.
- Data pipeline can produce repeatable hashed input snapshots.

Decision if gate fails:
- If formulation is sound but data is weak, improve strategy-basket features before runner productionization.
- If solve is too slow/OOM, reduce scenarios, shard sweeps, or reconsider problem size.
- If outputs are not actionable, stop before Phase 2.

ETA: 4 dev days + 1 CR + 1 SA + 2 QA/research validation = ~8 business days serial.

## Phase 2 — Production runner, conditional on Phase 1 shipping

Recommended ticket title:
`CUOPT Phase 2: managed gpubox runner API and KAI cuOpt client tools`

Scope:
- Implement runner API with submit/status/result/cancel/health.
- Implement job schema validation, idempotency, queueing, per-GPU locks, and artifact layout.
- Add KAI client tools for job submit/status/result/cancel/health.
- Add scoped token auth and redaction.
- Add runner restart reconciliation for queued/running/lost jobs.
- Add scheduled nightly research job support only after manual validation.

Acceptance criteria:
- KAI can submit a declared CVaR job without SSH/Docker access.
- Runner executes one job per GPU, queues excess jobs, and returns structured results.
- Restart or worker death leaves jobs in a clear terminal or recoverable state.
- Security audit verifies scoped tokens, read-only inputs, redacted logs, and no arbitrary code execution.
- QA verifies idempotency, failure statuses, and artifact integrity.

Gate to Phase 3/4/5:
- Runner is reliable for repeated CVaR research jobs.
- Observability is sufficient to diagnose OOM/timeouts.
- No security blockers around remote job submission.

Decision if gate fails:
- Keep Phase 1 as manual research scripts; do not expose scheduled or API-driven runner.

ETA: 6 dev days + 2 CR + 2 SA + 3 QA = ~13 business days serial.

## Phase 3 — Multi-leg arbitrage routing spike, CVRP variant

Recommended ticket title:
`CUOPT Phase 3: multi-leg arbitrage routing spike using CVRP/VRP formulation`

Scope:
- Model a small multi-leg arbitrage routing problem as a CVRP/VRP-like graph.
- Evaluate cuOpt routing service pattern separately from portfolio `Problem` API.
- Use EARLI/AI-initialized routing only as a reference pattern, not a production dependency.
- Define nodes, edges, capacities, time/latency/slippage constraints, and objective.
- Produce artifacts comparable to portfolio jobs: route solution, objective, constraints, logs, metrics.

Acceptance criteria:
- Demonstrates a valid formulation for at least one realistic small arbitrage-route scenario.
- Produces routes that respect capacity/latency/slippage constraints in the model.
- Establishes whether cuOpt routing adds value over simpler graph search for this use case.
- Does not compromise Phase 2 runner stability.

Gate to further routing work:
- cuOpt route quality or solve latency beats a simple baseline enough to justify complexity.
- Problem mapping is natural and maintainable, not a forced VRP analogy.

Decision if gate fails:
- Keep arbitrage routing in simpler graph/search engines and reserve cuOpt for portfolio/MIP work.

ETA: 5 dev/research days + 1 CR + 1 SA + 2 QA = ~9 business days serial.

## Phase 4 — Parameter sweep MIP, folds into #10020

Recommended ticket title:
`CUOPT Phase 4: parameter sweep MIP integration for #10020`

Scope:
- Identify #10020 parameter-sweep decision variables and constraints.
- Formulate sweep selection/resource allocation as LP/MIP/QP as appropriate.
- Shard independent sweep candidates across GPU workers where possible.
- Integrate with the runner contract from Phase 2.
- Emit reproducible sweep manifests and result rankings.

Acceptance criteria:
- #10020 has a concrete cuOpt-backed formulation or a documented decision not to use cuOpt.
- Runner can execute representative sweeps with bounded memory/time.
- Results are reproducible from snapshot hashes and settings hashes.

Gate:
- cuOpt formulation improves throughput, objective quality, or operational simplicity over existing sweep approach.
- Memory/time profile fits scheduled research windows.

Decision if gate fails:
- Fold learnings into #10020 but do not keep cuOpt in that path.

ETA: 4 dev days + 1 CR + 1 SA + 2 QA = ~8 business days serial.

## Phase 5 — Ops/observability/cost dashboard

Recommended ticket title:
`CUOPT Phase 5: runner observability, GPU utilization, artifact retention, and cost dashboard`

Scope:
- Add dashboard/API views for queue depth, active jobs, GPU memory HWM, solve time, failure rate, disk usage, and retention.
- Add alerts for GPU OOM spikes, driver health degradation, disk pressure, stale jobs, and repeated infeasible/invalid inputs.
- Add artifact retention policy and cleanup job.
- Add power/cost proxy if NVML power sampling is reliable on the hardware.
- Add runbook for restart, stuck job cleanup, and image upgrades.

Acceptance criteria:
- Operators can answer: what is running, on which GPU, for how long, using how much memory, and where are artifacts?
- Alerts catch common failure modes before silent backlog accumulation.
- Retention prevents unbounded disk growth.
- Runbook covers driver/container/runner restart and safe queue draining.

Gate:
- Phase 2 runner is used frequently enough that operational visibility matters.
- Metrics collected in Phase 2 are stable and trusted.

ETA: 4 dev days + 1 CR + 1 SA + 2 QA = ~8 business days serial.

---

# Overall ETA

Assuming serial developer / code review / security audit / QA cycles and no fix loops:

| Phase | Estimated duration |
|---|---:|
| Phase 0 bootstrap/smoke | ~5 business days |
| Phase 1 CVaR spike | ~8 business days |
| Phase 2 production runner | ~13 business days |
| Phase 3 arb routing spike | ~9 business days |
| Phase 4 #10020 parameter sweep MIP | ~8 business days |
| Phase 5 ops dashboard | ~8 business days |
| Total all phases | ~51 business days (~10 weeks) |

Critical path to useful research result: Phase 0 + Phase 1 = ~13 business days.

Critical path to productionized KAI-submitted cuOpt runner: Phase 0 + Phase 1 + Phase 2 = ~26 business days (~5 weeks).

The schedule is intentionally realistic for serial lifecycle gates. Parallelizing Phase 3 research with Phase 5 dashboard work could shorten calendar time later, but should not start until Phase 2 contracts stabilize.

---

# Key risks

1. **Memory pressure from CVaR scenarios**: CVaR LP size scales with scenario count and asset count. Mitigate with scenario caps, sharded parameter sweeps, and memory HWM gates.
2. **Misinterpreting NVLink**: do not assume 48GB unified solve memory. Treat GPUs independently unless cuOpt explicitly supports the target multi-GPU mode.
3. **Notebook-to-runner drift**: notebook state can hide setup. Convert formulation into deterministic scripts/modules with explicit manifests.
4. **Security creep**: KAI must not gain SSH/Docker/arbitrary-code access to `gpubox`. Keep a declarative runner contract.
5. **Research outputs mistaken for trade instructions**: enforce risk-manager/trader workflow separation.
6. **Routing complexity**: CVRP analogy for arbitrage may not outperform simpler graph algorithms. Phase 3 gate should be strict.
7. **Operational blind spots**: GPU jobs fail in ways that normal web services do not; metrics and artifact capture are required before recurrence.

# Final recommendation

Proceed with Phase 0 and Phase 1 only as the immediate implementation sequence. Treat Phase 2 as conditional on Phase 1 demonstrating useful, stable CVaR research outputs. Keep routing/EARLI and MIP sweeps as later spikes behind the same runner contract, not as requirements for the first production runner.

The most important architectural decision is the boundary: KAI submits declarative optimization jobs to a scoped `gpubox` runner and consumes structured artifacts; it does not operate `gpubox` directly. This preserves safety while leaving a clean path to scheduled GPU research, portfolio optimization, routing experiments, and future dashboards.
