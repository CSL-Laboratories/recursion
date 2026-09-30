#!/usr/bin/env bash
# work.sh — the build-loop executor (delegation-design.md "The pipeline", Plane 2).
#
#   queued task -> dispatch an arm to GENERATE -> run the ACCEPTANCE test (the gate) ->
#   on green: auto-commit output to loop/work/<id> (never main, never merge)
#   on red:   retry the arm once, then ESCALATE (INBOX + Telegram)
#
# The load-bearing rule (delegation-design.md): autonomy rests on the acceptance test, not
# trust. A task with no machine-checkable acceptance command is NEVER auto-integrated — it
# escalates. The generator is never trusted; only a green gate earns an auto-commit.
#
# Build output lives on a per-task branch in an isolated git WORKTREE, so it never collides
# with the main tree (which cron keeps on loop/auto). Every arm lands on the BOARD.
#
#   work.sh <id|path>   run a specific task (state/tasks/<id>.md, or a file path)
#   work.sh             pull the first queued task in state/tasks/ (alpha order)
set -euo pipefail
HOME="${HOME:-/root}"   # systemd gives a service no HOME; `set -u` then kills the script
ROOT="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"
[ -f .env ] && set -a && . ./.env && set +a
# .env pins RECURSION_ROOT for the deployed checkout. Sourcing it must never RETARGET a
# caller who passed a different root: that silently made a "sandbox" enqueue ten decisions
# into production on 2026-08-12. ROOT and RECURSION_ROOT agree from here on.
RECURSION_ROOT="$ROOT"; export RECURSION_ROOT
# Git Bash exposes an MSYS PID in $$ while the bundled Windows Python runtime validates
# Win32 process identities.  Use the translated PID when available; on Linux this is the
# ordinary shell PID and the fallback remains byte-for-byte compatible.
OWNER_PID="$(cat /proc/$$/winpid 2>/dev/null || echo $$)"

DATE="$(date +%F)"; STAMP="$(date +%H%M%S)"
LOG_DIR="$ROOT/loop/logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$DATE-work.log"
log(){ echo "[$(date -Is)] work: $*" | tee -a "$LOG" >&2; }
board(){ pause_checkpoint "board state transition" || return 75; bash scripts/board.sh "$@" >/dev/null 2>&1 || true; }
notify(){ [ -n "${PILOT_ID:-}" ] && return 0; [ "${RECURSION_NO_NOTIFY:-0}" = "1" ] && return 0; bash "$ROOT/scripts/notify.sh" ${REPO_DIR:+--project "$REPO_DIR"} "$@" >/dev/null 2>&1 || true; }

pause_checkpoint(){
  local stage="${1:-execution}"
  [ ! -f "$ROOT/state/PAUSE" ] || { log "PAUSED during $stage — refusing later gates/state/publication"; return 75; }
  if [ -n "${PILOT_ID:-}" ] && declare -F window_checkpoint >/dev/null 2>&1; then
    window_checkpoint "$stage" || return 75
  fi
}

terminate_worker_group(){
  local pgid="$1" signal="${2:-TERM}" grace="${3:-5}" i
  case "$signal" in TERM|INT) ;; *) log "unsupported cancellation signal=$signal"; return 1 ;; esac
  case "$grace" in ''|*[!0-9]*) return 1 ;; esac
  kill -"$signal" -- "-$pgid" 2>/dev/null || true
  for i in $(seq 1 $((grace * 10))); do
    kill -0 -- "-$pgid" 2>/dev/null || return 0
    sleep 0.1
  done
  kill -KILL -- "-$pgid" 2>/dev/null || true
  for i in $(seq 1 50); do
    kill -0 -- "-$pgid" 2>/dev/null || return 0
    sleep 0.1
  done
  log "worker process group $pgid did not confirm termination"
  return 1
}

# --- kill switch (architecture.md): state/PAUSE defers the executor without claiming success. --
pause_checkpoint startup || exit 75

# --- resolve the task -----------------------------------------------------------------
ARG="${1:-}"
if   [ -n "$ARG" ] && [ -f "$ARG" ];                  then TASK="$ARG"
elif [ -n "$ARG" ] && [ -f "state/tasks/$ARG.md" ];   then TASK="state/tasks/$ARG.md"
elif [ -z "$ARG" ]; then
  TASK="$(grep -l '^status: *queued' state/tasks/*.md 2>/dev/null | sort | head -1 || true)"
  [ -n "$TASK" ] || { log "no queued tasks in state/tasks/."; exit 0; }
else
  log "no such task: $ARG"; exit 2
fi

field(){ sed -n "s/^$1: *//p" "$TASK" | head -1 | tr -d '\r'; }
ID="$(field id)"; WORKSTREAM="$(field workstream)"; GOAL="$(field goal)"
PROJECT_ID="$(field project_id)"
PILOT_ID="$(field pilot_id)"
ENGINE="$(field engine)"; ACCEPTANCE="$(field acceptance)"; WORKDIR="$(field workdir)"
AUTONOMY="$(field autonomy)"; MODEL="$(field model)"
MODEL_ALIAS="$(field model_alias)"; VERIFIER_MODEL_ALIAS="$(field verifier_model_alias)"
CAPACITY_POOL="$(field capacity_pool)"; [ -n "$CAPACITY_POOL" ] || CAPACITY_POOL="$(field pool_id)"
DEADLINE_SECONDS="$(field deadline_seconds)"; VERIFIER_DEADLINE_SECONDS="$(field verifier_deadline_seconds)"
NETWORK_POLICY="$(field network_policy)"; EVIDENCE_CONTRACT="$(field evidence_contract)"
VERIFICATION_POLICY="$(field verification)"; EXPECTED_CHANGE="$(field expected_change)"
GATE_FILES="$(field gate_files)"
GATE_FILES="${GATE_FILES//,/ }"
CONTROLLER_GATE_FILES="$(field controller_gate_files)"
CONTROLLER_GATE_FILES="${CONTROLLER_GATE_FILES//,/ }"
APPROVED_GATE_CONTRACT="$(field gate_contract)"
SOURCE_HEAD="$(field source_head)"; SOURCE_BRANCH="$(field source_branch)"
WORKDIR="${WORKDIR:-.}"; AUTONOMY="${AUTONOMY:-auto}"; ENGINE="${ENGINE:-claude}"
# Cross-engine verification (D-015): the verifier may run on a different engine than the
# implementer. Absent field -> the implementer's engine, which is the historical behavior.
# Header-only read: the contract digest hashes header fields only, and this field is normally
# absent, so a body line must never be able to supply it.
VERIFIER_ENGINE="$(sed -n '/^$/q;s/^verifier_engine: *//p' "$TASK" | head -1)"
VERIFIER_ENGINE="${VERIFIER_ENGINE:-$ENGINE}"
ALLOWED_PATHS="$(field allowed_paths)"; ALLOWED_PATHS="${ALLOWED_PATHS:-$WORKDIR}"
STRICT_PHASE5=0
[ "$EVIDENCE_CONTRACT" != worker-result-v1 ] || STRICT_PHASE5=1
DEADLINE_SECONDS="${DEADLINE_SECONDS:-${RECURSION_WORKER_TIMEOUT_S:-3600}}"
VERIFIER_DEADLINE_SECONDS="${VERIFIER_DEADLINE_SECONDS:-${RECURSION_VERIFIER_TIMEOUT_S:-1800}}"
NETWORK_POLICY="${NETWORK_POLICY:-provider-only}"; EXPECTED_CHANGE="${EXPECTED_CHANGE:-optional}"
CAPACITY_POOL="${CAPACITY_POOL:-}"
# repo: which git repo the arm builds in. Default = this repo ($ROOT). An external repo
# (e.g. /opt/example-client) lets the loop build another codebase; its build output
# and loop/work/<id> branch live in THAT repo, gated by whatever acceptance the task names.
REPO_DIR="$(field repo)"; REPO_DIR="${REPO_DIR:-$ROOT}"
REPO_DIR="$(realpath -e "$REPO_DIR" 2>/dev/null || printf '%s' "$REPO_DIR")"
# seed: gitignored paths (space/comma separated, relative to REPO_DIR) to COPY into each fresh
# worktree. git worktree does NOT inherit gitignored files, so a required-but-untracked data
# corpus (e.g. data/models) is absent in a new worktree and every test that needs it fails.
# Declaring it here makes the dependency explicit.
SEED="$(field seed)"; SEED="${SEED//,/ }"
SEED_MANIFEST="$(field seed_manifest)"
SEED_MANIFEST_SHA256="$(field seed_manifest_sha256)"
SEED_DIGEST_EXPECTED="$(field seed_digest)"
# Git authority. sshkey proves read freshness and is handed only to the review-only publisher.
# The worker sandbox never mounts this key. There is no autonomous default-branch merge path.
SSHKEY="$(field sshkey)"
# A task that names no key falls back to the PORTFOLIO REGISTRY (state/portfolio.tsv). The
# repo->key mapping used to live only in whoever typed the command, so a task that forgot
# `sshkey:` fetched with no credential — and a credential-less git fetch fails QUIETLY.
if [ -z "$SSHKEY" ] && [ -x "$ROOT/scripts/resync.sh" ]; then
  SSHKEY="$(RECURSION_ROOT="$ROOT" bash "$ROOT/scripts/resync.sh" key "$REPO_DIR" 2>/dev/null || true)"
  [ -n "$SSHKEY" ] && log "deploy key from portfolio registry: $SSHKEY"
fi
PLANNED_FROM="$(field planned_from)"   # <roadmap>:<line> — flipped [~]->[x] when this task passes
GSSH=""; [ -n "$SSHKEY" ] && GSSH="ssh -i ${SSHKEY/#\~/$HOME} -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=12"
gitr(){ if [ -n "$GSSH" ]; then GIT_SSH_COMMAND="$GSSH" git -C "$REPO_DIR" "$@"; else git -C "$REPO_DIR" "$@"; fi; }
[ -n "$ID" ] || { log "task $TASK missing 'id:'"; exit 2; }
BRANCH="loop/work/$ID"
BASE_COMMIT=""; CANDIDATE_OID=""; LAST_SCOPE_FILE=""; LAST_RUN_ID=""; LAST_RUN_EVIDENCE=""
ASSIGNMENT_FILE=""; ASSIGNMENT_DIGEST=""
RESOLVER_ASSIGNMENT_FILE=""; RESOLVER_ASSIGNMENT_DIGEST=""
IMPLEMENTER_RUN_ID=""; VERIFIER_RUN_ID=""; RESOLVED_MODEL="$MODEL"; RESOLVED_VERIFIER_MODEL=""
LAST_USAGE_FILE=""; SEED_DIGEST_BEFORE=""
# Phase 3 (D-016): the immutable assignment resolved for the arm most recently launched by
# run_model_supervised. capacity_pool is an optional task-header field naming a
# scripts/capacity-pools.json account/subscription identity; absent for every task that
# predates it (legacy behavior: capacity_pool stays null on the assignment).
ASSIGNMENT_DIGEST=""; ASSIGNMENT_FILE=""
CAPACITY_POOL="$(field capacity_pool)"
LEASE_OWNER="work.sh:$$@$(hostname)"; LEASE_ATTEMPT=""
SEED_EVIDENCE_FILE="$ROOT/state/runs/$ID-seed-$STAMP-$$.json"
SOURCE_EVIDENCE_FILE="$ROOT/state/runs/$ID-source-$STAMP-$$.json"
DEFAULT_BRANCH="main"; INTEGRATION_BRANCH="recursion/integration"
PROJECT_CONFIG_FILE="$ROOT/state/project-config/${PROJECT_ID:-}.json"
if [ -f "$PROJECT_CONFIG_FILE" ]; then
  DEFAULT_BRANCH="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("default_branch") or "main")' "$PROJECT_CONFIG_FILE" 2>/dev/null || echo main)"
  INTEGRATION_BRANCH="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("integration_branch") or "recursion/integration")' "$PROJECT_CONFIG_FILE" 2>/dev/null || echo recursion/integration)"
fi
QUARANTINE_FILE="$ROOT/state/runs/$ID-termination-unconfirmed.json"
UNCONFIRMED_PROCESS_GROUP=0

if [ -e "$QUARANTINE_FILE" ]; then
  log "task $ID is quarantined after unconfirmed worker termination; explicit observed recovery is required"
  exit 75
fi

CONTRACT_DIGEST="$(python3 - "$TASK" <<'PY'
import hashlib,json,sys
text=open(sys.argv[1],encoding='utf-8',errors='surrogateescape').read()
header,sep,body=text.partition('\n\n')
keys={'id','project_id','pilot_id','goal_id','goal','engine','verifier_engine','repo',
       'acceptance','workdir',
       'allowed_paths','non_goals','worker_class','model_alias','verifier_model_alias','capacity_pool','pool_id',
       'deadline_seconds','verifier_deadline_seconds','network_policy','evidence_contract',
       'verification','expected_change','gate_class','gate_files','controller_gate_files',
       'gate_contract','source_head','source_branch','charter_sha256','plan_id','plan_digest',
       'autonomy','seed','seed_manifest','seed_manifest_sha256','seed_digest'}
fields={}
for line in header.splitlines():
    if ':' in line and not line[:1].isspace():
        key,value=line.split(':',1)
        if key.strip() in keys: fields[key.strip()]=value.strip()
raw=json.dumps({'fields':fields,'body':body if sep else ''},sort_keys=True,
               separators=(',',':'),ensure_ascii=False).encode()
print(hashlib.sha256(raw).hexdigest())
PY
)" || { log "task contract digest could not be measured"; exit 74; }

reject_preworker_contract(){ # stable controller-owned category
  local category="$1" fallback="${2:-2}" rc
  [ -n "${PILOT_ID:-}" ] || return "$fallback"
  [ -x "$ROOT/scripts/project-reconcile.py" ] || { log "pilot contract rejector is absent"; return 95; }
  pause_checkpoint "pre-worker contract rejection" || return $?
  RECURSION_ROOT="$ROOT" python3 "$ROOT/scripts/project-reconcile.py" \
      reject-task-contract "$ID" "$category" "$CONTRACT_DIGEST" >>"$LOG" 2>&1; rc=$?
  if [ "$rc" -eq 0 ]; then
    log "pilot task contract rejected before worker launch category=$category; task and goal are blocked"
    return 78
  fi
  log "pilot task contract rejection failed closed category=$category rc=$rc"; return "$rc"
}

# Pilot windows are an execution capability, not a caller convention. Every path into work.sh
# (queue actuator, legacy drain, manual continuation, assist resume) reaches this same check.
window_checkpoint(){
  local stage="${1:-execution}" rc
  WINDOW_AUTHORITY_FAILURE=""
  [ -z "$PILOT_ID" ] && return 0
  [ -x "$ROOT/scripts/project-reconcile.py" ] \
    || { WINDOW_AUTHORITY_FAILURE="window-authority-unavailable";
         log "pilot task $ID has no window authority checker during $stage"; return 75; }
  RECURSION_ROOT="$ROOT" python3 "$ROOT/scripts/project-reconcile.py" \
    check-work-authority "$ID" >/dev/null 2>>"$LOG"; rc=$?
  [ "$rc" -eq 0 ] && return 0
  case "$rc" in
    4)
      WINDOW_AUTHORITY_FAILURE="window-authority-revoked"
      log "pilot task $ID has proved-invalid open-window authority during $stage" ;;
    74)
      WINDOW_AUTHORITY_FAILURE="window-authority-unavailable"
      log "pilot task $ID open-window authority could not be measured during $stage" ;;
    75)
      WINDOW_AUTHORITY_FAILURE="window-authority-deferred"
      log "pilot task $ID open-window authority is temporarily deferred during $stage" ;;
    *)
      WINDOW_AUTHORITY_FAILURE="window-authority-unavailable"
      log "pilot task $ID window authority checker failed unexpectedly during $stage rc=$rc" ;;
  esac
  return 75
}
fresh_window_admission(){
  local rc
  [ -z "$PILOT_ID" ] && return 0
  RECURSION_ROOT="$ROOT" python3 "$ROOT/scripts/project-reconcile.py" admit-work "$ID" \
    >/dev/null 2>>"$LOG"; rc=$?
  if [ "$rc" -ne 0 ]; then
    log "pilot task $ID failed fresh remote/provider worker admission rc=$rc"
    [ "$rc" -eq 4 ] && return 4
    return 75
  fi
}
window_checkpoint startup || exit 75

# --- canonical per-project lock: at most one worker per project; different projects may run
# concurrently. The project id comes from the task, never the model-authored decision envelope.
# Legacy non-pilot tasks fall back to their workstream. mkdir is atomic. -------------------
mkdir -p "$ROOT/state/.locks" 2>/dev/null || true
LOCK_KEY="${PROJECT_ID:-${WORKSTREAM:-none}}"
case "$LOCK_KEY" in ''|*[!A-Za-z0-9._-]*) log "invalid canonical project/workstream lock identity"; exit 2 ;; esac
WSLOCK="$ROOT/state/.locks/project-$LOCK_KEY.lock"
WSLOCK_HELD=0
project_lock_acquire(){
  [ -x "$ROOT/scripts/runtime-state.py" ] || { log "witnessed project lock helper is absent"; return 73; }
  python3 "$ROOT/scripts/runtime-state.py" lock-acquire "$WSLOCK" "$OWNER_PID" >>"$LOG" 2>&1
}
project_lock_release(){
  [ "$WSLOCK_HELD" = 1 ] || return 0
  if python3 "$ROOT/scripts/runtime-state.py" lock-release "$WSLOCK" "$OWNER_PID" >>"$LOG" 2>&1; then
    WSLOCK_HELD=0
    return 0
  fi
  log "project lock release failed closed for $WSLOCK"
  return 74
}
if ! project_lock_acquire; then
  log "project $LOCK_KEY already has an active worker (held $WSLOCK) — deferring without success"
  exit 75
fi
WSLOCK_HELD=1
# ONE EXIT trap, both cleanups. A second `trap ... EXIT` REPLACES this one rather than
# adding to it — which is how the heavy-gate release silently disabled the workstream-lock
# release, leaking a lock per run and wedging the next run of that workstream at
# "already building". `declare -F` covers the window before release_heavy is defined,
# since work.sh can exit early (no acceptance test, unprovable base) with the trap armed.
trap 'declare -F release_heavy >/dev/null 2>&1 && release_heavy; declare -F engine_slot_release >/dev/null 2>&1 && engine_slot_release; project_lock_release || true' EXIT
ARMLOG="$LOG_DIR/$DATE-arm-$ID-$STAMP.out"
# body = the detailed spec (everything after the first blank line)
BODY="$(awk 'f{print} /^[[:space:]]*$/{f=1}' "$TASK")"

log "task=$ID ws=${WORKSTREAM:-?} engine=$ENGINE repo=$REPO_DIR workdir=$WORKDIR"
log "acceptance: ${ACCEPTANCE:-<none>}"

# --- status goes through taskctl (B3) so leases and events actually happen. --------------
# This used to be a bare `sed`. That is why a killed worker left `status: running` forever:
# nothing held a lease, so nothing could tell a live run from a corpse. Routing the lifecycle
# transitions through taskctl gives each one an expiring lease and an event-log entry, and the
# reaper can then reclaim what dies. Statuses taskctl has no verb for still fall back to a
# direct edit, so a checkout without taskctl degrades rather than breaks.
TASKCTL="$ROOT/scripts/taskctl.sh"
set_status(){
  local st="$1" note="${2:-}"
  pause_checkpoint "task transition to $st" || return 75
  if [ -x "$TASKCTL" ]; then
    case "$st" in
      passed|failed|moot|resolved|stale)
        RECURSION_ROOT="$ROOT" TASKCTL_FILE="$TASK" bash "$TASKCTL" finish "$ID" "$st" "$note" >/dev/null 2>&1
        return $? ;;
      blocked)
        RECURSION_ROOT="$ROOT" TASKCTL_FILE="$TASK" bash "$TASKCTL" block "$ID" "$note" >/dev/null 2>&1
        return $? ;;
      escalated)
        RECURSION_ROOT="$ROOT" TASKCTL_FILE="$TASK" bash "$TASKCTL" finish "$ID" escalated "$note" >/dev/null 2>&1
        return $? ;;
    esac
  fi
  sed -i "s#^status: .*#status: $st#" "$TASK" 2>/dev/null || true
}

terminal_status(){ # status note
  local st="$1" note="${2:-}" rc
  set_status "$st" "$note"; rc=$?
  if [ "$rc" -ne 0 ]; then
    log "canonical task transition to $st failed (rc=$rc) — refusing downstream notification/publication/cleanup"
  fi
  return "$rc"
}

terminal_side_effect_allowed(){ # stage
  [ ! -f "$ROOT/state/PAUSE" ] || {
    log "PAUSED after canonical terminal transition — suppressing ${1:-terminal side effect} without changing terminal outcome"
    return 1
  }
}

test_pause_after_terminal(){
  [ "${RECURSION_TEST_PAUSE_AFTER_TERMINAL:-0}" != 1 ] || {
    : > "$ROOT/state/PAUSE" 2>/dev/null \
      || log "test failpoint could not create PAUSE after terminal transition"
  }
}

terminal_ledger(){
  local rc
  if commit_state; then rc=0; else rc=$?; fi
  [ "$rc" -eq 0 ] \
    || log "post-terminal ledger commit deferred/failed (rc=$rc); canonical terminal state remains authoritative"
  return 0
}

# Claim the task. taskctl refuses a task whose lease is still LIVE, which is the double-run
# guard: on 2026-08-06 an arm auto-retried into a second hour-long run on the same task and the
# orphan had to be killed by hand. Refusing here is cheaper than detecting it later.
claim_task(){
  pause_checkpoint "task lease" || return 75
  [ -x "$TASKCTL" ] || { set_status running; return 0; }
  RECURSION_ROOT="$ROOT" TASKCTL_FILE="$TASK" bash "$TASKCTL" start "$ID" "work.sh:$$@$(hostname)" >>"$LOG" 2>&1
  local rc=$?
  case "$rc" in
    0) return 0 ;;
    4) log "$ID is already leased by a live worker — exiting rather than double-running it."; return 1 ;;
    # Any OTHER failure means the canonical state transition was not proven. Running anyway
    # creates an unleased action the control plane cannot reconcile, so fail closed.
    *) log "taskctl start failed (rc=$rc) — refusing to run without canonical state"; return "$rc" ;;
  esac
}

# ledger writes (task status + BOARD) go to loop/auto, like every other loop write.
# Under parallel loops several work.sh share this one $ROOT tree/index, so serialize the ledger
# commit behind a short lock (spin <=30s, then proceed). Serial callers acquire it instantly.
ledger_lock(){
  mkdir -p "$ROOT/state/.locks" 2>/dev/null || return 73
  python3 "$ROOT/scripts/runtime-state.py" lock-acquire "$ROOT/state/.locks/ledger.lock" "$OWNER_PID" \
    >>"$LOG" 2>&1 || { log "ledger lock contended — refusing unlocked commit"; return 73; }
}
ledger_unlock(){
  python3 "$ROOT/scripts/runtime-state.py" lock-release "$ROOT/state/.locks/ledger.lock" "$OWNER_PID" \
    >>"$LOG" 2>&1 || log "ledger lock release failed closed"
}
commit_state(){
  pause_checkpoint "state commit" || return 75
  if git rev-parse --abbrev-ref HEAD 2>/dev/null | grep -q '^loop/auto$'; then
    ledger_lock || return $?
    git add -A state/ 2>/dev/null || true
    git diff --cached --quiet 2>/dev/null || \
      git -c user.name="recursion-loop" -c user.email="loop@recursion.local" \
        commit -q -m "loop: task $ID -> $(field status)" 2>/dev/null || true
    ledger_unlock
  fi
}

# escalation carries the context handle (Workstream + Pointer) — morning-brief.md format.
ESCALATION_PENDING=0; ESCALATION_REASON=""; ESCALATION_RC=""
record_escalation_inbox(){
  local reason="$1" rc="${2:-}"
  { echo
    echo "## build escalation — $ID  ($(date -u +%FT%TZ))"
    echo "**Workstream:** ${WORKSTREAM:-unknown}"
    echo "**Pointer:** state/tasks/$ID.md · branch $BRANCH · arm log ${ARMLOG#$ROOT/}"
    echo "Blocked on: $reason"
    [ -n "$rc" ] && echo "Acceptance: \`$ACCEPTANCE\` exited $rc"
    echo "Options: inspect the worktree/branch, fix the task or its acceptance, then \`recursion work $ID\` (or \`recursion requeue $ID\`)."
    echo "Close it with: \`recursion resolve $ID \"<what you decided>\"\`."
  } >> state/INBOX.md || { log "durable escalation INBOX append failed"; return 74; }
  ESCALATION_PENDING=1; ESCALATION_REASON="$reason"; ESCALATION_RC="$rc"
  log "escalated: $reason"
}

prepare_escalation(){
  pause_checkpoint "escalation state transition" || return 75
  record_escalation_inbox "$@"
}

deliver_prepared_escalation(){
  [ "$ESCALATION_PENDING" = 1 ] || return 0
  terminal_side_effect_allowed "escalation notification" || return 0
  # Lead the ping with a [id] tag: there is no reply<->item correlation, so referencing the tag
  # (in a reply or a `recursion resolve <id>`) is how a decision reaches the right escalation.
  notify "🔔 [$ID] build blocked — $ESCALATION_REASON
Reply referencing [$ID], or on the box: recursion resolve $ID \"...\""
  ESCALATION_PENDING=0
}

escalate(){
  prepare_escalation "$@" || return $?
  deliver_prepared_escalation
}

# Timeout and verifier failures carry a stable run identity. Record that identity before the
# human-facing ask so reconciliation or a repeated caller cannot create duplicate escalations.
prepare_escalation_once(){ # key reason [acceptance-rc]
  local key="$1" reason="$2" acceptance_rc="${3:-}" recorded record_rc
  pause_checkpoint "canonical escalation transition" || return 75
  if [ -x "$TASKCTL" ]; then
    recorded="$(RECURSION_ROOT="$ROOT" bash "$TASKCTL" esc-record "$key" "$reason" \
      "${WORKSTREAM:-unknown}" "state/tasks/$ID.md" 2>>"$LOG")"; record_rc=$?
    [ "$record_rc" -eq 0 ] \
      || { log "canonical escalation record failed key=$key rc=$record_rc"; return "$record_rc"; }
    case "$recorded" in
      DUP*) log "suppressed duplicate escalation $key ($recorded)"; ESCALATION_PENDING=0; return 0 ;;
      NEW\ *) : ;;
      *) log "canonical escalation record returned an invalid acknowledgement: ${recorded:-empty}"; return 74 ;;
    esac
  elif [ -n "${PILOT_ID:-}" ]; then
    log "pilot escalation requires taskctl canonical recording"
    return 74
  fi
  record_escalation_inbox "$reason" "$acceptance_rc"
}

escalate_once(){ # key reason
  prepare_escalation_once "$@" || return $?
  deliver_prepared_escalation
}

complete_terminal_escalation(){ # status reason [key] [acceptance-rc]
  local status="$1" reason="$2" key="${3:-}" acceptance_rc="${4:-}" rc
  if [ -n "$key" ]; then
    prepare_escalation_once "$key" "$reason" "$acceptance_rc" || return $?
  else
    prepare_escalation "$reason" "$acceptance_rc" || return $?
  fi
  terminal_status "$status" "$reason"; rc=$?
  [ "$rc" -eq 0 ] || return "$rc"
  test_pause_after_terminal
  terminal_ledger
  deliver_prepared_escalation
  return 0
}

# An unconfirmed process-group cancellation is not a terminal task outcome. The worker may
# still be alive and may still hold files or credentials, so preserve the lease, worktree, and
# evidence behind a durable marker. Only taskctl quarantine-release, after an observed dead
# process group, may turn this into a recoverable stale task.
quarantine_unconfirmed(){
  local reason="$1" key="termination-unconfirmed:$ID:${LAST_RUN_ID:-none}" tmp
  mkdir -p "$ROOT/state/runs" || return 74
  tmp="${QUARANTINE_FILE}.tmp.$$"
  python3 - "$tmp" "$QUARANTINE_FILE" "$ID" "$LAST_RUN_ID" "$UNCONFIRMED_PROCESS_GROUP" "$WT" "$LAST_RUN_EVIDENCE" "$reason" <<'PY' || return 74
import json, os, sys, time
tmp, target, task, run_id, pgid, worktree, evidence, reason = sys.argv[1:]
value = {"schema_version": 1, "task_id": task, "run_id": run_id,
         "process_group": int(pgid), "worktree": worktree, "evidence": evidence,
         "reason": reason, "observed_terminal": False,
         "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
    json.dump(value, out, sort_keys=True); out.write("\n"); out.flush(); os.fsync(out.fileno())
os.replace(tmp, target)
PY
  # A restart/retry cannot steal this task even after its original TTL. Refresh the lease while
  # the marker is installed; failure to refresh is itself fail-closed and leaves the marker.
  if [ -x "$TASKCTL" ]; then
    RECURSION_ROOT="$ROOT" TASKCTL_FILE="$TASK" bash "$TASKCTL" heartbeat "$ID" >>"$LOG" 2>&1 \
      || { log "termination quarantine could not refresh the task lease"; return 74; }
  fi
  prepare_escalation_once "$key" "$reason" 96 || return $?
  log "termination quarantine installed for $ID; lease/worktree/evidence retained"
}

# --- acceptance is the contract: no test -> no autonomy (delegation-design.md) ----------
if [ -z "$ACCEPTANCE" ]; then
  if [ -n "${PILOT_ID:-}" ]; then
    reject_preworker_contract acceptance-missing; exit $?
  fi
  board add "$ID" "${WORKSTREAM:-?}" "$ENGINE" "${MODEL:-$ENGINE}" blocked "no acceptance test"
  complete_terminal_escalation escalated \
    "no machine-checkable acceptance test (autonomy requires one)" || exit $?
  exit 0
fi

# Refuse deterministic task-envelope defects before validating the gate projection.  Otherwise
# an independently malformed deadline/model/network field can be hidden behind a secondary
# missing/stale gate contract and terminalized under the wrong controller category.
validate_worker_metadata(){
  local max_timeout deadline verifier_deadline
  max_timeout="${RECURSION_WORKER_MAX_TIMEOUT_S:-86400}"
  case "$max_timeout" in ''|*[!0-9]*) log "worker maximum timeout must be a decimal integer"; exit 95 ;; esac
  [ "${#max_timeout}" -le 10 ] || { log "worker maximum timeout is too large"; exit 95; }
  max_timeout=$((10#$max_timeout))
  [ "$max_timeout" -ge 1 ] && [ "$max_timeout" -le 86400 ] \
    || { log "worker maximum timeout must be 1..86400"; exit 95; }
  case "$DEADLINE_SECONDS:$VERIFIER_DEADLINE_SECONDS" in
    *[!0-9:]*|:*|*:) reject_preworker_contract deadline-invalid 2; exit $? ;;
  esac
  [ "${#DEADLINE_SECONDS}" -le 10 ] && [ "${#VERIFIER_DEADLINE_SECONDS}" -le 10 ] \
    || { reject_preworker_contract deadline-invalid 2; exit $?; }
  deadline=$((10#$DEADLINE_SECONDS)); verifier_deadline=$((10#$VERIFIER_DEADLINE_SECONDS))
  [ "$deadline" -ge 1 ] && [ "$deadline" -le "$max_timeout" ] \
    || { reject_preworker_contract deadline-invalid 2; exit $?; }
  [ "$verifier_deadline" -ge 1 ] && [ "$verifier_deadline" -le "$max_timeout" ] \
    || { reject_preworker_contract deadline-invalid 2; exit $?; }
  case "$NETWORK_POLICY" in provider-only|none) ;; *) reject_preworker_contract network-policy-invalid 2; exit $? ;; esac
  [ "$VERIFICATION_POLICY" = independent ] \
    || { reject_preworker_contract verification-policy-invalid 2; exit $?; }
  [ "$ENGINE" != hermes ] \
    || { reject_preworker_contract worker-engine-invalid 93; exit $?; }
  case "$VERIFIER_ENGINE" in
    hermes|gate|none|deepseek|openrouter)
      reject_preworker_contract worker-engine-invalid 93; exit $? ;;
  esac
  [ -n "$MODEL_ALIAS" ] || { reject_preworker_contract model-alias-invalid 2; exit $?; }
  [ -n "$VERIFIER_MODEL_ALIAS" ] || { reject_preworker_contract verifier-model-alias-invalid 2; exit $?; }
  [ -n "$MODEL_ALIAS" ] && [ -n "$VERIFIER_MODEL_ALIAS" ] && [ "$MODEL_ALIAS" != "$VERIFIER_MODEL_ALIAS" ] \
    || { reject_preworker_contract model-separation-invalid 2; exit $?; }
  [ -x "$ROOT/scripts/resolve-model-alias.py" ] || { log "model alias resolver is missing"; exit 95; }
  if RESOLVED_MODEL="$(python3 "$ROOT/scripts/resolve-model-alias.py" "$MODEL_ALIAS" implementer 2>>"$LOG")"; then
    alias_rc=0
  else alias_rc=$?; fi
  if [ "$alias_rc" -ne 0 ]; then
    case "$alias_rc" in 2|3) reject_preworker_contract model-alias-invalid 95; exit $? ;;
      *) log "implementer model resolver controller failure rc=$alias_rc"; exit 95 ;; esac
  fi
  if RESOLVED_VERIFIER_MODEL="$(python3 "$ROOT/scripts/resolve-model-alias.py" "$VERIFIER_MODEL_ALIAS" verifier 2>>"$LOG")"; then
    alias_rc=0
  else alias_rc=$?; fi
  if [ "$alias_rc" -ne 0 ]; then
    case "$alias_rc" in 2|3) reject_preworker_contract verifier-model-alias-invalid 95; exit $? ;;
      *) log "verifier model resolver controller failure rc=$alias_rc"; exit 95 ;; esac
  fi
  # An engine-qualified alias value ("engine:model", D-015) may never reroute a role away from
  # its declared engine; the qualifier is stripped only after it proves equal to the contract
  # engine for that role (implementer -> engine:, verifier -> verifier_engine:).
  strip_alias_engine(){ # value role-engine -> stdout model part; fails on an engine mismatch
    local value="$1" role_engine="$2" engine_part model_part
    case "$value" in
      *:*)
        engine_part="${value%%:*}"; model_part="${value#*:}"
        [ "$engine_part" = "$role_engine" ] && [ -n "$model_part" ] || return 1
        printf '%s\n' "$model_part" ;;
      *) printf '%s\n' "$value" ;;
    esac
  }
  if ! RESOLVED_MODEL="$(strip_alias_engine "$RESOLVED_MODEL" "$ENGINE")"; then
    reject_preworker_contract model-alias-engine-mismatch 2; exit $?
  fi
  if ! RESOLVED_VERIFIER_MODEL="$(strip_alias_engine "$RESOLVED_VERIFIER_MODEL" "$VERIFIER_ENGINE")"; then
    reject_preworker_contract model-alias-engine-mismatch 2; exit $?
  fi
  [ -n "$CAPACITY_POOL" ] && [ "$CAPACITY_POOL" != unassigned ] \
    || { reject_preworker_contract capacity-pool-invalid 2; exit $?; }
}
# Phase-5 model pins must resolve BEFORE plugin admission: model.required=true
# refuses an empty RESOLVED_MODEL/RESOLVED_VERIFIER_MODEL. Formerly gated on
# pilot_id only, which left non-pilot worker-result-v1 canaries failing closed.
if [ "$STRICT_PHASE5" = 1 ]; then
  validate_worker_metadata
fi

# --- Declarative engine plugins (D-015). A non-builtin engine must resolve to a reviewed
# manifest plan BEFORE any lease, board, or worktree mutation. Refusals fail closed through
# the existing rejection paths; an unavailable engine (probe/capacity) DEFERS like a held
# project lock — the task stays queued; only ignored runtime plan files under state/runs are
# written, and a deferral removes them. Note this also runs before gate validation, so a
# NON-pilot task that has BOTH a dead engine and an invalid gate now escalates on the engine
# first — a deliberate narrowing of the historical invalid-gate precedence below.
ENGINE_PLAN_IMPLEMENTER=""; ENGINE_PLAN_VERIFIER=""; ENGINE_SLOT_LOCKS=()
ENGINE_TOKEN_IMPLEMENTER=""; ENGINE_TOKEN_VERIFIER=""
ENGINE_ASSIGNMENT_IMPLEMENTER=""; ENGINE_ASSIGNMENT_VERIFIER=""
engine_is_builtin(){ case "$1" in none|claude|hermes|gate|deepseek|openrouter) return 0 ;; *) return 1 ;; esac; }
plugin_probe_ok(){ # plan-file: absent probe passes; a declared probe needs a 2xx from its URL.
  # Probe from inside the provider-only namespace when it exists, so "reachable" is measured
  # where the worker will actually run, not on the controller's unrestricted network.
  local plan="$1" profile provider endpoints namespace profile_json controller_root controller_sha policy
  profile="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["network"]["profile"])' "$plan" 2>/dev/null || true)"
  provider="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["billing"]["provider"])' "$plan" 2>/dev/null || true)"
  endpoints="$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["network"]["endpoints"]))' "$plan" 2>/dev/null || true)"
  policy="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["network"]["policy"])' "$plan" 2>/dev/null || true)"
  [ -n "$profile" ] && [ -n "$provider" ] || return 1
  [ "$policy" = provider-only ] || return 0
  controller_root="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("authority",{}).get("controller_repository", ""))' "$plan" 2>/dev/null || true)"
  controller_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("authority",{}).get("controller_sha", ""))' "$plan" 2>/dev/null || true)"
  [ -n "$controller_root" ] && [ -n "$controller_sha" ] || return 1
  local -a profile_args=("$profile" --provider "$provider" --controller-root "$controller_root" --controller-commit "$controller_sha")
  local endpoint
  for endpoint in $endpoints; do profile_args+=(--endpoint "$endpoint"); done
  profile_json="$(python3 "$ROOT/scripts/provider-egress.py" "${profile_args[@]}" 2>/dev/null)" || return 1
  namespace="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["namespace"])' "$profile_json")"
  [ -e "/run/netns/$namespace" ] && command -v ip >/dev/null 2>&1 || return 1
  local -a runner=(ip netns exec "$namespace" python3)
  "${runner[@]}" - "$plan" <<'PY'
import json, os, sys, urllib.request
try:
    with open(sys.argv[1], "rb") as source:
        raw = source.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("engine plan is too large")
    plan = json.loads(raw.decode("utf-8", "strict"), parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)))
except (OSError, UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
    raise SystemExit(1)
if not isinstance(plan, dict):
    raise SystemExit(1)
probe = plan.get("availability_probe")
if not probe:
    raise SystemExit(0)
url = os.environ.get(probe.get("url_env", ""), "").strip()
if not url:
    raise SystemExit(1)
try:
    timeout = probe.get("timeout_s", 5)
    if type(timeout) is not int or not 0 < timeout <= 60:
        raise SystemExit(1)
    with urllib.request.urlopen(url, timeout=timeout) as response:
        raise SystemExit(0 if 200 <= response.status < 300 else 1)
except Exception:
    raise SystemExit(1)
PY
}
engine_slot_release(){
  local slot
  for slot in ${ENGINE_SLOT_LOCKS[@]+"${ENGINE_SLOT_LOCKS[@]}"}; do
    python3 "$ROOT/scripts/runtime-state.py" lock-release "$slot" "$OWNER_PID" >>"$LOG" 2>&1 \
      || log "engine slot release failed closed for $slot"
  done
  ENGINE_SLOT_LOCKS=()
}
plugin_slot_acquire(){ # engine cap: a witnessed per-slot lock, held for the run, is the cap —
  local engine="$1" cap="$2" slot i   # a unit count is stale by launch time and races projects.
  { [ -n "$cap" ] && [ "$cap" != null ]; } || return 0
  [ -x "$ROOT/scripts/runtime-state.py" ] || { log "witnessed engine slot helper is absent"; return 95; }
  for ((i = 0; i < cap; i++)); do
    slot="$ROOT/state/.locks/engine-$engine-slot-$i.lock"
    if python3 "$ROOT/scripts/runtime-state.py" lock-acquire "$slot" "$OWNER_PID" >>"$LOG" 2>&1; then
      ENGINE_SLOT_LOCKS+=("$slot")
      return 0
    fi
  done
  return 1
}
plugin_resolve_plan(){ # role engine plan-path: emit a token-free plan and mint ephemeral capability
  local role="$1" engine="$2" plan_path="$3" rc profile assignment key_file token
  if python3 "$ROOT/scripts/resolve-engine.py" "$engine" --role "$role" \
      --emit-plan "$plan_path" --controller-root "$ROOT" --controller-commit "$CONTROLLER_SHA" 2>>"$LOG"; then
    rc=0
  else rc=$?; fi
  if [ "$rc" -ne 0 ]; then
    log "engine=$engine refused by the plugin resolver for the $role role rc=$rc (2=invalid manifest/role, 3=disabled or absent)"
    return 92
  fi
profile="$(python3 - "$plan_path" <<'PY'
import json,sys
try:
    with open(sys.argv[1], "rb") as source:
        raw=source.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024: raise ValueError("engine plan is too large")
    plan=json.loads(raw.decode("utf-8", "strict"), parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)))
except (OSError,UnicodeError,ValueError,TypeError,OverflowError,RecursionError):
    raise SystemExit(1)
if not isinstance(plan,dict): raise SystemExit(1)
value=plan.get("egress_profile")
if value is not None: print(value)
PY
  )" || { log "engine=$engine provider profile could not be read"; return 95; }
  if [ -n "$profile" ]; then
    key_file="${RECURSION_PROVIDER_CAPABILITY_KEY_FILE:-/etc/recursion/provider-capability.key}"
    # Basic userinfo uses ':' as its sole delimiter; keep the canonical assignment identity
    # entirely in the unescaped userinfo-safe alphabet so standard clients cannot reinterpret it.
    assignment="$ID.$CONTRACT_DIGEST"
    token="$(python3 "$ROOT/scripts/assignment-token.py" "$profile" "$assignment" \
      --key-file "$key_file" 2>>"$LOG")" \
      || { log "engine=$engine provider assignment capability could not be issued"; return 96; }
    case "$role" in
      implementer) ENGINE_TOKEN_IMPLEMENTER="$token"; ENGINE_ASSIGNMENT_IMPLEMENTER="$assignment" ;;
      verifier) ENGINE_TOKEN_VERIFIER="$token"; ENGINE_ASSIGNMENT_VERIFIER="$assignment" ;;
      *) log "unknown plugin role '$role'"; return 92 ;;
    esac
  fi
}
issue_builtin_capability(){ # role engine: mint the same ephemeral proxy capability for builtin Claude
  local role="$1" engine="$2" key_file assignment token
  [ "$engine" = claude ] || return 0
  [ "$NETWORK_POLICY" = provider-only ] || return 0
  # The logical provider-network marker is an explicit hermetic test bypass.  It has no
  # production network namespace or proxy, so requiring a deployment key there would only
  # obscure the envelope tests; every real provider-only launch takes this branch.
  [ "${RECURSION_TEST_PROVIDER_NETWORK:-}" = logical ] && return 0
  key_file="${RECURSION_PROVIDER_CAPABILITY_KEY_FILE:-/etc/recursion/provider-capability.key}"
  assignment="$ID.$CONTRACT_DIGEST"
  token="$(python3 "$ROOT/scripts/assignment-token.py" anthropic "$assignment" \
    --key-file "$key_file" 2>>"$LOG")" \
    || { log "engine=$engine provider assignment capability could not be issued for builtin $role"; return 96; }
  case "$role" in
    implementer) ENGINE_TOKEN_IMPLEMENTER="$token"; ENGINE_ASSIGNMENT_IMPLEMENTER="$assignment" ;;
    verifier) ENGINE_TOKEN_VERIFIER="$token"; ENGINE_ASSIGNMENT_VERIFIER="$assignment" ;;
    *) log "unknown builtin role '$role'"; return 92 ;;
  esac
}
plugin_plan_checks(){ # plan resolved-model engine role: billing admission + model requirement
  local plan="$1" resolved_model="$2" engine="$3" role="$4" basis mreq
  basis="$(python3 - "$plan" <<'PY'
import json,sys
try:
    with open(sys.argv[1], "rb") as source: raw=source.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024: raise ValueError("engine plan is too large")
    plan=json.loads(raw.decode("utf-8", "strict"), parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)))
    print(plan["billing"]["basis"] if isinstance(plan,dict) else "")
except (OSError,UnicodeError,ValueError,TypeError,OverflowError,RecursionError,KeyError):
    raise SystemExit(1)
PY
  )" || { log "engine plan billing could not be read"; return 95; }
  if [ "$basis" = metered ] && [ "${RECURSION_ALLOW_METERED:-0}" != 1 ]; then
    log "engine=$engine ($role) bills as metered and RECURSION_ALLOW_METERED is unset"
    return 93
  fi
  mreq="$(python3 - "$plan" <<'PY'
import json,sys
try:
    with open(sys.argv[1], "rb") as source: raw=source.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024: raise ValueError("engine plan is too large")
    plan=json.loads(raw.decode("utf-8", "strict"), parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)))
    if not isinstance(plan,dict) or type(plan.get("model_required")) is not bool: raise ValueError("model requirement is invalid")
    print(1 if plan["model_required"] else 0)
except (OSError,UnicodeError,ValueError,TypeError,OverflowError,RecursionError,KeyError):
    raise SystemExit(1)
PY
  )" || { log "engine plan model requirement could not be read"; return 95; }
  if [ "$mreq" = 1 ] && [ -z "$resolved_model" ]; then
    log "engine=$engine requires a model and the $role has none resolved"
    return 92
  fi
}
plugin_engine_gate(){ # engine plan: availability probe + witnessed capacity slot
  local engine="$1" plan="$2" cap rc
  cap="$(python3 - "$plan" <<'PY'
import json,sys
try:
    with open(sys.argv[1], "rb") as source: raw=source.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024: raise ValueError("engine plan is too large")
    value=json.loads(raw.decode("utf-8", "strict"), parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)))
    if not isinstance(value,dict): raise ValueError("engine plan is invalid")
    cap=value["concurrency_cap"]
    if cap is None: print("")
    elif type(cap) is int and cap >= 1: print(cap)
    else: raise ValueError("engine concurrency cap is invalid")
except (OSError,UnicodeError,ValueError,TypeError,OverflowError,RecursionError,KeyError):
    raise SystemExit(1)
PY
  )" || { log "engine plan capacity could not be read"; return 95; }
  plugin_probe_ok "$plan" || { log "engine=$engine availability probe failed"; return 89; }
  if plugin_slot_acquire "$engine" "$cap"; then :; else
    rc=$?
    [ "$rc" -ne 95 ] || return 95
    log "engine=$engine at concurrency cap ($cap)"
    return 89
  fi
}
plugin_engine_admission(){
  local plan_dir rc
  # Self-fail-closed: this function only has meaning when at least one role runs a plugin.
  if engine_is_builtin "$ENGINE" \
     && { [ "${VERIFICATION_POLICY:-}" != independent ] || engine_is_builtin "$VERIFIER_ENGINE"; }; then
    log "plugin admission entered with no plugin engine"; return 92
  fi
  [ -f "$ROOT/scripts/resolve-engine.py" ] || { log "engine plugin resolver is missing"; return 95; }
  CONTROLLER_SHA="$(git -C "$ROOT" rev-parse --verify HEAD 2>/dev/null || true)"
  [ -n "$CONTROLLER_SHA" ] || { log "controller commit could not be established for engine admission"; return 95; }
  plan_dir="$ROOT/state/runs"
  mkdir -p "$plan_dir" 2>/dev/null || { log "engine plan directory could not be created"; return 95; }
  mkdir -p "$ROOT/state/.locks" 2>/dev/null || { log "engine slot lock directory could not be created"; return 95; }
  if ! engine_is_builtin "$ENGINE"; then
    ENGINE_PLAN_IMPLEMENTER="$plan_dir/$ID-engine-plan-implementer-$STAMP.json"
    plugin_resolve_plan implementer "$ENGINE" "$ENGINE_PLAN_IMPLEMENTER" \
      || { rc=$?; ENGINE_PLAN_IMPLEMENTER=""; return "$rc"; }
    plugin_plan_checks "$ENGINE_PLAN_IMPLEMENTER" "${RESOLVED_MODEL:-}" "$ENGINE" implementer || return $?
  fi
  if [ "${VERIFICATION_POLICY:-}" = independent ] && ! engine_is_builtin "$VERIFIER_ENGINE"; then
    ENGINE_PLAN_VERIFIER="$plan_dir/$ID-engine-plan-verifier-$STAMP.json"
    plugin_resolve_plan verifier "$VERIFIER_ENGINE" "$ENGINE_PLAN_VERIFIER" \
      || { rc=$?; ENGINE_PLAN_VERIFIER=""; return "$rc"; }
    plugin_plan_checks "$ENGINE_PLAN_VERIFIER" "${RESOLVED_VERIFIER_MODEL:-}" "$VERIFIER_ENGINE" verifier || return $?
  fi
  if [ -n "$ENGINE_PLAN_IMPLEMENTER" ]; then
    plugin_engine_gate "$ENGINE" "$ENGINE_PLAN_IMPLEMENTER" || return $?
  fi
  if [ -n "$ENGINE_PLAN_VERIFIER" ]; then
    if [ "$VERIFIER_ENGINE" != "$ENGINE" ] || [ -z "$ENGINE_PLAN_IMPLEMENTER" ]; then
      plugin_engine_gate "$VERIFIER_ENGINE" "$ENGINE_PLAN_VERIFIER" || return $?
    fi
  fi
  return 0
}
if ! engine_is_builtin "$ENGINE" \
   || { [ "${VERIFICATION_POLICY:-}" = independent ] && ! engine_is_builtin "$VERIFIER_ENGINE"; }; then
  if plugin_engine_admission; then padm=0; else padm=$?; fi
  if [ "$padm" -ne 0 ]; then
    case "$padm" in
      89)
        rm -f -- ${ENGINE_PLAN_IMPLEMENTER:+"$ENGINE_PLAN_IMPLEMENTER"} \
                 ${ENGINE_PLAN_VERIFIER:+"$ENGINE_PLAN_VERIFIER"} 2>/dev/null || true
        log "engine=$ENGINE/verifier=$VERIFIER_ENGINE unavailable — deferring without lease or board mutation; task stays queued"
        exit 75 ;;
      93)
        if [ -n "${PILOT_ID:-}" ]; then reject_preworker_contract worker-engine-invalid 93; exit $?; fi
        ENGINE_LABEL="$ENGINE"; [ "$VERIFIER_ENGINE" = "$ENGINE" ] || ENGINE_LABEL="$ENGINE/$VERIFIER_ENGINE"
        board add "$ID" "${WORKSTREAM:-?}" "$ENGINE_LABEL" "${MODEL_ALIAS:-${MODEL:-$ENGINE}}" blocked "metered engine, flag unset"
        complete_terminal_escalation escalated \
          "a required engine (engine=$ENGINE, verifier_engine=$VERIFIER_ENGINE) bills as metered — a human sets RECURSION_ALLOW_METERED=1 in .env to allow it" || exit $?
        exit 0 ;;
      *)
        if [ -n "${PILOT_ID:-}" ]; then reject_preworker_contract worker-engine-invalid "$padm"; exit $?; fi
        ENGINE_LABEL="$ENGINE"; [ "$VERIFIER_ENGINE" = "$ENGINE" ] || ENGINE_LABEL="$ENGINE/$VERIFIER_ENGINE"
        board add "$ID" "${WORKSTREAM:-?}" "$ENGINE_LABEL" "${MODEL_ALIAS:-${MODEL:-$ENGINE}}" blocked "engine not enabled"
        complete_terminal_escalation escalated \
          "a required engine (engine=$ENGINE, verifier_engine=$VERIFIER_ENGINE) is not an enabled plugin (plugins disabled, manifest absent/invalid, or role refused) — see the arm log" || exit $?
        exit 0 ;;
    esac
  fi
fi

# Validate the gate before leasing a worker. The validator rejects literal/no-op success and
# records any declared gate files whose hashes become part of the exact-commit evidence.
GATE_CONTRACT=""
if [ -x "$ROOT/scripts/validate-gate.py" ]; then
  GATE_SOURCE_COMMIT="$(git -C "$REPO_DIR" rev-parse --verify HEAD 2>/dev/null || true)"
  GATE_CONTROLLER_COMMIT="$(git -C "$ROOT" rev-parse --verify HEAD 2>/dev/null || true)"
  [ -n "$GATE_SOURCE_COMMIT" ] && [ -n "$GATE_CONTROLLER_COMMIT" ] \
    || { log "exact source/controller commits could not be established for gate admission"; exit 74; }
  GATE_STRICT_ARGS=()
  [ -z "$PILOT_ID" ] || GATE_STRICT_ARGS+=(--strict-direct)
  GATE_STRICT_ARGS+=(--read-only-paths "$SEED $SEED_MANIFEST")
  [ -n "$PILOT_ID" ] || GATE_STRICT_ARGS+=(--legacy-compound)
  GATE_ERR="$(mktemp "${TMPDIR:-/tmp}/validate-gate.XXXXXX")" || exit 74
  if GATE_CONTRACT="$(python3 "$ROOT/scripts/validate-gate.py" --acceptance "$ACCEPTANCE" \
      --allowed-paths "${ALLOWED_PATHS:-$WORKDIR}" --gate-files "$GATE_FILES" --repo "$REPO_DIR" \
      --source-commit "$GATE_SOURCE_COMMIT" \
      --controller-root "$ROOT" --controller-gate-files "$CONTROLLER_GATE_FILES" \
      --controller-commit "$GATE_CONTROLLER_COMMIT" \
      --read-only-paths "$SEED" \
      "${GATE_STRICT_ARGS[@]}" 2>"$GATE_ERR")"; then
    gate_rc=0
  else gate_rc=$?; fi
  cat "$GATE_ERR" >>"$LOG"; rm -f "$GATE_ERR"
  if [ "$gate_rc" -ne 0 ]; then
    if [ "$gate_rc" -eq 2 ] && [ -n "${PILOT_ID:-}" ]; then
      reject_preworker_contract acceptance-invalid; exit $?
    fi
    [ "$gate_rc" -eq 2 ] || { log "gate validator controller failure rc=$gate_rc"; exit "$gate_rc"; }
    board add "$ID" "${WORKSTREAM:-?}" "$ENGINE" "${MODEL_ALIAS:-${MODEL:-$ENGINE}}" blocked "invalid acceptance gate"
    complete_terminal_escalation escalated \
      "invalid or structurally vacuous acceptance gate; no worker was launched" || exit $?
    exit 0
  fi
  if [ -n "$PILOT_ID" ]; then
    [ -n "$APPROVED_GATE_CONTRACT" ] && [ -n "$SOURCE_HEAD" ] && [ -n "$SOURCE_BRANCH" ] \
      || { reject_preworker_contract contract-projection-invalid; exit $?; }
    python3 - "$APPROVED_GATE_CONTRACT" "$GATE_CONTRACT" <<'PY' || {
import json,sys
try:
    if any(len(value.encode())>2*1024*1024 for value in sys.argv[1:]): raise ValueError('gate contract is too large')
    reject=lambda n: (_ for _ in ()).throw(ValueError(n))
    approved=json.loads(sys.argv[1],parse_constant=reject); observed=json.loads(sys.argv[2],parse_constant=reject)
except (json.JSONDecodeError,ValueError,TypeError,UnicodeError,OverflowError,RecursionError):
    raise SystemExit(1)
raise SystemExit(0 if approved == observed else 1)
PY
      log "Phase 8 approved gate contract differs from execution bytes"
      reject_preworker_contract gate-contract-drift; exit $?
    }
    observed_head="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || true)"
    observed_branch="$(git -C "$REPO_DIR" branch --show-current 2>/dev/null || true)"
    observed_dirty="$(git -C "$REPO_DIR" status --porcelain --untracked-files=all 2>/dev/null || true)"
    [ "$observed_head" = "$SOURCE_HEAD" ] && [ "$observed_branch" = "$SOURCE_BRANCH" ] \
      && [ -z "$observed_dirty" ] \
      || {
        log "Phase 8 approved source HEAD differs before worker admission"
        reject_preworker_contract source-head-drift; exit $?
      }
  fi
elif [ "$STRICT_PHASE5" = 1 ]; then
  log "Phase 5 gate validator is missing"; exit 95
fi

# Preserve the historical non-pilot precedence: an invalid gate remains the primary durable
# escalation when a legacy task also has malformed envelope metadata.  Exact-digest pilots use
# the category-precise early check above.
# validate_worker_metadata already ran pre-plugin for STRICT_PHASE5


# --- create the isolated worktree on loop/work/<id> (never touches the main tree) -------
mk_worktree(){
  local wt="$REPO_DIR/.worktrees/$ID" base def
  # per-repo worktree lock (parallel-loops.md): serialize git worktree ops so two workstreams
  # sharing a repo do not race git ref/index locks. Cross-repo streams never contend.
  local rk wtlock; rk="$(printf '%s' "$REPO_DIR" | md5sum | cut -c1-12)"
  wtlock="$ROOT/state/.locks/wt-$rk.lock"; mkdir -p "$ROOT/state/.locks" 2>/dev/null || true
  if ! python3 "$ROOT/scripts/runtime-state.py" lock-acquire "$wtlock" "$OWNER_PID" >>"$LOG" 2>&1; then
    log "worktree lock contended — refusing unlocked worktree mutation"
    return 73
  fi
  if [ -e "$wt" ]; then
    if [ "$STRICT_PHASE5" = 1 ]; then
      # Strict runs retain an existing worktree as a quarantine: removing it could
      # destroy a live or unconfirmed worker's only recovery evidence.
      log "existing worktree is retained as a quarantine; refusing to remove it before proving no worker is live: $wt"
      return 96
    fi
    # Legacy Phase 0/1 tasks never create process/evidence quarantines. Preserve
    # their historical rerun contract: discard a stale worktree after the same
    # per-repository lock, then create the fresh isolated checkout.
    log "removing stale legacy worktree before fresh dispatch: $wt"
    git -C "$REPO_DIR" worktree remove --force "$wt" 2>/dev/null || true
    rm -rf "$wt" 2>/dev/null || true
  fi
  git -C "$REPO_DIR" worktree prune 2>/dev/null || true
  # Fetch latest so the task builds on the FRESHEST merged base — this is what makes work
  # ACCUMULATE across tasks (once you merge task N, task N+1 builds on top).
  #
  # This fetch is now a REFRESH, not the decision. require_fresh_base() already proved the repo
  # is current and refused to reach here otherwise. It used to be the decision, and it decided
  # badly: on failure it logged "building on local base" and carried on, so the arm built on a
  # stale tree, the gate passed (it was testing the old tree), and merging the result REVERTED
  # whatever had been fixed upstream in the meantime.
  if [ -z "$PILOT_ID" ] && [ "${RECURSION_GIT_FETCH:-1}" = "1" ]; then
    gitr fetch --prune --quiet origin >>"$LOG" 2>&1 && log "fetched origin" || log "fetch failed at worktree time (freshness was proven moments ago)"
  fi
  if [ -n "$PILOT_ID" ] && [ -n "$SOURCE_HEAD" ]; then
    base="$SOURCE_HEAD"
  else
    def="$(git -C "$REPO_DIR" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@' || true)"; def="${def:-master}"
    # prefer the remote-tracking tip (freshest merged main); fall back to local, then current HEAD.
    if git -C "$REPO_DIR" rev-parse --verify "origin/$def" >/dev/null 2>&1; then base="origin/$def"
    elif git -C "$REPO_DIR" rev-parse --verify "$def" >/dev/null 2>&1; then base="$def"
    else base="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)"; fi
  fi
  git -C "$REPO_DIR" worktree add -B "$BRANCH" "$wt" "$base" >>"$LOG" 2>&1; local addrc=$?
  python3 "$ROOT/scripts/runtime-state.py" lock-release "$wtlock" "$OWNER_PID" >>"$LOG" 2>&1 \
    || { log "worktree lock release failed closed"; return 74; }
  [ "$addrc" -eq 0 ] || return 1
  copy_seed_into_worktree "$wt" || return 1
  printf '%s\n' "$wt"
}

# Hash path, type, mode, and bytes for every declared seed. This controller-owned digest catches
# a logical/test-mode bypass and any future mount regression in addition to the read-only bind.
seed_digest(){
  local wt="$1"
  [ -n "$SEED" ] || { printf '%s\n' none; return 0; }
  [ -x "$ROOT/scripts/seed_contract.py" ] || { log "seed contract helper is absent"; return 1; }
  local -a args=(digest --root "$wt")
  local p result
  for p in $SEED; do args+=(--path "$p"); done
  result="$(python3 "$ROOT/scripts/seed_contract.py" "${args[@]}" 2>>"$LOG")" || return 1
  python3 -c 'import json,sys
try:
    if len(sys.argv[1].encode())>2*1024*1024: raise ValueError("seed result is too large")
    value=json.loads(sys.argv[1],parse_constant=lambda n: (_ for _ in ()).throw(ValueError(n)))
    print(value["content_sha256"] if isinstance(value,dict) else "")
except (json.JSONDecodeError,ValueError,TypeError,UnicodeError,OverflowError,RecursionError,KeyError):
    raise SystemExit(1)' "$result"
}

validate_seed_source(){
  [ -n "$SEED" ] || return 0
  if [ -n "$PILOT_ID" ] || [ -n "$SEED_MANIFEST$SEED_MANIFEST_SHA256$SEED_DIGEST_EXPECTED" ]; then
    [ -n "$SEED_MANIFEST" ] && [ -n "$SEED_MANIFEST_SHA256" ] && [ -n "$SEED_DIGEST_EXPECTED" ] \
      || { log "Phase-8 seed is not bound to an approved manifest and digest"; return 1; }
    case "$SEED_MANIFEST_SHA256:$SEED_DIGEST_EXPECTED" in
      *[!0-9a-f:]*) log "Phase-8 seed digest metadata is malformed"; return 1 ;;
    esac
    [ "${#SEED_MANIFEST_SHA256}" = 64 ] && [ "${#SEED_DIGEST_EXPECTED}" = 64 ] \
      || { log "Phase-8 seed digest metadata is malformed"; return 1; }
    local observed
    observed="$(python3 "$ROOT/scripts/seed_contract.py" inspect --repo "$REPO_DIR" \
      --manifest "$SEED_MANIFEST" 2>>"$LOG")" || return 1
    mkdir -p "$ROOT/state/runs"
    python3 - "$observed" "$SEED_MANIFEST" "$SEED_MANIFEST_SHA256" \
      "$SEED_DIGEST_EXPECTED" "${SEED// /,}" "$SEED_EVIDENCE_FILE" <<'PY'
import json, os, sys, tempfile
try:
    if len(sys.argv[1].encode())>2*1024*1024: raise ValueError('seed result is too large')
    row=json.loads(sys.argv[1],parse_constant=lambda n: (_ for _ in ()).throw(ValueError(n)))
except (json.JSONDecodeError,ValueError,TypeError,UnicodeError,OverflowError,RecursionError):
    raise SystemExit(1)
if not isinstance(row,dict): raise SystemExit(1)
expected_paths=[p for p in sys.argv[5].split(',') if p]
valid=(row.get('manifest_path')==sys.argv[2]
       and row.get('manifest_sha256')==sys.argv[3]
       and row.get('content_sha256')==sys.argv[4]
       and row.get('paths')==expected_paths)
if not valid: raise SystemExit('approved seed contract disagrees with live repository')
evidence={'schema_version':1,'manifest_path':row['manifest_path'],
          'manifest_sha256':row['manifest_sha256'],'content_sha256':row['content_sha256'],
          'path_count':len(row['paths']),'file_count':row.get('file_count',0),'verified_before':True}
target=sys.argv[6]; os.makedirs(os.path.dirname(target),exist_ok=True)
fd,name=tempfile.mkstemp(prefix='.seed.',dir=os.path.dirname(target))
with os.fdopen(fd,'w',encoding='utf-8') as out:
    json.dump(evidence,out,sort_keys=True,separators=(',',':')); out.write('\n'); out.flush(); os.fsync(out.fileno())
os.replace(name,target)
PY
  fi
  return 0
}

copy_seed_into_worktree(){
  local wt="$1"
  [ -n "$SEED" ] || return 0
  local expected="$SEED_DIGEST_EXPECTED" p result
  [ -n "$expected" ] || expected="$(seed_digest "$REPO_DIR")" || return 1
  local -a args=(copy --source "$REPO_DIR" --target "$wt" --expected-digest "$expected")
  for p in $SEED; do args+=(--path "$p"); done
  result="$(python3 "$ROOT/scripts/seed_contract.py" "${args[@]}" 2>>"$LOG")" || return 1
  log "seed contract copied and verified ($(python3 -c 'import json,sys
try:
    if len(sys.argv[1].encode())>2*1024*1024: raise ValueError("seed result is too large")
    value=json.loads(sys.argv[1],parse_constant=lambda n: (_ for _ in ()).throw(ValueError(n)))
    print(value.get("file_count",0) if isinstance(value,dict) else 0)
except (json.JSONDecodeError,ValueError,TypeError,UnicodeError,OverflowError,RecursionError):
    raise SystemExit(1)' "$result") files)"
}

verify_seed_integrity(){
  [ -n "$SEED" ] || return 0
  local observed
  observed="$(seed_digest "$1")" || { SCOPE_FAIL="declared seed integrity could not be measured"; return 1; }
  [ "$observed" = "${SEED_DIGEST_EXPECTED:-$SEED_DIGEST_BEFORE}" ] \
    || { SCOPE_FAIL="worker/gate mutated a declared read-only seed or oracle"; return 1; }
}

assignment_candidate_field(){
  python3 - "$1" "$2" <<'PY'
import json, sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8"))
    item = value.get(sys.argv[2]) if isinstance(value, dict) else None
    if item is None:
        print("")
    elif isinstance(item, str) and item:
        print(item)
    else:
        raise ValueError("assignment candidate field is not a string")
except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError,
        OverflowError, RecursionError):
    raise SystemExit(1)
PY
}

make_phase5_assignment(){
  [ "$STRICT_PHASE5" = 1 ] || return 0
  [ -n "$CAPACITY_POOL" ] && [ "$CAPACITY_POOL" != unassigned ] || { log "strict Phase 5 requires a declared capacity pool"; return 1; }
  local impl_provider verifier_provider impl_profile verifier_profile impl_endpoints verifier_endpoints source_branch host
  local impl_policy verifier_policy impl_argv verifier_argv impl_candidate verifier_candidate
  local resolved_impl_engine resolved_impl_provider resolved_impl_pool resolved_impl_model
  local resolved_verifier_engine resolved_verifier_provider resolved_verifier_pool resolved_verifier_model
  impl_provider="$(python3 - "$ENGINE" "${ENGINE_PLAN_IMPLEMENTER:-}" <<'PY'
import json,sys
engine, path = sys.argv[1:]
if engine == "claude": print("anthropic-claude-code")
elif path: print(json.load(open(path, encoding="utf-8"))["billing"]["provider"])
else: print("hermes")
PY
  )" || return 1
  verifier_provider="$(python3 - "$VERIFIER_ENGINE" "${ENGINE_PLAN_VERIFIER:-}" <<'PY'
import json,sys
engine, path = sys.argv[1:]
if engine == "claude": print("anthropic-claude-code")
elif path: print(json.load(open(path, encoding="utf-8"))["billing"]["provider"])
else: print("hermes")
PY
  )" || return 1
  impl_profile="$(python3 - "$ENGINE" "${ENGINE_PLAN_IMPLEMENTER:-}" <<'PY'
import json,sys
engine, path = sys.argv[1:]
if engine == "claude": print("anthropic")
elif path: print(json.load(open(path, encoding="utf-8"))["network"]["profile"])
else: print("none")
PY
  )" || return 1
  verifier_profile="$(python3 - "$VERIFIER_ENGINE" "${ENGINE_PLAN_VERIFIER:-}" <<'PY'
import json,sys
engine, path = sys.argv[1:]
if engine == "claude": print("anthropic")
elif path: print(json.load(open(path, encoding="utf-8"))["network"]["profile"])
else: print("none")
PY
  )" || return 1
  impl_endpoints="$(python3 - "$ENGINE" "${ENGINE_PLAN_IMPLEMENTER:-}" "$impl_profile" "$ROOT/scripts/provider-egress-profiles.json" <<'PY'
import json,sys
engine, path, profile, profiles_path = sys.argv[1:]
if engine == "none": print("[]")
elif path: print(json.dumps(json.load(open(path, encoding="utf-8"))["network"]["endpoints"], separators=(",", ":")))
else: print(json.dumps(json.load(open(profiles_path, encoding="utf-8"))["profiles"][profile]["endpoints"], separators=(",", ":")))
PY
  )" || return 1
  verifier_endpoints="$(python3 - "$VERIFIER_ENGINE" "${ENGINE_PLAN_VERIFIER:-}" "$verifier_profile" "$ROOT/scripts/provider-egress-profiles.json" <<'PY'
import json,sys
engine, path, profile, profiles_path = sys.argv[1:]
if engine == "none": print("[]")
elif path: print(json.dumps(json.load(open(path, encoding="utf-8"))["network"]["endpoints"], separators=(",", ":")))
else: print(json.dumps(json.load(open(profiles_path, encoding="utf-8"))["profiles"][profile]["endpoints"], separators=(",", ":")))
PY
  )" || return 1
  source_branch="$SOURCE_BRANCH"
  [ -n "$source_branch" ] || source_branch="$(git -C "$REPO_DIR" symbolic-ref --short HEAD 2>/dev/null || printf 'detached')"
  host="${RECURSION_HOST_ID:-gs-recursion}"
  impl_policy="$(python3 - "$ENGINE" "${ENGINE_PLAN_IMPLEMENTER:-}" <<'PY'
import json,sys
engine,path=sys.argv[1:]
if path: print(json.load(open(path,encoding="utf-8"))["network"]["policy"])
else: print("provider-only")
PY
  )" || return 1
  verifier_policy="$(python3 - "$VERIFIER_ENGINE" "${ENGINE_PLAN_VERIFIER:-}" <<'PY'
import json,sys
engine,path=sys.argv[1:]
if path: print(json.load(open(path,encoding="utf-8"))["network"]["policy"])
else: print("provider-only")
PY
  )" || return 1
  [ "$NETWORK_POLICY" = "$impl_policy" ] && [ "$impl_policy" = "$verifier_policy" ] \
    || { log "task network policy does not match the immutable engine policy"; return 1; }
  impl_argv="$(python3 - "$ENGINE" "${ENGINE_PLAN_IMPLEMENTER:-}" "$RESOLVED_MODEL" <<'PY'
import json,sys
engine,path,model=sys.argv[1:]
if path:
    plan=json.load(open(path,encoding="utf-8")); value=list(plan["argv"])
    if model: value.extend(plan["model_argv_suffix"])
elif engine == "claude":
    value=["claude","-p","{{PROMPT}}","--permission-mode","acceptEdits",
           "--allowedTools","Bash(node:*)"]
    if model: value.extend(["--model","{{MODEL}}"])
elif engine == "none":
    value=[]
else: value=[engine,"{{PROMPT}}"]
print(json.dumps(value,separators=(",",":")))
PY
  )" || return 1
  verifier_argv="$(python3 - "$VERIFIER_ENGINE" "${ENGINE_PLAN_VERIFIER:-}" "$RESOLVED_VERIFIER_MODEL" <<'PY'
import json,sys
engine,path,model=sys.argv[1:]
if path:
    plan=json.load(open(path,encoding="utf-8")); value=list(plan["argv"])
    if model: value.extend(plan["model_argv_suffix"])
elif engine == "claude":
    value=["claude","-p","{{PROMPT}}","--permission-mode","acceptEdits",
           "--allowedTools","Bash(node:*)"]
    if model: value.extend(["--model","{{MODEL}}"])
elif engine == "none":
    value=[]
else: value=[engine,"{{PROMPT}}"]
print(json.dumps(value,separators=(",",":")))
PY
  )" || return 1
  [ -n "$GATE_CONTRACT" ] || { log "strict Phase 5 requires the validated gate contract"; return 1; }
  # Phase 3 resolves the placement inputs, but its legacy record is not the strict
  # authority. Bind the resolved engine/provider/model/pool values into the Phase 5
  # contract, then discard only the private resolver candidates.
  resolve_run_assignment implementer "$WT" "$ENGINE" "${ENGINE_PLAN_IMPLEMENTER:-}" \
    "$RESOLVED_MODEL" "$impl_provider" "$BASE_COMMIT" || return 1
  impl_candidate="$RESOLVER_ASSIGNMENT_FILE"
  resolve_run_assignment verifier "$WT" "$VERIFIER_ENGINE" "${ENGINE_PLAN_VERIFIER:-}" \
    "$RESOLVED_VERIFIER_MODEL" "$verifier_provider" "$BASE_COMMIT" || return 1
  verifier_candidate="$RESOLVER_ASSIGNMENT_FILE"
  resolved_impl_engine="$(assignment_candidate_field "$impl_candidate" engine)" || return 1
  resolved_impl_provider="$(assignment_candidate_field "$impl_candidate" provider)" || return 1
  resolved_impl_pool="$(assignment_candidate_field "$impl_candidate" capacity_pool)" || return 1
  resolved_impl_model="$(assignment_candidate_field "$impl_candidate" model)" || return 1
  resolved_verifier_engine="$(assignment_candidate_field "$verifier_candidate" engine)" || return 1
  resolved_verifier_provider="$(assignment_candidate_field "$verifier_candidate" provider)" || return 1
  resolved_verifier_pool="$(assignment_candidate_field "$verifier_candidate" capacity_pool)" || return 1
  resolved_verifier_model="$(assignment_candidate_field "$verifier_candidate" model)" || return 1
  [ "$resolved_impl_engine" = "$ENGINE" ] && [ "$resolved_verifier_engine" = "$VERIFIER_ENGINE" ] \
    || { log "resolved assignment engine differs from the strict run contract"; return 1; }
  [ "$resolved_impl_pool" = "$CAPACITY_POOL" ] && [ "$resolved_verifier_pool" = "$CAPACITY_POOL" ] \
    || { log "resolved assignment capacity pool differs from the strict run contract"; return 1; }
  impl_provider="$resolved_impl_provider"; verifier_provider="$resolved_verifier_provider"
  RESOLVED_MODEL="$resolved_impl_model"; RESOLVED_VERIFIER_MODEL="$resolved_verifier_model"
  # Assignments are immutable provenance, not run records.  Keeping them outside state/runs
  # prevents reconciliation consumers from mistaking the assignment envelope for a worker
  # or verifier result when they enumerate run evidence.
  ASSIGNMENT_FILE="$ROOT/state/assignments/$ID-assignment-$STAMP-$$.json"
  mkdir -p "$ROOT/state/assignments"
  CONTROLLER_SHA="$(git -C "$ROOT" rev-parse --verify HEAD 2>/dev/null || true)"
  [ -n "$CONTROLLER_SHA" ] || return 1
  ASSIGNMENT_DIGEST="$(PYTHONPATH="$ROOT/scripts" python3 - "$ASSIGNMENT_FILE" "$ID" "$PROJECT_ID" "$REPO_DIR" "$source_branch" "$BASE_COMMIT" \
    "$ROOT" "$CONTROLLER_SHA" \
    "$SEED_MANIFEST_SHA256" "$resolved_impl_engine" "$impl_provider" "$resolved_impl_pool" "$host" "strict-linux" "$impl_profile" \
    "$ALLOWED_PATHS" \
    "$impl_endpoints" "$GATE_CONTRACT" "$impl_provider" "$resolved_impl_model" "$resolved_verifier_engine" "$verifier_provider" \
     "$verifier_profile" "$verifier_endpoints" "$resolved_verifier_model" \
     "$impl_policy" "$verifier_policy" "$impl_argv" "$verifier_argv" <<'PY'
import hashlib, json, os, sys, tempfile
from assignment_contract import digest, validate_assignment
(path, task, project, repo, branch, sha, controller_repo, controller_sha, seed, engine, provider, pool, host, sandbox, profile,
 paths, endpoints, gate_contract, _, model, verifier_engine, verifier_provider, verifier_profile,
 verifier_endpoints, verifier_model, impl_policy, verifier_policy, impl_argv_json, verifier_argv_json) = sys.argv[1:]
gate = hashlib.sha256(json.dumps(json.loads(gate_contract), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
value = {"task_id":task, "project_id":project, "source_repository":repo, "source_branch":branch,
         "controller_repository":controller_repo, "controller_sha":controller_sha,
         "source_sha":sha, "seed_manifest_digest":seed, "engine":engine, "provider":provider,
         "capacity_pool":pool, "model":model, "host":host, "sandbox_profile":sandbox,
         "network_policy":impl_policy, "argv":json.loads(impl_argv_json),
         "network_profile":profile, "network_endpoints":json.loads(endpoints),
         "allowed_paths":paths.split(), "gate_digest":gate, "gate_contract":json.loads(gate_contract),
         "publication_policy":"verified-integration-only",
         "role_assignments":{"implementer":{"engine":engine,"provider":provider,"model":model,
                              "network_policy":impl_policy,"argv":json.loads(impl_argv_json)},
                             "verifier":{"engine":verifier_engine,"provider":verifier_provider,"model":verifier_model,
                              "network_policy":verifier_policy,"argv":json.loads(verifier_argv_json)}}}
value["role_assignments"]["implementer"].update(network_profile=profile, network_endpoints=json.loads(endpoints))
value["role_assignments"]["verifier"].update(network_profile=verifier_profile, network_endpoints=json.loads(verifier_endpoints))
value = validate_assignment(value); serialized=json.dumps(value, indent=2, sort_keys=True)+"\n"
if os.path.lexists(path): raise SystemExit("assignment path already exists")
fd=os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o444)
with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
    out.write(serialized); out.flush(); os.fsync(out.fileno())
print(digest(value))
PY
  )" || return 1
  rm -f -- "$impl_candidate" "$verifier_candidate" || return 1
  log "immutable Phase 5 assignment=$ASSIGNMENT_FILE digest=$ASSIGNMENT_DIGEST"
}

# --- the WORKER ENVELOPE (CONTROL_PLANE.md role 4) ---------------------------------------
# The arm used to receive a goal and a gate. That is enough to build, and not enough to stay
# bounded: nothing stated the authority model, the paths it may touch, what it must NOT do, or
# what to do when the task is ambiguous. An arm with only a goal and a gate optimises for the
# gate — and the gate cannot see scope. This envelope states all of it explicitly, and the
# protected-path check below enforces the parts a prompt cannot.
#
# The arm is told to STOP on ambiguity rather than choose. A stopped job with a precise
# question beats hours of plausible wrong work — and the 0x68 run already proved the arm will
# honour that: it refused to narrow scope to manufacture a green, and no-op'd honestly.
# A human answer to THIS task's blocker, if one was given. Without this the seam is one-way:
# the arm stops with a precise question, a human answers into state/INBOX.md — a file the arm
# never reads — and the next run receives the identical prompt and blocks on the identical
# question. See scripts/assist.sh.
HUMAN_GUIDANCE=""
if [ -x "$ROOT/scripts/assist.sh" ]; then
  HUMAN_GUIDANCE="$(RECURSION_ROOT="$ROOT" bash "$ROOT/scripts/assist.sh" guidance "$ID" 2>/dev/null || true)"
  [ -n "$HUMAN_GUIDANCE" ] && log "envelope carries a human answer for $ID"
fi
NON_GOALS="$(field non_goals)"
GOAL_ID="$(field goal_id)"

BUILD_PROMPT="You are a BUILD ARM for the Recursion system — a bounded executor.
${HUMAN_GUIDANCE:+
--------------------------- $HUMAN_GUIDANCE
---------------------------
}
AUTHORITY
Hermes owns planning, priority, and product direction. You do not. You implement ONE task
inside ONE boundary and report what you did. You may not redefine the task, widen its scope,
reprioritise, create new workstreams, or change control-plane policy.

TASK: $GOAL${GOAL_ID:+
PARENT GOAL: $GOAL_ID}
REPO: already checked out at the current directory; your output goes here.
WORK DIR: $WORKDIR

ALLOWED PATHS — you may create or modify files ONLY under:
  $ALLOWED_PATHS

PROTECTED — never modify, for any reason, even if it would make the gate pass:
  - test files (*.test.cjs, *.test.js, scripts/tests/**)
  - truth fixtures, golden data, oracles, baselines (render/data/**, *baseline*, *oracle*)
  - the control plane: CONTROL_PLANE.md, ANCHOR.md, CLAUDE.md, state/**, scripts/**, .env,
    cron, any auth/model/provider config
  Editing a test or a fixture to turn the gate green is TAMPERING. The gate rejects it, and
  so does a separate path check that runs before the gate. Fix the IMPLEMENTATION.
${NON_GOALS:+
EXPLICIT NON-GOALS for this task (in scope for the project, NOT for you, right now):
  $NON_GOALS
}
ACCEPTANCE — this exact command must exit 0:
  $ACCEPTANCE
You MAY read the codebase and run \`node\` (e.g. \`node --test <file>\`) to check your work.
You may NOT run installers, servers, git, or any other shell command.

ON AMBIGUITY — STOP, do not guess.
If the task can be read two materially different ways, or completing it needs a decision that
is not written down, do NOT pick one. Write .recursion-result.json with status \"blocked\" and
your precise question, change nothing else, and stop. A stopped job with a clear question is a
GOOD outcome and is recorded as such. Inventing scope is the failure mode this system exists
to prevent.

WHEN DONE — write .recursion-result.json in the repo root:
{
  \"status\": \"done\" | \"blocked\" | \"no-op\",
  \"summary\": \"one or two sentences on what you changed and why\",
  \"files_changed\": [\"path\", ...],
  \"tests_run\": \"what you ran and what it said\",
  \"risks\": [\"anything a reviewer should check\"],
  \"blocker\": \"\"
}
RESULT SCHEMA RULE — blocker MUST always be a JSON string, never null or omitted:
  - status done or no-op: \"blocker\": \"\" (empty string)
  - status blocked: \"blocker\": \"the precise question\"
\"no-op\" is legitimate: if the correct action is to change nothing, say so and explain why.
Do not manufacture a change to look productive.

Keep changes minimal and CORRECT. Minimal-and-correct beats broad-and-broken.

$BODY"

if [ "$STRICT_PHASE5" = 1 ]; then
  BUILD_PROMPT="$BUILD_PROMPT

PHASE 5 EXECUTION CONTRACT
PROJECT / GOAL / TASK: ${WORKSTREAM:-unknown} / ${GOAL_ID:-unknown} / $ID
EXACT BASE COMMIT: populated by the controller before launch and immutable for this run.
MODEL ALIAS: $MODEL_ALIAS (the human-owned mapping is not editable by this worker)
DEADLINE: ${DEADLINE_SECONDS}s
NETWORK POLICY: $NETWORK_POLICY
EVIDENCE CONTRACT: $EVIDENCE_CONTRACT
VERIFICATION: a distinct $VERIFIER_MODEL_ALIAS run will review the exact machine-gated commit.
RESULT CONTRACT (worker-result-v1): .recursion-result.json blocker MUST be a JSON string.
For status done or no-op write \"blocker\":\"\" (empty string). Never null. Never omit.
Changing tests, fixtures, gate files, result policy, model routing, or any out-of-scope path
invalidates the run even if committed before the controller inspects the worktree."
fi

ARM_USAGE="${ARMLOG%.out}.usage.json"
# --- Phase 3 (D-016): immutable run assignment, resolved and durably persisted BEFORE the
# worker launches. Binds this exact arm to its exact engine/provider/pool/model/host/sandbox
# and exact source commit; the digest becomes the identity every downstream evidence/usage/
# scorecard record carries, instead of each consumer re-deriving those facts later from a
# task header that can change after the run (docs/audits/
# recursion-v2-infrastructure-audit-2026-08-28.md, "Missing execution-fabric contracts").
# Fail-closed by construction: every input here is already known and non-empty by the time an
# arm is about to launch, so a resolution failure indicates a controller defect, not a
# legitimate legacy gap -- matching the existing return-95 ("controller failure") convention
# used immediately above for worker-sandbox.sh itself.
resolve_run_assignment(){ # role wt active-engine engine-plan resolved-model provider commit-oid
  local role="$1" wt="$2" active_engine="$3" engine_plan="$4" resolved_model="$5"
  local provider="$6" commit_oid="$7" seed_digest="" gate_digest="" src_branch="" out_file assignment_host
  local billing_basis="" metered_flag=()
  RESOLVER_ASSIGNMENT_DIGEST=""; RESOLVER_ASSIGNMENT_FILE=""
  if [ "$STRICT_PHASE5" = 1 ]; then assignment_host="${RECURSION_HOST_ID:-gs-recursion}";
  else assignment_host="$(hostname)"; fi
  if [ -s "$SEED_EVIDENCE_FILE" ]; then
    seed_digest="$(python3 - "$SEED_EVIDENCE_FILE" <<'PY' 2>>"$LOG"
import json, sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(0)
digest = value.get("content_sha256")
if isinstance(digest, str) and digest:
    print(digest)
PY
    )" || true
  fi
  gate_digest="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' \
    "$ACCEPTANCE" 2>>"$LOG")" || return 95
  src_branch="${SOURCE_BRANCH:-}"
  [ -n "$src_branch" ] || src_branch="$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  [ -n "$src_branch" ] || src_branch="unknown"
  case "$active_engine" in
    claude) billing_basis="flat-subscription" ;;
    hermes) billing_basis="metered" ;;
    *) billing_basis="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["billing"]["basis"])' "$engine_plan" 2>>"$LOG")" || return 95 ;;
  esac
  [ "$billing_basis" != metered ] || metered_flag=(--metered-approved)
  # This is a controller-private candidate, not run evidence.  Keep it out of state/runs:
  # Phase 5 consumers scan that directory for implementer/verifier evidence, and an assignment
  # candidate has task/role fields but no seed_evidence of its own.
  mkdir -p "$ROOT/state/assignments/$ID"
  out_file="$ROOT/state/assignments/$ID/.candidate-$role-$STAMP-$$.json"
  local -a acmd=(python3 "$ROOT/scripts/assignment.py" resolve
    --task-id "$ID" --project-id "${PROJECT_ID:-$ID}" --role "$role"
    --source-repository "$REPO_DIR" --source-branch "$src_branch"
    --source-sha "$BASE_COMMIT"
    --engine "$active_engine" --provider "$provider" --host "$assignment_host"
    --sandbox-profile "${RECURSION_SANDBOX_PROFILE:-strict-linux}" --network-profile "$NETWORK_POLICY"
    --allowed-paths "$ALLOWED_PATHS" --publication-policy verified-integration-only
    --out "$out_file" --root "$ROOT/state/assignments" --default-branch "$DEFAULT_BRANCH"
    --integration-branch "$INTEGRATION_BRANCH" --source-evidence-file "$SOURCE_EVIDENCE_FILE" --no-persist
    --billing-basis "$billing_basis" "${metered_flag[@]}")
  [ -z "$seed_digest" ] || acmd+=(--seed-manifest-digest "$seed_digest")
  [ -z "$CAPACITY_POOL" ] || acmd+=(--capacity-pool "$CAPACITY_POOL")
  [ -z "$resolved_model" ] || acmd+=(--model "$resolved_model")
  [ -z "$gate_digest" ] || acmd+=(--gate-digest "$gate_digest")
  if ! "${acmd[@]}" >>"$LOG" 2>&1; then
    log "assignment resolution refused for role=$role engine=$active_engine (see log above)"
    return 95
  fi
  RESOLVER_ASSIGNMENT_DIGEST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["assignment_digest"])' \
    "$out_file" 2>>"$LOG")" || return 95
  RESOLVER_ASSIGNMENT_FILE="$out_file"
  [ -f "$RESOLVER_ASSIGNMENT_FILE" ] || { log "assignment candidate is absent after resolution"; return 95; }
  # Strict Phase 5 binds its own immutable contract after both role candidates resolve.
  # The legacy caller still receives the Phase 3 assignment globals when this helper is
  # used outside that strict binding flow.
  if [ "$STRICT_PHASE5" != 1 ]; then
    ASSIGNMENT_DIGEST="$RESOLVER_ASSIGNMENT_DIGEST"
    ASSIGNMENT_FILE="$RESOLVER_ASSIGNMENT_FILE"
  fi
}

run_model_supervised(){ # role worktree prompt resolved-model alias usage deadline run-id [commit] [implementer-run] [evidence-dir] [readonly] [arm-attempt]
  local role="$1" wt="$2" prompt="$3" resolved_model="$4" alias="$5" usage="$6"
  local deadline_s="$7" run_id="$8" commit_oid="${9:-}" implementer_run="${10:-}"
  local evidence_dir="${11:-}" readonly="${12:-0}" arm_attempt="${13:-}" engine_plan=""
  local provider="" billing_basis="" plan_network_policy="" metered_approved=0
  local assignment_token="" assignment_identity="" cancel_signal=TERM cancel_grace=5
  # Cross-engine verification (D-015): each role runs on its own contract engine.
  local active_engine="$ENGINE"
  [ "$role" != verifier ] || active_engine="$VERIFIER_ENGINE"
  case "$active_engine" in
    none) return 0 ;;   # verify-only: gate whatever is already in the worktree
    claude|hermes) ;;
    deepseek|openrouter)
      return 91 ;;   # raw chat-completion has no file tools — use an opencode engine manifest (D-015)
    *)
      # Plugin engines run only from the plan admitted before lease/board mutation.
      if [ "$role" = verifier ]; then engine_plan="$ENGINE_PLAN_VERIFIER"; else engine_plan="$ENGINE_PLAN_IMPLEMENTER"; fi
      [ -n "$engine_plan" ] && [ -f "$engine_plan" ] || return 92 ;;
  esac
  if [ -n "$engine_plan" ]; then
    read -r cancel_signal cancel_grace < <(python3 - "$engine_plan" <<'PY'
import json,sys
c=json.load(open(sys.argv[1], encoding="utf-8"))["execution"]["cancellation"]
print(c["signal"], c["grace_s"])
PY
    ) || return 95
  fi
  case "$active_engine" in
    claude) provider="anthropic-claude-code"; billing_basis="flat-subscription" ;;
    hermes) provider="hermes"; billing_basis="metered" ;;
    *) provider="$(python3 - "$engine_plan" <<'PY'
import json,sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8"))["billing"]["provider"])
except Exception:
    raise SystemExit(1)
PY
  )" || return 95
       billing_basis="$(python3 - "$engine_plan" <<'PY'
import json,sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8"))["billing"]["basis"])
except Exception:
    raise SystemExit(1)
PY
  )" || return 95 ;;
  esac
  [ "$billing_basis" != metered ] || metered_approved=1

  issue_builtin_capability "$role" "$active_engine" || return $?
  [ "$active_engine" != hermes ] || [ "${RECURSION_ALLOW_METERED:-0}" = "1" ] || return 93
  [ -x "$ROOT/scripts/worker-sandbox.sh" ] || return 95
  LAST_USAGE_FILE="$usage"
  if [ "$active_engine" = claude ]; then
    # Claude Code flat-subscription arms have no metered usage receipt. Record that policy fact
    # explicitly outside the worker's mount namespace; an absent record remains a hard failure.
    mkdir -p "$(dirname "$usage")"
    ( umask 077; printf '%s\n' \
      '{"schema_version":1,"provider":"anthropic-claude-code","billing_basis":"flat-subscription","tokens":0,"cost_usd":0,"evidence_source":"controller-engine-policy-v1"}' \
      > "$usage" ) || return 98
  elif [ -n "$engine_plan" ]; then
    # Plugin engines record the manifest's billing declaration; an absent record stays fatal.
    mkdir -p "$(dirname "$usage")"
    ( umask 077; python3 - "$engine_plan" "$CAPACITY_POOL" > "$usage" <<'PY'
import json, sys
try:
    with open(sys.argv[1], "rb") as source: raw=source.read(2*1024*1024+1)
    if len(raw)>2*1024*1024: raise ValueError("engine plan is too large")
    plan=json.loads(raw.decode("utf-8","strict"),parse_constant=lambda n: (_ for _ in ()).throw(ValueError(n)))
    if not isinstance(plan,dict): raise ValueError("engine plan is invalid")
    value={"schema_version": 1, "provider": plan["billing"]["provider"],
           "capacity_pool": sys.argv[2],
           "billing_basis": plan["billing"]["basis"], "tokens": 0, "cost_usd": 0,
           "evidence_source": "engine-manifest-v1"}
    print(json.dumps(value,allow_nan=False))
except (OSError,json.JSONDecodeError,ValueError,TypeError,UnicodeError,OverflowError,RecursionError,KeyError):
    raise SystemExit(1)
PY
    ) || return 98
  fi

  # Phase 5 binds one immutable contract before any role launches.  The Phase 3 resolver
  # supplied the engine/provider/model/pool inputs while make_phase5_assignment created that
  # contract; do not clear or replace its identity here.
  if [ "$STRICT_PHASE5" = 1 ]; then
    [ -n "$ASSIGNMENT_DIGEST" ] && [ -f "$ASSIGNMENT_FILE" ] \
      || { log "strict Phase 5 assignment is not bound before worker launch"; return 98; }
  fi

  if [ -n "$engine_plan" ]; then
    plan_network_policy="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["network"]["policy"])' "$engine_plan" 2>/dev/null)" \
      || return 95
    [ "$NETWORK_POLICY" = "$plan_network_policy" ] || return 92
  fi
  local -a cmd=(bash "$ROOT/scripts/worker-sandbox.sh" --engine "$active_engine" --worktree "$wt"
    --prompt "$prompt" --role "$role" --network-policy "$NETWORK_POLICY")
  [ -n "$resolved_model" ] && cmd+=(--model "$resolved_model")
  [ -z "$engine_plan" ] || cmd+=(--engine-plan "$engine_plan")
  [ "$STRICT_PHASE5" != 1 ] || cmd+=(--execution-evidence "$ROOT/state/runs/$run_id-execution.json")
  [ "$STRICT_PHASE5" != 1 ] || cmd+=(--assignment-digest "$ASSIGNMENT_DIGEST")
  [ "$active_engine" != hermes ] || cmd+=(--usage-file "$usage")
  [ "$readonly" != 1 ] || cmd+=(--read-only-worktree)
  [ -z "$evidence_dir" ] || cmd+=(--evidence-dir "$evidence_dir")
  local protected_path seen_paths=" "
  for protected_path in $SEED $SEED_MANIFEST $GATE_FILES; do
    case "$seen_paths" in *" $protected_path "*) continue ;; esac
    seen_paths+="$protected_path "
    [ ! -e "$wt/$protected_path" ] || cmd+=(--read-only-path "$protected_path")
  done

  pause_checkpoint "worker launch" || return 75
  local worker_pgid rc now_epoch now_epoch_ns deadline_epoch deadline_epoch_ns heartbeat_epoch log_path
  local -a evidence_meta=()
  log_path="$ARMLOG"; [ "$role" != verifier ] || log_path="$ROOT/loop/logs/$DATE-verifier-$ID-$STAMP.out"
  # The deadline bounds the supervised worker process itself.  Controller preparation (the
  # assignment/evidence writes below) can be relatively expensive on a cold filesystem and is
  # not worker execution.  Anchor the precise deadline immediately before launch, while still
  # recording it in the immutable start evidence before any watchdog decision.
  deadline_epoch_ns=""; deadline_epoch=""; heartbeat_epoch=0
  LAST_RUN_ID="$run_id"; LAST_RUN_EVIDENCE="$ROOT/state/runs/$run_id.json"
  if [ "$STRICT_PHASE5" = 1 ]; then
    mkdir -p "$ROOT/state/runs"
    # Evidence binds to the task lease attempt, not the bounded worker retry ordinal.  A retry
    # is run id a2 but reuses the same lease and immutable assignment; recording 2 here would
    # make the validator reject the result that was actually verified against lease attempt 1.
    LEASE_ATTEMPT="$(field attempts)"
    [ -n "$LEASE_ATTEMPT" ] || return 98
    if [ "$role" = implementer ]; then
      if [ "${arm_attempt:-1}" = 1 ]; then
        RECURSION_ROOT="$ROOT" TASKCTL_FILE="$TASK" \
          bash "$TASKCTL" set-assignment "$ID" "$ASSIGNMENT_DIGEST" "$ASSIGNMENT_FILE" \
            "$LEASE_OWNER" "$LEASE_ATTEMPT" "$run_id" >>"$LOG" 2>&1 || return 98
      else
        # Keep the lease's assignment_run_id bound to the first arm while proving that the
        # reused immutable record is still present and valid for this retry evidence.
        RECURSION_ROOT="$ROOT" TASKCTL_FILE="$TASK" \
          bash "$TASKCTL" persist-assignment "$ASSIGNMENT_FILE" "$ID" "$ASSIGNMENT_DIGEST" \
            >>"$LOG" 2>&1 || return 98
      fi
    else
      RECURSION_ROOT="$ROOT" TASKCTL_FILE="$TASK" \
        bash "$TASKCTL" persist-assignment "$ASSIGNMENT_FILE" "$ID" "$ASSIGNMENT_DIGEST" \
          >>"$LOG" 2>&1 || return 98
    fi
    [ -f "$ASSIGNMENT_FILE" ] || { log "persisted assignment file is absent before worker evidence"; return 98; }
    python3 "$ROOT/scripts/worker-evidence.py" provision-key --controller-root "$ROOT" \
      >>"$LOG" 2>&1 || { log "controller could not provision the fixed evidence key"; return 98; }
    # The strict assignment supplies the immutable role/shared projection.  The current Phase 5
    # envelope does not carry billing_basis, metered_approved, or the two branch-policy fields;
    # pass those already-validated controller facts at the same immutable start boundary.
    evidence_meta=(--engine "$active_engine" --provider "$provider" --capacity-pool "$CAPACITY_POOL"
      --model "$resolved_model" --host "${RECURSION_HOST_ID:-gs-recursion}"
      --sandbox-profile strict-linux --billing-basis "$billing_basis"
      --default-branch "$DEFAULT_BRANCH" --integration-branch "$INTEGRATION_BRANCH"
      --assignment-digest "$ASSIGNMENT_DIGEST" --assignment-file "$ASSIGNMENT_FILE")
    [ "$metered_approved" = 1 ] && evidence_meta+=(--metered-approved)
  fi
  if [ "$role" = verifier ]; then assignment_token="$ENGINE_TOKEN_VERIFIER"; assignment_identity="$ENGINE_ASSIGNMENT_VERIFIER";
  else assignment_token="$ENGINE_TOKEN_IMPLEMENTER"; assignment_identity="$ENGINE_ASSIGNMENT_IMPLEMENTER"; fi
  deadline_epoch_ns="$(( $(date +%s%N) + deadline_s * 1000000000 ))"
  deadline_epoch="$(( deadline_epoch_ns / 1000000000 ))"
  RECURSION_VERIFICATION_COMMIT="$commit_oid" RECURSION_IMPLEMENTER_RUN_ID="$implementer_run" \
    RECURSION_ASSIGNMENT_PROXY_TOKEN="$assignment_token" RECURSION_ASSIGNMENT_IDENTITY="$assignment_identity" \
    setsid --wait "${cmd[@]}" >"$log_path" 2>&1 &
  worker_pgid=$!
  UNCONFIRMED_PROCESS_GROUP="$worker_pgid"
  log "$role process group=$worker_pgid engine=$active_engine alias=${alias:-legacy} deadline=${deadline_s}s"
  if [ "$STRICT_PHASE5" = 1 ]; then
    python3 "$ROOT/scripts/worker-evidence.py" start --path "$LAST_RUN_EVIDENCE" --run-id "$run_id" \
      --task-id "$ID" --role "$role" --model-alias "$alias" --worktree "$wt" \
      --base-commit "$BASE_COMMIT" --pid "$worker_pgid" --process-group "$worker_pgid" \
      --deadline-epoch "$deadline_epoch" --deadline-epoch-ns "$deadline_epoch_ns" \
      --log "${log_path#$ROOT/}" --network-policy "$NETWORK_POLICY" \
      "${evidence_meta[@]}" \
      ${SEED_EVIDENCE_FILE:+--seed-evidence "$SEED_EVIDENCE_FILE"} \
      ${implementer_run:+--implementer-run-id "$implementer_run"} ${commit_oid:+--commit-oid "$commit_oid"} \
      --attempt "$LEASE_ATTEMPT" \
      >>"$LOG" 2>&1 || { terminate_worker_group "$worker_pgid" "$cancel_signal" "$cancel_grace" || return 96; wait "$worker_pgid" 2>/dev/null || true; return 98; }
  fi
  while kill -0 "$worker_pgid" 2>/dev/null; do
    if [ -f "$ROOT/state/PAUSE" ]; then
      log "PAUSE observed with $role process group $worker_pgid in flight"
      terminate_worker_group "$worker_pgid" "$cancel_signal" "$cancel_grace" || { wait "$worker_pgid" 2>/dev/null || true; return 96; }
      wait "$worker_pgid" 2>/dev/null || true
      kill -0 -- "-$worker_pgid" 2>/dev/null \
        && { log "worker group still alive after cancellation"; return 96; }
      [ "$STRICT_PHASE5" != 1 ] || python3 "$ROOT/scripts/worker-evidence.py" finish \
        "$LAST_RUN_EVIDENCE" cancelled pause 75 \
        --execution-evidence "$ROOT/state/runs/$run_id-execution.json" >>"$LOG" 2>&1 || true
      log "$role process group $worker_pgid terminated before gates/publication"
      return 75
    fi
    now_epoch_ns="$(date +%s%N)"
    now_epoch="$(( now_epoch_ns / 1000000000 ))"
    if [ "$STRICT_PHASE5" = 1 ] && [ "$now_epoch_ns" -ge "$deadline_epoch_ns" ]; then
      local terminate_rc=0
      log "$role deadline expired; terminating process group $worker_pgid"
      terminate_worker_group "$worker_pgid" "$cancel_signal" "$cancel_grace" || terminate_rc=$?
      wait "$worker_pgid" 2>/dev/null || true
      # terminate_worker_group owns the process-group liveness check.  A second kill -0 here
      # races with reaping setsid --wait's session leader and can turn an already-terminated
      # timeout into generic arm error 96, skipping the durable timeout evidence and blocked
      # escalation that the lease controller requires.
      [ "$terminate_rc" -eq 0 ] || return 96
      python3 "$ROOT/scripts/worker-evidence.py" finish "$LAST_RUN_EVIDENCE" failed timeout 124 \
        --execution-evidence "$ROOT/state/runs/$run_id-execution.json" \
        >>"$LOG" 2>&1 || true
      return 97
    fi
    if [ "$now_epoch" -ge "$heartbeat_epoch" ]; then
      heartbeat_epoch="$(( now_epoch + ${RECURSION_WORKER_HEARTBEAT_S:-5} ))"
      window_rc=0
      window_checkpoint "$role execution" || window_rc=$?
      if [ "$window_rc" -ne 0 ]; then
        log "pilot-window checkpoint stopped $role process group $worker_pgid in flight reason=$WINDOW_AUTHORITY_FAILURE"
        terminate_worker_group "$worker_pgid" "$cancel_signal" "$cancel_grace" || { wait "$worker_pgid" 2>/dev/null || true; return 96; }
        wait "$worker_pgid" 2>/dev/null || true
        [ "$STRICT_PHASE5" != 1 ] || python3 "$ROOT/scripts/worker-evidence.py" finish \
          "$LAST_RUN_EVIDENCE" cancelled "$WINDOW_AUTHORITY_FAILURE" 75 \
          --execution-evidence "$ROOT/state/runs/$run_id-execution.json" >>"$LOG" 2>&1 || true
        return 75
      fi
      if [ "$STRICT_PHASE5" = 1 ]; then
        python3 "$ROOT/scripts/worker-evidence.py" heartbeat "$LAST_RUN_EVIDENCE" "$deadline_epoch" \
          >>"$LOG" 2>&1 || true
        [ "$role" != implementer ] || RECURSION_ROOT="$ROOT" TASKCTL_FILE="$TASK" \
          bash "$TASKCTL" heartbeat "$ID" >>"$LOG" 2>&1 || true
      fi
    fi
    sleep 0.1
  done
  if wait "$worker_pgid"; then rc=0; else rc=$?; fi
  if [ "$STRICT_PHASE5" = 1 ] && [ -f "$LAST_RUN_EVIDENCE" ]; then
    python3 "$ROOT/scripts/worker-evidence.py" mark-finished "$LAST_RUN_EVIDENCE" "$(date +%s%N)" \
      >>"$LOG" 2>&1 || return 98
  fi
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 75 ] && [ "$rc" -ne 97 ] \
       && [ "$STRICT_PHASE5" = 1 ] && [ -f "$LAST_RUN_EVIDENCE" ]; then
    case "$rc" in
      90) execution_outcome=missing-auth ;;
      94) execution_outcome=missing-runtime ;;
      96) execution_outcome=missing-egress ;;
      *) execution_outcome=engine-exit ;;
    esac
    local -a refusal_finish=(finish "$LAST_RUN_EVIDENCE" failed "$execution_outcome" "$rc")
    refusal_finish+=(--execution-evidence "$ROOT/state/runs/$run_id-execution.json")
    python3 "$ROOT/scripts/worker-evidence.py" "${refusal_finish[@]}" >>"$LOG" 2>&1 || true
  fi
  return "$rc"
}

run_arm_supervised(){
  local wt="$1" attempt="${2:-1}" run_id
  run_id="$ID-impl-$STAMP-$$-a$attempt"
  run_model_supervised implementer "$wt" "$BUILD_PROMPT
EXACT BASE COMMIT: $BASE_COMMIT" "$RESOLVED_MODEL" "${MODEL_ALIAS:-legacy}" "$ARM_USAGE" \
    "$DEADLINE_SECONDS" "$run_id" "" "" "" 0 "$attempt"
  local rc=$?; IMPLEMENTER_RUN_ID="$LAST_RUN_ID"; return "$rc"
}

# --- SCOPE GATE: runs BEFORE the acceptance gate, and outranks it. -----------------------
# A prompt is a request; this is the enforcement. It answers a question the acceptance gate
# structurally cannot: the gate scores the OUTCOME, so a diff that quietly edits a fixture, a
# test, or a control-plane file to reach that outcome still exits 0. Order matters — checking
# scope only after a green gate means a tampered pass has already been declared.
#
# Two rules, deny-first:
#   PROTECTED  never writable, regardless of allowed_paths (tests, fixtures, control plane)
#   ALLOWED    everything else must sit under the task's declared allowed_paths
PROTECTED_RE='(^|/)([^/]*\.test\.(cjs|js|mjs)$|scripts/|state/|CONTROL_PLANE\.md$|ANCHOR\.md$|CLAUDE\.md$|\.env|\.github/)|(^|/)(render/(data|tools)|deploy/systemd|fixtures|golden|oracle)/|(^|/)[^/]*baseline[^/]*$'
scope_check(){
  local wt="$1" changed outside protected
  if [ -n "$SEED_DIGEST_BEFORE" ]; then
    verify_seed_integrity "$wt" || return 1
  fi
  if [ -n "$BASE_COMMIT" ] && [ -x "$ROOT/scripts/git-scope.py" ]; then
    local scope_id="${LAST_RUN_ID:-$ID-scope-$STAMP-$$}" err rc
    mkdir -p "$ROOT/state/runs"; LAST_SCOPE_FILE="$ROOT/state/runs/$scope_id-scope.json"
    err="$LAST_SCOPE_FILE.err"
    python3 "$ROOT/scripts/git-scope.py" --repo "$wt" --base "$BASE_COMMIT" \
      --allowed-paths "$ALLOWED_PATHS" --protected-extra "$GATE_FILES $SEED_MANIFEST $SEED" \
      --ignore .recursion-result.json --check >"$LAST_SCOPE_FILE" 2>"$err"; rc=$?
    if [ "$rc" -ne 0 ]; then
      SCOPE_FAIL="$(cat "$err" 2>/dev/null)"; [ -n "$SCOPE_FAIL" ] || SCOPE_FAIL="complete Git scope inspection failed (rc=$rc)"
      return 1
    fi
    rm -f "$err"
    return 0
  fi
  changed="$(git -C "$wt" status --porcelain --untracked-files=all 2>/dev/null \
             | sed -E 's/^.{3}//' | sed -E 's/^"(.*)"$/\1/' | grep -v '^\.recursion-result\.json$' || true)"
  [ -z "$changed" ] && return 0

  protected="$(printf '%s\n' "$changed" | grep -E "$PROTECTED_RE" || true)"
  if [ -n "$protected" ]; then
    SCOPE_FAIL="modified PROTECTED paths (tests/fixtures/control-plane):
$(printf '%s\n' "$protected" | head -10)"
    return 1
  fi
  # allowed_paths may be a space/comma list; "." means the whole repo (legacy tasks).
  local ap; ap="${ALLOWED_PATHS//,/ }"
  case " $ap " in *" . "*) return 0 ;; esac
  outside="$(printf '%s\n' "$changed" | while read -r p; do
      [ -z "$p" ] && continue
      ok=0; for a in $ap; do a="${a%/}"; case "$p" in "$a"|"$a"/*) ok=1; break;; esac; done
      [ "$ok" = "0" ] && printf '%s\n' "$p"
    done)"
  if [ -n "$outside" ]; then
    SCOPE_FAIL="modified files OUTSIDE allowed_paths ($ap):
$(printf '%s\n' "$outside" | head -10)"
    return 1
  fi
  return 0
}

# --- RESULT SCHEMA: the arm must say what it did, in a parseable form. -------------------
# Absent or malformed is NOT a silent pass. "The gate went green" is the implementer marking
# its own homework; the envelope requires a statement a reviewer (or Hermes) can read without
# opening the diff. A `blocked` result is a legitimate, recorded outcome, not a failure.
read_result(){
  local wt="$1" f="$1/.recursion-result.json"
  RESULT_STATUS=""; RESULT_SUMMARY=""; RESULT_BLOCKER=""
  [ -f "$f" ] || return 1
  eval "$(python3 - "$f" <<'PY' 2>/dev/null || true
import json,sys,shlex
try:
    with open(sys.argv[1],'rb') as source: raw=source.read(2*1024*1024+1)
    if len(raw)>2*1024*1024: raise ValueError('worker result is too large')
    d=json.loads(raw.decode('utf-8','strict'),parse_constant=lambda n: (_ for _ in ()).throw(ValueError(n)))
except (OSError,json.JSONDecodeError,ValueError,TypeError,UnicodeError,OverflowError,RecursionError): sys.exit(0)
if not isinstance(d,dict): sys.exit(0)
st=str(d.get('status','') or '')
if st not in ('done','blocked','no-op'): st=''
print("RESULT_STATUS=%s"  % shlex.quote(st))
print("RESULT_SUMMARY=%s" % shlex.quote(str(d.get('summary','') or '')[:400]))
print("RESULT_BLOCKER=%s" % shlex.quote(str(d.get('blocker','') or '')[:400]))
PY
)"
  [ -n "$RESULT_STATUS" ]
}

phase5_record_run(){ # status outcome rc [commit]
  [ "$STRICT_PHASE5" = 1 ] || return 0
  local status="$1" outcome="$2" rc="$3" commit_oid="${4:-}" result_file="${VALIDATED_RESULT_FILE:-}"
  local -a args=(finish "$LAST_RUN_EVIDENCE" "$status" "$outcome" "$rc")
  [ -z "$result_file" ] || args+=(--result-file "$result_file")
  [ -z "$LAST_SCOPE_FILE" ] || args+=(--scope-file "$LAST_SCOPE_FILE")
  [ -z "$LAST_USAGE_FILE" ] || [ ! -f "$LAST_USAGE_FILE" ] || args+=(--usage-file "$LAST_USAGE_FILE")
  execution_evidence="$ROOT/state/runs/$LAST_RUN_ID-execution.json"
  args+=(--execution-evidence "$execution_evidence")
  [ -z "$commit_oid" ] || args+=(--commit-oid "$commit_oid")
  [ ! -f "$SEED_EVIDENCE_FILE" ] || args+=(--seed-evidence "$SEED_EVIDENCE_FILE" --seed-verified-after)
  args+=(--keep-open)
  python3 "$ROOT/scripts/worker-evidence.py" "${args[@]}" >>"$LOG" 2>&1
}

phase5_seal_run(){ # status outcome rc
  [ "$STRICT_PHASE5" = 1 ] || return 0
  local requested_status="$1" outcome="$2" requested_rc="$3"
  local seal_status="$requested_status" seal_rc="$requested_rc" execution_evidence
  execution_evidence="$ROOT/state/runs/$LAST_RUN_ID-execution.json"
  # The worker execution receipt is authoritative for the run's terminal status/code.
  # A later gate/result decision may reject the candidate without changing the worker's
  # already-recorded process outcome (for example, a successful worker followed by a red gate).
  if [ -f "$execution_evidence" ]; then
    read -r seal_status seal_rc < <(python3 - "$execution_evidence" <<'PY'
import json, sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8"))
    status = value.get("status")
    code = value.get("exit_code")
    if status == "finished" and isinstance(code, int) and not isinstance(code, bool):
        print(("succeeded" if code == 0 else "failed"), code)
    elif status == "cancelled" and code == 143:
        print("cancelled 143")
    else:
        raise SystemExit(1)
except Exception:
    raise SystemExit(1)
PY
    ) || { seal_status="$requested_status"; seal_rc="$requested_rc"; }
  fi
  python3 "$ROOT/scripts/worker-evidence.py" seal "$LAST_RUN_EVIDENCE" "$seal_status" "$outcome" "$seal_rc" \
    >>"$LOG" 2>&1
}

phase5_validate_result(){
  [ "$STRICT_PHASE5" = 1 ] || return 0
  local result="$1/.recursion-result.json"
  VALIDATED_RESULT_FILE="$ROOT/state/runs/$LAST_RUN_ID-result.json"
  [ -f "$result" ] || { SCOPE_FAIL="required worker result is missing"; phase5_record_run failed missing-result 98 || true; return 1; }
  python3 "$ROOT/scripts/validate-worker-result.py" "$result" "$LAST_SCOPE_FILE" \
    --expected-change "$EXPECTED_CHANGE" >"$VALIDATED_RESULT_FILE" 2>"$VALIDATED_RESULT_FILE.err" \
    || {
      SCOPE_FAIL="$(cat "$VALIDATED_RESULT_FILE.err")"
      [ -n "$SCOPE_FAIL" ] || SCOPE_FAIL="worker result validation failed"
      phase5_record_run failed invalid-result 98 || true
      return 1
    }
  rm -f "$VALIDATED_RESULT_FILE.err"
  read_result "$1" || { SCOPE_FAIL="validated result could not be projected"; return 1; }
  if phase5_record_run succeeded worker-complete 0; then
    :
  else
    local record_rc=$?
    SCOPE_FAIL="worker run evidence could not be recorded atomically"
    return "$record_rc"
  fi
}

phase5_commit_candidate(){
  [ "$STRICT_PHASE5" = 1 ] || return 0
  local wt="$1"
  rm -f "$wt/.recursion-result.json"
  git -C "$wt" add -A 2>>"$LOG" || return 1
  if ! git -C "$wt" diff --cached --quiet; then
    pause_checkpoint "candidate commit" || return 75
    git -C "$wt" -c user.name="recursion-loop" -c user.email="loop@recursion.local" \
      commit -q -m "build($ID): $GOAL

Phase-5 evidence contract: $EVIDENCE_CONTRACT" 2>>"$LOG" || return 1
  fi
  CANDIDATE_OID="$(git -C "$wt" rev-parse HEAD)" || return 1
  if [ -n "$(git -C "$wt" status --porcelain --untracked-files=all)" ]; then
    SCOPE_FAIL="candidate worktree is not clean after commit"; return 1
  fi
  scope_check "$wt" || return 1
  phase5_record_run succeeded candidate-committed 0 "$CANDIDATE_OID" || return $?
}

phase5_reject(){ # short board note, evidence-backed reason
  local note="$1" reason="$2" escalation_key
  [ "$STRICT_PHASE5" != 1 ] || [ -z "${LAST_RUN_EVIDENCE:-}" ] \
    || phase5_seal_run failed phase5-rejected 98 || true
  board finish "$ID" blocked - "$note"
  escalation_key="phase5-reject:$ID:${CANDIDATE_OID:-${IMPLEMENTER_RUN_ID:-none}}:$note"
  complete_terminal_escalation escalated "$reason" "$escalation_key" || exit $?
  log "$reason; worktree retained for inspection: ${WT:-unknown}"
  exit 0
}

# --- HEAVY GATES ARE EXCLUSIVE ----------------------------------------------------------
# A task may declare `gate_class: heavy` when its acceptance is real local compute (node --test
# over a corpus, a renderer diff) rather than a cheap assertion. Two heavy gates that each FIT in
# memory still ruin each other: they contend for 2 cores, push into swap, and the timeouts that
# fire read as flaky tests instead of as a starved box. A gate whose verdict depends on what else
# happened to be running is not a gate, so heavy ones serialise box-wide.
GATE_CLASS="$(field gate_class)"; GATE_CLASS="${GATE_CLASS:-light}"
HEAVY_HELD=0
acquire_heavy(){
  [ "$GATE_CLASS" = "heavy" ] || return 0
  [ -x "$ROOT/scripts/capacity.sh" ] \
    || { log "heavy-gate capacity controller is unavailable — deferring without running unlocked"; return 75; }
  local waited=0 out
  while :; do
    pause_checkpoint "heavy-gate wait" || return 75
    out="$(RECURSION_ROOT="$ROOT" RECURSION_HEAVY_TAG="$ID" RECURSION_HEAVY_PID="$OWNER_PID" \
      bash "$ROOT/scripts/capacity.sh" heavy acquire "$ID" 2>&1)"
    case "$out" in ACQUIRED*) HEAVY_HELD=1; log "heavy-gate lock acquired after ${waited}s"; return 0 ;; esac
    waited=$((waited+10))
    if [ "$waited" -ge "${RECURSION_HEAVY_WAIT_S:-1800}" ]; then
      log "heavy-gate lock still held after ${waited}s ($out) — deferring without running unlocked"
      return 75
    fi
    sleep 10
  done
}
release_heavy(){
  [ "$HEAVY_HELD" = "1" ] || return 0
  if ! RECURSION_ROOT="$ROOT" RECURSION_HEAVY_PID="$OWNER_PID" \
      bash "$ROOT/scripts/capacity.sh" heavy release >/dev/null 2>&1; then
    log "heavy-gate lock release failed closed"
    return 76
  fi
  HEAVY_HELD=0
}

# --- the gate: run the acceptance command in the app dir; its exit code is the verdict. --
verify(){
  pause_checkpoint "acceptance gate" || return 75
  verify_seed_integrity "$1" || { log "$SCOPE_FAIL"; return 99; }
  acquire_heavy || return $?
  local wt="$1" gate_workdir="$WORKDIR" rc protected_path seen_paths=" "
  [ -d "$wt/$gate_workdir" ] || gate_workdir="."
  local -a gate_cmd=(bash "$ROOT/scripts/worker-sandbox.sh" --engine gate --role gate
    --worktree "$wt" --workdir "$gate_workdir" --network-policy none
    --gate-command "$ACCEPTANCE")
  for protected_path in $SEED $SEED_MANIFEST $GATE_FILES; do
    case "$seen_paths" in *" $protected_path "*) continue ;; esac
    seen_paths+="$protected_path "
    gate_cmd+=(--read-only-path "$protected_path")
  done
  if "${gate_cmd[@]}" >>"$ARMLOG" 2>&1; then rc=0; else rc=$?; fi
  local integrity_rc=0
  verify_seed_integrity "$wt" || { log "$SCOPE_FAIL"; integrity_rc=99; }
  # The heavy-gate lock is exclusive for the GATE, not for what follows a verdict: the retry arm
  # run and the independent verifier do not need it, and a second verify() on the retry path
  # would spin re-acquiring a lock this same process still holds. Release in every outcome here;
  # the EXIT trap is only the panic safety net.
  local release_rc=0
  release_heavy || release_rc=$?
  [ "$integrity_rc" = 0 ] || return 99
  [ "$release_rc" = 0 ] || return "$release_rc"
  return "$rc"
}

phase5_record_gate(){ # exit-code
  [ "$STRICT_PHASE5" = 1 ] || return 0
  python3 "$ROOT/scripts/worker-evidence.py" gate "$LAST_RUN_EVIDENCE" "$CANDIDATE_OID" \
    "$ACCEPTANCE" "$1" "${ARMLOG#$ROOT/}" >>"$LOG" 2>&1
}

phase5_require_gate_record(){ # exit-code
  local gate_rc="$1" evidence_rc
  phase5_record_gate "$gate_rc"; evidence_rc=$?
  if [ "$evidence_rc" -ne 0 ]; then
    log "machine-gate evidence could not be committed (rc=$evidence_rc) — refusing retry/pass/publication"
  fi
  return "$evidence_rc"
}

phase5_assert_gate_commit(){
  [ "$STRICT_PHASE5" = 1 ] || return 0
  [ "$(git -C "$1" rev-parse HEAD 2>/dev/null)" = "$CANDIDATE_OID" ] \
    && git -C "$1" diff --quiet --ignore-submodules -- \
    && git -C "$1" diff --cached --quiet --ignore-submodules -- \
    && verify_seed_integrity "$1" \
    && phase5_gate_files_unchanged "$1"
}

phase5_gate_files_unchanged(){
  [ "$STRICT_PHASE5" = 1 ] || return 0
  [ -n "$ASSIGNMENT_FILE" ] || return 1
  PYTHONPATH="$ROOT/scripts" python3 - "$ASSIGNMENT_FILE" "$1" <<'PY'
import sys
from assignment_contract import AssignmentError, load_assignment, validate_bound_gate_files

try:
    assignment, _ = load_assignment(sys.argv[1])
    validate_bound_gate_files(assignment, sys.argv[2])
except (AssignmentError, OSError, TypeError, ValueError):
    raise SystemExit(1)
PY
}

phase5_validate_assignment_result(){
  [ "$STRICT_PHASE5" = 1 ] || return 0
  [ -n "$ASSIGNMENT_DIGEST" ] && [ -f "$ASSIGNMENT_FILE" ] || { log "missing assignment before result validation"; return 98; }
  [ -n "$CANDIDATE_OID" ] || { log "missing candidate commit before result validation"; return 98; }
  local -a args=(--assignment-root "$ROOT/state/assignments" --task-id "$ID"
    --assignment-file "$ASSIGNMENT_FILE"
    --assignment-digest "$ASSIGNMENT_DIGEST" --run-id "$IMPLEMENTER_RUN_ID"
    --run-evidence "$ROOT/state/runs/$IMPLEMENTER_RUN_ID.json" --result-repository "$REPO_DIR"
    --result-branch "$BRANCH" --result-commit "$CANDIDATE_OID" --attempt "$LEASE_ATTEMPT" --record)
  local changed
  if [ -s "$LAST_SCOPE_FILE" ]; then
    while IFS= read -r changed; do
      [ -n "$changed" ] && args+=(--changed-path "$changed")
    done < <(python3 - "$LAST_SCOPE_FILE" <<'PY'
import json,sys
try: value=json.load(open(sys.argv[1],encoding='utf-8'))
except Exception: raise SystemExit(1)
for entry in value.get('entries',[]):
    if isinstance(entry,str): print(entry)
    elif isinstance(entry,dict) and isinstance(entry.get('path'),str): print(entry['path'])
PY
    ) || return 98
  fi
  python3 "$ROOT/scripts/validate-assignment-result.py" "${args[@]}" >>"$LOG" 2>&1
}

run_independent_verifier(){
  [ "$STRICT_PHASE5" = 1 ] || return 0
  local impl_run="$IMPLEMENTER_RUN_ID" impl_evidence="$LAST_RUN_EVIDENCE"
  local impl_assignment_digest="$ASSIGNMENT_DIGEST" impl_assignment_file="$ASSIGNMENT_FILE"
  local short verify_wt verifier_id evidence_dir verdict verifier_log
  short="${CANDIDATE_OID:0:12}"; verify_wt="$REPO_DIR/.worktrees/$ID-verifier-$short-$$"
  verifier_id="$ID-verify-$STAMP-$$"; evidence_dir="$ROOT/state/runs/$verifier_id-evidence"
  verdict="$evidence_dir/verdict.json"; verifier_log="$ROOT/loop/logs/$DATE-verifier-$ID-$STAMP.out"
  local prompt rc head dirty
  rm -rf "$evidence_dir"; mkdir -p "$evidence_dir"
  git -C "$REPO_DIR" worktree add --detach "$verify_wt" "$CANDIDATE_OID" >>"$LOG" 2>&1 || return 99
  if ! copy_seed_into_worktree "$verify_wt" || ! verify_seed_integrity "$verify_wt"; then
    git -C "$REPO_DIR" worktree remove "$verify_wt" >/dev/null 2>&1 || true
    LAST_RUN_EVIDENCE="$impl_evidence"; IMPLEMENTER_RUN_ID="$impl_run"
    return 99
  fi
  # Materialize controller-owned review artifacts into /evidence (sandbox masks host /opt and .git).
  git -C "$REPO_DIR" diff --stat "$BASE_COMMIT..$CANDIDATE_OID" >"$evidence_dir/diff.stat" 2>>"$LOG" || true
  git -C "$REPO_DIR" diff "$BASE_COMMIT..$CANDIDATE_OID" >"$evidence_dir/diff.patch" 2>>"$LOG" || true
  git -C "$REPO_DIR" log --oneline "$BASE_COMMIT..$CANDIDATE_OID" >"$evidence_dir/commits.txt" 2>>"$LOG" || true
  if [ -n "$impl_evidence" ] && [ -f "$impl_evidence" ]; then
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); json.dump(d.get("machine_gate") or {}, open(sys.argv[2],"w"), indent=2)' \
      "$impl_evidence" "$evidence_dir/machine_gate.json" 2>>"$LOG" || true
  fi
  prompt="You are an INDEPENDENT VERIFIER, not the implementer. Review task $ID for goal ${GOAL_ID:-unknown}.
The checkout is an isolated read-only worktree at exact commit $CANDIDATE_OID, based on $BASE_COMMIT.
Inspect provided evidence artifacts and checkout files only; do not run git npm or gates in sandbox.
Do not modify source, tests, fixtures, gates, or the implementer's branch. You are not allowed
to repair the change you review. Write only /evidence/verdict.json with this schema:
{\"verdict\":\"pass|fail|blocked\",\"commit_oid\":\"$CANDIDATE_OID\",\"implementer_run_id\":\"$impl_run\",\"summary\":\"...\",\"evidence\":[\"...\"],\"risks\":[\"...\"]}
Acceptance command (already machine-run against this OID): $ACCEPTANCE
Allowed implementation paths: $ALLOWED_PATHS
Explicit non-goals: ${NON_GOALS:-none}."
  if run_model_supervised verifier "$verify_wt" "$prompt" "$RESOLVED_VERIFIER_MODEL" \
      "$VERIFIER_MODEL_ALIAS" "${verifier_log%.out}.usage.json" "$VERIFIER_DEADLINE_SECONDS" \
      "$verifier_id" "$CANDIDATE_OID" "$impl_run" "$evidence_dir" 1 "$LEASE_ATTEMPT"; then rc=0; else rc=$?; fi
  VERIFIER_RUN_ID="$LAST_RUN_ID"
  # run_model_supervised uses the same globals for each arm. Restore the implementer's binding
  # before the caller validates the candidate result; the verifier's assignment must never be
  # accidentally presented as the implementer's publication identity.
  ASSIGNMENT_DIGEST="$impl_assignment_digest"; ASSIGNMENT_FILE="$impl_assignment_file"
  if [ "$rc" -ne 0 ]; then
    [ "$rc" = 96 ] || git -C "$REPO_DIR" worktree remove "$verify_wt" >/dev/null 2>&1 || true
    LAST_RUN_EVIDENCE="$impl_evidence"; IMPLEMENTER_RUN_ID="$impl_run"
    return "$rc"
  fi
  head="$(git -C "$verify_wt" rev-parse HEAD 2>/dev/null || true)"
  dirty="$(git -C "$verify_wt" status --porcelain --untracked-files=all 2>/dev/null || true)"
  if [ "$head" != "$CANDIDATE_OID" ] || [ -n "$dirty" ] || ! verify_seed_integrity "$verify_wt"; then
    python3 "$ROOT/scripts/worker-evidence.py" finish "$LAST_RUN_EVIDENCE" succeeded verifier-mutated-source 0 \
      --execution-evidence "$ROOT/state/runs/$VERIFIER_RUN_ID-execution.json" \
      --commit-oid "$CANDIDATE_OID" \
      ${SEED_EVIDENCE_FILE:+--seed-evidence "$SEED_EVIDENCE_FILE"} >>"$LOG" 2>&1 || true
    git -C "$REPO_DIR" worktree remove "$verify_wt" >/dev/null 2>&1 || true
    LAST_RUN_EVIDENCE="$impl_evidence"; IMPLEMENTER_RUN_ID="$impl_run"
    return 99
  fi
  if ! python3 "$ROOT/scripts/validate-verifier-result.py" "$verdict" --commit "$CANDIDATE_OID" \
      --implementer-run "$impl_run" >"$evidence_dir/validated-verdict.json" 2>>"$LOG"; then
    python3 "$ROOT/scripts/worker-evidence.py" finish "$LAST_RUN_EVIDENCE" succeeded invalid-verdict 0 \
      --execution-evidence "$ROOT/state/runs/$VERIFIER_RUN_ID-execution.json" \
      --commit-oid "$CANDIDATE_OID" >>"$LOG" 2>&1 || true
    git -C "$REPO_DIR" worktree remove "$verify_wt" >/dev/null 2>&1 || true
    LAST_RUN_EVIDENCE="$impl_evidence"; IMPLEMENTER_RUN_ID="$impl_run"
    return 99
  fi
  mv -f "$evidence_dir/validated-verdict.json" "$verdict"
  local -a finish_args=(finish "$LAST_RUN_EVIDENCE" succeeded verified 0
    --result-file "$verdict" --commit-oid "$CANDIDATE_OID"
    --execution-evidence "$ROOT/state/runs/$VERIFIER_RUN_ID-execution.json" --keep-open)
  [ ! -f "$SEED_EVIDENCE_FILE" ] \
    || finish_args+=(--seed-evidence "$SEED_EVIDENCE_FILE" --seed-verified-after)
  [ -z "$LAST_USAGE_FILE" ] || [ ! -f "$LAST_USAGE_FILE" ] \
    || finish_args+=(--usage-file "$LAST_USAGE_FILE")
  python3 "$ROOT/scripts/worker-evidence.py" verify "$LAST_RUN_EVIDENCE" "$CANDIDATE_OID" \
    "$impl_run" "$verdict" >>"$LOG" 2>&1 || return 98
  python3 "$ROOT/scripts/worker-evidence.py" link-verifier "$impl_evidence" "$VERIFIER_RUN_ID" \
    "$CANDIDATE_OID" "$verdict" >>"$LOG" 2>&1 || return 98
  python3 "$ROOT/scripts/worker-evidence.py" "${finish_args[@]}" >>"$LOG" 2>&1 || return 98
  python3 "$ROOT/scripts/worker-evidence.py" seal "$LAST_RUN_EVIDENCE" succeeded verified 0 \
    >>"$LOG" 2>&1 || return 98
  LAST_RUN_EVIDENCE="$impl_evidence"
  python3 "$ROOT/scripts/worker-evidence.py" seal "$LAST_RUN_EVIDENCE" succeeded candidate-committed 0 \
    >>"$LOG" 2>&1 || return 98
  local verdict_value; verdict_value="$(python3 - "$verdict" <<'PY'
import json,sys
try:
    with open(sys.argv[1],'rb') as source: raw=source.read(65537)
    if len(raw)>65536: raise ValueError('verdict is too large')
    value=json.loads(raw.decode('utf-8','strict'),parse_constant=lambda n: (_ for _ in ()).throw(ValueError(n)))
    print(value['verdict'] if isinstance(value,dict) else '')
except (OSError,json.JSONDecodeError,ValueError,TypeError,UnicodeError,OverflowError,RecursionError,KeyError):
    raise SystemExit(1)
PY
)" || return 98
  if [ "$STRICT_PHASE5" = 1 ]; then
    git -C "$REPO_DIR" worktree remove "$verify_wt" >/dev/null 2>&1 || true
  else
    git -C "$REPO_DIR" worktree remove --force "$verify_wt" >/dev/null 2>&1 || true
  fi
  LAST_RUN_EVIDENCE="$impl_evidence"; LAST_RUN_ID="$impl_run"; IMPLEMENTER_RUN_ID="$impl_run"
  [ "$verdict_value" = pass ]
}

# --- after a GREEN build: optionally PUBLISH the canonical review branch. -----------------
# RECURSION_ALLOW_PUSH remains human-set and off by default. publish.sh accepts only a direct
# loop/work/<task-id> source/destination and has no default-branch merge path.
REMOTE_INTEGRATION_ADMITTED=0
admit_remote_integration(){
  REMOTE_INTEGRATION_ADMITTED=0
  [ -z "${PILOT_ID:-}" ] || return 0  # Phase 6 owns exact-commit pilot publication.
  [ "${RECURSION_ALLOW_PUSH:-0}" = "1" ] || return 0
  pause_checkpoint "review-branch publication admission" || return 75
  REMOTE_INTEGRATION_ADMITTED=1
}
integrate_remote(){
  [ "$REMOTE_INTEGRATION_ADMITTED" = 1 ] || return 0
  local -a args=(--branch "$BRANCH" --repo "$REPO_DIR")
  [ -z "$SSHKEY" ] || args+=(--key "$SSHKEY")
  if RECURSION_ROOT="$ROOT" bash "$ROOT/scripts/publish.sh" "${args[@]}" >>"$LOG" 2>&1; then
    log "published $BRANCH to origin through the review-only publisher"
    notify "📤 [$ID] published $BRANCH for review"
  else
    log "publisher refused or failed — branch stays local on the box"
  fi
}

# --- FRESHNESS IS A PRECONDITION, NOT BEST EFFORT ---------------------------------------
# Prove the base before board/lease mutation. A healthy active source pin or brief shared-lock
# contention returns 75 and leaves the queued task and its attempt budget untouched.
FRESH_WHY=""
require_fresh_base(){
  if [ -n "$PILOT_ID" ] && [ -n "$SOURCE_HEAD" ]; then
    [ "$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || true)" = "$SOURCE_HEAD" ] \
      || { FRESH_WHY="project HEAD moved after exact Phase 8 admission"; return 1; }
    log "base freshness: exact approved Phase 8 source $SOURCE_HEAD"
    return 0
  fi
  if [ "${RECURSION_REQUIRE_FRESH_BASE:-1}" != "1" ]; then
    log "FRESH-BASE CHECK DISABLED (RECURSION_REQUIRE_FRESH_BASE=0) — building on whatever base is local"
    return 0
  fi
  if [ ! -x "$ROOT/scripts/resync.sh" ]; then
    FRESH_WHY="scripts/resync.sh is missing — base freshness cannot be proven"; return 1
  fi
  local out rc
  out="$(RECURSION_ROOT="$ROOT" bash "$ROOT/scripts/resync.sh" check "$REPO_DIR" 2>&1)"; rc=$?
  case "$rc" in
    0) log "base freshness: $out"; return 0 ;;
    3) FRESH_WHY="cannot prove $REPO_DIR is current with origin — $out" ;;
    4) FRESH_WHY="$REPO_DIR needs a human before more work lands on it — $out" ;;
    75) FRESH_WHY="base freshness is temporarily deferred for $REPO_DIR — $out"; return 75 ;;
    *) FRESH_WHY="resync check exited $rc — $out" ;;
  esac
  return 1
}

# ============================ pipeline ============================
if ! validate_seed_source; then
  if [ -n "$PILOT_ID" ]; then
    reject_preworker_contract seed-contract-invalid; exit $?
  fi
  board finish "$ID" blocked - "seed contract invalid"
  complete_terminal_escalation escalated \
    "approved seed manifest or content digest is absent, unsafe, or stale; no worker was launched" || exit $?
  exit 0
fi
fresh_window_admission || exit $?
fresh_rc=0
require_fresh_base || fresh_rc=$?
if [ "$fresh_rc" -eq 75 ]; then
  log "$FRESH_WHY; deferring before board or task lease mutation"
  exit 75
fi
if [ "$fresh_rc" -ne 0 ]; then
  board finish "$ID" blocked - "stale/unprovable base"
  complete_terminal_escalation blocked "$FRESH_WHY" || exit $?
  exit 0
fi
pause_checkpoint "board transition" || exit 75
board add "$ID" "${WORKSTREAM:-?}" "$ENGINE" "${MODEL:-$ENGINE}" running "${GOAL:0:40}"
claim_task; claim_rc=$?
[ "$claim_rc" -eq 0 ] || exit "$claim_rc"
commit_state

if ! WT="$(mk_worktree)"; then
  board finish "$ID" blocked - "worktree create failed"
  complete_terminal_escalation escalated "could not create git worktree $BRANCH" || exit $?
  exit 0
fi
log "worktree: $WT (branch $BRANCH)"
BASE_COMMIT="$(git -C "$WT" rev-parse HEAD)"
[ -n "$BASE_COMMIT" ] || phase5_reject "base evidence missing" "exact worker base commit could not be recorded"
if [ -z "$SOURCE_BRANCH" ]; then
  for candidate in "$DEFAULT_BRANCH" master main "$(git -C "$REPO_DIR" branch --show-current 2>/dev/null || true)"; do
    [ -n "$candidate" ] || continue
    if [ "$(git -C "$REPO_DIR" rev-parse "$candidate" 2>/dev/null || true)" = "$BASE_COMMIT" ]; then
      SOURCE_BRANCH="$candidate"; break
    fi
  done
fi
[ -n "$SOURCE_BRANCH" ] || phase5_reject "source evidence missing" "source branch could not be resolved before worker launch"
source_observed="$(git -C "$REPO_DIR" rev-parse "$SOURCE_BRANCH" 2>/dev/null || true)"
[ "$source_observed" = "$BASE_COMMIT" ] || phase5_reject "source evidence mismatch" \
  "source branch $SOURCE_BRANCH does not resolve to exact worker base $BASE_COMMIT"
python3 - "$SOURCE_EVIDENCE_FILE" "$REPO_DIR" "$SOURCE_BRANCH" "$BASE_COMMIT" <<'PY'
import json, os, sys, tempfile
target, repository, branch, sha = sys.argv[1:]
value = {"schema_version": 1, "repository": repository,
         "branch": branch, "sha": sha, "verified": True}
os.makedirs(os.path.dirname(target), exist_ok=True)
fd, name = tempfile.mkstemp(prefix='.source.', dir=os.path.dirname(target))
with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as out:
    json.dump(value, out, sort_keys=True, separators=(',', ':'))
    out.write('\n'); out.flush(); os.fsync(out.fileno())
os.replace(name, target)
PY
if ! make_phase5_assignment; then
  phase5_reject "assignment evidence missing" "strict Phase 5 assignment could not be validated or immutably recorded"
fi
if ! SEED_DIGEST_BEFORE="$(seed_digest "$WT")"; then
  phase5_reject "seed evidence missing" "declared seed/oracle could not be checksummed before worker launch"
fi

# --- EXECUTE (attempt 1) --------------------------------------------------------------
if run_arm_supervised "$WT" 1; then arc=0; else arc=$?; fi
if [ "$arc" -ne 0 ]; then
  if [ "$arc" = 75 ]; then
    log "execution authority cancelled $ID; worktree retained and no gate/state/publication step ran"
    exit 75
  fi
  if [ "$arc" = 97 ]; then
    board finish "$ID" blocked - "worker timeout"
    complete_terminal_escalation blocked \
      "WORKER TIMEOUT — $ID exceeded ${DEADLINE_SECONDS}s; process group terminated, lease cleared, inspect $LAST_RUN_EVIDENCE" \
      "worker-timeout:$ID:$IMPLEMENTER_RUN_ID" || exit $?
    log "worker timeout retained worktree for inspection: $WT"
    exit 0
  fi
  if [ "$arc" = "96" ]; then
    msg="engine=$ENGINE missing-egress or worker process group could not be confirmed terminated"
    board finish "$ID" blocked - "$msg" || true
    quarantine_unconfirmed "$msg" || true
    log "quarantine: termination was not confirmed; retaining worktree, lease, and evidence at $WT"
    exit 96
  fi
  case "$arc" in
    90) msg="engine=$ENGINE missing-auth (for claude: run claude /login)" ;;
    91) msg="engine=$ENGINE has no file-writing tool (use an opencode engine manifest instead, D-015)" ;;
    92) msg="engine=$ENGINE is not runnable (unknown engine, or its admitted plugin plan is missing)" ;;
    93) msg="engine=$ENGINE is metered — a human sets RECURSION_ALLOW_METERED=1 in .env to allow it" ;;
    94) msg="engine=$ENGINE missing-runtime (CLI or declared runtime tree unavailable)" ;;
    95) msg="worker sandbox unavailable or dedicated runner not provisioned" ;;
    96) msg="engine=$ENGINE missing-egress or worker process group could not be confirmed terminated" ;;
    98) msg="worker run evidence could not be recorded atomically" ;;
    *)  msg="arm dispatch error (code $arc)" ;;
  esac
  board finish "$ID" blocked - "$msg"
  complete_terminal_escalation escalated "$msg" || exit $?
  git -C "$REPO_DIR" worktree remove --force "$WT" 2>/dev/null || true
  exit 0
fi

# --- SCOPE + RESULT, BEFORE the acceptance gate ----------------------------------------
# Order is the point: a tampered diff that reaches a green gate has already been declared a
# pass by the time a post-hoc check runs.
SCOPE_FAIL=""
pause_checkpoint "scope gate" || exit 75
if ! scope_check "$WT"; then
  board finish "$ID" blocked - "scope violation"
  complete_terminal_escalation escalated \
    "SCOPE VIOLATION — the arm went outside its boundary, so the acceptance result is not trusted and was not run:
$SCOPE_FAIL" || exit $?
  log "SCOPE VIOLATION on $ID — left worktree for inspection: $WT"
  exit 0
fi

if [ "$STRICT_PHASE5" = 1 ] && ! phase5_validate_result "$WT"; then
  phase5_reject "invalid worker result" "RESULT CONTRACT VIOLATION — $SCOPE_FAIL"
fi

# A deliberate, reasoned block is a GOOD outcome (CLAUDE.md: escalate, don't guess). Record it
# as blocked with the arm's own question rather than running a gate that is certain to fail.
if read_result "$WT" && [ "$RESULT_STATUS" = "blocked" ]; then
  phase5_seal_run succeeded worker-blocked 0 || exit $?
  pause_checkpoint "blocked-result transition" || exit 75
  board finish "$ID" blocked - "arm blocked on ambiguity"
  # Record the ask as an ANSWERABLE item, not just a notification. assist.sh open returns 3 when
  # the arm is re-asking a question a human already answered — that is the answer failing to
  # reach the arm, and it needs a different escalation than "the arm has a question", because
  # re-asking it looks like the system working while it burns a human every cycle.
  ASSIST_RC=0
  if [ -x "$ROOT/scripts/assist.sh" ]; then
    RECURSION_ROOT="$ROOT" bash "$ROOT/scripts/assist.sh" open "$ID" "$(field assist_kind)" \
      "${RESULT_BLOCKER:-<no question given>}" >>"$LOG" 2>&1 || ASSIST_RC=$?
  fi
  if [ "$ASSIST_RC" = "3" ]; then
    blocked_reason="the arm re-blocked on a question that was ALREADY ANSWERED — the answer is not reaching it:
${RESULT_BLOCKER:-<no question given>}"
  else
    blocked_reason="the arm STOPPED on ambiguity instead of guessing (this is correct behaviour):
${RESULT_BLOCKER:-<no question given>}
${RESULT_SUMMARY:+context: $RESULT_SUMMARY}
Answer it with: recursion assist $ID \"...\"  (that requeues this task with your answer attached)"
  fi
  complete_terminal_escalation blocked "$blocked_reason" || exit $?
  log "$ID: arm blocked on ambiguity — escalated with its question"
  exit 0
fi

if [ "$STRICT_PHASE5" = 1 ] && ! phase5_commit_candidate "$WT"; then
  phase5_reject "candidate commit refused" "PHASE 5 CANDIDATE REFUSED — ${SCOPE_FAIL:-commit or evidence failure}"
fi

# --- VERIFY (+ retry the arm once on red, per design) ---------------------------------
PASS=0; rc=0
if verify "$WT"; then
  if ! phase5_assert_gate_commit "$WT"; then
    if ! phase5_gate_files_unchanged "$WT"; then
      phase5_reject "gate mutated candidate" \
        "MACHINE GATE REFUSED because a gate file changed after assignment binding; exact commit $CANDIDATE_OID was not certified"
    fi
    phase5_require_gate_record 99 || exit $?
    phase5_reject "gate mutated candidate" \
      "MACHINE GATE MUTATED tracked candidate state; exact commit $CANDIDATE_OID was not certified"
  fi
  phase5_require_gate_record 0 || exit $?
  PASS=1; rc=0
else
  rc=$?
  [ "$rc" -ne 75 ] || { log "acceptance gate deferred before execution; no gate evidence or later step ran"; exit 75; }
  [ "$rc" -ne 76 ] || { log "heavy-gate release failed; refusing retry or later actuation"; exit 76; }
  phase5_require_gate_record "$rc" || exit $?
  phase5_seal_run failed gate-failed "$rc" || exit $?
  log "acceptance FAILED (exit $rc), attempt 1"
  # A deliberate NO-OP (rc=2 no-progress with NO implementation change) will not benefit from a
  # retry — the arm chose not to ship (0x68, overlay-0x6c both did this). "No change" is proven
  # by commit identity, not by a working-tree snapshot: rc2 is treated as unchanged no-progress
  # ONLY when BASE_COMMIT == CANDIDATE_OID, i.e. the arm committed no candidate at all. A changed
  # rc2 candidate (CANDIDATE_OID != BASE_COMMIT) is exactly "tried but failed" and receives the
  # one bounded retry. Legacy runs never set CANDIDATE_OID, so they keep the working-tree
  # snapshot as their no-op signal; otherwise their uncommitted changes would read as a no-op.
  impl_changed=$(git -C "$WT" status --porcelain -- . ":(exclude)docs" 2>/dev/null | grep -c . || true)
  candidate_changed=0
  if [ -n "$CANDIDATE_OID" ]; then
    [ "$BASE_COMMIT" != "$CANDIDATE_OID" ] && candidate_changed=1
  else
    [ "${impl_changed:-0}" -gt 0 ] && candidate_changed=1
  fi
  if [ "$ENGINE" != "none" ] && { [ "$rc" != "2" ] || [ "$candidate_changed" = 1 ]; }; then
    log "retry once (design: retry once then escalate)"
    if run_arm_supervised "$WT" 2; then retry_arc=0; else retry_arc=$?; fi
    [ "$retry_arc" != 75 ] || { log "execution authority cancelled retry; no later step ran"; exit 75; }
    if [ "$retry_arc" = 97 ]; then
      board finish "$ID" blocked - "worker timeout on retry"
      complete_terminal_escalation blocked \
        "WORKER TIMEOUT — $ID retry exceeded ${DEADLINE_SECONDS}s; process group terminated, lease cleared, inspect $LAST_RUN_EVIDENCE" \
        "worker-timeout:$ID:$IMPLEMENTER_RUN_ID" || exit $?
      exit 0
    fi
    if [ "$retry_arc" = 96 ]; then
      quarantine_unconfirmed "engine=$ENGINE retry worker process group could not be confirmed terminated" || true
      log "quarantine: retry termination was not confirmed; retaining worktree, lease, and evidence at $WT"
      exit 96
    fi
    if [ "$retry_arc" = 0 ]; then
      if [ "$STRICT_PHASE5" = 1 ]; then
        SCOPE_FAIL=""
        scope_check "$WT" || phase5_reject "scope violation on retry" \
          "SCOPE VIOLATION after retry — retry output was rejected before its gate: $SCOPE_FAIL"
        phase5_validate_result "$WT" || phase5_reject "invalid retry result" \
          "RESULT CONTRACT VIOLATION after retry — $SCOPE_FAIL"
        if [ "$RESULT_STATUS" = blocked ]; then
          phase5_reject "retry blocked on ambiguity" \
            "retry worker stopped on ambiguity: ${RESULT_BLOCKER:-no precise question supplied}"
        fi
        phase5_commit_candidate "$WT" || phase5_reject "retry candidate refused" \
          "PHASE 5 retry candidate refused — ${SCOPE_FAIL:-commit or evidence failure}"
      fi
      if verify "$WT"; then
        if ! phase5_assert_gate_commit "$WT"; then
          if ! phase5_gate_files_unchanged "$WT"; then
            phase5_reject "retry gate mutated candidate" \
              "MACHINE GATE REFUSED because a retry gate file changed after assignment binding; exact commit $CANDIDATE_OID was not certified"
          fi
          phase5_require_gate_record 99 || exit $?
          phase5_reject "retry gate mutated candidate" \
            "MACHINE GATE MUTATED tracked retry state; exact commit $CANDIDATE_OID was not certified"
        fi
        phase5_require_gate_record 0 || exit $?
        PASS=1; rc=0
      else
        rc=$?
        [ "$rc" -ne 75 ] || { log "retry acceptance gate deferred before execution; no gate evidence or later step ran"; exit 75; }
        [ "$rc" -ne 76 ] || { log "heavy-gate release failed after retry; refusing later actuation"; exit 76; }
        phase5_require_gate_record "$rc" || exit $?
        phase5_seal_run failed gate-failed "$rc" || exit $?
      fi
    else
      rc="$retry_arc"
    fi
  else
    log "no retry: deliberate no-op (rc=$rc, candidate_changed=$candidate_changed)"
  fi
fi

# --- INTEGRATE ------------------------------------------------------------------------
if [ "$PASS" = "1" ]; then
  pause_checkpoint "post-gate integration" || exit 75
  # Re-check scope AFTER the retry: attempt 2 gets its own chance to wander, and a green gate
  # on an out-of-scope diff is exactly the outcome the scope gate exists to refuse.
  if ! scope_check "$WT"; then
    board finish "$ID" blocked - "scope violation (post-retry)"
    complete_terminal_escalation escalated \
      "SCOPE VIOLATION after retry — the gate went GREEN but the diff left its boundary, so the pass is not accepted:
$SCOPE_FAIL" || exit $?
    log "SCOPE VIOLATION (post-gate) on $ID — not committed. Worktree: $WT"
    exit 0
  fi
  # The envelope requires a result file. Absent/malformed is not fatal — the machine-checkable
  # gate is still the contract — but it IS recorded, because "tests passed" without a statement
  # of what changed is the implementer marking its own homework.
  if [ "$STRICT_PHASE5" = 1 ]; then
    log "$ID result contract validated before exact candidate commit $CANDIDATE_OID"
  elif read_result "$WT"; then
    log "$ID result: $RESULT_STATUS — ${RESULT_SUMMARY:0:160}"
  else
    log "$ID: WARNING — no valid .recursion-result.json (envelope requires one)"
  fi
  COMMITTED=0
  if [ "$STRICT_PHASE5" = 1 ]; then
    [ -n "$CANDIDATE_OID" ] || phase5_reject "missing candidate OID" \
      "machine gate passed without an exact candidate commit identity"
    if run_independent_verifier; then verifier_rc=0; else verifier_rc=$?; fi
    if [ "$verifier_rc" -ne 0 ]; then
      if [ "$verifier_rc" = 97 ]; then
        phase5_reject "verifier timeout" \
          "VERIFIER TIMEOUT — $ID independent verifier exceeded ${VERIFIER_DEADLINE_SECONDS}s; it was terminated and cannot certify $CANDIDATE_OID"
      fi
      phase5_reject "independent verification failed" \
        "INDEPENDENT VERIFICATION REFUSED — verifier run ${VERIFIER_RUN_ID:-missing} did not return a clean pass for exact commit $CANDIDATE_OID"
    fi
    COMMITTED=1; NOTE="passed -> $BRANCH at $CANDIDATE_OID (independently verified)"
    log "exact commit $CANDIDATE_OID passed machine gate and independent verifier $VERIFIER_RUN_ID"
  else
    rm -f "$WT/.recursion-result.json" 2>/dev/null || true
    git -C "$WT" add -A 2>>"$LOG" || true
    if git -C "$WT" diff --cached --quiet 2>/dev/null; then
      NOTE="passed (no file changes)"; log "acceptance green; nothing to commit (verify-only)"
    else
      pause_checkpoint "build-output commit" || exit 75
      git -C "$WT" -c user.name="recursion-loop" -c user.email="loop@recursion.local" \
        commit -q -m "build($ID): $GOAL

acceptance: $ACCEPTANCE (green)
engine: $ENGINE" 2>>"$LOG" || true
      NOTE="passed -> $BRANCH"; COMMITTED=1; log "committed build output to $BRANCH"
    fi
  fi
  pause_checkpoint "terminal completion admission" || exit 75
  if ! phase5_validate_assignment_result; then
    phase5_reject "assignment result validation failed" \
      "DURABLE ASSIGNMENT RESULT REFUSED — candidate was not integrated or published"
  fi
  log "durable assignment result accepted for $IMPLEMENTER_RUN_ID at $CANDIDATE_OID"
  [ "$COMMITTED" != "1" ] || admit_remote_integration || exit $?
  if [ -f "$ARM_USAGE" ]; then board finish "$ID" done "$ARM_USAGE" "$NOTE"; else board finish "$ID" done - "$NOTE"; fi
  terminal_status passed "$NOTE" || exit $?
  test_pause_after_terminal
  terminal_ledger
  if terminal_side_effect_allowed "success notification"; then
    notify "✅ Recursion build passed: $ID -> $BRANCH (acceptance green). Review/merge at your pace."
  fi
  if [ "$COMMITTED" = "1" ] && terminal_side_effect_allowed "review-branch publication"; then
    integrate_remote   # publication authority was admitted before the terminal transition
  fi
  # close the plan->build->done loop: a passed planned task flips its roadmap item [~] -> [x].
  if [ -n "$PLANNED_FROM" ]; then
    if terminal_side_effect_allowed "roadmap transition" \
       && RECURSION_ROOT="$ROOT" bash "$ROOT/scripts/plan.sh" --complete "$PLANNED_FROM" >>"$LOG" 2>&1; then
      log "roadmap $PLANNED_FROM -> done"
    else
      log "roadmap completion refused for $PLANNED_FROM; canonical row was not advanced"
    fi
  fi
  if terminal_side_effect_allowed "worktree removal"; then
    git -C "$REPO_DIR" worktree remove --force "$WT" 2>/dev/null || true   # branch persists for review
  fi
  log "DONE $ID passed"
else
  board finish "$ID" blocked - "acceptance failed (exit $rc)"
  complete_terminal_escalation failed "acceptance test failed after retry" "" "$rc" || exit $?
  log "left worktree for inspection: $WT (branch $BRANCH)"
fi
