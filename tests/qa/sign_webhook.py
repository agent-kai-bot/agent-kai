#!/usr/bin/env python3
"""Sign a taskboard webhook payload for the local KAI daemon.

Produces the four headers the daemon validates in
``daemon.webhook_auth.verify_signature`` (HMAC-SHA256 over
``<unix_ts>.<delivery_uuid>.<body_bytes>``).

Usage:
  echo '{"task_id":1,"from_status":"Backlog","to_status":"In Progress"}' \\
    | tests/qa/sign_webhook.py --event task.status_changed --secret-env KAI_TASKBOARD_WEBHOOK_SECRET

Prints, to stdout, four `Header: value` lines suitable for piping into
``curl --header @-`` or ``xargs -I{} -d '\\n' curl -H {}``.

Exits non-zero if the secret is missing.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import sys
import time
import uuid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event",
        default="task.status_changed",
        help="X-Taskboard-Event value (default: task.status_changed)",
    )
    parser.add_argument(
        "--delivery-id",
        default=None,
        help="Override the random UUID used as X-Taskboard-Delivery (for replay tests)",
    )
    parser.add_argument(
        "--timestamp",
        type=int,
        default=None,
        help="Override unix timestamp (default: now)",
    )
    parser.add_argument(
        "--secret-env",
        default="KAI_TASKBOARD_WEBHOOK_SECRET",
        help="Env var name holding the HMAC secret (default: KAI_TASKBOARD_WEBHOOK_SECRET)",
    )
    parser.add_argument(
        "--body-file",
        default="-",
        help="File containing the JSON body, or '-' for stdin",
    )
    args = parser.parse_args()

    secret = os.environ.get(args.secret_env)
    if not secret:
        print(f"missing secret in env: {args.secret_env}", file=sys.stderr)
        return 2

    if args.body_file == "-":
        body_bytes = sys.stdin.buffer.read()
    else:
        with open(args.body_file, "rb") as f:
            body_bytes = f.read()

    delivery_id = args.delivery_id or str(uuid.uuid4())
    timestamp = args.timestamp if args.timestamp is not None else int(time.time())

    signed_string = f"{timestamp}.{delivery_id}.".encode("utf-8") + body_bytes
    digest = hmac.new(
        secret.encode("utf-8"),
        signed_string,
        hashlib.sha256,
    ).hexdigest()

    print(f"X-Taskboard-Event: {args.event}")
    print(f"X-Taskboard-Delivery: {delivery_id}")
    print(f"X-Taskboard-Timestamp: {timestamp}")
    print(f"X-Taskboard-Signature: sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
