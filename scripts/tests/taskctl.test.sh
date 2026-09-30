#!/usr/bin/env bash
# taskctl.test.sh — the two properties the split state could not provide.
#
# Every assertion below is keyed to an observed failure, not a hypothetical:
#   LEASES        a killed worker left `status: running` forever (the 0x68 orphan).
#   ESC IDENTITY  the same ask was appended to INBOX.md four times in nine hours because
#                 escalations had no id and nothing deduped them.
#
# ★ Each group ends with a CONTROL proving the mechanism can still fire — a reaper that
# never reaps and a dedup that swallows everything would both pass a naive suite.
#
#   run: bash scripts/tests/taskctl.test.sh
set -uo pipefail
export LC_ALL=C

# This suite drives the live loop arms (flock, procfs, POSIX lock semantics) — off-box it can only
# produce noise, so it reports an explicit SKIP. The box baseline runs it for real.
if [ "$(uname -s)" != "Linux" ]; then
  echo "taskctl: SKIP (Linux loop runtime required — flock/procfs/POSIX lock semantics; certified in the box suite)"
  exit 0
fi

SRC="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
fails=0
ok(){ printf 'ok    %s\n' "$1"; }
no(){ printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
R="$TMP/repo"; mkdir -p "$R/scripts" "$R/state/tasks"
for s in assignment.py assignment_contract.py phase5_safety.py assist.sh author.sh board.sh capacity.sh conduct.sh engine_contract.py git-scope.py \
  instruction_routing.py intake.sh jsonl_durable.py notify.sh pilot_instruction.py plan.sh project-pilot.py \
  project-reconcile.py project_authority.py provider-connect-proxy.py provider_contract.py provider-network.sh provider-egress.py publish.sh \
  remote_evidence.py resolve-engine.py resolve-model-alias.py resync.sh run_contract.py runtime-state.py \
  seed_contract.py taskctl.sh transition_receipt.py validate-assignment-result.py validate-control-state.py \
  validate-decision.py validate-gate.py validate-verifier-result.py validate-worker-result.py work.sh assignment-token.py \
  worker-evidence.py worker-sandbox.sh; do
  cp "$SRC/scripts/$s" "$R/scripts/"
done
chmod +x "$R/scripts/taskctl.sh"
tc(){ RECURSION_ROOT="$R" bash "$R/scripts/taskctl.sh" "$@"; }

mktask(){ printf 'id: %s\nworkstream: %s\ngoal: %s\nstatus: queued\n\nbody\n' "$1" "${2:-t}" "${3:-a goal}" > "$R/state/tasks/$1.md"; }

# ============================ crash-safe writer lock ============================
mkdir -p "$R/state/.locks/taskctl.lock"
printf '%s\n' "$$" > "$R/state/.locks/taskctl.lock/pid"
if RECURSION_ROOT="$R" RECURSION_LOCK_ATTEMPTS=2 RECURSION_LOCK_DELAY_S=0 \
     bash "$R/scripts/taskctl.sh" event lock-live '' probe >/dev/null 2>&1; then
  no "lock: a live owner was reclaimed"
else
  [ -d "$R/state/.locks/taskctl.lock" ] \
    && ok "lock: a live owner remains fail-closed" \
    || no "lock: live owner evidence was removed"
fi
printf '999999999\n' > "$R/state/.locks/taskctl.lock/pid"
if RECURSION_ROOT="$R" RECURSION_LOCK_ATTEMPTS=3 RECURSION_LOCK_DELAY_S=0 \
     bash "$R/scripts/taskctl.sh" event lock-recovered '' probe >/dev/null 2>&1 \
   && [ ! -d "$R/state/.locks/taskctl.lock" ]; then
  ok "lock: a provably dead owner is reclaimed and the next canonical writer succeeds"
else
  no "lock: a dead owner permanently wedged canonical state"
  rm -rf "$R/state/.locks/taskctl.lock"
fi

# ============================ leases ============================
mktask t1
tc start t1 "worker-a" >/dev/null 2>&1
[ "$(tc get t1)" = "running" ] && ok "start: status is running" || no "start: status is $(tc get t1)"
[ "$(tc get t1 attempts)" = "1" ] && ok "start: attempts incremented to 1" || no "start: attempts=$(tc get t1 attempts)"

# a LIVE lease may not be stolen — two workers on one task is what produced a redundant
# hour-long arm run that had to be killed by hand.
if tc start t1 "worker-b" >/dev/null 2>&1; then
  no "lease: a second worker STOLE a live lease"
else
  ok "lease: a live lease cannot be stolen by a second worker"
fi

# reap must NOT touch a task whose lease is still valid
tc reap >/dev/null 2>&1
[ "$(tc get t1)" = "running" ] && ok "reap: leaves a LIVE lease alone" || no "reap: killed a live lease ($(tc get t1))"

# expire the lease -> reap must mark it stale
RECURSION_ROOT="$R" python3 - "$R/state/tasks/t1.md" <<'PY'
import sys
f=sys.argv[1]; s=open(f).read().split('\n')
s=[('lease_expires: 1' if l.startswith('lease_expires:') else l) for l in s]
open(f,'w',newline='\n').write('\n'.join(s))
PY
tc reap >/dev/null 2>&1
if [ "$(tc get t1)" = "stale" ]; then
  ok "★ reap: an EXPIRED lease becomes stale (no permanent 'running' row)"
else
  no "★ reap: expired lease still reads '$(tc get t1)' — the orphan bug survives"
fi
# idempotent: reaping again must not churn
tc reap >/dev/null 2>&1
[ "$(tc get t1)" = "stale" ] && ok "reap: idempotent" || no "reap: not idempotent ($(tc get t1))"

# after reaping, the task can be picked up again
tc start t1 "worker-c" >/dev/null 2>&1
[ "$(tc get t1)" = "running" ] && [ "$(tc get t1 attempts)" = "2" ] \
  && ok "CONTROL — a reaped task is re-leasable (attempt 2)" \
  || no "CONTROL FAILED — reaped task not re-leasable (status=$(tc get t1) attempts=$(tc get t1 attempts))"

tc finish t1 passed "gate green" >/dev/null 2>&1
[ "$(tc get t1)" = "passed" ] && ok "finish: terminal status recorded" || no "finish: $(tc get t1)"

# ============================ event log ============================
EV="$R/state/events.jsonl"
# Two SEPARATE properties. Folding them into one assertion made a wrong count threshold
# report itself as "malformed JSON", which points at the wrong defect entirely.
if [ -s "$EV" ]; then
  if python3 -c 'import json,sys
[json.loads(l) for l in open(sys.argv[1],encoding="utf-8") if l.strip()]' "$EV" 2>/dev/null; then
    ok "events: every line is valid JSON"
  else
    no "events: at least one line is malformed JSON"
  fi
  # start(t1) + stale(t1) + start(t1 again) + finish(t1) — the lifecycle must be on the record.
  nev="$(grep -c . "$EV")"
  if [ "$nev" -ge 4 ]; then
    ok "events: the full lifecycle is recorded ($nev events)"
  else
    no "events: only $nev events — lifecycle transitions are being lost"
  fi
  for k in start stale finish; do
    grep -q "\"event\":\"$k\"" "$EV" || no "events: no '$k' event emitted"
  done
else
  no "events: no event log written"
fi
grep -q '"event":"stale"' "$EV" && ok "events: the stale reap is on the record" || no "events: no stale event emitted"

# a goal containing quotes/backslashes must not produce malformed JSON
mktask t2 t 'a "quoted" goal with \ backslash'
tc start t2 >/dev/null 2>&1
if python3 -c 'import json,sys
[json.loads(l) for l in open(sys.argv[1],encoding="utf-8") if l.strip()]' "$EV" 2>/dev/null; then
  ok "events: quotes and backslashes in a goal stay valid JSON"
else
  no "events: a quoted goal produced malformed JSON"
fi

# ============================ escalation identity ============================
K="approve-the-native-diff-frontier"
r1="$(tc esc-record "$K" "Approve the Example-game native-diff frontier" example-game ptr)"
case "$r1" in NEW*) ok "esc: first sighting is NEW ($r1)" ;; *) no "esc: first sighting returned '$r1'" ;; esac
EID="${r1#NEW }"

# ★ the four-times bug: the same ask, three more cycles
dups=0
for _ in 1 2 3; do
  r="$(tc esc-record "$K" "Approve the Example-game native-diff frontier" example-game ptr)"
  case "$r" in DUP\ "$EID"*) dups=$((dups+1)) ;; esac
done
if [ "$dups" = "3" ]; then
  ok "★ esc: the SAME ask returns DUP $EID three times (the four-times bug cannot recur)"
else
  no "★ esc: re-raising the same ask produced $dups/3 DUPs — duplicates would still accumulate"
fi
cnt="$(awk -F'\t' -v e="$EID" '$1==e {print $6}' "$R/state/escalations.tsv")"
[ "$cnt" = "4" ] && ok "esc: sighting count is 4 (1 new + 3 repeats), not 4 rows" || no "esc: count=$cnt"
rows="$(grep -c "	$K	" "$R/state/escalations.tsv")"
[ "$rows" = "1" ] && ok "esc: exactly ONE row for the ask" || no "esc: $rows rows for one ask"

# CONTROL: a DIFFERENT ask must still open a new escalation, or dedup is just swallowing.
r2="$(tc esc-record "some-other-question" "A different ask" example-ops ptr2)"
case "$r2" in NEW*) ok "CONTROL — a different ask still opens a NEW escalation ($r2)" ;;
              *)    no "CONTROL FAILED — dedup swallowed a genuinely different ask ('$r2')" ;; esac

# resolve, then the same key may open again (the ask can legitimately recur later)
tc esc-resolve "$EID" "decided" >/dev/null 2>&1
[ "$(tc esc-list open | grep -c "	$K	")" = "0" ] && ok "esc: resolved drops out of the open list" || no "esc: still open after resolve"
r3="$(tc esc-record "$K" "same ask, new cycle" example-game ptr)"
case "$r3" in NEW*) ok "CONTROL — after resolution the ask can be raised again ($r3)" ;;
              *)    no "CONTROL FAILED — a resolved ask can never be re-raised ('$r3')" ;; esac

tabbed="$(tc esc-record $'tab\tkey' $'title\tforged\nrow' $'ws\tbad' $'ptr\nline')"
tabbed_id="${tabbed#NEW }"
if [ -n "$tabbed_id" ] && [ "$(awk -F'\t' -v e="$tabbed_id" '$1==e{print NF}' "$R/state/escalations.tsv")" = 9 ]; then
  ok "esc: model-shaped tabs/newlines cannot forge TSV columns or rows"
else
  no "esc: control characters corrupted escalation identity state"
fi

# resolving an unknown id must fail loudly rather than silently succeed
if tc esc-resolve "esc-9999" >/dev/null 2>&1; then
  no "esc: resolving a nonexistent escalation reported success"
else
  ok "esc: resolving a nonexistent escalation fails loudly"
fi

echo
[ "$fails" -eq 0 ] && { echo "taskctl: ALL PASS"; exit 0; }
echo "taskctl: $fails FAILED"; exit 1
