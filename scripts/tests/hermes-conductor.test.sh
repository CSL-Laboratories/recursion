#!/usr/bin/env bash
# hermes-conductor.test.sh — the actuator must be unable to do anything outside its whitelist,
# whatever the model returns.
#
# WHY THESE TESTS. hermes-decide.sh turns model output into a decision file; conduct-hermes.sh
# turns that file into process arguments. That is a genuine injection surface, and the file
# also sits on disk between the two steps, so anything could rewrite it. These tests treat the
# decision file as HOSTILE INPUT rather than as something a friendly model produced.
#
# ★ Test 1 is the CONTROL: a valid whitelisted decision must actually execute. Without it,
# every refusal below would also pass on an actuator that refuses everything.
#
#   run: bash scripts/tests/hermes-conductor.test.sh
set -uo pipefail
if [ "$(uname -s)" != "Linux" ]; then
  echo "hermes-conductor: SKIP (Linux loop runtime required; certified in the box suite)"
  exit 0
fi
export LC_ALL=C
SRC="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
fails=0
ok(){ printf 'ok    %s\n' "$1"; }
no(){ printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }

# Fixtures must never page Lucas: `hermes send` uses the gateway's own creds,
# so an empty fixture .env does not stop delivery (observed twice, 2026-08-06).
export RECURSION_NO_NOTIFY=1
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
R="$TMP/repo"; mkdir -p "$R/scripts/tests" "$R/state/tasks" "$R/state/roadmaps" "$R/loop/logs"
for s in assignment.py assignment_contract.py assist.sh author.sh board.sh branch-review.sh capacity.sh conduct-hermes.sh conduct.sh decision-queue.py \
  engine_contract.py git-scope.py hermes-decide.sh instruction_routing.py intake.sh jsonl_durable.py \
  notification-outbox.py notify.sh phase5_safety.py pilot_instruction.py plan.sh project-pilot.py project-reconcile.py \
  project_authority.py provider-connect-proxy.py provider_contract.py provider-network.sh provider-egress.py publish.sh remote_evidence.py \
  resolve-engine.py resolve-model-alias.py resync.sh run_contract.py runtime-state.py seed_contract.py \
  taskctl.sh transition_receipt.py validate-assignment-result.py validate-control-state.py validate-decision.py \
  validate-gate.py validate-verifier-result.py validate-worker-result.py work.sh assignment-token.py worker-evidence.py \
  worker-sandbox.sh; do
  cp "$SRC/scripts/$s" "$R/scripts/"
done
chmod +x "$R"/scripts/*.sh 2>/dev/null
: > "$R/.env"

# A marker-based stand-in for the real executors: we are testing DISPATCH, not what each
# script does (those have their own suites). Overwriting them keeps the test hermetic.
for s in plan work conduct publish; do
  cat > "$R/scripts/$s.sh" <<STUB
#!/usr/bin/env bash
printf '%s %s\n' "$s" "\$*" >> "$TMP/ran.log"
exit 0
STUB
  chmod +x "$R/scripts/$s.sh"
done

cat > "$R/state/roadmaps/example-ops.md" <<'T'
acceptance: true

- [ ] a planned item
T
cat > "$R/state/tasks/good-001.md" <<'T'
id: good-001
workstream: example-ops
goal: a real goal
acceptance: true
status: queued

body
T
cat > "$R/state/tasks/nogate-001.md" <<'T'
id: nogate-001
workstream: example-ops
goal: a task with no gate
status: queued

body
T
cat > "$R/state/tasks/pilot-deferred-001.md" <<'T'
id: pilot-deferred-001
pilot_id: pilot-example-ops
workstream: example-ops
goal: a pilot task waiting on final window authority
acceptance: true
status: queued

body
T

decide(){
  local raw="$1" bound
  bound="$(python3 - "$raw" <<'PY' 2>/dev/null
import json,sys
row=json.loads(sys.argv[1])
row.setdefault('actor','head-hermes')
row.setdefault('decision_source','hermes-model')
row.setdefault('routed_decider',row['actor'])
print(json.dumps(row,separators=(',',':')))
PY
)" || { printf '%s' "$raw" > "$TMP/d.json"; return; }
  printf '%s' "$bound" | RECURSION_ROOT="$R" python3 "$R/scripts/decision-queue.py" normalize \
    > "$TMP/d.json" 2>/dev/null || printf '%s' "$bound" > "$TMP/d.json"
}
conduct(){ ( cd "$R" && RECURSION_ROOT="$R" bash scripts/conduct-hermes.sh --file "$TMP/d.json" ) >"$TMP/out" 2>&1; echo $?; }
ran(){ grep -q "$1" "$TMP/ran.log" 2>/dev/null; }

: > "$TMP/ran.log"

# --- 1) ★ CONTROL: a valid whitelisted decision EXECUTES -----------------------------------
decide '{"verb":"work","args":["good-001"],"goal":"g","wrong_if":"w","why":"because"}'
rc=$(conduct)
if [ "$rc" = "0" ] && ran "work good-001"; then
  ok "★ CONTROL — a valid whitelisted decision executes (the actuator is not refusing everything)"
else
  no "★ CONTROL FAILED — a valid decision did not run (rc=$rc); every refusal below is meaningless"
  sed 's/^/        /' "$TMP/out" | tail -4
fi

# --- 2) an unknown verb executes NOTHING ---------------------------------------------------
: > "$TMP/ran.log"
decide '{"verb":"rm","args":["-rf"],"goal":"g","wrong_if":"w"}'
rc=$(conduct)
if [ "$rc" != "0" ] && [ ! -s "$TMP/ran.log" ]; then
  ok "an unknown verb is refused and executes nothing"
else
  no "an unknown verb reached execution (rc=$rc, ran=$(cat "$TMP/ran.log"))"
fi

# --- 3) ★ INJECTION: shell metacharacters in args never reach a shell ----------------------
: > "$TMP/ran.log"
PWNED="$TMP/pwned"
decide "{\"verb\":\"work\",\"args\":[\"good-001; touch $PWNED\"],\"goal\":\"g\",\"wrong_if\":\"w\"}"
rc=$(conduct)
if [ ! -f "$PWNED" ] && [ "$rc" != "0" ]; then
  ok "★ command injection in an argument is refused (no shell evaluation)"
else
  no "★ INJECTION SUCCEEDED — the actuator evaluated a model-supplied argument"
fi

# --- 4) path traversal is refused ----------------------------------------------------------
decide '{"verb":"publish","args":["../../etc/passwd"],"goal":"g","wrong_if":"w"}'
rc=$(conduct)
[ "$rc" != "0" ] && ok "path traversal in an argument is refused" \
                 || no "path traversal was accepted"

# --- 5) a default branch is never pushed ---------------------------------------------------
for b in main master HEAD; do
  decide "{\"verb\":\"publish\",\"args\":[\"$b\"],\"goal\":\"g\",\"wrong_if\":\"w\"}"
  rc=$(conduct)
  [ "$rc" != "0" ] || { no "publish accepted the default branch '$b'"; break; }
done
[ "$rc" != "0" ] && ok "publishing a default branch (main/master/HEAD) is refused"

# --- 6) a task with NO acceptance gate is refused ------------------------------------------
# delegation-design.md: no machine-checkable gate, no autonomy — it escalates.
: > "$TMP/ran.log"
decide '{"verb":"work","args":["nogate-001"],"goal":"g","wrong_if":"w"}'
rc=$(conduct)
if [ "$rc" != "0" ] && [ ! -s "$TMP/ran.log" ]; then
  ok "a task with no acceptance gate is refused (no gate, no autonomy)"
else
  no "a gateless task was dispatched"
fi

# A final local authority loss is a retryable controller condition.  The conductor must retain
# rc75 instead of converting it to a generic refusal/failure before work.sh can run.
cat > "$R/scripts/project-reconcile.py" <<'PY'
#!/usr/bin/env python3
import os, sys
if len(sys.argv) == 3 and sys.argv[1] == "check-work-authority":
    raise SystemExit(int(os.environ.get("AUTHORITY_RC", "75")))
raise SystemExit(2)
PY
chmod +x "$R/scripts/project-reconcile.py"
: > "$TMP/ran.log"
decide '{"verb":"work","args":["pilot-deferred-001"],"goal":"g","wrong_if":"w","why":"because"}'
mkdir -p "$R/state/actions"
find "$R/state/actions" -maxdepth 1 -type f -name 'action-*.json' -printf '%f\n' \
  | sort > "$TMP/actions.before"
AUTHORITY_RC=75; export AUTHORITY_RC
rc=$(conduct)
durable_defer="$(python3 - "$R" "$TMP/actions.before" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); before=set(Path(sys.argv[2]).read_text(encoding='utf-8').splitlines())
paths=sorted((root/'state/actions').glob('action-*.json'))
new=[path for path in paths if path.name not in before]
if len(new) != 1:
    print(f'new-actions={len(new)}'); raise SystemExit
row=json.loads(new[0].read_text(encoding='utf-8'))
events=[]
for line in (root/'state/action-events.jsonl').read_text(encoding='utf-8').splitlines():
    event=json.loads(line)
    if event.get('action_id') == row.get('id'): events.append(event)
valid=(row.get('status')=='pending' and row.get('attempts')==1
       and row.get('lease_owner') is None and row.get('lease_expires') is None
       and 'deferred before terminal transition rc=75' in str(row.get('result_evidence') or '')
       and [event.get('status') for event in events] == ['pending','running','pending']
       and all(event.get('status') != 'failed' for event in events))
print('OK' if valid else f'bad-action={row!r} events={events!r}')
PY
)"
if [ "$rc" = 75 ] && [ ! -s "$TMP/ran.log" ] && [ "$durable_defer" = OK ]; then
  ok "final pilot-window revocation remains deferred before worker dispatch"
else
  no "pilot-window revocation lost its durable pending action (rc=$rc durable=$durable_defer): $(tail -3 "$TMP/out" | tr '\n' ' ')"
fi
AUTHORITY_RC=74
rc=$(conduct)
if [ "$rc" = 75 ] && [ ! -s "$TMP/ran.log" ]; then
  ok "transient local authority I/O remains deferred before worker dispatch"
else
  no "transient authority I/O became a hard action failure (rc=$rc): $(tail -3 "$TMP/out" | tr '\n' ' ')"
fi
AUTHORITY_RC=4
rc=$(conduct)
if [ "$rc" = 5 ] && [ ! -s "$TMP/ran.log" ]; then
  ok "proved local authority invalidity remains a hard refusal before worker dispatch"
else
  no "proved invalid authority was not refused distinctly (rc=$rc): $(tail -3 "$TMP/out" | tr '\n' ' ')"
fi
unset AUTHORITY_RC
rm -f "$R/scripts/project-reconcile.py"

# --- 7) a nonexistent task / roadmap is refused ---------------------------------------------
decide '{"verb":"work","args":["does-not-exist"],"goal":"g","wrong_if":"w"}'
[ "$(conduct)" != "0" ] && ok "a nonexistent task is refused" || no "a nonexistent task was dispatched"
decide '{"verb":"plan","args":["no-such-ws"],"goal":"g","wrong_if":"w"}'
[ "$(conduct)" != "0" ] && ok "planning a workstream with no roadmap is refused" || no "planned a nonexistent workstream"

# --- 8) ★ PAUSE: acting is halted, and the decision is preserved not discarded --------------
: > "$TMP/ran.log"
printf 'PAUSED\nreason: test\n' > "$R/state/PAUSE"
decide '{"verb":"work","args":["good-001"],"goal":"g","wrong_if":"w","why":"because"}'
rc=$(conduct)
if [ "$rc" = 75 ] && [ ! -s "$TMP/ran.log" ]; then
  ok "★ under PAUSE the decision is DEFERRED, not executed (and not discarded)"
else
  no "★ PAUSE did not return the deferred contract (rc=$rc, executor_bytes=$(wc -c < "$TMP/ran.log"))"
  sed 's/^/        /' "$TMP/out" | tail -4
fi
# noop must still be allowed while paused — deciding is awareness
decide '{"verb":"noop","args":[],"goal":"g","wrong_if":"w","why":"nothing to do"}'
[ "$(conduct)" = "0" ] && ok "noop is still allowed while paused" || no "noop refused while paused"
rm -f "$R/state/PAUSE"

# --- 9) malformed / empty decision files fail closed ----------------------------------------
decide 'this is not json'
[ "$(conduct)" != "0" ] && ok "a malformed decision file fails closed" || no "malformed decision accepted"
decide '{"args":["x"],"goal":"g"}'
[ "$(conduct)" != "0" ] && ok "a decision with no verb fails closed" || no "verbless decision accepted"

# --- 10) hermes-decide validation: goal and falsifier are mandatory -------------------------
# Exercised through the validator directly — no model call, no spend.
# Exercise the REAL validator (scripts/validate-decision.py), not a re-implementation of its
# rules — a test that restates the rule cannot catch the rule being wrong. No model call.
val(){ python3 "$R/scripts/validate-decision.py" "noop plan work conduct publish escalate resolve-esc reap" "$1" 2>&1 | cut -d' ' -f1; }
[ "$(val '{"verb":"noop","args":[],"goal":"g","wrong_if":"w"}')" = "OK" ] \
  && ok "validator accepts a complete decision" || no "validator rejected a valid decision"
[ "$(val '{"verb":"noop","args":[],"wrong_if":"w"}')" = "ERR" ] \
  && ok "validator rejects a decision with no parent GOAL (invariant 12)" || no "goalless decision accepted"
[ "$(val '{"verb":"noop","args":[],"goal":"g"}')" = "ERR" ] \
  && ok "validator rejects a decision with no FALSIFIER" || no "falsifier-less decision accepted"
[ "$(val '{"verb":"deploy","args":[],"goal":"g","wrong_if":"w"}')" = "ERR" ] \
  && ok "validator rejects an off-whitelist verb" || no "off-whitelist verb accepted"

pilot_val(){ python3 "$R/scripts/validate-decision.py" \
  "noop plan work conduct publish escalate pilot-plan-propose" "$1" pilot-example-ops 2>&1 | cut -d' ' -f1; }
[ "$(pilot_val '{"actor":"pilot-example-ops","verb":"conduct","args":[],"goal":"g","wrong_if":"w"}')" = ERR ] \
  && ok "a routed project pilot cannot validate a Head-Hermes verb" \
  || no "project-pilot authority escaped through advisory prompting"
[ "$(pilot_val '{"actor":"pilot-example-ops","verb":"noop","args":[],"goal":"g","wrong_if":"w"}')" = OK ] \
  && ok "a routed project pilot retains the safe noop control" \
  || no "project-pilot authority gate rejected noop"
decide '{"actor":"pilot-example-ops","routed_decider":"pilot-example-ops","decision_source":"hermes-model","verb":"conduct","args":[],"goal":"g","wrong_if":"w"}'
[ "$(conduct)" != 0 ] && ok "the actuator independently rejects a project-pilot Head-Hermes verb" \
                       || no "the actuator trusted a project pilot to drain the portfolio"

echo
[ "$fails" -eq 0 ] && { echo "hermes-conductor: ALL PASS"; exit 0; }
echo "hermes-conductor: $fails FAILED"; exit 1
