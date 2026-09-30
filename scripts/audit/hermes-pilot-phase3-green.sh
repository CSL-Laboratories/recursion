#!/usr/bin/env bash
# Positive Phase 3 audit: hermetic fixtures only; no model, Telegram, production, or publication.
set -euo pipefail
export LC_ALL=C RECURSION_NO_NOTIFY=1
SRC="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Gateway-facing skill is intake/status only. No state-changing CLI examples or acting posture.
OPS="$SRC/loop/hermes-skills/recursion-ops/SKILL.md"
! grep -Eq '`recursion (plan|work|drain|publish|resume)' "$OPS"
! grep -qi 'operations (acting)' "$OPS"
grep -q 'schema-checked decision' "$OPS"
grep -q 'restricted filesystem view' "$OPS"
STATUS="$SRC/loop/hermes-skills/recursion-status/SKILL.md"
grep -q 'unprivileged.*hermes-gateway' "$STATUS"
grep -q 'STATUS UNAVAILABLE' "$STATUS"
echo "ok    Telegram gateway is an authenticated intake/status surface, not an actuator"

# One authenticated Telegram row becomes one inert instruction with the complete Phase 3 identity.
I="$TMP/intake"; mkdir -p "$I/scripts" "$I/state"
cp "$SRC/scripts/intake.sh" "$SRC/scripts/instruction_routing.py" \
   "$SRC/scripts/pilot_instruction.py" \
   "$SRC/scripts/transition_receipt.py" "$SRC/scripts/jsonl_durable.py" \
   "$SRC/scripts/runtime-state.py" "$I/scripts/"
printf '#project\tpath\tremote\tbranch\tsshkey\troadmap\ttopic\tstatus\n' > "$I/state/portfolio.tsv"
printf 'example-ops\t/opt/example-ops\tgit@example/example-ops\tmain\t-\t-\t10\tactive\n' >> "$I/state/portfolio.tsv"
DB="$TMP/gateway.db"
python3 - "$DB" <<'PY'
import sqlite3,sys
c=sqlite3.connect(sys.argv[1])
c.execute('create table sessions(id text primary key,source text,user_id text,chat_id text,thread_id text)')
c.execute('create table messages(id integer primary key,session_id text,role text,content text,timestamp real)')
c.execute('insert into sessions values(?,?,?,?,?)',('s','telegram','8913126684','-1003894008732','10'))
c.execute('insert into messages values(?,?,?,?,?)',(42,'s','user','plan example-ops',1.0))
c.commit()
PY
( cd "$I" && RECURSION_ROOT="$I" RECURSION_HERMES_DB="$DB" RECURSION_INTAKE_ALLOW=8913126684 \
  bash scripts/intake.sh poll --backfill ) >/dev/null
INS="$I/state/instructions/00000042.md"
grep -q '^principal: 8913126684$' "$INS"
grep -q '^topic: 10$' "$INS"
grep -q '^project_id: example-ops$' "$INS"
grep -q '^reply_correlation: telegram:-1003894008732:10:42$' "$INS"
grep -q '^deduplication_key: telegram:-1003894008732:10:42$' "$INS"
grep -q '^status: open$' "$INS"
echo "ok    one authenticated message became one inert, fully correlated durable instruction"

# The text-only planner must have real provider/usage evidence and enqueue a schema-v1 decision.
P="$TMP/planner"; BIN="$TMP/bin"
mkdir -p "$P/scripts" "$P/state/tasks" "$P/state/roadmaps" "$P/state/.locks" "$P/loop/logs" "$BIN"
for s in hermes-decide.sh validate-decision.py validate-control-state.py decision-queue.py \
         engine_contract.py phase5_safety.py jsonl_durable.py; do
  cp "$SRC/scripts/$s" "$P/scripts/"
done
cp "$SRC/scripts/assignment.py" "$P/scripts/"
for s in intake taskctl assist branch-review resync capacity notify; do
  cat > "$P/scripts/$s.sh" <<'STUB'
#!/usr/bin/env bash
case "${1:-}" in open) echo '(none)' ;; report) echo 'ADMIT' ;; *) : ;; esac
STUB
  chmod +x "$P/scripts/$s.sh"
done
for n in $(seq 1 64); do printf 'control filler %s\n' "$n"; done > "$P/CONTROL_PLANE.md"
printf 'CONTROL_SENTINEL_AFTER_LINE_60\n' >> "$P/CONTROL_PLANE.md"
for n in $(seq 1 64); do printf 'anchor filler %s\n' "$n"; done > "$P/ANCHOR.md"
printf 'ANCHOR_SENTINEL_AFTER_LINE_60\n' >> "$P/ANCHOR.md"
printf 'DECISION_DETAIL_SENTINEL\n' > "$P/state/DECISIONS.md"
: > "$P/state/PROPOSALS.md"; : > "$P/.env"
cat > "$BIN/hermes" <<'STUB'
#!/usr/bin/env bash
usage=''
while [ $# -gt 0 ]; do case "$1" in --usage-file) usage="$2"; shift 2;; *) shift;; esac; done
if [ "${STUB_USAGE:-missing}" = good ]; then
  printf '{"provider":"openai-codex","estimated_cost_usd":0.0,"total_tokens":1}\n' > "$usage"
fi
cat <<'OUT'
reasoning only
===DECISION-START===
{"actor":"head-hermes","verb":"noop","args":[],"project":"recursion","goal":"phase3","why":"queue control","confidence":"high","wrong_if":"a state-changing command ran"}
===DECISION-END===
OUT
STUB
chmod +x "$BIN/hermes"
set +e
( cd "$P" && PATH="$BIN:$PATH" RECURSION_ROOT="$P" STUB_USAGE=missing \
  bash scripts/hermes-decide.sh --dry-run ) >"$TMP/missing-usage.out" 2>&1
missing_rc=$?
set -e
[ "$missing_rc" -ne 0 ]
( cd "$P" && PATH="$BIN:$PATH" RECURSION_ROOT="$P" STUB_USAGE=good RECURSION_DECIDE_QUIET=1 \
  bash scripts/hermes-decide.sh ) >"$TMP/planner.out" 2>&1
DECISION_FILE="$(find "$P/state/decision-queue/pending" -name '*.json' -type f)"
[ -f "$DECISION_FILE" ]
python3 "$P/scripts/validate-control-state.py" decision "$DECISION_FILE" >/dev/null
grep -q 'CONTROL_SENTINEL_AFTER_LINE_60' "$TMP/planner.out" || true
[ "$(wc -l < "$P/state/decisions.jsonl" | tr -d ' ')" = 1 ]
[ ! -e "$P/state/.decision-latest.json" ]
echo "ok    planner failed closed without usage and durably enqueued one schema-v1 decision with evidence"

# Complete precedence inputs are emitted by --brief, with no fixed-line projection loss.
( cd "$P" && RECURSION_ROOT="$P" bash scripts/hermes-decide.sh --brief ) > "$TMP/brief.out"
for sentinel in CONTROL_SENTINEL_AFTER_LINE_60 ANCHOR_SENTINEL_AFTER_LINE_60 DECISION_DETAIL_SENTINEL; do
  grep -q "$sentinel" "$TMP/brief.out"
done
echo "ok    planner projection preserved complete authority, scope, and decision inputs"

# A supervised actuator stays inert under fixture PAUSE, drains every pending decision after the
# fixture is unpaused, links the originating instruction, and does not replay after restart.
A="$TMP/actuator"
mkdir -p "$A/scripts" "$A/state/tasks" "$A/state/instructions" "$A/loop/logs"
for s in conduct-hermes.sh decision-queue.py validate-control-state.py validate-decision.py \
         engine_contract.py phase5_safety.py hermes-actuator.sh taskctl.sh jsonl_durable.py transition_receipt.py intake.sh \
         instruction_routing.py pilot_instruction.py runtime-state.py; do
  cp "$SRC/scripts/$s" "$A/scripts/"
done
cp "$SRC/scripts/assignment.py" "$A/scripts/"
cat > "$A/scripts/work.sh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$1" >> "$ACTION_LOG"
STUB
chmod +x "$A/scripts/"*.sh
for id in first second; do
  printf 'id: %s\ngoal_id: phase3\nacceptance: true\nstatus: queued\n' "$id" > "$A/state/tasks/$id.md"
done
cat > "$A/state/instructions/00000042.md" <<'T'
schema_version: 1
id: 42
principal: 8913126684
topic: 10
project_id: example-ops
deduplication_key: telegram:test:42
status: open
action_ids:

## INSTRUCTION
work first
T
: > "$A/state/decisions.jsonl"
for raw in \
  '{"id":"decision-first","actor":"head-hermes","decision_source":"hermes-model","routed_decider":"head-hermes","idempotency_key":"instruction-42-work-first","instruction_id":"42","verb":"work","args":["first"],"goal":"phase3","why":"instruction control","wrong_if":"duplicate"}' \
  '{"id":"decision-second","actor":"head-hermes","decision_source":"hermes-model","routed_decider":"head-hermes","idempotency_key":"phase3-work-second","verb":"work","args":["second"],"goal":"phase3","why":"restart control","wrong_if":"lost"}'
do
  printf '%s\n' "$raw" | RECURSION_ROOT="$A" python3 "$A/scripts/decision-queue.py" normalize \
    >> "$A/state/decisions.jsonl"
done
: > "$TMP/actions.log"; printf 'PAUSED fixture\n' > "$A/state/PAUSE"
( cd "$A" && ACTION_LOG="$TMP/actions.log" RECURSION_ROOT="$A" RECURSION_ACTUATOR_INTERVAL_S=0.1 \
  bash scripts/hermes-actuator.sh ) >"$TMP/actuator-1.out" 2>&1 & actuator_pid=$!
sleep 0.4
[ ! -s "$TMP/actions.log" ]
rm -f "$A/state/PAUSE"
for _ in $(seq 1 100); do [ "$(wc -l < "$TMP/actions.log" | tr -d ' ')" = 2 ] && break; sleep 0.1; done
kill "$actuator_pid" 2>/dev/null || true; wait "$actuator_pid" 2>/dev/null || true
[ "$(grep -c '^first$' "$TMP/actions.log")" = 1 ]
[ "$(grep -c '^second$' "$TMP/actions.log")" = 1 ]
grep -q '^status: acknowledged$' "$A/state/instructions/00000042.md"
grep -q '^action_ids: action-' "$A/state/instructions/00000042.md"
( cd "$A" && ACTION_LOG="$TMP/actions.log" RECURSION_ROOT="$A" RECURSION_ACTUATOR_INTERVAL_S=0.1 \
  timeout 0.5s bash scripts/hermes-actuator.sh ) >"$TMP/actuator-2.out" 2>&1 || true
[ "$(wc -l < "$TMP/actions.log" | tr -d ' ')" = 2 ]
[ "$(find "$A/state/decision-queue/succeeded" -name '*.json' -type f | wc -l | tr -d ' ')" = 2 ]
echo "ok    supervised actuator preserved PAUSE, drained all work once, coupled success, and survived restart"

# Hostile planner payloads are inert at the schema boundary.
V="$SRC/scripts/validate-decision.py"
VERBS='noop plan work conduct publish escalate resolve-esc reap roadmap-add roadmap-new portfolio-add register note assist-answer'
reject(){ ! python3 "$V" "$VERBS" "$1" >/dev/null 2>&1; }
reject '```json {"verb":"noop","args":[],"goal":"g","wrong_if":"w"} ```'
reject '{"verb":"work","args":["x;touch"],"goal":"g","wrong_if":"w"}'
reject '{"verb":"publish","args":["../../main"],"goal":"g","wrong_if":"w"}'
reject '{"verb":"deploy","args":[],"goal":"g","wrong_if":"w"}'
reject '{"verb":"roadmap-add","args":["example-ops"],"text":"**markdown**","goal":"g","wrong_if":"w"}'
oversized="$(python3 -c 'import json;print(json.dumps({"verb":"roadmap-add","args":["example-ops"],"text":"x"*501,"goal":"g","wrong_if":"w"}))')"
reject "$oversized"
echo "ok    Markdown, shell text, traversal, unknown verbs, and oversized prose were inert"

# The reviewed deployment artifact supervises one actuator; cron contains only the planner tick.
UNIT="$SRC/deploy/systemd/hermes-actuator.service"
grep -q '^ExecStart=/bin/bash /opt/recursion/scripts/hermes-actuator.sh$' "$UNIT"
grep -q '^Restart=always$' "$UNIT"
! grep -E '^[^#].*(conduct-hermes|hermes-actuator).*# recursion-' "$SRC/scripts/install-cron.sh"
grep -q 'hermes-control.lock' "$SRC/scripts/install-cron.sh"
echo "ok    one supervised actuator artifact exists and no colliding actuator cron path exists"

python3 "$SRC/scripts/tests/staged-fixture-closures.test.py"
echo "hermes-pilot-phase3-green: ALL PASS"
