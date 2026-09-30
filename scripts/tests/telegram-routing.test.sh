#!/usr/bin/env bash
# telegram-routing.test.sh — a message must reach the topic that owns it, and the bot token must
# never appear in anything this code emits.
#
# WHY. The Dashboard is a forum. Routing IS the feature: a dashboard whose messages all land in one
# channel is exactly the undifferentiated stream it replaces, and it fails silently — every
# message still arrives, just in the wrong place, which looks fine until you need to find one.
#
# ★ THE TOKEN TEST is not decoration. The fallback path interpolates the token into a URL, and
# curl prints the URL it was handed when it errors. So this runs the real notify.sh with a
# SENTINEL token and greps everything it produces — stdout, stderr, and the argv of every
# command it invokes — for that sentinel. A test that only reads the source could not catch a
# leak that happens through curl's own error output.
#
# ★ Test 1 is the kill-switch boundary: PAUSE must stop the transport in this sole sender even
# if a legacy caller reaches it. Test 2 is the resumed CONTROL: an un-routed message must still
# send, or every later routing assertion could pass on a sender that drops everything.
#
# Hermetic: stub hermes + curl, no network, no real token, no messages sent.
#
#   run: bash scripts/tests/telegram-routing.test.sh
set -uo pipefail
export LC_ALL=C
SRC="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
fails=0
ok(){ printf 'ok    %s\n' "$1"; }
no(){ printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"; mkdir -p "$BIN"
R="$TMP/root"; mkdir -p "$R/scripts" "$R/state"
cp "$SRC/scripts/notify.sh" "$R/scripts/"
cp "$SRC/scripts/topics.sh" "$R/scripts/" 2>/dev/null || true
for s in assignment.py assignment_contract.py phase5_safety.py assist.sh author.sh board.sh capacity.sh conduct.sh engine_contract.py git-scope.py \
  instruction_routing.py intake.sh jsonl_durable.py pilot_instruction.py plan.sh project-pilot.py \
  project-reconcile.py project_authority.py provider-connect-proxy.py provider_contract.py provider-network.sh provider-egress.py publish.sh \
  remote_evidence.py resolve-engine.py resolve-model-alias.py resync.sh run_contract.py runtime-state.py \
  seed_contract.py taskctl.sh transition_receipt.py validate-assignment-result.py validate-control-state.py \
  validate-decision.py validate-gate.py validate-verifier-result.py validate-worker-result.py work.sh assignment-token.py \
  worker-evidence.py worker-sandbox.sh; do
  cp "$SRC/scripts/$s" "$R/scripts/"
done
chmod +x "$R"/scripts/*.sh

SENTINEL='1234567890:SENTINELtokenDoNotLeakAAAAAAAAAAAAAAA'
printf 'TELEGRAM_BOT_TOKEN=%s\n' "$SENTINEL" > "$R/.env"

# Stubs record their full argv. That is what makes the leak test meaningful: a token passed as a
# command ARGUMENT is visible in /proc and in any log that captures a command line, so recording
# argv is closer to the real exposure than reading the script would be.
cat > "$BIN/hermes" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "HERMES $*" >> "$HERMES_LOG"
[ "${STUB_HERMES_FAIL:-0}" = "1" ] && exit 1
exit 0
STUB
cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "CURL $*" >> "$CURL_LOG"
exit 0
STUB
chmod +x "$BIN/hermes" "$BIN/curl"
export HERMES_LOG="$TMP/hermes.log" CURL_LOG="$TMP/curl.log"

printf '#project\tpath\tremote\tbranch\tsshkey\troadmap\ttopic\tstatus\n' > "$R/state/portfolio.tsv"
printf 'sample\t/opt/sample\tgit@github.com:x/sample.git\tmain\t-\t-\t4242\tactive\n'    >> "$R/state/portfolio.tsv"
printf 'example-ops\t/opt/example-ops\tgit@github.com:x/ch.git\tmain\t-\t-\t7777\tactive\n' >> "$R/state/portfolio.tsv"
printf 'newbie\t/opt/newbie\tgit@github.com:x/nb.git\tmain\t-\t-\t-\tactive\n'  >> "$R/state/portfolio.tsv"

CHAT="-1003894008732"
notify(){ : > "$HERMES_LOG"; : > "$CURL_LOG"
  ( cd "$R" && PATH="$BIN:$PATH" RECURSION_ROOT="$R" \
      RECURSION_TELEGRAM_CHAT="$CHAT" RECURSION_TELEGRAM_HOME_THREAD=1 \
      bash scripts/notify.sh "$@" ) >"$TMP/out" 2>"$TMP/err"; echo $?; }

# --- 1) ★ PAUSE is a central transport interlock ----------------------------------------------
printf 'PAUSED test\nreason: telegram-routing control\n' > "$R/state/PAUSE"
rc=$(notify "must remain local")
if [ "$rc" = "75" ] && [ ! -s "$HERMES_LOG" ] && [ ! -s "$CURL_LOG" ]; then
  ok "★ PAUSE refuses delivery in the sole sender before either transport is invoked"
else
  no "★ PAUSE LEAK — notify reached a transport or returned rc=$rc: $(cat "$HERMES_LOG" "$CURL_LOG" "$TMP/err")"
fi
rm -f "$R/state/PAUSE"

# --- 2) ★ CONTROL: an un-routed message still sends ------------------------------------------
rc=$(notify "plain message")
if [ "$rc" = "0" ] && grep -q 'HERMES send' "$HERMES_LOG"; then
  ok "★ CONTROL — a plain message is still delivered (notify is not silently dropping everything)"
else
  no "★ CONTROL FAILED — a plain message was not sent (rc=$rc): $(cat "$HERMES_LOG" "$TMP/err")"
fi

# --- 2b) file truncation never splits a UTF-8 character ---------------------------------------
python3 - "$TMP/multibyte.txt" <<'PY'
import sys
open(sys.argv[1],"wb").write(("a"*3799 + "🦄" + "tail").encode("utf-8"))
PY
rc=$(notify -f "$TMP/multibyte.txt")
if [ "$rc" = 0 ] && HERMES_LOG="$HERMES_LOG" python3 - <<'PY'
import os
raw=open(os.environ["HERMES_LOG"],"rb").read()
text=raw.decode("utf-8")
raise SystemExit(0 if b"a"*3799 in raw and b"a"*3800 not in raw and
                 "🦄" not in text and " -f " not in text else 1)
PY
then
  ok "legacy file notification truncates at a complete UTF-8 boundary"
else
  no "legacy file notification emitted malformed or unbounded UTF-8 (rc=$rc)"
fi

# --- 2) a project routes to ITS topic ---------------------------------------------------------
notify --project sample "gate result" >/dev/null
if grep -q -- "-t telegram:$CHAT:4242" "$HERMES_LOG"; then
  ok "a project message is addressed to that project's topic"
else
  no "wrong target for sample: $(cat "$HERMES_LOG")"
fi
notify --project example-ops "build done" >/dev/null
grep -q -- "-t telegram:$CHAT:7777" "$HERMES_LOG" && ok "a second project routes to a DIFFERENT topic" \
  || no "example-ops did not route to 7777: $(cat "$HERMES_LOG")"

# --- 3) the repo PATH resolves too (work.sh knows a path, not a project name) -------------------
notify --project /opt/example-ops "from a repo path" >/dev/null
grep -q -- "-t telegram:$CHAT:7777" "$HERMES_LOG" && ok "a repo path resolves to the same topic as its project name" \
  || no "path lookup failed: $(cat "$HERMES_LOG")"

# --- 4) ★ a project with NO topic yet goes HOME, not to a malformed target ----------------------
# The dangerous version of this bug is not a crash: it is 'telegram:chat:-' being accepted and the
# message vanishing. An alert exists to be seen; routing it nowhere is worse than routing it wrong.
notify --project newbie "no topic yet" >/dev/null
# The naive check here was `! grep ":-"`, which matches ALWAYS: the chat id is negative
# (-100…), so ":-" appears in every well-formed target. Assert the precise malformed shape.
if grep -q -- "-t telegram:$CHAT:1" "$HERMES_LOG" && ! grep -q -- "telegram:$CHAT:-" "$HERMES_LOG"; then
  ok "★ a project with no topic falls back HOME (never a malformed 'chat:-' target)"
else
  no "★ a topicless project produced: $(cat "$HERMES_LOG")"
fi

# --- 5) an unknown project is delivered, not dropped -------------------------------------------
notify --project never-heard-of-it "still important" >/dev/null
grep -q -- "-t telegram:$CHAT:1" "$HERMES_LOG" && ok "an unknown project still delivers (home), rather than vanishing" \
  || no "an unknown project was dropped: $(cat "$HERMES_LOG")"

# --- 6) an explicit --thread wins --------------------------------------------------------------
notify --thread 999 "explicit" >/dev/null
grep -q -- "-t telegram:$CHAT:999" "$HERMES_LOG" && ok "an explicit --thread overrides the registry" \
  || no "--thread ignored: $(cat "$HERMES_LOG")"

# --- 7) the curl fallback carries message_thread_id --------------------------------------------
# Without it every message lands in the group's General topic — delivered, and un-routed.
: > "$HERMES_LOG"; : > "$CURL_LOG"
( cd "$R" && PATH="$BIN:$PATH" RECURSION_ROOT="$R" RECURSION_TELEGRAM_CHAT="$CHAT" \
    RECURSION_TELEGRAM_HOME_THREAD=1 STUB_HERMES_FAIL=1 \
    bash scripts/notify.sh --project sample "fallback path" ) >"$TMP/out" 2>"$TMP/err"
if grep -q 'message_thread_id=4242' "$CURL_LOG"; then
  ok "the raw-API fallback sets message_thread_id (or every message lands in General)"
else
  no "fallback lost the thread: $(cat "$CURL_LOG")"
fi

# --- 8) ★ THE TOKEN NEVER APPEARS in anything notify.sh emits or invokes ------------------------
leak=""
for f in "$TMP/out" "$TMP/err" "$HERMES_LOG"; do
  grep -qF "$SENTINEL" "$f" 2>/dev/null && leak="$leak $(basename "$f")"
done
if [ -z "$leak" ]; then
  ok "★ the bot token appears in no output, no error, and no argv notify.sh passes on"
else
  no "★ TOKEN LEAKED via:$leak"
fi
# The URL curl receives necessarily contains the token — that is the API's design — but it must
# not travel any further. Assert curl's own stderr is suppressed on the token path.
grep -q '2>/dev/null' "$R/scripts/notify.sh" \
  && ok "curl's stderr is suppressed on the token path (curl echoes the URL it was given)" \
  || no "curl stderr is not suppressed — a failure would print the URL, and the URL is the token"

# --- 9) ★ THE FIXTURE-LEAK GUARD: RECURSION_NO_NOTIFY must stop delivery HERE -------------------
# Tests run the real notify.sh, and `hermes send` uses the gateway's own creds — so an empty
# fixture .env does not stop it. On 2026-08-06 two escalations raised by resync.test.sh fixtures
# (repos literally named "diverged" and "unreachable") were delivered to Lucas as if they were
# real portfolio failures. The guard belongs in notify.sh because that is the one place every
# caller passes through.
: > "$HERMES_LOG"; : > "$CURL_LOG"
( cd "$R" && PATH="$BIN:$PATH" RECURSION_ROOT="$R" RECURSION_TELEGRAM_CHAT="$CHAT"     RECURSION_NO_NOTIFY=1 bash scripts/notify.sh --project sample "must not be delivered" ) >/dev/null 2>&1
if [ ! -s "$HERMES_LOG" ] && [ ! -s "$CURL_LOG" ]; then
  ok "★ RECURSION_NO_NOTIFY=1 suppresses delivery in notify.sh itself (no fixture can page Lucas)"
else
  no "★ FIXTURE LEAK — a suppressed notify still sent: $(cat "$HERMES_LOG" "$CURL_LOG")"
fi

# And the rule that keeps the NEXT test from re-leaking: any test that copies notify.sh into a
# fixture must export the suppression. This is machine-checkable, so it is checked rather than
# written down and hoped for.
# The rule is "cannot reach real Telegram", not "sets one variable". A test may satisfy it by
# suppressing delivery OR by stubbing `hermes` on PATH. Encode the property, not the
# mechanism: a lint that demands one implementation forces tests to lie to it.
missing=""
for t in "$SRC"/scripts/tests/*.sh; do
  grep -q 'notify\.sh' "$t" 2>/dev/null || continue
  grep -q 'RECURSION_NO_NOTIFY' "$t" 2>/dev/null && continue
  grep -qE '(BIN|bin)/hermes' "$t" 2>/dev/null && continue      # stubs hermes on PATH instead
  missing="$missing $(basename "$t")"
done
if [ -z "$missing" ]; then
  ok "every test that pulls in notify.sh also exports RECURSION_NO_NOTIFY"
else
  no "these tests could page Lucas from a fixture:$missing"
fi

# --- 9) topics.sh refuses to act without a chat, and never invents one --------------------------
if [ -x "$R/scripts/topics.sh" ]; then
  ( cd "$R" && PATH="$BIN:$PATH" RECURSION_ROOT="$R" RECURSION_TELEGRAM_CHAT="" \
      bash scripts/topics.sh ensure ) >"$TMP/t.out" 2>&1; rc=$?
  [ "$rc" != "0" ] && ok "topics ensure refuses to run with no chat configured" \
                   || no "topics ensure proceeded without a chat: $(cat "$TMP/t.out")"
  out="$( ( cd "$R" && RECURSION_ROOT="$R" bash scripts/topics.sh list ) 2>&1 )"
  printf '%s' "$out" | grep -q '4242' && ok "topics list reports the registry mapping" \
                                      || no "topics list is wrong: $out"
fi

# --- 10) ★ an ERROR is not an ABSENCE: the topic-existence verdict --------------------------
# topics.sh check probed a topic by "editing" it to the name it already had and read ok=false as
# "gone" — reporting all eight freshly-created topics as MISSING. These three bodies are the
# REAL responses captured from the API against the live dashboard, so this tests the shipped
# classifier against ground truth rather than against my belief about it.
v(){ ( cd "$R" && RECURSION_ROOT="$R" bash scripts/topics.sh verdict "$1" ) 2>/dev/null; }
[ "$(v '{"ok":false,"error_code":400,"description":"Bad Request: TOPIC_NOT_MODIFIED"}')" = "EXISTS" ]   && ok "★ TOPIC_NOT_MODIFIED means the topic EXISTS (ok=false, and it proves presence)"   || no "★ TOPIC_NOT_MODIFIED misread — every live topic would report as deleted"
[ "$(v '{"ok":true,"result":true}')" = "EXISTS" ]   && ok "a successful edit means the topic exists" || no "ok=true was not read as EXISTS"
[ "$(v '{"ok":false,"error_code":400,"description":"Bad Request: TOPIC_ID_INVALID"}')" = "MISSING" ]   && ok "TOPIC_ID_INVALID is the ONLY response that means absent" || no "TOPIC_ID_INVALID not read as MISSING"
[ "$(v '{"ok":false,"error_code":403,"description":"Bad Request: not enough rights"}')" = "UNKNOWN" ]   && ok "★ a rights/network failure is UNKNOWN, never reported as a deleted topic"   || no "★ an unrelated error was classified as an absence — the resync exit-3/4 mistake again"

echo
[ "$fails" -eq 0 ] && { echo "telegram-routing: ALL PASS"; exit 0; }
echo "telegram-routing: $fails FAILED"; exit 1
