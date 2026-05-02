#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <host-expression>" >&2
  exit 64
fi

host_expr="$1"
local_host="$(hostname)"
if ! local_addr_line="$(getent hosts "$local_host" 2>/dev/null | head -n 1)"; then
  local_addr_line="unresolved"
fi
if ! target_addr_line="$(getent hosts "$host_expr" 2>/dev/null | head -n 1)"; then
  target_addr_line="unresolved"
fi

echo "[host-verify] preamble: hostname; getent hosts $host_expr"
echo "[host-verify] local_hostname=$local_host"
echo "[host-verify] local_getent=$local_addr_line"
echo "[host-verify] intended_host=$host_expr"
echo "[host-verify] target_getent=$target_addr_line"

if [[ "$target_addr_line" == "unresolved" ]]; then
  echo "[host-verify] actual=unresolved"
  exit 3
fi

target_ip="$(awk '{print $1}' <<<"$target_addr_line")"
target_name="$(awk '{print $2}' <<<"$target_addr_line")"
echo "[host-verify] actual_ip=$target_ip"
echo "[host-verify] actual_name=${target_name:-unknown}"
