#!/usr/bin/env bash
# assist.test.sh — a human answer must actually REACH the arm that asked for it, and unblock
# exactly the one task it answers.
#
# WHY. The escalation loop was one-directional: an arm could stop with a precise question, and a
# human could close or requeue it, but the ANSWER landed in state/INBOX.md — a file the arm never
# reads. The next run then produced the identical prompt and the identical block. Nothing in the
# system could tell that apart from progress, which is why the SAMPLE pilot (whose blockers are all
# "only Lucas can supply this") could not run.
#
# ★ Test 1 is the CONTROL and it is the whole point: the answer TEXT must appear in the arm's
#   prompt. Every other assertion here would pass on a seam that records answers beautifully and
#   delivers none of them.
#
# Hermetic: a stub arm that blocks on demand, a local origin, no model call, no spend.
#
#   run: bash scripts/tests/assist.test.sh
set -uo pipefail
export LC_ALL=C

# This suite drives the live loop arms (flock, procfs, POSIX lock semantics) — off-box it can only
# produce noise, so it reports an explicit SKIP. The box baseline runs it for real.
if [ "$(uname -s)" != "Linux" ]; then
  echo "assist: SKIP (Linux loop runtime required — flock/procfs/POSIX lock semantics; certified in the box suite)"
  exit 0
fi

SRC="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
fails=0
ok(){ printf 'ok    %s\n' "$1"; }
no(){ printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"; mkdir -p "$BIN"
R="$TMP/repo"; mkdir -p "$R/scripts/tests" "$R/state/tasks" "$R/state/assists" "$R/loop/logs" "$R/app"
for s in assignment.py assignment_contract.py assist.sh author.sh board.sh capacity.sh conduct.sh engine_contract.py git-scope.py \
  instruction_routing.py intake.sh jsonl_durable.py notify.sh phase5_safety.py pilot_instruction.py plan.sh project-pilot.py \
  project-reconcile.py project_authority.py provider-connect-proxy.py provider_contract.py provider-network.sh provider-egress.py publish.sh \
  remote_evidence.py resolve-engine.py resolve-model-alias.py resync.sh run_contract.py runtime-state.py \
  seed_contract.py taskctl.sh transition_receipt.py validate-assignment-result.py validate-control-state.py \
  validate-decision.py validate-gate.py validate-verifier-result.py validate-worker-result.py work.sh assignment-token.py \
  worker-evidence.py worker-sandbox.sh; do
  cp "$SRC/scripts/$s" "$R/scripts/"
done
chmod +x "$R"/scripts/*.sh 2>/dev/null
: > "$R/.env"; : > "$R/state/INBOX.md"; echo anchor > "$R/ANCHOR.md"; echo seed > "$R/app/seed.txt"
export RECURSION_NO_NOTIFY=1

# The stub arm records the prompt it was handed, then writes whatever .recursion-result.json the
# case under test wants. Recording the PROMPT is what makes test 1 possible: the question is not
# "was the answer stored" but "did the arm see it".
cat > "$BIN/claude" <<'STUB'
#!/usr/bin/env bash
prompt=""
while [ $# -gt 0 ]; do case "$1" in -p) prompt="$2"; shift 2;; *) shift;; esac; done
printf '%s' "$prompt" > "${STUB_PROMPT_FILE:-/dev/null}"
[ -n "${STUB_RESULT:-}" ] && printf '%s' "$STUB_RESULT" > .recursion-result.json
exit 0
STUB
chmod +x "$BIN/claude"

GIT="git -c user.name=t -c user.email=t@t -c init.defaultBranch=main"
ORIGIN="$TMP/origin.git"; $GIT init -q --bare "$ORIGIN"
( cd "$R" && $GIT init -q . && $GIT add -A >/dev/null 2>&1 && $GIT commit -qm init >/dev/null 2>&1 \
  && $GIT branch -M main && $GIT remote add origin "$ORIGIN" && $GIT push -q origin main >/dev/null 2>&1 \
  && $GIT fetch -q origin ) >/dev/null 2>&1
printf '#project\tpath\tremote\tbranch\tsshkey\troadmap\ttopic\tstatus\n' > "$R/state/portfolio.tsv"
printf 'atest\t%s\t%s\tmain\t-\t-\t-\tactive\n' "$R" "$ORIGIN" >> "$R/state/portfolio.tsv"

mktask(){ cat > "$R/state/tasks/$1.md" <<T
id: $1
workstream: atest
goal: ${2:-a task that will need help}
engine: claude
network_policy: none
acceptance: test -s app/seed.txt
allowed_paths: app
workdir: .
autonomy: auto
status: queued
T
}
blocked_result(){ printf '{"status":"blocked","summary":"stopped","files_changed":[],"blocker":"%s"}' "$1"; }
st(){ sed -n 's/^status: *//p' "$R/state/tasks/$1.md" | head -1 | tr -d '\r'; }
a_field(){ sed -n "s/^$2: *//p" "$R/state/assists/$1.md" 2>/dev/null | head -1 | tr -d '\r'; }
run_work(){ ( cd "$R" && PATH="$BIN:$PATH" RECURSION_ROOT="$R" RECURSION_NO_NOTIFY=1 \
    RECURSION_TEST_WORKER=logical STUB_PROMPT_FILE="$TMP/prompt.txt" STUB_RESULT="${STUB_RESULT:-}" \
    bash scripts/work.sh "$1" ) >"$TMP/w.out" 2>&1; }
assist(){ ( cd "$R" && RECURSION_ROOT="$R" RECURSION_NO_NOTIFY=1 bash scripts/assist.sh "$@" ) >"$TMP/a.out" 2>&1; echo $?; }

QUESTION='Does op 0x6c carry the overlay tint, or is the tint a separate pass? I cannot tell from the corpus.'
ANSWER='Separate pass. The tint splits the two features — see the 0x6c overlay note, captures/S-series-04.bin @ 0x1f40.'

# --- 1) the arm blocks -> an assist exists, and the task is blocked --------------------------
mktask sample-help
STUB_RESULT="$(blocked_result "$QUESTION")" run_work sample-help
if [ "$(st sample-help)" = "blocked" ] && [ -f "$R/state/assists/sample-help.md" ] && [ "$(a_field sample-help status)" = "open" ]; then
  ok "an arm that stops on ambiguity produces an OPEN assist carrying its question"
else
  no "no assist was opened (status=$(st sample-help), assist=$(a_field sample-help status))"
  tail -4 "$TMP/w.out" | sed 's/^/        /'
fi

# --- 2) answering requeues that task ---------------------------------------------------------
rc=$(assist answer sample-help "$ANSWER")
if [ "$rc" = "0" ] && [ "$(st sample-help)" = "queued" ] && [ "$(a_field sample-help status)" = "answered" ]; then
  ok "answering an assist requeues the task and marks the assist answered"
else
  no "answer did not requeue (rc=$rc status=$(st sample-help) assist=$(a_field sample-help status)): $(cat "$TMP/a.out")"
fi

# --- 3) ★ THE CONTROL: the answer TEXT reaches the arm --------------------------------------
# Everything above is bookkeeping. This is the seam.
: > "$TMP/prompt.txt"
STUB_RESULT='{"status":"done","summary":"used the guidance","files_changed":[]}' run_work sample-help
if grep -qF "$ANSWER" "$TMP/prompt.txt" && grep -q 'HUMAN GUIDANCE' "$TMP/prompt.txt"; then
  ok "★ CONTROL — the human's answer is INSIDE the arm's prompt (the seam actually delivers)"
else
  no "★ SEAM BROKEN — the answer never reached the arm; it would re-block on the same question"
  echo "        prompt bytes: $(wc -c < "$TMP/prompt.txt")"
fi

# --- 4) ★ exactly ONE task is unblocked -------------------------------------------------------
mktask other-help "a different question"
STUB_RESULT="$(blocked_result 'Which realm should the census sample first?')" run_work other-help
mktask third-help "a third question"
STUB_RESULT="$(blocked_result 'Should the walker settle before or after the drop?')" run_work third-help
assist answer other-help "Sample furys first." >/dev/null
if [ "$(st other-help)" = "queued" ] && [ "$(st third-help)" = "blocked" ] && [ "$(a_field third-help status)" = "open" ]; then
  ok "★ answering one assist unblocks EXACTLY that task (the others stay blocked)"
else
  no "★ an answer leaked: other=$(st other-help) third=$(st third-help)/$(a_field third-help status)"
fi

# --- 5) an empty answer is refused ------------------------------------------------------------
rc=$(assist answer third-help "   ")
if [ "$rc" != "0" ] && [ "$(st third-help)" = "blocked" ]; then
  ok "an EMPTY answer is refused (it would unblock the task with nothing new and re-block)"
else
  no "an empty answer was accepted (rc=$rc status=$(st third-help))"
fi

# --- 6) answering something nobody asked about fails closed -----------------------------------
rc=$(assist answer no-such-task "here you go")
[ "$rc" != "0" ] && ok "answering a task with no open assist fails closed" || no "invented an assist"

# --- 7) re-asking the SAME question while still open is one item, not a second ------------------
before="$(a_field third-help asked)"
rc=$(assist open third-help decision 'Should the walker settle   BEFORE or after the drop?')
if grep -q '^DUP' "$TMP/a.out" && [ "$(a_field third-help asked)" = "$before" ]; then
  ok "re-asking an open question (reflowed/re-cased) is the SAME item, not a second one"
else
  no "a reflow of the same open question created a new ask: $(cat "$TMP/a.out")"
fi

# --- 8) ★ THE LOOP DETECTOR: re-blocking on an ALREADY-ANSWERED question is its own failure ----
# This is the case the seam exists to make impossible-to-miss: the answer did not land, and
# silently re-asking would burn a human every cycle while looking like the system working.
: > "$TMP/prompt.txt"
rc=$(assist open sample-help decision "$QUESTION")
if [ "$rc" = "3" ] && grep -q '^LOOP' "$TMP/a.out"; then
  ok "★ re-blocking on an ALREADY-ANSWERED question is detected as a LOOP, not re-asked"
else
  no "★ LOOP UNDETECTED (rc=$rc): the same question would be put to a human again every cycle"
fi
esc="$( ( cd "$R" && RECURSION_ROOT="$R" bash scripts/taskctl.sh esc-list open 2>/dev/null ) | grep -c 'assist-loop-sample-help' )"
[ "$esc" = "1" ] && ok "the loop raises its own escalation (distinct from 'the arm has a question')" \
                 || no "no distinct escalation for the loop case (found $esc)"

# --- 9) ★ an arm CANNOT answer its own blocker ------------------------------------------------
# assists live under state/, which the scope gate treats as protected — so this is refused before
# the acceptance gate ever runs. An arm that could write its own answer could unblock itself.
mktask self-answer
cat > "$BIN/claude" <<'STUB2'
#!/usr/bin/env bash
prompt=""; while [ $# -gt 0 ]; do case "$1" in -p) prompt="$2"; shift 2;; *) shift;; esac; done
printf '%s' "$prompt" > "${STUB_PROMPT_FILE:-/dev/null}"
mkdir -p state/assists
printf 'task: self-answer\nstatus: answered\n\n## ANSWER\nI unblocked myself.\n' > state/assists/self-answer.md
printf '{"status":"done","summary":"answered myself","files_changed":["state/assists/self-answer.md"]}' > .recursion-result.json
exit 0
STUB2
chmod +x "$BIN/claude"
run_work self-answer
if [ "$(st self-answer)" != "passed" ] && grep -qi 'scope\|protected\|OUTSIDE' "$TMP/w.out"; then
  ok "★ an arm that writes its own assist answer is REFUSED by the scope gate"
else
  no "★ an arm ANSWERED ITS OWN BLOCKER (status=$(st self-answer)) — self-unblocking is possible"
fi

# --- 10) list shows what is waiting on a human -------------------------------------------------
out="$( ( cd "$R" && RECURSION_ROOT="$R" bash scripts/assist.sh list open ) 2>&1 )"
if printf '%s' "$out" | grep -q 'third-help' && ! printf '%s' "$out" | grep -q 'other-help'; then
  ok "list open shows only what is still waiting on a human"
else
  no "list open is wrong: $out"
fi

echo
[ "$fails" -eq 0 ] && { echo "assist: ALL PASS"; exit 0; }
echo "assist: $fails FAILED"; exit 1
