#!/usr/bin/env bash
# intake.test.sh — what Lucas says must become durable state, and NOTHING ELSE may.
#
# WHY. Telegram was outbound-only, so every instruction had to be typed into a coding session:
# the human was the transport, and Hermes could only choose among work someone else wrote down.
# Intake closes that, and the danger it introduces is obvious — a chat line that becomes a
# command is an arbitrary-execution channel wearing a friendly face. So intake writes durable
# INSTRUCTIONS; the actuator whitelist still decides what may run.
#
# ★ Test 1 is the CONTROL: a real message must actually be ingested, or every refusal below is
#   satisfied by an intake that ingests nothing at all.
# ★ Test 2 is the trap that nearly shipped: most role='user' rows in the gateway store are OUR
#   OWN prompts (source='cli' — the heartbeat, the daily brief, hermes-decide) and subagent
#   prompts. Ingesting those feeds the system its own output back as instructions from Lucas.
#
# Hermetic: a synthetic gateway store with the real schema. No network, no Telegram, no spend.
#
#   run: bash scripts/tests/intake.test.sh
set -uo pipefail
export LC_ALL=C
SRC="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
fails=0
ok(){ printf 'ok    %s\n' "$1"; }
no(){ printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }

# PLATFORM-BOUND (measured 2026-08-27 on Windows Git Bash). Every mutating intake path is gated on
# a witnessed directory lock: intake.sh hands `$$` to scripts/runtime-state.py lock-acquire, and
# runtime-state.py witnesses the owner by asking the OS for that PID's process start time. Under
# Git Bash `$$` is the MSYS process id (e.g. 27160); the Windows process id is a different number
# (13888, readable at /proc/$$/winpid). OpenProcess() on the MSYS id finds nothing, the witness
# raises "cannot witness lock owner process start", and every `intake poll` returns 73 before one
# instruction is written.
#
# That failure shape is the dangerous one, which is why this refuses to run rather than reporting
# it: with nothing ever ingested, the self-feed trap, the unauthorised-sender refusal, the ASYNC
# filter and the watermark hold all report ok — they are satisfied by an intake that ingests
# nothing — while only the ★ CONTROLs go red. Both intake.sh and runtime-state.py are production
# code and read-only here, so the fix is recorded for review, not faked in the fixture.
if [ "$(uname -s)" != Linux ]; then
  echo "intake: SKIP on $(uname -s) — scripts/intake.sh passes the MSYS \$\$ to runtime-state.py's witnessed lock, which reads it as a Windows PID; every poll refuses with rc=73 before ingesting anything."
  echo "intake: NOT RUN — the whole suite, including the ★ ingest CONTROL, the self-feed trap, sender authorisation, watermark seeding/hold/recovery, gateway-store selection and replay canonicalisation, the release handshake controls, and the crash-replay durability checks."
  exit 0
fi

export RECURSION_NO_NOTIFY=1
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
R="$TMP/root"; mkdir -p "$R/scripts" "$R/state/tasks" "$R/state/assists" \
  "$R/state/project-config" "$R/state/pilots" "$R/loop/logs"
# The staging list is an IMPORT CLOSURE, not a wish list. instruction_routing.py opens with
# `from pilot_instruction import project_window_open_terms`; leaving pilot_instruction.py out
# makes every routing call raise ModuleNotFoundError, intake ingests nothing, and the refusal
# tests below all pass vacuously while the ★ CONTROL is the only thing that goes red. Anything
# added to the staged modules' imports has to be added here too.
for s in assignment.py assignment_contract.py phase5_safety.py assist.sh author.sh board.sh capacity.sh conduct.sh engine_contract.py git-scope.py \
         instruction_routing.py intake.sh jsonl_durable.py notify.sh pilot_instruction.py plan.sh project-pilot.py \
         project-reconcile.py project_authority.py provider-connect-proxy.py provider_contract.py provider-network.sh provider-egress.py publish.sh \
         remote_evidence.py resolve-engine.py resolve-model-alias.py resync.sh run_contract.py runtime-state.py \
         seed_contract.py taskctl.sh transition_receipt.py validate-assignment-result.py validate-control-state.py \
         validate-decision.py validate-gate.py validate-verifier-result.py validate-worker-result.py work.sh assignment-token.py \
         worker-evidence.py worker-sandbox.sh; do
  cp "$SRC/scripts/$s" "$R/scripts/"
  [ -f "$R/scripts/$s" ] || no "staged module $s is missing from $SRC/scripts"
done
chmod +x "$R"/scripts/*.sh 2>/dev/null
: > "$R/.env"; : > "$R/state/INBOX.md"
printf '#project\tpath\tremote\tbranch\tsshkey\troadmap\ttopic\tstatus\n' > "$R/state/portfolio.tsv"
printf 'example-ops\t/opt/example-ops\tgit@github.com:x/c.git\tmain\t-\t-\t10\tactive\n' >> "$R/state/portfolio.tsv"
printf 'example-client\t/opt/example-client\tgit@github.com:x/sample.git\tmain\t-\t-\t6\tactive\n' \
  >> "$R/state/portfolio.tsv"
cat > "$R/state/project-config/example-client.json" <<'JSON'
{"id":"example-client","pilot_id":"pilot-example-client","status":"active","topic":"6"}
JSON
cat > "$R/state/pilots/pilot-example-client.json" <<'JSON'
{"id":"pilot-example-client","project_id":"example-client","status":"active","topic":"6","memory_key":"telegram-topic:6"}
JSON

DB="$TMP/state.db"
LUCAS=8913126684
STRANGER=5550001111

# The real schema, reduced to the columns intake reads.
python3 - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("create table sessions(id text primary key, source text, user_id text, chat_id text, thread_id text)")
c.execute("""create table messages(id integer primary key autoincrement, session_id text,
             role text, content text, timestamp real, platform_message_id text)""")
c.commit()
PY

add(){ # add <session> <source> <user> <chat> <thread> <role> <text>
  python3 - "$DB" "$@" <<'PY'
import sqlite3, sys, time
db, sess, source, uid, chat, thread, role, text = sys.argv[1:9]
c = sqlite3.connect(db)
c.execute("insert or ignore into sessions(id,source,user_id,chat_id,thread_id) values(?,?,?,?,?)",
          (sess, source, uid, chat, None if thread == "-" else thread))
c.execute("insert into messages(session_id,role,content,timestamp,platform_message_id) values(?,?,?,?,?)",
          (sess, role, text, int(time.time()) if source == "telegram" else time.time(), None))
c.commit()
PY
}
replay(){ # replay <source-message-id> <exact|fractional>
  python3 - "$DB" "$@" <<'PY'
import sqlite3, sys, time
db, source_id, mode = sys.argv[1:4]
c = sqlite3.connect(db)
row = c.execute("select session_id,role,content,timestamp,platform_message_id from messages where id=?",
                (source_id,)).fetchone()
if not row:
    raise SystemExit("missing replay source %s" % source_id)
session, role, content, stamp, platform_id = row
if mode == "fractional":
    stamp = time.time() + 0.125
if len(sys.argv) > 4:
    platform_id = sys.argv[4]
cur = c.execute("insert into messages(session_id,role,content,timestamp,platform_message_id) values(?,?,?,?,?)",
                (session, role, content, stamp, platform_id))
c.commit()
print(cur.lastrowid)
PY
}
intake(){ ( cd "$R" && RECURSION_ROOT="$R" RECURSION_HERMES_DB="$DB" \
    RECURSION_INTAKE_ALLOW="$LUCAS" bash scripts/intake.sh "$@" ) >"$TMP/out" 2>&1; echo $?; }
ingested(){ ls "$R/state/instructions"/*.md 2>/dev/null | wc -l | tr -d ' '; }
body_of(){ awk '/^## INSTRUCTION/{f=1;next} f' "$R/state/instructions/$1.md" 2>/dev/null; }

# --- empty first run: marker existence, not numeric zero, distinguishes initialization -------
intake poll >/dev/null
add s-empty telegram "$LUCAS" -1003894008732 10 user "[Lucas Guerra] first message after an empty bootstrap"
intake poll >/dev/null
if [ "$(cat "$R/state/.intake-watermark")" = 1 ] \
   && [ -f "$R/state/instructions/00000001.md" ]; then
  ok "an empty first run does not reseed away the first later Telegram instruction"
else
  no "empty bootstrap dropped the first later instruction (marker=$(cat "$R/state/.intake-watermark") files=$(ingested))"
fi
python3 - "$DB" <<'PY'
import sqlite3,sys
c=sqlite3.connect(sys.argv[1]); c.execute('delete from messages'); c.execute('delete from sessions')
c.execute("delete from sqlite_sequence where name='messages'"); c.commit()
PY
rm -f "$R/state/.intake-watermark" "$R/state/instructions"/*.md \
  "$R/state/instructions"/.intake-anchor-*.json

# --- first run seeds rather than resurrecting history --------------------------------------
add s-dm telegram "$LUCAS" "$LUCAS" - user "[Lucas Guerra] an old message from before intake existed"
intake poll >/dev/null
if [ "$(ingested)" = "0" ] && grep -q 'FIRST RUN' "$TMP/out"; then
  ok "a first run SEEDS the watermark — days of old DMs do not arrive as live orders"
else
  no "first run ingested history: $(ingested) file(s)"
fi

# --- 1) ★ CONTROL: a real message IS ingested ------------------------------------------------
add s-dashboard telegram "$LUCAS" -1003894008732 10 user "[Lucas Guerra] please prioritise the overlay work"
intake poll >/dev/null
if [ "$(ingested)" = "1" ]; then
  ok "★ CONTROL — an authorised Telegram message becomes a durable instruction"
else
  no "★ CONTROL FAILED — nothing was ingested ($(ingested) files); every refusal below is vacuous"
  sed 's/^/        /' "$TMP/out" | head -4
fi

# --- 2) ★ THE SELF-FEED TRAP: our own prompts are not instructions from Lucas ------------------
before="$(ingested)"
add s-cli  cli      -  -  - user "You are HERMES, the executive control plane for the Recursion system..."
add s-sub  subagent -  -  - user "Independently analyze the current Recursion architecture against..."
intake poll >/dev/null
if [ "$(ingested)" = "$before" ]; then
  ok "★ source='cli'/'subagent' rows are IGNORED (the system does not read its own prompts as orders)"
else
  no "★ SELF-FEED — the system ingested its own prompts as instructions from Lucas"
fi

# --- 3) a chat is not an authorisation ---------------------------------------------------------
before="$(ingested)"
add s-other telegram "$STRANGER" -1003894008732 10 user "[Someone Else] delete everything"
intake poll >/dev/null
[ "$(ingested)" = "$before" ] && ok "a message from an unauthorised user id is ignored" \
                              || no "an unauthorised sender was ingested"

# --- 4) the gateway's own async notices are not instructions ------------------------------------
# This leaked in the first dry run: the display-name strip ate "[ASYNC DELEGATION ...]" before the
# marker check ran, so a system notice was about to be filed as an order from Lucas.
before="$(ingested)"
add s-dm telegram "$LUCAS" "$LUCAS" - user "[ASYNC DELEGATION BATCH COMPLETE - deleg_abc]
A background fan-out of 1 subagent(s) you dispatched earlier has finished."
intake poll >/dev/null
[ "$(ingested)" = "$before" ] && ok "the gateway's own ASYNC notices are filtered (raw text is checked FIRST)" \
                              || no "a system notice was filed as an instruction from Lucas"

# --- 5) the watermark: no repeats, and it does not go deaf --------------------------------------
before="$(ingested)"
intake poll >/dev/null
[ "$(ingested)" = "$before" ] && ok "re-polling ingests nothing (the watermark holds)" \
                              || no "re-polling duplicated instructions"
add s-dashboard telegram "$LUCAS" -1003894008732 10 user "[Lucas Guerra] and add an example-site roadmap"
intake poll >/dev/null
if [ "$(ingested)" -gt "$before" ]; then
  ok "★ CONTROL — a NEW message after a quiet poll is still picked up (not deaf)"
else
  no "★ the watermark went deaf: a new message was never ingested"
fi

# --- 5b) an existing corrupt watermark is damage, not a first-run bootstrap -------------------
# Coercing this to zero re-enters the seed path and silently skips authenticated messages that
# arrived after the last durable watermark. Preserve both the marker and the unread instruction.
good_mark="$(cat "$R/state/.intake-watermark")"
printf 'torn' > "$R/state/.intake-watermark"
before="$(ingested)"
add s-dashboard telegram "$LUCAS" -1003894008732 10 user "[Lucas Guerra] retain this after watermark recovery"
set +e
corrupt_rc="$(intake poll)"
# errexit stays OFF: it was never enabled at the top of this file, and turning it on here
# truncated the suite on the first nonzero (the fails counter is the reporting path).
if [ "$corrupt_rc" -ne 0 ] && [ "$(cat "$R/state/.intake-watermark")" = torn ] \
   && [ "$(ingested)" = "$before" ]; then
  ok "an existing corrupt watermark fails closed without skipping an authenticated instruction"
else
  no "corrupt watermark was treated as first-run state (rc=$corrupt_rc marker=$(cat "$R/state/.intake-watermark"))"
fi
printf '%s\n' "$good_mark" > "$R/state/.intake-watermark"
intake poll >/dev/null
[ "$(ingested)" -gt "$before" ] \
  && ok "the held instruction is ingested after the last known-good watermark is restored" \
  || no "watermark recovery lost the held authenticated instruction"

# --- 5c) intake and runtime snapshots share one crash-safe mutation boundary -----------------
if [ ! -d "$R/state/.locks/taskctl.lock" ]; then
  ok "successful intake releases the shared runtime-state boundary"
else
  no "successful intake leaked taskctl.lock and would block the next backup"
fi
intake poll --dry-run >/dev/null
[ ! -d "$R/state/.locks/taskctl.lock" ] \
  && ok "dry-run intake releases the shared runtime-state boundary" \
  || no "dry-run intake leaked taskctl.lock"

mkdir -p "$R/state/.locks/runtime-state.lock"
runtime_mark="$(cat "$R/state/.intake-watermark")"; runtime_before="$(ingested)"
set +e
runtime_rc="$(RECURSION_LOCK_ATTEMPTS=1 RECURSION_LOCK_DELAY_S=0 intake poll)"
# errexit stays OFF (see the first set +e block).
if [ "$runtime_rc" -ne 0 ] && [ "$(cat "$R/state/.intake-watermark")" = "$runtime_mark" ] \
   && [ "$(ingested)" = "$runtime_before" ] && [ ! -d "$R/state/.locks/taskctl.lock" ]; then
  ok "runtime-state maintenance excludes instruction creation and watermark advance"
else
  no "intake overlapped runtime-state maintenance (rc=$runtime_rc) or leaked its lock"
fi
rmdir "$R/state/.locks/runtime-state.lock"

mv "$DB" "$DB.good"; printf 'not-sqlite\n' > "$DB"
set +e
reader_rc="$(intake poll)"
# errexit stays OFF (see the first set +e block).
rm -f "$DB"; mv "$DB.good" "$DB"
if [ "$reader_rc" -ne 0 ] && [ ! -d "$R/state/.locks/taskctl.lock" ]; then
  ok "reader failure releases the shared runtime-state boundary"
else
  no "reader failure returned rc=$reader_rc or leaked taskctl.lock"
fi

# --- 6) an instruction carries complete authenticated routing/correlation metadata ---------------
f="$(ls -1 "$R/state/instructions"/*.md | head -1)"
if grep -q '^principal: 8913126684$' "$f" \
   && grep -q '^topic: 10$' "$f" \
   && grep -q '^project_id: example-ops$' "$f" \
   && grep -q '^reply_correlation: telegram:' "$f" \
   && grep -q '^deduplication_key: telegram:' "$f"; then
  ok "authenticated instruction preserves principal, topic, project, reply, and dedup metadata"
else
  no "instruction routing/correlation metadata is incomplete: $(sed -n '1,12p' "$f" | tr '\n' ' ')"
fi

# --- 7) ★ AN INSTRUCTION IS NOT A COMMAND --------------------------------------------------------
# The whole safety argument rests here: text arrives as data for Hermes to decide on, and the
# actuator whitelist governs execution. A message that reads like a command must do nothing.
add s-dashboard telegram "$LUCAS" -1003894008732 10 user "[Lucas Guerra] publish main and merge everything now"
: > "$TMP/ran.log"
cat > "$R/scripts/publish.sh" <<'STUB'
#!/usr/bin/env bash
printf 'PUBLISH %s\n' "$*" >> "$RAN_LOG"
STUB
chmod +x "$R/scripts/publish.sh"
RAN_LOG="$TMP/ran.log" intake poll >/dev/null
if [ ! -s "$TMP/ran.log" ]; then
  ok "★ a message that READS like a command executes nothing — it is filed for Hermes to decide"
else
  no "★ ARBITRARY EXECUTION — a chat line ran a script: $(cat "$TMP/ran.log")"
fi

# --- 8) even an assist answer stays inert until the sole actuator handles it ----------------------
cat > "$R/state/tasks/sample-q.md" <<'T'
id: sample-q
workstream: sample
goal: blocked on a human
engine: none
acceptance: true
status: blocked
T
( cd "$R" && RECURSION_ROOT="$R" bash scripts/assist.sh open sample-q decision \
    "Is the 0x6c tint a separate pass?" ) >/dev/null 2>&1
add s-dashboard telegram "$LUCAS" -1003894008732 10 user "[Lucas Guerra] assist sample-q Separate pass — see the 0x6c note."
intake poll >/dev/null
ast="$(sed -n 's/^status: *//p' "$R/state/assists/sample-q.md" 2>/dev/null | head -1)"
tst="$(sed -n 's/^status: *//p' "$R/state/tasks/sample-q.md" | head -1)"
latest_instruction="$(ls -1 "$R/state/instructions"/*.md | tail -1)"
if [ "$ast" = "open" ] && [ "$tst" = "blocked" ] \
   && grep -q '^status: open$' "$latest_instruction"; then
  ok "★ assist-shaped Telegram text remains a durable open instruction, not a direct action"
else
  no "★ assist text bypassed intake (instruction=$(sed -n 's/^status: *//p' "$latest_instruction" | head -1) assist=$ast task=$tst)"
fi

# --- 9) ★ a FAILED action must be able to release an instruction it never consumed ------------
# Production wedge, 2026-08-12: an action failed BEFORE its consume handshake, so its instruction
# was still `open` with EMPTY action_ids. `release` read `open` as "a previous release already
# reopened this" and demanded the stored ids equal the ones being released. They could not match.
# release exited 2, recover-only returned 74 where the actuator needs 1, and the same decision was
# retried every ~6s -- 52 iterations before the service was stopped by hand.
mkdir -p "$R/state/actions"
mk_action(){ # mk_action <action-id> <instruction-id> <status>
  python3 - "$R/state/actions/$1.json" "$1" "$2" "$3" <<'PY'
import json, sys
path, aid, iid, status = sys.argv[1:5]
json.dump({"schema_version": 1, "id": aid, "instruction_id": iid, "status": status,
           "verb": "noop", "arguments": [], "goal_id": "g", "attempts": 1,
           "idempotency_key": "k-" + aid, "lease_expires": None,
           "result_evidence": "instruction coupling failed before dispatch"},
          open(path, "w", encoding="utf-8"))
PY
}
rel_field(){ sed -n "s/^$2: *//p" "$R/state/instructions/$(printf '%08d' "$1").md" | head -1; }
rel_run(){ ( cd "$R" && RECURSION_ROOT="$R" bash scripts/intake.sh release "$1" "$2" ) >"$3" 2>&1; echo $?; }

add s-rel telegram "$LUCAS" "$LUCAS" - user "[Lucas Guerra] a question whose action dies before consume"
intake poll >/dev/null
REL_ID="$(basename "$(ls -1 "$R/state/instructions"/*.md | tail -1)" .md | sed 's/^0*//')"
A1=action-1111111111111111111111aa
A2=action-2222222222222222222222bb
mk_action "$A1" "$REL_ID" failed
mk_action "$A2" "$REL_ID" failed

before_ids="$(rel_field "$REL_ID" action_ids)"
rel_rc="$(rel_run "$REL_ID" "$A1" "$TMP/rel1")"
after_ids="$(rel_field "$REL_ID" action_ids)"
after_status="$(rel_field "$REL_ID" status)"
if [ -z "$before_ids" ] && [ "$rel_rc" = 0 ] && [ "$after_ids" = "$A1" ] && [ "$after_status" = open ]; then
  ok "★ a never-consumed instruction is releasable, and records the action that failed"
else
  no "★ never-consumed release (rc=$rel_rc before=[$before_ids] after=[$after_ids] status=$after_status)"
fi

# ★ CONTROL: the refusal this branch exists for must still fire. If admitting the empty case also
# admitted a DIFFERENT action, the fix would have deleted the invariant instead of narrowing it.
rel2_rc="$(rel_run "$REL_ID" "$A2" "$TMP/rel2")"
still_ids="$(rel_field "$REL_ID" action_ids)"
if [ "$rel2_rc" != 0 ] && [ "$still_ids" = "$A1" ] && grep -q 'different action evidence' "$TMP/rel2"; then
  ok "★ CONTROL: a different action still cannot claim an already-released instruction"
else
  no "★ CONTROL leaked — a foreign action claimed the instruction (rc=$rel2_rc ids=[$still_ids])"
fi

# Replay safety: the actuator retries, so the same release must stay a no-op success.
rel3_rc="$(rel_run "$REL_ID" "$A1" "$TMP/rel3")"
if [ "$rel3_rc" = 0 ] && [ "$(rel_field "$REL_ID" action_ids)" = "$A1" ]; then
  ok "releasing the same action again is an idempotent no-op"
else
  no "release is not idempotent on replay (rc=$rel3_rc)"
fi

# --- 10) ★ intake must survive a systemd service environment, which has no HOME ---------------
# hermes-actuator.service sets only RECURSION_ROOT. systemd supplies no HOME, and intake.sh runs
# under `set -u`, so `DB="${RECURSION_HERMES_DB:-$HOME/.hermes/state.db}"` killed the script at
# line 44 -- BEFORE any subcommand ran. consume, release and acknowledge were all equally dead,
# and none of them even uses DB. That is why the actuator could never couple an instruction to an
# action, and why one decision retried forever on 2026-08-12.
#
# Every suite passed while this was broken because nothing ever ran a script without HOME.
nohome_out="$( (cd "$R" && env -u HOME RECURSION_ROOT="$R" bash scripts/intake.sh release "$REL_ID" "$A1") 2>&1 )"
nohome_rc=$?
if [ "$nohome_rc" = 0 ] && ! printf '%s' "$nohome_out" | grep -q 'HOME: unbound variable'; then
  ok "★ intake runs with HOME unset — the environment a systemd service actually has"
else
  no "★ intake dies without HOME (rc=$nohome_rc): $(printf '%s' "$nohome_out" | head -1)"
fi

# --- 11) project intent wins over incidental inflections of control-plane verbs -------------
# Production instruction 707 contained the adjective "deployed" in an otherwise canonical
# project-pilot plan request. The old deploy\w* expression treated that adjective as a deployment
# command and routed the instruction to Head Hermes, which cannot emit pilot-plan-propose.
exact_707='Propose a fresh offline example-client pilot plan to reduce the verified 0x6c shade-tops under-emission in user-client/render, using new v2 plan, goal, and task identities and the deployed required seed, scope, provider, and forced-on gate contracts. Do not reuse or edit the closed v1 plan, goal, task, or window.'
add s-sample telegram "$LUCAS" -1003894008732 6 user "[Lucas Guerra] $exact_707"
intake poll >/dev/null
route_file="$(ls -1 "$R/state/instructions"/*.md | tail -1)"
if grep -q '^project_id: example-client$' "$route_file" \
   && grep -q '^routing_target: pilot-example-client$' "$route_file"; then
  ok "exact instruction 707 wording routes to the bound project pilot"
else
  no "exact instruction 707 wording was hijacked by incidental 'deployed' text: $(sed -n 's/^routing_target: *//p' "$route_file")"
fi

# Positive control: tightening adjective handling must not send real deployment/policy commands
# to a project pilot. These remain Head-Hermes authority.
add s-sample telegram "$LUCAS" -1003894008732 6 user \
  "[Lucas Guerra] Deploy the reviewed recursion change and preserve deployment policy."
intake poll >/dev/null
control_file="$(ls -1 "$R/state/instructions"/*.md | tail -1)"
if grep -q '^routing_target: head-hermes$' "$control_file"; then
  ok "genuine deployment and policy intent still routes to Head Hermes"
else
  no "deployment/policy authority leaked to a project pilot: $(sed -n 's/^routing_target: *//p' "$control_file")"
fi

# The route is grammar-shaped, not a keyword blacklist: project plan prose may describe Head-owned
# concerns, while command and requirement forms (including deploy inflections) remain Head-owned.
incidental='Propose a fresh pilot plan documenting assist workflow, escalation policy, portfolio reporting, and previously merged/deployed evidence.'
add s-sample telegram "$LUCAS" -1003894008732 6 user "[Lucas Guerra] $incidental"
intake poll >/dev/null
incidental_file="$(ls -1 "$R/state/instructions"/*.md | tail -1)"
if grep -q '^routing_target: pilot-example-client$' "$incidental_file"; then
  ok "incidental Head-owned nouns in project-plan prose do not hijack pilot routing"
else
  no "incidental authority prose hijacked pilot routing: $(sed -n 's/^routing_target: *//p' "$incidental_file")"
fi

for command in \
  'Deployments must preserve the reviewed production gate.' \
  'Redeploy the reviewed renderer fix.' \
  'Deploys the reviewed provider change after Lucas approval.' \
  'Deployed the reviewed fix; inspect the production result.' \
  'Review the credential policy for this project.'; do
  add s-sample telegram "$LUCAS" -1003894008732 6 user "[Lucas Guerra] $command"
  intake poll >/dev/null
  command_file="$(ls -1 "$R/state/instructions"/*.md | tail -1)"
  if grep -q '^routing_target: head-hermes$' "$command_file"; then
    ok "Head-Hermes command form remains Head-routed: $command"
  else
    no "Head-Hermes command leaked to pilot: $command"
  fi
done

# --- 12) gateway-store selection and session replay canonicalisation ------------------------
# The production gateway runs with HERMES_HOME=/var/lib/hermes-gateway and writes state.db at
# that root. Root cron has a different HOME. After a gateway compaction, historical Telegram
# rows are copied back into the active session with new database ids; the real platform rows
# retain integral Telegram timestamps, while internal rehydration can also append a fractional
# timestamp copy. Intake must select the gateway store, suppress both replay shapes, and preserve
# the first real post-watermark row as the one durable instruction.
prior_file="$(ls -1 "$R/state/instructions"/*.md | head -1)"
prior_id="$(basename "$prior_file" .md | sed 's/^0*//')"
stale_replay_id="$(replay "$prior_id" exact)"
fresh_request='Propose one fresh bounded offline renderer plan for example-client with one renderer-only task and new v2 identities.'
add s-sample telegram "$LUCAS" -1003894008732 6 user "[Lucas Guerra] $fresh_request"
fresh_id="$(python3 - "$DB" <<'PY'
import sqlite3,sys
print(sqlite3.connect(sys.argv[1]).execute("select max(id) from messages").fetchone()[0])
PY
)"
fractional_replay_id="$(replay "$fresh_id" fractional)"
replay_before="$(ingested)"
intake poll >/dev/null
replay_after="$(ingested)"
fresh_file="$R/state/instructions/$(printf '%08d' "$fresh_id").md"
stale_file="$R/state/instructions/$(printf '%08d' "$stale_replay_id").md"
fractional_file="$R/state/instructions/$(printf '%08d' "$fractional_replay_id").md"
if [ "$replay_after" -eq $((replay_before + 1)) ] \
   && [ -f "$fresh_file" ] && [ ! -e "$stale_file" ] && [ ! -e "$fractional_file" ] \
   && grep -q '^routing_target: pilot-example-client$' "$fresh_file" \
   && [ "$(cat "$R/state/.intake-watermark")" = "$fractional_replay_id" ]; then
  ok "gateway compaction replay yields exactly one canonical project instruction"
else
  no "gateway replay was not canonical (before=$replay_before after=$replay_after fresh=$fresh_id stale=$stale_replay_id fractional=$fractional_replay_id watermark=$(cat "$R/state/.intake-watermark"))"
fi

anchor_file="$R/state/instructions/.intake-anchor-$(printf '%08d' "$fractional_replay_id").json"
WRONGDB="$TMP/wrong-store.db"
cp "$DB" "$WRONGDB"
python3 - "$WRONGDB" "$fractional_replay_id" <<'PY'
import sqlite3,sys
c=sqlite3.connect(sys.argv[1]); c.execute("update messages set content='different source row' where id=?",(sys.argv[2],)); c.commit()
PY
anchor_mark_before="$(cat "$R/state/.intake-watermark")"
set +e
( cd "$R" && RECURSION_ROOT="$R" RECURSION_HERMES_DB="$WRONGDB" \
  RECURSION_INTAKE_ALLOW="$LUCAS" bash scripts/intake.sh poll --dry-run ) >"$TMP/wrong-store.out" 2>&1
wrong_store_rc=$?
# errexit stays OFF (see the first set +e block).
if [ -f "$anchor_file" ] && [ "$wrong_store_rc" -ne 0 ] \
   && [ "$(cat "$R/state/.intake-watermark")" = "$anchor_mark_before" ]; then
  ok "a skipped high-water row still leaves a durable store-identity witness"
else
  no "skipped watermark row did not bind the source store (anchor=$([ -f "$anchor_file" ] && echo yes || echo no) rc=$wrong_store_rc)"
fi

# Compatibility control: legacy gateway schemas without platform_message_id remain readable.
R_NO_PLATFORM="$TMP/root-no-platform"; cp -a "$R" "$R_NO_PLATFORM"
NO_PLATFORM_DB="$TMP/no-platform.db"
no_platform_id="$(python3 - "$DB" "$NO_PLATFORM_DB" <<'PY'
import sqlite3,sys,time
src=sqlite3.connect(sys.argv[1]); dst=sqlite3.connect(sys.argv[2])
dst.execute("create table sessions(id text primary key, source text, user_id text, chat_id text, thread_id text)")
dst.execute("create table messages(id integer primary key autoincrement, session_id text, role text, content text, timestamp real)")
dst.executemany("insert into sessions(id,source,user_id,chat_id,thread_id) values(?,?,?,?,?)",
                src.execute("select id,source,user_id,chat_id,thread_id from sessions"))
dst.executemany("insert into messages(id,session_id,role,content,timestamp) values(?,?,?,?,?)",
                src.execute("select id,session_id,role,content,timestamp from messages"))
cur=dst.execute("insert into messages(session_id,role,content,timestamp) values(?,?,?,?)",
                ('s-sample','user','[Lucas Guerra] schema compatibility control',int(time.time())+3))
dst.commit(); print(cur.lastrowid)
PY
)"
set +e
( cd "$R_NO_PLATFORM" && RECURSION_ROOT="$R_NO_PLATFORM" RECURSION_HERMES_DB="$NO_PLATFORM_DB" \
  RECURSION_INTAKE_ALLOW="$LUCAS" bash scripts/intake.sh poll ) >"$TMP/no-platform.out" 2>&1
no_platform_rc=$?
# errexit stays OFF (see the first set +e block).
if [ "$no_platform_rc" = 0 ] \
   && [ -f "$R_NO_PLATFORM/state/instructions/$(printf '%08d' "$no_platform_id").md" ]; then
  ok "legacy gateway schema without platform_message_id remains supported"
else
  no "legacy gateway schema compatibility failed (rc=$no_platform_rc id=$no_platform_id)"
fi

# Stable platform ids deduplicate directly. During a schema transition, a populated replay also
# carries the legacy timestamp/content alias so it cannot duplicate an earlier NULL-id row.
add s-sample telegram "$LUCAS" -1003894008732 6 user "[Lucas Guerra] stable platform replay control"
platform_id="$(python3 - "$DB" <<'PY'
import sqlite3,sys
c=sqlite3.connect(sys.argv[1]); mid=c.execute('select max(id) from messages').fetchone()[0]
c.execute("update messages set platform_message_id='telegram-message-1001' where id=?",(mid,)); c.commit(); print(mid)
PY
)"
platform_replay_id="$(replay "$platform_id" exact)"
platform_before="$(ingested)"; intake poll >/dev/null; platform_after="$(ingested)"
if [ "$platform_after" -eq $((platform_before + 1)) ] \
   && [ -f "$R/state/instructions/$(printf '%08d' "$platform_id").md" ] \
   && [ ! -e "$R/state/instructions/$(printf '%08d' "$platform_replay_id").md" ]; then
  ok "a non-null platform message id is exact-once across replay"
else
  no "stable platform id replay duplicated (before=$platform_before after=$platform_after)"
fi

add s-sample telegram "$LUCAS" -1003894008732 6 user "[Lucas Guerra] mixed platform replay control"
mixed_id="$(python3 - "$DB" <<'PY'
import sqlite3,sys
print(sqlite3.connect(sys.argv[1]).execute('select max(id) from messages').fetchone()[0])
PY
)"
mixed_replay_id="$(replay "$mixed_id" exact telegram-message-1002)"
mixed_before="$(ingested)"; intake poll >/dev/null; mixed_after="$(ingested)"
if [ "$mixed_after" -eq $((mixed_before + 1)) ] \
   && [ -f "$R/state/instructions/$(printf '%08d' "$mixed_id").md" ] \
   && [ ! -e "$R/state/instructions/$(printf '%08d' "$mixed_replay_id").md" ]; then
  ok "mixed NULL/non-null platform ids share a legacy replay alias"
else
  no "mixed platform-id replay duplicated (before=$mixed_before after=$mixed_after)"
fi

# Two authoritative platform ids are two messages even when Telegram timestamps/content match.
add s-sample telegram "$LUCAS" -1003894008732 6 user "[Lucas Guerra] two distinct same-second platform messages"
distinct_platform_one="$(python3 - "$DB" <<'PY'
import sqlite3,sys
c=sqlite3.connect(sys.argv[1]); mid=c.execute('select max(id) from messages').fetchone()[0]
c.execute("update messages set platform_message_id='telegram-message-2001' where id=?",(mid,)); c.commit(); print(mid)
PY
)"
distinct_platform_two="$(replay "$distinct_platform_one" exact telegram-message-2002)"
distinct_before="$(ingested)"; intake poll >/dev/null; distinct_after="$(ingested)"
if [ "$distinct_after" -eq $((distinct_before + 2)) ] \
   && [ -f "$R/state/instructions/$(printf '%08d' "$distinct_platform_one").md" ] \
   && [ -f "$R/state/instructions/$(printf '%08d' "$distinct_platform_two").md" ]; then
  ok "distinct non-null platform ids survive same-second identical content"
else
  no "authoritative platform ids collapsed distinct messages (before=$distinct_before after=$distinct_after)"
fi

mkdir -p "$TMP/gateway-home" "$TMP/cron-home"
GWDB="$TMP/gateway-home/state.db"
cp "$DB" "$GWDB"
R_GATEWAY_DEFAULT="$TMP/root-gateway-default"; cp -a "$R" "$R_GATEWAY_DEFAULT"
gateway_fresh_id="$(python3 - "$GWDB" "$LUCAS" <<'PY'
import sqlite3,sys,time
db,uid=sys.argv[1:3]
c=sqlite3.connect(db)
c.execute("insert or ignore into sessions(id,source,user_id,chat_id,thread_id) values(?,?,?,?,?)",
          ('s-gateway-live','telegram',uid,'-1003894008732','6'))
cur=c.execute("insert into messages(session_id,role,content,timestamp,platform_message_id) values(?,?,?,?,?)",
              ('s-gateway-live','user','[Lucas Guerra] verify the live gateway store',int(time.time())+2,None))
c.commit(); print(cur.lastrowid)
PY
)"
gateway_file="$R_GATEWAY_DEFAULT/state/instructions/$(printf '%08d' "$gateway_fresh_id").md"
gateway_rc="$( ( cd "$R_GATEWAY_DEFAULT" && HOME="$TMP/cron-home" HERMES_HOME="$TMP/gateway-home" \
  RECURSION_ROOT="$R_GATEWAY_DEFAULT" RECURSION_INTAKE_ALLOW="$LUCAS" env -u RECURSION_HERMES_DB \
  bash scripts/intake.sh poll ) >"$TMP/gateway-default.out" 2>&1; echo $? )"
if [ "$gateway_rc" = 0 ] && [ -f "$gateway_file" ]; then
  ok "default intake follows HERMES_HOME/state.db instead of root cron HOME"
else
  no "default intake missed the live gateway store (rc=$gateway_rc id=$gateway_fresh_id): $(head -1 "$TMP/gateway-default.out")"
fi

# A crash after the instruction fsync but before watermark commit leaves a valid durable file.
# Retry must verify its immutable source identity and preserve lifecycle changes byte-for-byte.
crash_mark="$(cat "$R/state/.intake-watermark")"
add s-sample telegram "$LUCAS" -1003894008732 6 user "[Lucas Guerra] preserve this instruction across intake crash replay"
crash_id="$(python3 - "$DB" <<'PY'
import sqlite3,sys
print(sqlite3.connect(sys.argv[1]).execute('select max(id) from messages').fetchone()[0])
PY
)"
crash_rc="$(RECURSION_INTAKE_FAILPOINT=after-instruction-fsync intake poll)"
crash_file="$R/state/instructions/$(printf '%08d' "$crash_id").md"
# Old implementations lack the failpoint. Rewind only the disposable fixture to reproduce the
# same crash boundary so the overwrite assertion below still proves red.
[ "$crash_rc" != 0 ] || printf '%s\n' "$crash_mark" > "$R/state/.intake-watermark"
if [ -f "$crash_file" ]; then
  python3 - "$crash_file" <<'PY'
import sys
p=sys.argv[1]; text=open(p,encoding='utf-8').read()
text=text.replace('status: open','status: consumed',1)
text=text.replace('action_ids: ','action_ids: action-aaaaaaaaaaaaaaaaaaaaaaaa',1)
text += '\n## CONSUMED 2026-08-14T09:00:00Z\nresulting actions: action-aaaaaaaaaaaaaaaaaaaaaaaa\n'
open(p,'w',encoding='utf-8',newline='\n').write(text)
PY
fi
crash_sha_before="$(sha256sum "$crash_file" 2>/dev/null | awk '{print $1}')"
retry_rc="$(intake poll)"
crash_sha_after="$(sha256sum "$crash_file" 2>/dev/null | awk '{print $1}')"
if [ "$crash_rc" -ne 0 ] && [ "$retry_rc" = 0 ] \
   && [ "$(cat "$R/state/.intake-watermark")" = "$crash_id" ] \
   && [ -n "$crash_sha_before" ] && [ "$crash_sha_after" = "$crash_sha_before" ]; then
  ok "crash replay preserves an existing instruction and its lifecycle byte-for-byte"
else
  no "crash replay overwrote durable state (crash_rc=$crash_rc retry_rc=$retry_rc marker=$(cat "$R/state/.intake-watermark") hashes=$crash_sha_before/$crash_sha_after)"
fi

# A conflicting same-id file is corruption, never permission to overwrite source evidence.
conflict_mark="$(cat "$R/state/.intake-watermark")"
add s-sample telegram "$LUCAS" -1003894008732 6 user "[Lucas Guerra] conflicting source identity control"
conflict_id="$(python3 - "$DB" <<'PY'
import sqlite3,sys
print(sqlite3.connect(sys.argv[1]).execute('select max(id) from messages').fetchone()[0])
PY
)"
conflict_file="$R/state/instructions/$(printf '%08d' "$conflict_id").md"
printf 'schema_version: 1\nid: %s\nprincipal: 8913126684\nchat: -1003894008732\nthread: 6\nstatus: acknowledged\n\n## INSTRUCTION\ndifferent body\n' \
  "$conflict_id" > "$conflict_file"
conflict_sha_before="$(sha256sum "$conflict_file" | awk '{print $1}')"
conflict_rc="$(intake poll)"
conflict_sha_after="$(sha256sum "$conflict_file" | awk '{print $1}')"
if [ "$conflict_rc" -ne 0 ] && [ "$(cat "$R/state/.intake-watermark")" = "$conflict_mark" ] \
   && [ "$conflict_sha_after" = "$conflict_sha_before" ]; then
  ok "conflicting pre-existing instruction fails closed without watermark movement"
else
  no "conflicting instruction was overwritten or skipped (rc=$conflict_rc marker=$(cat "$R/state/.intake-watermark") hashes=$conflict_sha_before/$conflict_sha_after)"
fi

echo
[ "$fails" -eq 0 ] && { echo "intake: ALL PASS"; exit 0; }
echo "intake: $fails FAILED"; exit 1
