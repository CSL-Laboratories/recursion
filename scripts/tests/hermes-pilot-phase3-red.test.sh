#!/usr/bin/env bash
# Phase 0 control: reproduce the Phase 3 decision/actuation-path defects before fixing them.
# This suite is EXPECTED TO FAIL on the pre-change commit recorded in the production plan.
set -uo pipefail
export LC_ALL=C
SRC="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
fails=0
ok(){ printf 'ok    %s\n' "$1"; }
no(){ printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }
stage_controller_closure(){
  local root="$1"
  for helper in assignment.py assignment_contract.py assist.sh author.sh board.sh branch-review.sh capacity.sh conduct-hermes.sh conduct.sh \
      decision-queue.py engine_contract.py git-scope.py hermes-decide.sh instruction_routing.py intake.sh \
      jsonl_durable.py notification-outbox.py notify.sh phase5_safety.py pilot_instruction.py plan.sh project-pilot.py \
      project-reconcile.py project_authority.py provider-connect-proxy.py provider_contract.py provider-network.sh provider-egress.py publish.sh \
      remote_evidence.py resolve-engine.py resolve-model-alias.py resync.sh run_contract.py runtime-state.py \
      seed_contract.py taskctl.sh transition_receipt.py validate-assignment-result.py validate-control-state.py \
      validate-decision.py validate-gate.py validate-verifier-result.py validate-worker-result.py work.sh assignment-token.py \
      worker-evidence.py worker-sandbox.sh; do
    cp "$SRC/scripts/$helper" "$root/scripts/"
  done
}
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
normalize_head_decision(){ # root file
  local root="$1" file="$2" temporary="$2.normalized"
  python3 - "$file" <<'PY' | RECURSION_ROOT="$root" python3 "$root/scripts/decision-queue.py" normalize > "$temporary"
import json,sys
row=json.load(open(sys.argv[1],encoding='utf-8'))
row.setdefault('actor','head-hermes')
row.setdefault('decision_source','hermes-model')
row.setdefault('routed_decider','head-hermes')
row.setdefault('why',row.get('reason') or 'Phase 3 red control')
print(json.dumps(row,separators=(',',':')))
PY
  mv -f "$temporary" "$file"
}

check_staged_python_dependencies(){
  local root="$1"
  if python3 "$SRC/scripts/tests/staged-dependency-guard.py" "$SRC/scripts" "$root/scripts"
  then
    ok "staged fixtures contain every static, dynamic, shell, and nested first-party dependency"
  else
    no "a staged fixture is missing a first-party dependency"
  fi
}

# 1. The Telegram-facing gateway must have no direct execution path. A message is recorded once;
# only the durable actuator may later act. The current gateway skill explicitly authorizes both.
OPS="$SRC/loop/hermes-skills/recursion-ops/SKILL.md"
if ! grep -Eq '`recursion (plan|work|drain|publish|resume)' "$OPS" 2>/dev/null \
   && ! grep -qi 'operations (acting)' "$OPS" 2>/dev/null; then
  ok "Telegram gateway has no direct state-changing execution path"
else
  no "Telegram gateway still authorizes direct plan/work/drain/publish execution alongside intake"
fi

# 2. A valid authoring decision must carry its validated text through the actuator. This catches
# the current undefined DECISION variable, which silently empties text after file validation.
A="$TMP/author-text"; mkdir -p "$A/scripts" "$A/state" "$A/loop/logs"
cp "$SRC/scripts/conduct-hermes.sh" "$A/scripts/"
cp "$SRC/scripts/taskctl.sh" "$A/scripts/"
cp "$SRC/scripts/jsonl_durable.py" "$SRC/scripts/transition_receipt.py" \
  "$SRC/scripts/runtime-state.py" "$A/scripts/"
cp "$SRC/scripts/decision-queue.py" "$SRC/scripts/validate-control-state.py" \
  "$SRC/scripts/validate-decision.py" "$SRC/scripts/engine_contract.py" \
  "$SRC/scripts/phase5_safety.py" \
  "$SRC/scripts/assignment.py" "$SRC/scripts/assignment_contract.py" "$A/scripts/"
stage_controller_closure "$A"
check_staged_python_dependencies "$A"
cat > "$A/scripts/author.sh" <<'STUB'
#!/usr/bin/env bash
printf '%s|%s|%s\n' "$1" "$2" "$3" > "$AUTHOR_LOG"
STUB
chmod +x "$A/scripts/"*.sh
cat > "$TMP/author.json" <<'J'
{"id":"author-1","verb":"roadmap-add","args":["example-ops"],"text":"add a reviewed pilot control","goal":"g","wrong_if":"text lost"}
J
normalize_head_decision "$A" "$TMP/author.json"
( cd "$A" && AUTHOR_LOG="$TMP/author.log" RECURSION_ROOT="$A" \
    bash scripts/conduct-hermes.sh --file "$TMP/author.json" ) >"$TMP/author.out" 2>&1
author_rc=$?
if [ "$author_rc" -eq 0 ] && grep -qF 'roadmap-add|example-ops|add a reviewed pilot control' "$TMP/author.log" 2>/dev/null; then
  ok "validated authoring text reached the sole actuator"
else
  no "validated authoring text was dropped before actuation (rc=$author_rc)"
fi

# 3. An instruction cannot become handled without a link to a succeeded durable action.
I="$TMP/intake"; mkdir -p "$I/scripts" "$I/state/instructions"
cp "$SRC/scripts/intake.sh" "$I/scripts/"
cp "$SRC/scripts/runtime-state.py" "$SRC/scripts/transition_receipt.py" \
  "$SRC/scripts/jsonl_durable.py" "$SRC/scripts/instruction_routing.py" \
  "$SRC/scripts/pilot_instruction.py" "$I/scripts/"
stage_controller_closure "$I"
check_staged_python_dependencies "$I"
cat > "$I/state/instructions/00000042.md" <<'T'
id: 42
received: 2026-08-06T00:00:00Z
from: principal
status: open

## INSTRUCTION
plan example-ops
T
( cd "$I" && RECURSION_ROOT="$I" bash scripts/intake.sh handled 42 'done' ) >"$TMP/handled.out" 2>&1
handled_rc=$?
handled_status="$(sed -n 's/^status: *//p' "$I/state/instructions/00000042.md" | head -1)"
if [ "$handled_rc" -ne 0 ] && [ "$handled_status" = open ]; then
  ok "instruction stayed open without a succeeded action id"
else
  no "instruction was marked $handled_status with no successful action link"
fi

# 4. Missing provider/usage evidence must fail closed before a planning decision is accepted.
H="$TMP/decide"; mkdir -p "$H/scripts" "$H/state/tasks" "$H/state/roadmaps" "$H/loop/logs" "$TMP/hermes-bin"
cp "$SRC/scripts/hermes-decide.sh" "$H/scripts/"
cp "$SRC/scripts/validate-decision.py" "$H/scripts/"
stage_controller_closure "$H"
check_staged_python_dependencies "$H"
printf 'authority\n' > "$H/CONTROL_PLANE.md"; printf 'scope\n' > "$H/ANCHOR.md"
: > "$H/state/DECISIONS.md"; : > "$H/state/PROPOSALS.md"
cat > "$TMP/hermes-bin/hermes" <<'STUB'
#!/usr/bin/env bash
# Deliberately do not create the requested --usage-file.
cat <<'OUT'
reasoning
===DECISION-START===
{"verb":"noop","args":[],"project":"recursion","goal":"g","why":"test","confidence":"high","wrong_if":"usage missing"}
===DECISION-END===
OUT
STUB
chmod +x "$TMP/hermes-bin/hermes"
( cd "$H" && PATH="$TMP/hermes-bin:$PATH" RECURSION_ROOT="$H" \
    bash scripts/hermes-decide.sh --dry-run ) >"$TMP/cost.out" 2>&1
cost_rc=$?
[ "$cost_rc" -ne 0 ] && ok "decision failed closed when provider/usage evidence was absent" \
                      || no "decision was accepted with no provider/usage evidence"

# 5. Authority, scope, and decisions must be precedence-aware projections, not fixed line slices.
for n in $(seq 1 64); do printf 'control filler %s\n' "$n"; done > "$H/CONTROL_PLANE.md"
printf 'CONTROL_SENTINEL_AFTER_LINE_60\n' >> "$H/CONTROL_PLANE.md"
for n in $(seq 1 64); do printf 'anchor filler %s\n' "$n"; done > "$H/ANCHOR.md"
printf 'ANCHOR_SENTINEL_AFTER_LINE_60\n' >> "$H/ANCHOR.md"
cat > "$H/state/DECISIONS.md" <<'D'
## D-999 — projection control
Date: 2026-08-06
**Chose:** safe
The load-bearing reason is DECISION_DETAIL_SENTINEL and must not be truncated.
**Falsifier:** absent
D
( cd "$H" && RECURSION_ROOT="$H" bash scripts/hermes-decide.sh --brief ) >"$TMP/brief.out" 2>&1
missing=0
for s in CONTROL_SENTINEL_AFTER_LINE_60 ANCHOR_SENTINEL_AFTER_LINE_60 DECISION_DETAIL_SENTINEL; do
  grep -q "$s" "$TMP/brief.out" || missing=$((missing+1))
done
[ "$missing" = 0 ] && ok "planner projection preserved complete precedence inputs" \
                     || no "planner projection truncated $missing load-bearing authority/decision inputs"

# 6. Restart must preserve every pending decision. A singleton latest file drops the older one.
DROOT="$TMP/pending"; mkdir -p "$DROOT/scripts" "$DROOT/state/tasks" "$DROOT/loop/logs"
for helper in conduct-hermes.sh taskctl.sh jsonl_durable.py transition_receipt.py runtime-state.py decision-queue.py validate-control-state.py validate-decision.py engine_contract.py phase5_safety.py; do
  cp "$SRC/scripts/$helper" "$DROOT/scripts/"
done
cp "$SRC/scripts/assignment.py" "$DROOT/scripts/"
stage_controller_closure "$DROOT"
check_staged_python_dependencies "$DROOT"
cat > "$DROOT/scripts/work.sh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$1" >> "$PENDING_LOG"
STUB
chmod +x "$DROOT/scripts/"*.sh
for id in first second; do printf 'id: %s\ngoal_id: g\nacceptance: true\nstatus: queued\n' "$id" > "$DROOT/state/tasks/$id.md"; done
: > "$DROOT/state/decisions.jsonl"
for item in d1:first d2:second; do
  did="${item%%:*}"; task="${item#*:}"
  printf '{"id":"%s","verb":"work","args":["%s"],"goal":"g","why":"queue control","wrong_if":"lost"}\n' \
    "$did" "$task" > "$TMP/$did.json"
  normalize_head_decision "$DROOT" "$TMP/$did.json"
  cat "$TMP/$did.json" >> "$DROOT/state/decisions.jsonl"
done
tail -1 "$DROOT/state/decisions.jsonl" > "$DROOT/state/.decision-latest.json"
: > "$TMP/pending.log"
( cd "$DROOT" && PENDING_LOG="$TMP/pending.log" RECURSION_ROOT="$DROOT" \
    bash scripts/conduct-hermes.sh ) >/dev/null 2>&1 || true
first_n="$(grep -c '^first$' "$TMP/pending.log" || true)"; second_n="$(grep -c '^second$' "$TMP/pending.log" || true)"
if [ "$first_n" = 1 ] && [ "$second_n" = 1 ]; then
  ok "restart/drain preserved and executed every pending decision once"
else
  no "singleton decision pointer lost pending work (first=$first_n second=$second_n)"
fi

echo
[ "$fails" -eq 0 ] && { echo "hermes-pilot-phase3-red: ALL PASS"; exit 0; }
echo "hermes-pilot-phase3-red: $fails FAILED (expected on the Phase 0 pre-fix commit)"; exit 1
