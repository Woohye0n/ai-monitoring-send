#!/usr/bin/env bash
# Start the sender loop in the background (nohup). Logs: data/sender.log
#
# Safe to run repeatedly: it exits immediately when the sender is already up.
# That is what lets the same line serve as a cron watchdog (see setup.sh).
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
# cron runs with a minimal PATH, so a conda/pyenv python3 recorded at install
# time may not resolve. Fall back rather than dying silently in a cron job.
command -v "$PY" >/dev/null 2>&1 || PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=/usr/bin/python3
mkdir -p data

# A pid file alone is not proof: Linux reuses pids, and a stale file pointing at
# some unrelated process would keep the watchdog from ever restarting us.
alive() {
  local pid="$1"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  if [ -r "/proc/$pid/cmdline" ]; then
    tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q 'sender.main' || return 1
  fi
  return 0
}

if [ -f data/sender.pid ] && alive "$(cat data/sender.pid 2>/dev/null)"; then
  echo "already running (pid $(cat data/sender.pid))"; exit 0
fi

# One process appends here for months; without this the log is the thing that
# eventually fills the disk.
if [ -f data/sender.log ]; then
  SIZE=$(wc -c < data/sender.log 2>/dev/null || echo 0)
  if [ "$SIZE" -gt 10485760 ]; then
    mv -f data/sender.log data/sender.log.1
  fi
fi

nohup env PYTHONPATH=. "$PY" -m sender.main >> data/sender.log 2>&1 &
echo $! > data/sender.pid
echo "started pid $(cat data/sender.pid) — logs: data/sender.log"
