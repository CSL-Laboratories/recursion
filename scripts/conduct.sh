#!/usr/bin/env bash
# conduct.sh — the PARALLEL continuation engine (parallel-loops.md). Where drain.sh runs queued
# tasks strictly one at a time, conduct.sh runs up to N at once — at most ONE build per workstream
# (work.sh's per-workstream lock is the backstop), so independent workstreams build
# concurrently. Same bounds as drain so it can never run away or grind the Max quota.
#
# Bounds (env, human-set):
#   RECURSION_CONDUCT_CONCURRENCY  max concurrent builds        (default 2)
#   RECURSION_DRAIN_MAX            max tasks total this run      (default 6)
#   RECURSION_DRAIN_FAILSTOP       stop launching after N non-passes (default 2; total, not consec.)
#   RECURSION_DRAIN_DEADLINE_MIN   wall-clock cap in minutes     (default 360)
#   RECURSION_CONDUCT_POLL_S       poll interval seconds         (default 5)
#   state/PAUSE                    hard stop (checked every poll; work.sh checks it too)
#
#   conduct.sh          drain the queue IN PARALLEL under the bounds above
set -uo pipefail
ROOT="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; cd "$ROOT"
[ -f .env ] && set -a && . ./.env && set +a
# .env pins RECURSION_ROOT for the deployed checkout. Sourcing it must never RETARGET a
# caller who passed a different root: that silently made a "sandbox" enqueue ten decisions
# into production on 2026-08-12. ROOT and RECURSION_ROOT agree from here on.
RECURSION_ROOT="$ROOT"; export RECURSION_ROOT
CONC="${RECURSION_CONDUCT_CONCURRENCY:-2}"
MAX="${RECURSION_DRAIN_MAX:-6}"
FAILSTOP="${RECURSION_DRAIN_FAILSTOP:-2}"
DEADLINE="${RECURSION_DRAIN_DEADLINE_MIN:-360}"
POLL="${RECURSION_CONDUCT_POLL_S:-5}"
# How long to keep holding for capacity before giving the queue back to the next cycle. Waiting
# forever would hold the single-instance .conduct.lock forever, and a held lock makes every later
# conduct run exit at once — one full box wedging all future conducting, silently.
CAPWAIT="${RECURSION_CONDUCT_CAP_WAIT_S:-600}"
CAP_SINCE=0
LOCK="$ROOT/state/.conduct.lock"
LOG="$ROOT/loop/logs/$(date +%F)-conduct.log"
log(){ echo "[$(date -Is)] conduct: $*" | tee -a "$LOG"; }

# single-instance, and never double-drive the queue against a serial drainer.
if ! mkdir "$LOCK" 2>/dev/null; then log "another conductor holds $LOCK — exiting"; exit 0; fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
if [ -d "$ROOT/state/.drain.lock" ]; then log "a serial drain holds .drain.lock — exiting"; exit 0; fi

START="$(date +%s)"; launched=0; done_n=0; nonpass=0
declare -A PID2ID PID2WS RUNNING_WS

status_of(){ sed -n 's/^status: *//p' "state/tasks/$1.md" 2>/dev/null | head -1 | tr -d '\r'; }

# $(n_running) on an EMPTY associative array is an unbound-variable error under `set -u` in
# bash 5.3, and ${#A[@]-0} / ${#A[@]:-0} are both "bad substitution" — there is no default-value
# form for it. Iterating ${!A[@]} IS safe when empty, so the count goes through here. This fired
# only when the conductor launched nothing, which capacity deferral now makes an ordinary path.
n_running(){ local n=0 k; for k in "${!PID2ID[@]}"; do n=$((n+1)); done; printf '%s' "$n"; }

# next queued task whose workstream is NOT already running under this conductor.
# emits "<id>\t<ws>" and returns 0, or returns 1 when nothing is launchable.
next_launchable(){
  local f id ws
  for f in $(grep -l '^status: *queued' state/tasks/*.md 2>/dev/null | sort); do
    id="$(sed -n 's/^id: *//p' "$f" | head -1 | tr -d '\r')"
    ws="$(sed -n 's/^workstream: *//p' "$f" | head -1 | tr -d '\r')"; ws="${ws:-none}"
    [ -n "${RUNNING_WS[$ws]:-}" ] && continue
    printf '%s\t%s\n' "$id" "$ws"; return 0
  done
  return 1
}

# reap finished children: update counters, free the workstream slot.
reap(){
  local pid id ws st
  for pid in "${!PID2ID[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      id="${PID2ID[$pid]}"; ws="${PID2WS[$pid]}"
      wait "$pid" 2>/dev/null || true
      st="$(status_of "$id")"
      log "$id ($ws) -> ${st:-?}"
      [ "$st" = "passed" ] || nonpass=$((nonpass+1))
      done_n=$((done_n+1))
      unset "PID2ID[$pid]" "PID2WS[$pid]" "RUNNING_WS[$ws]"
    fi
  done
}

log "start (concurrency=$CONC max=$MAX failstop=$FAILSTOP deadline=${DEADLINE}m poll=${POLL}s)"
while :; do
  [ -f "$ROOT/state/PAUSE" ] && { log "PAUSED — stop launching"; break; }
  emin=$(( ( $(date +%s) - START ) / 60 ))
  [ "$emin" -ge "$DEADLINE" ] && { log "deadline reached (${emin}m) — stop launching"; break; }
  [ "$nonpass" -ge "$FAILSTOP" ] && { log "$FAILSTOP non-passes — stop launching (approach not landing)"; break; }

  # launch while we have a free slot, budget left, and an idle workstream with queued work.
  while [ "$(n_running)" -lt "$CONC" ] && [ "$launched" -lt "$MAX" ]; do
    # ADMISSION CONTROL. RECURSION_CONDUCT_CONCURRENCY is a fixed count, and a fixed count
    # assumes every task costs the same. It also cannot see what ELSE is on the box — an
    # overnight gate, a stray node, the reflection pass. Ask before launching; a deferral is not
    # a failure, it just means this poll launches nothing and the next one asks again.
    if [ -x scripts/capacity.sh ]; then
      cap="$(bash scripts/capacity.sh check arm 2>&1)"; caprc=$?
      if [ "$caprc" -ne 0 ]; then
        [ "$cap" != "${LAST_CAP:-}" ] && log "holding: $cap"   # log the reason once, not every poll
        LAST_CAP="$cap"
        [ "$CAP_SINCE" = "0" ] && CAP_SINCE="$(date +%s)"
        break
      fi
      LAST_CAP=""; CAP_SINCE=0
    fi
    line="$(next_launchable)" || break
    id="${line%%$'\t'*}"; ws="${line##*$'\t'}"
    log "launch $id (ws=$ws) [#$((launched+1))/$MAX, running $(( $(n_running) + 1 ))/$CONC]"
    nohup bash scripts/work.sh "$id" >>"$LOG" 2>&1 &
    pid=$!
    PID2ID[$pid]="$id"; PID2WS[$pid]="$ws"; RUNNING_WS[$ws]=1
    launched=$((launched+1))
  done

  # nothing running and nothing launchable -> the queue is drained.
  # ...unless CAPACITY is what stopped us. "The box is busy" is not "the queue is empty", and
  # reporting the second when the first is true is a silent success on work that never ran.
  if [ "$(n_running)" -eq 0 ]; then
    if [ -n "${LAST_CAP:-}" ]; then
      capwaited=$(( $(date +%s) - CAP_SINCE ))
      if [ "$capwaited" -ge "$CAPWAIT" ]; then
        log "gave up after ${capwaited}s waiting on capacity — the queue stays for the next cycle ($LAST_CAP)"
        break
      fi
    else
      log "queue empty / no idle workstream with work — done ($done_n ran)"; break
    fi
  fi

  sleep "$POLL"
  reap
done

# let in-flight builds finish (bounded by their own logic), then reap.
if [ "$(n_running)" -gt 0 ]; then
  log "draining $(n_running) in-flight build(s) to completion"
  while [ "$(n_running)" -gt 0 ]; do sleep "$POLL"; reap; done
fi
log "done ($done_n tasks, $nonpass non-pass)"
