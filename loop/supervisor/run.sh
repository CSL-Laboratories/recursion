#!/usr/bin/env bash
# Recursion supervisor. Headless Hermes (ChatGPT Pro via Codex OAuth) on a schedule.
# Runs on the $200 sub, NOT `claude -p` — see state/DECISIONS.md D-006/D-008.
#
# Design: this script is a pure text transform. It re-derives the ledger from disk
# every run (carry no state — memory: handoff-prompts-must-carry-no-state), feeds it
# into the prompt, and Hermes returns the brief on stdout. The agent needs no tools;
# run.sh owns file writes, escalation extraction, git, and delivery.
#
# Usage: run.sh <job>    e.g. run.sh morning
set -euo pipefail

ROOT="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
JOB="${1:-morning}"
LOG_DIR="$ROOT/loop/logs"; mkdir -p "$LOG_DIR"
DATE="${RECURSION_CYCLE_DATE:-$(date +%F)}"
[[ "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
  || { echo "run: invalid RECURSION_CYCLE_DATE '$DATE'" >&2; exit 2; }
export RECURSION_CYCLE_DATE="$DATE"
LOG="$LOG_DIR/$DATE-$JOB.log"
OUT="$LOG_DIR/$DATE-$JOB.out"
USAGE="$LOG_DIR/$DATE-$JOB.usage.json"
BRANCH="loop/auto"

cd "$ROOT"
[ -f .env ] && set -a && . ./.env && set +a

log(){ echo "[$(date -Is)] $*" | tee -a "$LOG"; }

# --- Kill switch (D-012): PAUSE halts EXECUTION, preserves AWARENESS. --------------
# The heartbeat and the brief are MONITORING, not execution: they call one text-only model,
# write a brief, and extract escalations. None of that changes a repo, a branch, or anything
# external. They therefore keep running while paused — otherwise the box goes silent exactly
# when you have most reason to want its read on the situation. What stays halted (in
# cycle.sh, and re-checked in each of them): arms, planning, drain/conduct, reflection.
# Pushing also stays halted — this script only ever commits locally to loop/auto.
PAUSED=0
[ -f "$ROOT/state/PAUSE" ] && PAUSED=1
[ "${RECURSION_PAUSED:-}" = "1" ] && PAUSED=1
[ "$PAUSED" = "1" ] && log "PAUSED — running $JOB in MONITOR-ONLY mode (no execution)."

# --- Per-tier model (realizes Lua/Terra/Sol routing via --model). -----------------
# Decisions/brief = terra; cheap heartbeat = luna. See loop/config/routing.yaml.
case "$JOB" in
  morning)   PROMPT_FILE="loop/supervisor/morning-brief.md"; MODEL="gpt-5.6-terra"; MODE="brief" ;;
  heartbeat) PROMPT_FILE="loop/supervisor/heartbeat.md";     MODEL="gpt-5.6-luna";  MODE="signal" ;;
  *) echo "unknown job: $JOB" >&2; exit 2 ;;
esac

# --- Work on the auto branch. Never the default branch (CLAUDE.md hard rule 2). ----
# PAUSE permits monitoring but halts merges. In monitor-only mode the complete self-repo Git
# projection is observation-only: do not discard BOARD, checkout/create a branch, merge, move
# HEAD, touch the index, or append a reflog entry.
if [ "$PAUSED" = "0" ]; then
DEFAULT_BRANCH="$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@' || true)"
DEFAULT_BRANCH="${DEFAULT_BRANCH:-master}"
# state/BOARD.md is RENDERED every run and tracked, so it is dirty almost always — and a dirty
# tracked file makes `git checkout` refuse. That is how the box got stuck on master with the
# heartbeat and the brief failing on every cycle while the digests (which need no branch switch)
# kept arriving. Discarding it is safe precisely because it is regenerated below.
git checkout -q -- state/BOARD.md 2>/dev/null || true
git rev-parse --verify "$BRANCH" >/dev/null 2>&1 \
  && git checkout -q "$BRANCH" \
  || git checkout -q -b "$BRANCH"

# ★ THE RUNTIME BRANCH MUST CARRY THE REVIEWED CODE. cron runs whatever is in this working tree,
# and this checkout makes that tree $BRANCH — so if $BRANCH lags the default branch, the box runs
# OLD CODE while every log line still looks healthy. It was 21 commits behind when this was found.
#
# The previous line here was `git merge --ff-only "$DEFAULT_BRANCH" || true`, which cannot ever
# succeed: $BRANCH accumulates its own ledger commits, so it is never a strict ancestor. It had
# been failing silently under `|| true` since the first one. A real merge is the operation that
# was actually intended — the default branch is what Lucas has reviewed and merged, so taking it
# is "run what he approved". A conflict is a human's call, so it aborts and escalates rather than
# guessing.
if ! git merge -q --no-edit "$DEFAULT_BRANCH" 2>/dev/null; then
  git merge --abort 2>/dev/null || true
  log "MERGE CONFLICT bringing $DEFAULT_BRANCH into $BRANCH — the box is running code older than review"
  [ -x "$ROOT/scripts/taskctl.sh" ] && bash "$ROOT/scripts/taskctl.sh" esc-record \
      "runtime-branch-conflict" \
      "$BRANCH cannot merge $DEFAULT_BRANCH — the box is running code older than what you merged" \
      infra "$ROOT" >/dev/null 2>&1 || true
fi
BEHIND="$(git rev-list --count "$BRANCH..$DEFAULT_BRANCH" 2>/dev/null || echo 0)"
[ "${BEHIND:-0}" -gt 0 ] && log "WARNING: $BRANCH is $BEHIND commit(s) behind $DEFAULT_BRANCH — running stale code"

else
  log "PAUSE preserves self-repo Git state; skipping BOARD discard, branch checkout/create, and default-branch merge."
fi

mkdir -p state/briefs
log "start $JOB (model=$MODEL)"

# --- Assemble the ledger (re-derived fresh every run). ----------------------------
SNAP="$ROOT/state/.recursion-snapshot.txt"
CTX="$(
  echo "===== ANCHOR (scope + success criteria) ====="; sed -n '1,90p' ANCHOR.md 2>/dev/null || true
  echo; echo "===== OPEN DECISIONS ====="; cat state/DECISIONS.md 2>/dev/null || true
  echo; echo "===== PROJECT STATUS (pushed from repos) ====="; cat state/projects/*.md 2>/dev/null || true
  echo; bash scripts/board-summary.sh --ledger 2>/dev/null || true
  echo; echo "===== DURABLE LESSONS (the box's own memory) ====="; cat state/LESSONS.md 2>/dev/null || true
  echo; echo "===== OPEN SELF-IMPROVEMENT PROPOSALS (surface, don't apply) ====="; tail -n 40 state/PROPOSALS.md 2>/dev/null || true
  echo; echo "===== CURRENT INBOX ====="; cat state/INBOX.md 2>/dev/null || true
  echo; echo "===== YESTERDAY'S SNAPSHOT (for the 'changed since' diff) ====="; cat "$SNAP" 2>/dev/null || true
)" || true

PROMPT="$(cat "$PROMPT_FILE")
=================== LEDGER (ground truth, re-derived this run) ===================
$CTX"

# --- Engine: one-shot Hermes on the sub. -----------------------------------------
if ! hermes -z "$PROMPT" -m "$MODEL" --usage-file "$USAGE" >"$OUT" 2>>"$LOG"; then
  log "FAILED $JOB — see $LOG"
  bash "$ROOT/scripts/notify.sh" "⚠ Recursion: $JOB failed. See $LOG" || true
  exit 1
fi
cat "$OUT" >> "$LOG"

if [ "${MODE:-brief}" = "brief" ]; then   # signal-mode (heartbeat) stops here — no brief/deliver/commit
# --- Write the brief (everything before the escalation block). --------------------
BRIEF="state/briefs/$DATE.md"
awk 'BEGIN{p=1} /^===INBOX-START===/{p=0} p{print} /^===INBOX-END===/{p=1}' "$OUT" > "$BRIEF"

# A brief produced while paused MUST say so on its first line. Otherwise it reads exactly
# like a normal brief, and "the queue did not move" looks like a stall rather than the
# kill switch doing its job (D-012).
if [ "$PAUSED" = "1" ]; then
  PAUSE_REASON="$(sed -n '2s/^reason: *//p' "$ROOT/state/PAUSE" 2>/dev/null)"
  { echo "> ⏸ **PAUSED** — execution is halted; this brief is monitoring only."
    [ -n "$PAUSE_REASON" ] && echo "> reason: $PAUSE_REASON"
    echo "> No arms, planning, drain, or reflection ran. Lift with \`scripts/resume.sh\`."
    echo
    cat "$BRIEF"
  } > "$BRIEF.tmp" && mv "$BRIEF.tmp" "$BRIEF"
fi

# --- Extract escalations → durable INBOX queue + a distinct Telegram ping. ---------
ESC="$(awk '/^===INBOX-START===/{f=1;next} /^===INBOX-END===/{f=0} f' "$OUT")"
if [ -n "${ESC//[[:space:]]/}" ]; then
  # --- ESCALATION IDENTITY (the four-times bug) -------------------------------------------
  # This block used to append whatever the brief emitted, unconditionally. Meanwhile the
  # heartbeat signals `brief` for as long as the INBOX holds an unanswered item — so an
  # unanswered ask regenerated itself every cycle. The SAME escalation was appended four
  # times in nine hours (21:15, 00:15, 03:15, 06:15). A queue that refills itself faster
  # than it is worked cannot be worked down, and each copy re-pinged Telegram.
  #
  # Fix: derive a stable key from the ask, and let taskctl decide NEW vs DUP. On DUP we
  # neither append nor re-notify — we bump the sighting count, so "asked 5 times" is visible
  # as a number instead of five rows.
  ESC_TITLE="$(printf '%s' "$ESC" | sed -n 's/^## *//p' | head -1)"
  # Strip the leading date/time stamp — the same ask carries a different one each cycle and
  # would otherwise key differently every time, which is exactly how the duplicates arose.
  ESC_KEY="$(printf '%s' "$ESC_TITLE" \
    | sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:]+Z?)?[[:space:]]*[—-]*[[:space:]]*//' \
    | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed -E 's/^-+|-+$//g' | cut -c1-72)"
  ESC_WS="$(printf '%s' "$ESC" | sed -n 's/^\*\*Workstream:\*\* *//p' | head -1)"
  ESC_PTR="$(printf '%s' "$ESC" | sed -n 's/^\*\*Pointer:\*\* *//p' | head -1 | cut -c1-120)"

  VERDICT="NEW -"
  if [ -x "$ROOT/scripts/taskctl.sh" ] && [ -n "$ESC_KEY" ]; then
    set +e
    VERDICT="$(RECURSION_ROOT="$ROOT" bash "$ROOT/scripts/taskctl.sh" \
                 esc-record "$ESC_KEY" "$ESC_TITLE" "$ESC_WS" "$ESC_PTR" 2>&1)"
    verdict_rc=$?
    set -e
    if [ "$verdict_rc" -ne 0 ]; then
      log "escalation state write REFUSED (rc=$verdict_rc): ${VERDICT:0:160}"
      VERDICT="REFUSED -"
    fi
  fi
  EID="$(printf '%s' "$VERDICT" | awk '{print $2}')"

  case "$VERDICT" in
    REFUSED*)
      log "escalation held because canonical state could not be locked/updated; no duplicate fallback write"
      ;;
    DUP*)
      SEEN="$(printf '%s' "$VERDICT" | awk '{print $3}')"
      log "escalation SUPPRESSED as duplicate of $EID (seen ${SEEN}x) — not appended, not re-pinged"
      ;;
    *)
      { echo; printf '%s\n' "$ESC" | sed "1s/^## /## [$EID] /"; } >> state/INBOX.md
      log "escalation $EID appended to state/INBOX.md"
      PFX="🔔 Recursion needs a decision [$EID]:"
      if [ "$PAUSED" = "1" ]; then
        log "escalation $EID retained in INBOX; PAUSE suppresses human-visible delivery"
      else
        bash "$ROOT/scripts/notify.sh" "$PFX
$ESC

Resolve with: recursion resolve-esc $EID \"<what you decided>\"" || true
      fi
      ;;
  esac
fi

# Build the complete brief before the state commit. Delivery is represented by one canonical
# source event; the Phase 7 scheduler/decision/action/actuator/outbox path owns the eventual send.
# PAUSE queues nothing stale for replay into a later production window.
if [ "${DELIVER_BRIEF:-1}" = "1" ] && [ -f "$BRIEF" ]; then
  { echo; echo; bash "$ROOT/scripts/branch-review.sh" 2>/dev/null; } >> "$BRIEF"
  if [ "$PAUSED" = "1" ]; then
    log "PAUSE suppresses portfolio brief notification enqueue"
  elif [ -x "$ROOT/scripts/taskctl.sh" ]; then
    BODY="$(<"$BRIEF")"
    bash "$ROOT/scripts/taskctl.sh" notify-event portfolio-brief recursion "$BODY" >/dev/null \
      || log "portfolio brief notification enqueue failed"
  else
    log "portfolio brief notification enqueue unavailable — taskctl missing"
  fi
fi

# --- Save today's snapshot for tomorrow's diff. -----------------------------------
cat state/projects/*.md 2>/dev/null > "$SNAP" || true

# --- Commit any state changes (agent output). Never main. -------------------------
CURRENT_BRANCH="$(git branch --show-current 2>/dev/null || true)"
if [ "$PAUSED" = "1" ]; then
  # Monitoring artifacts remain durable local files, but PAUSE must not move HEAD or a reflog.
  # A later resumed cycle reaches loop/auto and commits through the ordinary reviewed path.
  log "PAUSE preserves self-repo Git state; monitoring artifacts retained locally without commit."
elif [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
  log "REFUSED state commit on branch '${CURRENT_BRANCH:-detached}'; only $BRANCH may receive supervisor state."
elif ! git diff --quiet || ! git diff --cached --quiet; then
  git add -A state/ || true
  if git commit -q -m "loop: $JOB $DATE"; then
    log "committed to $BRANCH"
  else
    log "commit FAILED (non-fatal — check git identity on the box); continuing to delivery"
  fi
else
  log "no changes"
fi

fi   # end brief-mode block

# --- Per-job fallback check: did this run drop off the sub onto metered billing? ---
PROV="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("provider",""))' "$USAGE" 2>/dev/null || true)"
COST="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("estimated_cost_usd",0))' "$USAGE" 2>/dev/null || true)"
if [ -n "$PROV" ] && [ "$PROV" != "openai-codex" ]; then
  bash "$ROOT/scripts/notify.sh" "⚠ Recursion $JOB ran on FALLBACK (provider=$PROV, cost=\$$COST) — not the ChatGPT sub. Check quota/gateway." || true
fi

log "done $JOB"
