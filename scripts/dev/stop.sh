#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
RUNTIME="$ROOT/.runtime/dev"
stop_pid() {
  file=$1
  [ -r "$file" ] || return 0
  pid=$(cat "$file")
  case "$pid" in ''|*[!0-9]*) echo "Invalid PID file $file; refusing to signal." >&2; return 1 ;; esac
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    count=0
    while kill -0 "$pid" 2>/dev/null && [ "$count" -lt 10 ]; do sleep 1; count=$((count + 1)); done
    if kill -0 "$pid" 2>/dev/null; then echo "Process $pid did not stop; leaving PID file." >&2; return 1; fi
  fi
  rm -f "$file"
}
stop_pid "$RUNTIME/web.pid"
stop_pid "$RUNTIME/api.pid"
echo "Channel360 development processes stopped."
