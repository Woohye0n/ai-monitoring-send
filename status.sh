#!/usr/bin/env bash
# Show sender status: process, last log lines, and latest batch on the NAS.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ -f data/sender.pid ] && kill -0 "$(cat data/sender.pid)" 2>/dev/null; then
  echo "running (pid $(cat data/sender.pid))"
else
  echo "NOT running"
fi
NAS_ROOT="$("$PY" -c "import json;print(json.load(open('config.json')).get('nas_root','?'))" 2>/dev/null || echo '?')"
NODE_ID="$("$PY" -c "import json;print(json.load(open('config.json')).get('node_id','?'))" 2>/dev/null || echo '?')"
echo "node=$NODE_ID  nas=$NAS_ROOT"
DIR="$NAS_ROOT/inbox/$NODE_ID"
echo "latest batches in $DIR:"
ls -1t "$DIR" 2>/dev/null | head -3 | sed 's/^/  /' || echo "  (none yet / NAS not mounted)"
# Coverage first: a sender that runs and delivers but reads the wrong
# directory looks perfectly healthy in the lines above.
echo "--- coverage (what is actually being read) ---"
"$PY" scripts/where-landed.py 2>/dev/null | sed -n '1,12p' || echo "  (unavailable)"
WARN="$(grep -c 'WARN' data/sender.log 2>/dev/null || echo 0)"
[ "$WARN" -gt 0 ] && echo "--- warnings ($WARN in log; latest 3) ---" \
  && grep 'WARN' data/sender.log | tail -n 3 | sed 's/^/  /'
echo "--- last log lines ---"
tail -n 8 data/sender.log 2>/dev/null || echo "  (no log yet)"
