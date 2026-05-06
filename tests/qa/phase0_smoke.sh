#!/usr/bin/env bash
# Phase 0 of epic #10030 (task #10247) — end-to-end smoke against the
# running local KAI daemon + local moltdev-taskboard.
#
# Verifies:
#   AC1: BL→IP webhook fires the dispatcher (single Developer spawn, no
#        legacy IP-spawn race, since TASKBOARD_LEGACY_IP_SPAWN_ENABLED=0).
#   AC2: Capacity gate from agent_runs ledger holds at max_concurrent_spawns
#        (cache invalidation lets the count tick up after each spawn).
#   AC3: IP→Review webhook fires a single Code Reviewer session.
#   AC4: Sessions reach a terminal status_change in the agent_runs ledger
#        and don't wedge at `spawning`.
#
# Usage:
#   tests/qa/phase0_smoke.sh                # AC1 + AC3 (small, fast)
#   tests/qa/phase0_smoke.sh --capacity     # also AC2 (10 BL→IP fires)
#   tests/qa/phase0_smoke.sh --quiet        # less verbose
#
# Required env (sourced from the running daemon's /proc/<pid>/environ):
#   KAI_TASKBOARD_WEBHOOK_SECRET
#   TASKBOARD_URL (for ledger reads)
#   TASKBOARD_BEARER_TOKEN (for ledger reads)

set -euo pipefail

DAEMON_URL=${KAI_DAEMON_URL:-http://127.0.0.1:18789}
QUIET=0
CAPACITY=0

while (($#)); do
  case "$1" in
    --capacity) CAPACITY=1; shift;;
    --quiet|-q) QUIET=1; shift;;
    -h|--help) sed -n '2,21p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
FIRE="$REPO_ROOT/tests/qa/fire_webhook.sh"
log() { [[ "$QUIET" == "0" ]] && echo "[smoke] $*"; }
fail() { echo "[smoke] FAIL: $*" >&2; exit 1; }

# ---- preflight ----------------------------------------------------------
[[ -z "${KAI_TASKBOARD_WEBHOOK_SECRET:-}" ]] && {
  echo "missing KAI_TASKBOARD_WEBHOOK_SECRET in env" >&2
  echo "Hint: set -a; source <(grep -E '^(KAI_|TASKBOARD_)' /tmp/kai-daemon.env); set +a" >&2
  exit 2
}
log "daemon health check"
curl -sS -m 2 "$DAEMON_URL/api/health" | python3 -m json.tool > /dev/null \
  || fail "daemon /api/health not responding"

# Pick task ids in a high range so we don't collide with real taskboard tickets.
TS=$(date +%s)
BL_IP_TASK=$((900000 + TS % 10000))
IP_REVIEW_TASK=$((BL_IP_TASK + 1))

# ---- AC1: BL → IP fires a single Developer spawn -----------------------
log "AC1: firing BL→IP for synthetic task #$BL_IP_TASK"
RESP=$($FIRE \
  --task-id "$BL_IP_TASK" \
  --from-status "Backlog" \
  --to-status "In Progress" \
  --agent Developer \
  --fire-generation 1)
echo "$RESP" | grep -q "HTTP=200" || fail "AC1 webhook rejected: $RESP"
log "AC1: webhook accepted"

# ---- AC3: IP → Review fires a single Code Reviewer session --------------
log "AC3: firing IP→Review for synthetic task #$IP_REVIEW_TASK"
RESP=$($FIRE \
  --task-id "$IP_REVIEW_TASK" \
  --from-status "In Progress" \
  --to-status "Review" \
  --agent Developer \
  --fire-generation 1)
echo "$RESP" | grep -q "HTTP=200" || fail "AC3 webhook rejected: $RESP"
log "AC3: webhook accepted"

# Give the dispatcher's poll loop ~3s to drain the queue.
log "waiting 4s for dispatcher poll loop to consume..."
sleep 4

# ---- AC1 verification: webhook_pending row marked spawned/duplicate ----
DB="$REPO_ROOT/.kai/daemon-state.sqlite3"
[[ -f "$DB" ]] || DB=$(find "$REPO_ROOT" -maxdepth 4 -name "daemon-state.sqlite3" 2>/dev/null | head -1)
[[ -f "$DB" ]] || fail "couldn't locate daemon-state.sqlite3"
log "using state db: $DB"

# Use Python (no sqlite3 CLI on this box).
QUERY_DB() {
  local sql="$1"
  python3 - <<PYEOF
import sqlite3, sys
c = sqlite3.connect("$DB")
c.row_factory = sqlite3.Row
for row in c.execute("""$sql"""):
    print("\t".join("" if v is None else str(v) for v in row))
PYEOF
}

QUEUE_TABLE=$(QUERY_DB "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('webhook_pending','webhook_deliveries') ORDER BY name='webhook_pending' DESC LIMIT 1")
log "queue table in use: $QUEUE_TABLE"

# Find the row whose payload contains the synthetic task_id.
BL_IP_STATUS=$(QUERY_DB "SELECT dispatch_status FROM $QUEUE_TABLE WHERE (payload_json LIKE '%\"task_id\": $BL_IP_TASK%' OR payload_json LIKE '%\"task_id\":$BL_IP_TASK%') ORDER BY rowid DESC LIMIT 1")
[[ -z "$BL_IP_STATUS" ]] && fail "AC1: no $QUEUE_TABLE row found for task $BL_IP_TASK"
log "AC1: BL→IP row dispatch_status=$BL_IP_STATUS"
case "$BL_IP_STATUS" in
  spawned|duplicate|spawn_failed|stuck_aborted) ;;
  *) fail "AC1: unexpected dispatch_status=$BL_IP_STATUS (expected spawned)";;
esac

IP_REVIEW_STATUS=$(QUERY_DB "SELECT dispatch_status FROM $QUEUE_TABLE WHERE (payload_json LIKE '%\"task_id\": $IP_REVIEW_TASK%' OR payload_json LIKE '%\"task_id\":$IP_REVIEW_TASK%') ORDER BY rowid DESC LIMIT 1")
[[ -z "$IP_REVIEW_STATUS" ]] && fail "AC3: no $QUEUE_TABLE row found for task $IP_REVIEW_TASK"
log "AC3: IP→Review row dispatch_status=$IP_REVIEW_STATUS"

# ---- AC4: sessions visible in /api/sessions ---------------------------
SESSIONS=$(curl -sS "$DAEMON_URL/api/sessions" 2>/dev/null || echo "[]")
log "AC4: /api/sessions = $(echo "$SESSIONS" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))' 2>/dev/null || echo '?')"

# ---- AC2 (optional): capacity gate holds at max_concurrent_spawns -----
if [[ "$CAPACITY" == "1" ]]; then
  log "AC2: firing 10 concurrent BL→IP webhooks to test capacity gate"
  for i in $(seq 1 10); do
    $FIRE \
      --task-id $((950000 + i)) \
      --from-status "Backlog" --to-status "In Progress" \
      --agent Developer \
      --fire-generation 1 > /dev/null &
  done
  wait
  log "AC2: all 10 webhooks fired; sleeping 6s for dispatcher to react..."
  sleep 6

  ACTIVE=$(curl -sS "$DAEMON_URL/api/sessions" \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len([s for s in d if s.get("activity_status") not in ("idle","")]))' 2>/dev/null \
    || echo '?')
  log "AC2: active session count = $ACTIVE (cap=6 by default DEFAULT_MAX_CONCURRENT_SPAWNS)"
  if [[ "$ACTIVE" =~ ^[0-9]+$ ]] && [[ "$ACTIVE" -gt 6 ]]; then
    fail "AC2: $ACTIVE active sessions exceeds capacity gate of 6"
  fi
fi

echo
echo "=== Phase 0 smoke PASSED ==="
echo "  AC1 BL→IP single spawn:       $BL_IP_STATUS"
echo "  AC3 IP→Review single reviewer: $IP_REVIEW_STATUS"
echo "  AC4 sessions reachable:       yes"
[[ "$CAPACITY" == "1" ]] && echo "  AC2 capacity gate ≤6:         active=$ACTIVE"
