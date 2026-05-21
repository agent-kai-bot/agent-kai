#!/usr/bin/env bash
# Restart helper for the local KAI daemon (atc-home box).
# Captures the env we observed on the running pid 2602914 launch and
# brings the daemon back up under nohup so it survives shell logout.
#
# Usage:
#   scripts/start-kai-daemon.sh                 # start (refuses if already running)
#   scripts/start-kai-daemon.sh --force         # kill any existing daemon first
#
# Logs to /tmp/kai-daemon.log; writes pid to /tmp/kai-daemon.pid.

set -euo pipefail

REPO=${KAI_DAEMON_REPO:-/home/atc/git/claude-local-ai-agent}
LOG=${KAI_DAEMON_LOG:-/tmp/kai-daemon.log}
PIDFILE=${KAI_DAEMON_PIDFILE:-/tmp/kai-daemon.pid}
PORT=${KAI_DAEMON_PORT:-18789}

cd "$REPO"

force=0
[[ "${1:-}" == "--force" ]] && force=1

existing=$(pgrep -f "main.py --daemon --host 0.0.0.0 --port $PORT" || true)
if [[ -n "$existing" && "$force" != "1" ]]; then
  echo "Daemon already running: $existing (use --force to replace)" >&2
  exit 1
fi

# Required env (caller must export these before invocation).
required=(
  TASKBOARD_URL
  TASKBOARD_BEARER_TOKEN
  KAI_TASKBOARD_WEBHOOK_SECRET
  TASKBOARD_AGENT_TOKEN_DEVELOPER
  TASKBOARD_AGENT_TOKEN_CODE_REVIEWER
  TASKBOARD_AGENT_TOKEN_SECURITY_AUDITOR
  TASKBOARD_AGENT_TOKEN_QA_AGENT
  TASKBOARD_AGENT_TOKEN_ARCHITECT
  TASKBOARD_AGENT_TOKEN_ORCHESTRATOR
  KAI_TRUSTED_AUTONOMOUS
)
missing=()
for k in "${required[@]}"; do
  [[ -z "${!k:-}" ]] && missing+=("$k")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "Missing required env: ${missing[*]}" >&2
  echo "Hint: source the env from the prior daemon, e.g." >&2
  echo "  set -a; source <(grep -E '^(KAI_|TASKBOARD_|FORGEJO_|AGENTKAI_)' /tmp/kai-daemon.env); set +a" >&2
  exit 2
fi

if [[ -n "$existing" ]]; then
  echo "Stopping existing daemon pid(s): $existing" >&2
  kill $existing || true
  for i in $(seq 1 20); do
    sleep 0.5
    pgrep -f "main.py --daemon --host 0.0.0.0 --port $PORT" >/dev/null || break
  done
  if pgrep -f "main.py --daemon --host 0.0.0.0 --port $PORT" >/dev/null; then
    echo "Daemon still alive after SIGTERM, sending SIGKILL" >&2
    pkill -9 -f "main.py --daemon --host 0.0.0.0 --port $PORT" || true
    sleep 1
  fi
fi

echo "Starting daemon from $REPO at $(date -Is)" | tee -a "$LOG"
nohup .venv/bin/python main.py --daemon --host 0.0.0.0 --port "$PORT" \
  >>"$LOG" 2>&1 &
new_pid=$!
disown
echo "$new_pid" > "$PIDFILE"
echo "Started pid $new_pid (log: $LOG, pidfile: $PIDFILE)"

# Probe /health once it's up.
for i in $(seq 1 30); do
  if curl -sS -m 1 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "Daemon healthy on :$PORT after ${i}s"
    curl -sS "http://127.0.0.1:$PORT/health" | head -c 800; echo
    exit 0
  fi
  sleep 1
done
echo "Daemon did not become healthy in 30s — check $LOG" >&2
exit 3
