#!/usr/bin/env bash
# Fire a single taskboard webhook at the local KAI daemon.
#
# Usage:
#   tests/qa/fire_webhook.sh \
#     --task-id 9001 \
#     --from-status Backlog --to-status "In Progress" \
#     [--agent Developer] [--fire-generation 1] \
#     [--event task.status_changed]
#
# Required env (read from the running daemon's environ if missing):
#   KAI_DAEMON_URL                 (default http://127.0.0.1:18789)
#   KAI_TASKBOARD_WEBHOOK_SECRET   (required)
#
# Prints the daemon's JSON response and exits 0 on HTTP 200/201/202,
# non-zero otherwise.

set -euo pipefail

DAEMON_URL=${KAI_DAEMON_URL:-http://127.0.0.1:18789}
EVENT=task.status_changed
TASK_ID=
FROM_STATUS=
TO_STATUS=
AGENT=Developer
FIRE_GEN=1

while (($#)); do
  case "$1" in
    --task-id) TASK_ID=$2; shift 2;;
    --from-status) FROM_STATUS=$2; shift 2;;
    --to-status) TO_STATUS=$2; shift 2;;
    --agent) AGENT=$2; shift 2;;
    --fire-generation) FIRE_GEN=$2; shift 2;;
    --event) EVENT=$2; shift 2;;
    -h|--help) sed -n '2,16p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

[[ -z "$TASK_ID" ]] && { echo "missing --task-id" >&2; exit 2; }
[[ -z "$FROM_STATUS" ]] && { echo "missing --from-status" >&2; exit 2; }
[[ -z "$TO_STATUS" ]] && { echo "missing --to-status" >&2; exit 2; }
[[ -z "${KAI_TASKBOARD_WEBHOOK_SECRET:-}" ]] && {
  echo "missing KAI_TASKBOARD_WEBHOOK_SECRET in env" >&2; exit 2; }

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SIGNER="$REPO_ROOT/tests/qa/sign_webhook.py"

# Compose the JSON body. event_id makes the upstream-replay branch testable
# (taskboard's own collision detection); delivery_id rotates per fire so two
# back-to-back fires for the same task spawn two ledger rows the dispatcher
# can deduplicate downstream by (task_id, fire_generation, agent_id).
BODY=$(python3 -c "
import json, sys
print(json.dumps({
    'event_id': sys.argv[1],
    'task_id': int(sys.argv[2]),
    'from_status': sys.argv[3],
    'to_status': sys.argv[4],
    'agent': sys.argv[5],
    'fire_generation': int(sys.argv[6]),
}))" "$(uuidgen)" "$TASK_ID" "$FROM_STATUS" "$TO_STATUS" "$AGENT" "$FIRE_GEN")

# Write body to a tempfile so signer + curl read the SAME bytes (no shell
# quoting drift).
BODY_FILE=$(mktemp)
trap "rm -f $BODY_FILE" EXIT
printf '%s' "$BODY" > "$BODY_FILE"

HEADERS=$(KAI_TASKBOARD_WEBHOOK_SECRET="$KAI_TASKBOARD_WEBHOOK_SECRET" \
  python3 "$SIGNER" --event "$EVENT" --body-file "$BODY_FILE")

# Build curl -H args from the four header lines.
CURL_ARGS=()
while IFS= read -r line; do
  CURL_ARGS+=(-H "$line")
done <<< "$HEADERS"

curl -sS -w "\n---HTTP=%{http_code}\n" \
  "${CURL_ARGS[@]}" \
  -H "Content-Type: application/json" \
  --data-binary "@$BODY_FILE" \
  "$DAEMON_URL/api/webhooks/taskboard"
