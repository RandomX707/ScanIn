#!/bin/sh
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
for f in api web; do p="$ROOT/.runtime/test-stack/$f.pid"; [ -f "$p" ] && kill "$(cat "$p")" 2>/dev/null; rm -f "$p"; done
echo "test stack stopped"
