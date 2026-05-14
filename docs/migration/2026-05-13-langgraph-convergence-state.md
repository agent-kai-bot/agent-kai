# Migration State Snapshot — LangGraph Convergence + Auto-SDLC Closure
**Date**: 2026-05-13 → 2026-05-14 session
**Author**: agent-orchestrator (Claude Opus 4.7)
**Purpose**: Capture **everything required for the migration** before switching gears. No work or context lost.

---

## TL;DR — Where we are

1. **Auto-sdlc loop closed end-to-end tonight** — #10413 cycle 11 hit `Done` autonomously (CR→SA→QA all canonical, status auto-promoted). The pipeline works.
2. **Big arch pivot identified**: current agent execution uses **4+ parallel paths** (legacy `AgentExecutor`, `sub_agents`, subprocess `codex-cli`/`claude-cli` clients, SDLC `codex-spawn.sh` dispatcher). Operator (Dan) wants **convergence to LangGraph**.
3. **Two arch specs landed tonight** to enable the convergence: **#10428 LangGraph convergence** (899 lines) and **#10429 Claude Agent SDK + OAuth** (700 lines).
4. **3 code commits await next daemon restart**: scheduler overrides (#10427), reasoning-effort config flip, QA stuck-aborted hygiene.
5. **6 operator questions across the two specs** block Phase 1 implementation start — listed below.

---

## 1. Daemon integration branch commits (this session)

Branch: `integration/qa-2026-05-10-step1-codex-oauth` in `claude-local-ai-agent`.

| Commit | Layer | Status | Notes |
|---|---|---|---|
| `de51bb9` | docs | live (cherry-picked) | LangGraph convergence spec (#10428) — 899 lines |
| `3f190b9` | docs | live (cherry-picked) | Claude Agent SDK + OAuth spec (#10429) — 700 lines |
| `4139021` | docs | live (cherry-picked) | Commit #10405 scheduled-jobs spec (was untracked) |
| `f934594` | code | **awaits restart** | #10427 per-job target_agent_role + reasoning + thinking + extra_env overrides (1039 +, 11 -) |
| `3dea1cf` | config | **awaits restart** | reasoning_effort medium → xhigh for codex-cli models + auto_loop_brain |
| `1fdbde8` | code | **awaits restart** | QA stuck-aborted terminal cleanup (local sessions row marked terminal in `finally`) |
| `075b59d` | docs | live | #10424 UI redesign spec — 936 lines |
| `3ced38d` | code | LIVE in production daemon | #10423 hot-config phase 2 (dispatcher per-role resolve + attach-order fix + subprocess env overlay) |
| `40c946d` | code | LIVE in production daemon | #10422 review verdict webhook routing |
| `e790d36` | code | LIVE | #10421 gate bearer for reviewer sessions |
| `cbb529f` | code | LIVE | #10420 verdict tool URL + body shape |
| `f87ec2d` | code | LIVE | #10419 prompt-renderer role normalization |
| `e80ec59` | code | LIVE | #10416 phase 1 — runtime config resolver |
| `502f36a` | code | LIVE | #10418 reaper attribution fix |
| `c5d8bd1` | code | LIVE | #10417 submit_review_verdict tool |

**Restart needed to land**: `f934594`, `3dea1cf`, `1fdbde8` (3 commits, single restart, ~12s downtime, no breaking changes).

---

## 2. Taskboard repo commits (this session)

Repo: `openclawdev-taskboard`, branch `feat/spec-v23-move-statuses`.

| Commit | Status | Notes |
|---|---|---|
| `c0d153f` | LIVE | Role-name alias map for review gate authorization (qa, qa-agent, agent-qa, qa_agent all accepted) |
| `f038d3a` | LIVE | Enterprise review gate aggregator: canonicalize legacy comment/status approvals; clears active_session_id on final QA; aggregator scans status-only rows |
| `c91b90c` | LIVE | Emit `review.verdict_submitted` webhook events |

**Plus runtime DB write done tonight**: added 6 `role_assignments` rows linking `qa-agent` agent_id to `role_definition_id=8` (qa role) for all 6 projects, mirroring CR/SA pattern. **Not in version control** — operator-side data fix.

---

## 3. Tickets filed this session

| # | Title | Status | Type |
|---|---|---|---|
| #10422 | CR verdict APPROVED → SA must auto-spawn (verdict-event webhook missing) | Backlog (done) | bug |
| #10423 | Hot-config phase 2: dispatcher per-role resolve + attach-order fix | Backlog (done) | feat |
| #10424 | ARCH-SPEC: KAI web UI redesign — chat-first, 1920×1024 + mobile, view toggles, agent tool-use visibility | Backlog | spec |
| #10425 | FEAT: KAI dashboard view toggle + chat-focus layout (#10424 phase 1) | Backlog | feat |
| #10426 | BUG: #10413 cycle 9 — QA reviewer_user wrong + status not promoted | Backlog (resolved) | bug |
| #10427 | FEAT: scheduled jobs target_agent_role + reasoning + thinking_level overrides (#10405 impl) | Backlog (done) | feat |
| **#10428** | **ARCH-SPEC: converge all agent execution to LangGraph** | **Backlog** | **spec — KEYSTONE** |
| **#10429** | **ARCH-SPEC: Claude transport via Anthropic Agent SDK + OAuth subscription** | **Backlog** | **spec — KEYSTONE** |

**Marked "done" inline**: implementation exists but ticket status not yet flipped to `Done`. The taskboard aggregator should pick this up next time it runs against these.

---

## 4. Architecture specs committed (this session)

All in `docs/architecture/` on integration branch:

| File | Lines | Topic |
|---|---|---|
| `10424-ui-redesign-spec.md` | 936 | Chat-first dashboard, 3 view modes, tool-use stream, mobile, 5 phases |
| `10428-langgraph-convergence-spec.md` | 899 | StateGraph unification of all agent paths, 6 phases, TypedDict state, daemon stays outside graph |
| `10429-claude-agent-sdk-oauth-spec.md` | 700 | `claude-agent-sdk` 0.1.81 + OAuth subscription auth, Vault `claude/oauth-token`, 5 phases |
| `10405-scheduled-jobs-role-thinking.md` | 908 | Per-fire `target_agent_role` + reasoning override, sidecar persistence |
| `10416-runtime-config-resolver.md` (earlier) | — | Hot-config Vault resolver |

---

## 5. Open operator questions (BLOCK Phase 1 starts)

### From #10428 LangGraph (codex Appendix B)

- **CRITICAL**: LangChain version conflict — `langgraph 1.2.0` requires `langchain-core>=1.4.0,<2` but repo pins `<1.0`. Pick: **(a) upgrade family** to 1.4+ as coordinated PR, OR **(b) use older LangGraph (~0.x)** compatible with langchain<1.0.
- Which sessions/roles/agents are acceptable canaries for Phase 1?
- How long should legacy `AgentExecutor` rollback remain after Phase 2?
- Does any deployment still own/call `bin/codex-spawn.sh`?
- Should `codex_exec` / `claude_exec` remain explicit tools after graph model nodes exist?
- Should #10424 richer tool envelopes land before Phase 2 cutover?
- Checkpoint retention + redaction policy for SDLC graph runs?

### From #10429 Claude SDK (codex final report)

- **BLOCKING Phase 1**: Which subscription tier should Phase 1 assume for Opus 4.7 access?
- **BLOCKING Phase 1**: Which Vault write mechanism for OAuth token bootstrap? (CLI helper, existing resolver pattern, or admin endpoint?)
- Operator-owned internal only, or multi-user via Claude.ai through KAI?
- Opus 4.7 hidden when unavailable, or shown with warning?
- SDK-backed `claude_exec` use Claude built-in file/bash tools or only wrapped KAI tools?
- Block recursive Claude escalation globally?
- Rotation interval enforcement for `claude setup-token` tokens?
- API-key fallback permanent or bring-up only?
- When flip `agents.yaml` logical `claude` from `claude-cli` to `claude-sdk`?
- For #10428, chat start with SDK-managed or graph-managed tool loops?

### From #10424 UI redesign (answered tonight, captured here for #10425)

- ✅ First-visit desktop default = `chat-focus` for ALL desktop users
- ✅ Watchlist + Positions = compact sections in rail (alerts → NATS → signals → scheduler → watchlist → positions)
- ✅ Tool-use history persistence across refresh = YES (Phase 3 scope)
- ✅ Mobile chart = full-screen

---

## 6. Sequencing recommendation (for the LangGraph migration)

Once operator answers the LangChain-version blocker:

### Wave 1 — unblock foundations (parallelizable)
- **#10428 Phase 1**: LangGraph dependency + canary StateGraph (gated by `KAI_AGENT_GRAPH_BACKEND=langgraph-canary`)
- **#10429 Phase 1**: `claude-agent-sdk` install + Vault OAuth bootstrap CLI + smoke standalone
- **Daemon restart** lands the 3 staged commits (scheduler overrides, config flip, stuck-aborted) — separate from these phases

### Wave 2 — interim bridges (sequential after Wave 1)
- **#10428 Phase 2**: Migrate main `AgentRunner` to LangGraph
- **#10429 Phase 2**: `ChatClaudeSDK` LangChain wrapper + agent-config endpoint
- **#10425**: UI redesign Phase 1 — view toggle + chat-focus layout (operator answers already locked)

### Wave 3 — integration
- **#10428 Phase 3**: `sub_agents` → LangGraph sub-graph
- **#10429 Phase 3**: Migrate auto_loop_brain Claude CLI path to SDK
- **#10424 Phase 2-3**: Extract OpsRail + AgentActivityStream (consumes richer tool-use envelopes)

### Wave 4 — SDLC unification
- **#10428 Phase 5**: SDLC dispatcher (taskboard_dispatcher + codex-spawn.sh + verdict router) → LangGraph sub-graph
- **#10428 Phase 4**: auto_loop_brain critic as graph node
- **#10429 Phase 4**: `claude_exec` tool → SDK-backed

### Wave 5 — cleanup
- **#10428 Phase 6**: Dead-code sweep
- **#10429 Phase 5**: Subprocess Claude CLI scaffolding removal
- **#10424 Phase 5**: UI dead CSS sweep + chart-mode polish

---

## 7. Tonight's auto-sdlc residual issues (NOT blocking the migration)

These were discovered tonight but are NOT blockers for the LangGraph migration. They're tracked here so they don't get lost:

1. **`verdict=None` column on approved review rows** — cosmetic; aggregator's "fill missing verdict" branch doesn't fire on per-approve writes, only at promote-time. Status field is the source of truth and works correctly. Future: tighten the aggregator OR populate verdict at endpoint level.
2. **Pre-existing Claude scaffolding** that's unused: `agent/auto_loop_brain.py:127 ClaudeCLIClient`, `agent/tool_policy.py:77 claude_exec`, `agent/prompts.py:44,78,98,102` mentions. To be migrated/replaced by #10429 — not deleted prematurely.
3. **Reaper `reaped_runs` schema gap** — fixed in #1fdbde8 via `CREATE TABLE IF NOT EXISTS` (additive). Awaits restart to apply.
4. **`bin/codex-spawn.sh` legacy** — still drives all SDLC dev/CR/SA/QA sessions. To be replaced by #10428 Phase 5 (SDLC sub-graph).
5. **`docs/architecture/10405-scheduled-jobs-role-thinking.md`** was untracked until commit `4139021` tonight. Audit for other untracked specs.

---

## 8. Memory anchors for future sessions

If continuing this work in a new session, the load-bearing context is:

- **Daemon HEAD**: `de51bb9` on `integration/qa-2026-05-10-step1-codex-oauth` — 3 code/config commits ahead of last-restart point
- **Production daemon PID**: see `/tmp/kai-daemon.pid`; HEAD at restart was `3ced38d` (#10423 hot-config phase 2)
- **Taskboard HEAD**: `c0d153f` on `feat/spec-v23-move-statuses` — fully deployed (container restarted tonight)
- **Operator subscription**: ChatGPT (used via codex-cli OAuth → `chatgpt.com/backend-api/codex`). Claude.ai subscription exists (this is what #10429 will use).
- **Operator constraint tonight**: "no more daemon restarts" — was last verbal at ~21:30 ET 2026-05-13. Stale by morning.
- **3 weeks behind schedule** — operator's framing. Migration urgency is high.
- **Loop closure proven**: #10413 cycle 11 hit Done autonomously at T+14min via `Backlog → In Progress → Review → CR APPROVE → (verdict webhook) → SA APPROVE → (verdict webhook) → QA APPROVE → aggregator → Done`.

---

## 9. Files to read first in a fresh session

1. This file — `docs/migration/2026-05-13-langgraph-convergence-state.md`
2. `docs/architecture/10428-langgraph-convergence-spec.md` — what we're building toward
3. `docs/architecture/10429-claude-agent-sdk-oauth-spec.md` — Claude transport plan
4. `/tmp/codex_xhigh_10428_FINAL.md`, `/tmp/codex_xhigh_10429_FINAL.md` — codex's own summaries
5. Tonight's git log on `integration/qa-2026-05-10-step1-codex-oauth`

---

## 10. What "switching gears" should NOT lose

| Asset | Where it lives |
|---|---|
| 9 architecture specs from this and prior sessions | `docs/architecture/*.md` on integration branch |
| 8 tickets filed this session | taskboard #10422-#10429 |
| Auto-sdlc closure proof | #10413 cycle 11 Done state in taskboard + watcher output `/tmp/.../b3i2fdd0l.output` |
| Codex final reports | `/tmp/codex_xhigh_*_FINAL.md` (10421 through 10429) |
| Operator answers on #10424 | Inline in #10425 ticket description |
| QA role_assignments data fix | Live taskboard DB (6 rows for projects 1, 3, 4, 5, 6, 7) — should be added to a startup-bootstrap migration so future taskboard rebuilds keep them |
| Hot-config Vault resolver (LIVE) | `agent/runtime_config_resolver.py` |
| 3 commits awaiting next daemon restart | `1fdbde8`, `3dea1cf`, `f934594` on integration branch |

---

## 11. Single next action

When operator returns:

1. Answer the LangChain version blocker question (#10428 — option a or b)
2. Answer the Vault OAuth bootstrap mechanism question (#10429 — CLI helper or resolver path)
3. Approve daemon restart to land the 3 staged commits
4. Decide whether to fire #10428 Phase 1 first, or #10429 Phase 1 first, or both in parallel (they're independent at Phase 1)

After those four decisions, the migration starts.
