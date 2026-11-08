#!/bin/sh
set -u
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
RUNTIME="$ROOT/.runtime/dev"
WEB_PORT=${IDW_WEB_PORT:-5173}
show_pid() {
  label=$1 file=$2
  if [ -r "$file" ]; then
    pid=$(cat "$file")
    if kill -0 "$pid" 2>/dev/null; then echo "$label: running (pid $pid)"; else echo "$label: stale pid file ($pid)"; fi
  else echo "$label: stopped"; fi
}
show_pid API "$RUNTIME/api.pid"
show_pid Web "$RUNTIME/web.pid"
if command -v curl >/dev/null 2>&1; then
  curl --silent --show-error --max-time 2 http://127.0.0.1:8000/health/ready 2>/dev/null || echo "API readiness: unavailable"
  curl --silent --output /dev/null --max-time 2 "http://127.0.0.1:$WEB_PORT/" && echo "Web: responding at http://127.0.0.1:$WEB_PORT" || echo "Web: unavailable"
fi
