#!/usr/bin/env bash
# drain.sh — the continuation engine. Runs QUEUED tasks one at a time (any workstream) until the
# queue is empty or a bound trips. This is what lets the loop keep going after a review/merge
# without a human hand-running each task.
#
# Bounds (so it can never run away / grind Max quota):
#   - RECURSION_DRAIN_MAX      max tasks per run           (default 6)
#   - RECURSION_DRAIN_FAILSTOP stop after N consecutive non-passes (default 2)
#   - state/PAUSE              hard stop (checked every iteration; work.sh checks it too)
#   - RECURSION_DRAIN_DEADLINE_MIN  wall-clock cap in minutes (default 360)
# Outcomes land on the BOARD + each task's status; escalations ping as usual.
#
#   drain.sh            drain the queue under the bounds above
set -uo pipefail
ROOT="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; cd "$ROOT"
[ -f .env ] && set -a && . ./.env && set +a
# .env pins RECURSION_ROOT for the deployed checkout. Sourcing it must never RETARGET a
# caller who passed a different root: that silently made a "sandbox" enqueue ten decisions
# into production on 2026-08-12. ROOT and RECURSION_ROOT agree from here on.
RECURSION_ROOT="$ROOT"; export RECURSION_ROOT
MAX="${RECURSION_DRAIN_MAX:-6}"
FAILSTOP="${RECURSION_DRAIN_FAILSTOP:-2}"
DEADLINE="${RECURSION_DRAIN_DEADLINE_MIN:-360}"
LOCK="$ROOT/state/.drain.lock"
LOG="$ROOT/loop/logs/$(date +%F)-drain.log"
log(){ echo "[$(date -Is)] drain: $*" | tee -a "$LOG"; }

# single-instance: a stale-safe lock so a cron/heartbeat kick can't stack drainers.
if ! mkdir "$LOCK" 2>/dev/null; then
  log "another drain holds $LOCK — exiting"; exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
START="$(date +%s)"; done_n=0; consec_fail=0

first_queued(){ grep -l '^status: *queued' state/tasks/*.md 2>/dev/null | sort | head -1 || true; }

log "start (max=$MAX failstop=$FAILSTOP deadline=${DEADLINE}m)"
while :; do
  [ -f "$ROOT/state/PAUSE" ] && { log "PAUSED — stopping"; break; }
  [ "$done_n" -ge "$MAX" ] && { log "hit max ($MAX tasks) — stopping"; break; }
  emin=$(( ( $(date +%s) - START ) / 60 ))
  [ "$emin" -ge "$DEADLINE" ] && { log "deadline reached (${emin}m) — stopping"; break; }

  f="$(first_queued)"; [ -n "$f" ] || { log "queue empty — done ($done_n ran)"; break; }
  id="$(sed -n 's/^id: *//p' "$f" | head -1 | tr -d '\r')"
  log "running $id (#$((done_n+1)), elapsed ${emin}m)"
  bash scripts/work.sh "$id" || true
  done_n=$((done_n+1))
  st="$(sed -n 's/^status: *//p' "state/tasks/$id.md" 2>/dev/null | head -1 | tr -d '\r')"
  log "$id -> $st"
  if [ "$st" = "passed" ]; then consec_fail=0; else
    consec_fail=$((consec_fail+1))
    [ "$consec_fail" -ge "$FAILSTOP" ] && { log "$FAILSTOP consecutive non-passes — stopping (approach not landing)"; break; }
  fi
done
log "done ($done_n tasks)"
