#!/usr/bin/env bash
# assist.sh — the seam where a human answer reaches the arm that asked for it.
#
# WHY THIS EXISTS. The escalation loop was one-directional. An arm could stop and ask a precise
# question (work.sh records `blocked` with the arm's own words, which is correct behaviour), and
# a human could `resolve` it or `requeue` it — but the ANSWER had nowhere to go. requeue deletes
# `blocked_on:` and appends a note to state/INBOX.md, which is a file the arm never reads. So on
# the next run the arm receives the IDENTICAL prompt, reasons identically, and blocks on the
# identical question. That is not an escalation; it is a loop with a human inside it, and it is
# the reason a pilot cannot run — project work blocks on things only Lucas can supply (a live
# capture, an in-game observation, a call about what the RE actually means).
#
# So an assist is a first-class object with two halves that must both exist:
#   the ASK      — recorded when a task blocks, with the question and what would satisfy it
#   the ANSWER   — written by a human, and INJECTED INTO THE ARM'S ENVELOPE on the next run
# and it unblocks exactly the one task it answers.
#
# ★ THE LOOP DETECTOR is the load-bearing part. If a task blocks again with the same question it
# already had answered, the answer did not land — a stale worktree, an envelope that dropped it,
# an answer that did not address the ask. Silently re-asking would burn a human every cycle and
# look like progress. That case gets its own escalation, and it is machine-detectable: normalise
# the question and compare.
#
# An arm CANNOT answer its own blocker: assists live under state/, which work.sh's scope gate
# treats as protected, so a diff that writes one is refused before the acceptance gate runs.
#
#   assist.sh open <task-id> <kind> <question>   record/refresh the ask (work.sh calls this)
#   assist.sh answer <task-id> <text...>         the human answers; the task returns to queued
#   assist.sh show <task-id>                     print one assist
#   assist.sh list [open|answered|all]           what is waiting on a human
#   assist.sh withdraw <task-id> <why>           the ask became moot
#   assist.sh guidance <task-id>                 the answer, formatted for the arm envelope
#
# kind: decision | evidence | access | in-game.  `in-game` carries the standing rule — a live
# game action is human-only, on a throwaway alt and a clean proxy; the loop never drives the
# client (CLAUDE.md). Naming the kind is what lets a human triage a queue of asks at a glance.
set -uo pipefail
export LC_ALL=C
ROOT="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"
DIR="$ROOT/state/assists"; mkdir -p "$DIR"
TASKCTL="$ROOT/scripts/taskctl.sh"
NOTIFY="$ROOT/scripts/notify.sh"
CMD="${1:-list}"; shift 2>/dev/null || true
now(){ date -u +%FT%TZ; }
# An assist is routed by the TASK's repo, so a project question lands in that project's topic
# rather than in a general stream where it competes with build noise for attention.
taskrepo(){ sed -n 's/^repo: *//p' "$ROOT/state/tasks/$1.md" 2>/dev/null | head -1 | tr -d '\r'; }
ASSIST_PROJECT="$(taskrepo "${1:-}" 2>/dev/null || true)"
DURABLE_REPORTING=0
is_durable_task(){
  local t="$ROOT/state/tasks/$1.md"
  [ -n "$(sed -n 's/^project_id: *//p' "$t" 2>/dev/null | head -1)" ] \
    && [ -n "$(sed -n 's/^pilot_id: *//p' "$t" 2>/dev/null | head -1)" ]
}
notify(){ [ "$DURABLE_REPORTING" = 1 ] && return 0; [ "${RECURSION_NO_NOTIFY:-0}" = "1" ] && return 0; [ -x "$NOTIFY" ] && bash "$NOTIFY" ${ASSIST_PROJECT:+--project "$ASSIST_PROJECT"} "$@" >/dev/null 2>&1 || true; }
event(){ [ -x "$TASKCTL" ] && RECURSION_ROOT="$ROOT" bash "$TASKCTL" event "$1" "$2" "$3" >/dev/null 2>&1 || true; }

f(){ sed -n "s/^$1: *//p" "$2" 2>/dev/null | head -1 | tr -d '\r'; }
sect(){ awk -v s="## $1" 'index($0,s)==1{f=1;next} /^## /{f=0} f' "$2" 2>/dev/null; }

# A question is "the same question" up to whitespace and case. Comparing raw text would let a
# reflowed line masquerade as a new ask, which is exactly how a loop hides.
norm(){ tr 'A-Z' 'a-z' | tr -s '[:space:]' ' ' | sed 's/^ //; s/ $//'; }
question_snippet(){
  python3 -c 'import sys; print(sys.stdin.buffer.read(65536)[:240].decode("utf-8", "ignore"), end="")'
}

case "$CMD" in

open)
  ID="${1:?usage: assist.sh open <task-id> <kind> <question>}"
  is_durable_task "$ID" && DURABLE_REPORTING=1
  KIND="${2:-decision}"; QUESTION="${3:-}"
  QUESTION_SNIPPET="$(printf '%s' "$QUESTION" | question_snippet)"
  A="$DIR/$ID.md"
  [ -n "${QUESTION//[[:space:]]/}" ] || { echo "assist: refusing to open an assist with no question" >&2; exit 2; }
  case "$KIND" in decision|evidence|access|in-game) : ;; *) echo "assist: unknown kind '$KIND'" >&2; exit 2 ;; esac

  if [ -f "$A" ]; then
    ST="$(f status "$A")"
    OLDQ="$(sect QUESTION "$A" | norm)"; NEWQ="$(printf '%s' "$QUESTION" | norm)"
    if [ "$ST" = "answered" ] && [ "$OLDQ" = "$NEWQ" ]; then
      # ★ THE LOOP. It was asked, a human answered, and the arm is asking the identical thing.
      # The answer did not reach the arm, or did not settle it. Re-asking would burn the human
      # again and look like the system working.
      echo "LOOP $ID"
      [ -x "$TASKCTL" ] && RECURSION_ROOT="$ROOT" bash "$TASKCTL" esc-record \
        "assist-loop-$ID" "$ID re-blocked on a question that was ALREADY ANSWERED — the answer is not reaching the arm" \
        assist "state/assists/$ID.md" >/dev/null 2>&1
      notify "🔁 [$ID] blocked AGAIN on an already-answered question — the assist is not reaching the arm. See state/assists/$ID.md"
      exit 3
    fi
    if [ "$ST" = "open" ] && [ "$OLDQ" = "$NEWQ" ]; then
      echo "DUP $ID"; exit 0     # same ask, still open — one item, not a second ping
    fi
  fi

  { echo "task: $ID"
    echo "kind: $KIND"
    echo "status: open"
    echo "asked: $(now)"
    echo "asked_by: ${RECURSION_ACTOR:-work.sh}"
    echo "answered:"
    echo "answered_by:"
    echo
    echo "## QUESTION"
    printf '%s\n' "$QUESTION"
    echo
    echo "## WHAT WOULD SATISFY THIS"
    case "$KIND" in
      decision) echo "A call, in one or two sentences, plus the reason — the reason is what stops the next task re-asking." ;;
      evidence) echo "The artifact itself (a capture, a log, a byte range) with its filename and offset, per CLAUDE.md's citation rule." ;;
      access)   echo "A credential, permission, or endpoint provisioned by a human — never pasted into this file." ;;
      in-game)  echo "An in-game observation Lucas performs by hand on a throwaway alt over a clean proxy. The loop must not drive the client." ;;
    esac
    echo
    echo "## ANSWER"
    echo "_(unanswered — reply with: recursion assist $ID \"...\")_"
  } > "$A"
  event assist-open "$ID" "$KIND: $QUESTION_SNIPPET"
  echo "NEW $ID"
  notify "🙋 [$ID] needs you ($KIND): $QUESTION_SNIPPET
Answer with: recursion assist $ID \"...\"" ;;

answer)
  ID="${1:?usage: assist.sh answer <task-id> <text...>}"; shift
  is_durable_task "$ID" && DURABLE_REPORTING=1
  TEXT="$*"
  A="$DIR/$ID.md"
  [ -f "$A" ] || { echo "assist: no assist for '$ID' — nothing asked for it" >&2; exit 2; }
  # An empty answer is worse than none: it clears the block, the arm re-runs with nothing new,
  # and it blocks again — the loop this file exists to prevent, started by the fix for it.
  [ -n "${TEXT//[[:space:]]/}" ] || { echo "assist: refusing an empty answer (it would unblock the task with nothing new)" >&2; exit 2; }
  ST="$(f status "$A")"
  [ "$ST" = "open" ] || { echo "assist: $ID is status=$ST, not open" >&2; exit 4; }

  python3 - "$A" "$TEXT" "$(now)" "${RECURSION_ANSWERED_BY:-lucas}" <<'PY' || exit $?
import io,os,sys,tempfile
path,text,ts,who = sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]
s = io.open(path,encoding='utf-8',newline='').read()
head,_,rest = s.partition('## ANSWER')
head = head.replace('answered:\n','answered: %s\n' % ts)
head = head.replace('answered_by:\n','answered_by: %s\n' % who)
directory=os.path.dirname(path)
fd,tmp=tempfile.mkstemp(prefix='.'+os.path.basename(path)+'.tmp.',dir=directory)
try:
    with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as out:
        out.write(head + '## ANSWER\n' + text.rstrip() + '\n')
        out.flush(); os.fsync(out.fileno())
    os.replace(tmp,path)
    dfd=os.open(directory,os.O_RDONLY)
    try: os.fsync(dfd)
    finally: os.close(dfd)
except BaseException:
    try: os.unlink(tmp)
    except FileNotFoundError: pass
    raise
PY

  # Unblock EXACTLY this task. Other blocked tasks are other questions.
  T="$ROOT/state/tasks/$ID.md"
  if [ -f "$T" ]; then
    if [ "$DURABLE_REPORTING" = 1 ]; then
      RECURSION_ROOT="$ROOT" python3 "$ROOT/scripts/project-reconcile.py" resume-assist \
        "$ID" "${RECURSION_INSTRUCTION_ID:-uncorrelated}" || exit $?
    else
      sed -i 's/^status: .*/status: queued/' "$T"
      sed -i '/^blocked_on:/d' "$T"
    fi
    echo "answered $ID — task returned to queued; the next run carries your answer in the arm's envelope."
  else
    echo "answered $ID — recorded (no task file; nothing to requeue)."
  fi
  python3 - "$A" <<'PY' || exit $?
import os,re,sys,tempfile
path=sys.argv[1]; directory=os.path.dirname(path)
raw=open(path,encoding='utf-8').read()
if re.search(r'(?m)^status: answered\r?$',raw):
    raise SystemExit(0)
value,count=re.subn(r'(?m)^status: open\r?$','status: answered',raw,count=1)
if count != 1:
    raise SystemExit('assist status changed before answered commit')
fd,tmp=tempfile.mkstemp(prefix='.'+os.path.basename(path)+'.tmp.',dir=directory)
try:
    with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as out:
        out.write(value); out.flush(); os.fsync(out.fileno())
    os.replace(tmp,path)
    dfd=os.open(directory,os.O_RDONLY)
    try: os.fsync(dfd)
    finally: os.close(dfd)
except BaseException:
    try: os.unlink(tmp)
    except FileNotFoundError: pass
    raise
PY
  { echo; echo "## ASSIST ANSWERED $(now) — $ID"; printf '%s\n' "$TEXT"; } >> "$ROOT/state/INBOX.md"
  [ -x "$TASKCTL" ] && RECURSION_ROOT="$ROOT" bash "$TASKCTL" esc-resolve \
      "$(RECURSION_ROOT="$ROOT" bash "$TASKCTL" esc-list open 2>/dev/null | awk -F'\t' -v k="assist-loop-$ID" '$2==k{print $1;exit}')" \
      "answered" >/dev/null 2>&1
  [ "$DURABLE_REPORTING" = 1 ] || event assist-answer "$ID" "answered by ${RECURSION_ANSWERED_BY:-lucas}"
  notify "✅ [$ID] answered — requeued" ;;

guidance)
  # What work.sh injects into the envelope. Empty output = nothing to inject.
  ID="${1:?usage: assist.sh guidance <task-id>}"
  A="$DIR/$ID.md"
  [ -f "$A" ] || exit 0
  # A crash can occur after the resume CAS but before the final status projection. The durable
  # answer fields/body are the authority; allow that open intermediate so requeued work never
  # runs without Lucas's guidance.
  AST="$(f status "$A")"
  case "$AST" in answered|open) ;; *) exit 0 ;; esac
  [ -n "$(f answered "$A")" ] && [ -n "$(f answered_by "$A")" ] || exit 0
  [ -n "$(sect ANSWER "$A" | sed '/^_(unanswered/d' | tr -d '[:space:]')" ] || exit 0
  echo "HUMAN GUIDANCE — AUTHORITATIVE. You stopped on this task before and asked:"
  sect QUESTION "$A" | sed 's/^/  /'
  echo
  echo "$(f answered_by "$A") answered:"
  sect ANSWER "$A" | sed 's/^/  /'
  echo
  echo "Proceed on that basis. If it still does not settle the task, block again with a"
  echo "DIFFERENT question — re-asking this same question is a loop and will be flagged as one." ;;

show)
  ID="${1:?usage: assist.sh show <task-id>}"; cat "$DIR/$ID.md" ;;

withdraw)
  ID="${1:?usage: assist.sh withdraw <task-id> <why>}"; shift; WHY="${*:-no longer needed}"
  A="$DIR/$ID.md"; [ -f "$A" ] || { echo "assist: no assist for '$ID'" >&2; exit 2; }
  sed -i "s/^status: .*/status: withdrawn/" "$A"
  { echo; echo "## WITHDRAWN $(now)"; printf '%s\n' "$WHY"; } >> "$A"
  event assist-withdraw "$ID" "$WHY"; echo "withdrew $ID" ;;

list)
  W="${1:-open}"
  printf '%-24s %-9s %-9s %s\n' TASK KIND STATUS QUESTION
  found=0
  for A in "$DIR"/*.md; do
    [ -f "$A" ] || continue
    st="$(f status "$A")"
    [ "$W" = "all" ] || [ "$st" = "$W" ] || continue
    found=1
    printf '%-24s %-9s %-9s %s\n' "$(f task "$A")" "$(f kind "$A")" "$st" "$(sect QUESTION "$A" | tr '\n' ' ' | head -c 90)"
  done
  [ "$found" = "1" ] || echo "(none $W)" ;;

*) sed -n '30,37p' "$0"; exit 2 ;;
esac
