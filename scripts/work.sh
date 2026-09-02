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
  local pgid="$1" i
  kill -TERM -- "-$pgid" 2>/dev/null || true
  for i in $(seq 1 50); do
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
# repo: which git repo the arm builds in. Default = this repo ($ROOT). An external repo
# (e.g. /opt/golden-stallion-client) lets the loop build another codebase; its build output
# and loop/work/<id> branch live in THAT repo, gated by whatever acceptance the task names.
REPO_DIR="$(field repo)"; REPO_DIR="${REPO_DIR:-$ROOT}"
# seed: gitignored paths (space/comma separated, relative to REPO_DIR) to COPY into each fresh
# worktree. git worktree does NOT inherit gitignored files, so a required-but-untracked data
# corpus (e.g. the GSC block-model corpus user-client/render/data/models) is absent in a new
# worktree and every test that needs it fails. Declaring it here makes the dependency explicit.
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
IMPLEMENTER_RUN_ID=""; VERIFIER_RUN_ID=""; RESOLVED_MODEL="$MODEL"; RESOLVED_VERIFIER_MODEL=""
LAST_USAGE_FILE=""; SEED_DIGEST_BEFORE=""
SEED_EVIDENCE_FILE="$ROOT/state/runs/$ID-seed-$STAMP-$$.json"

CONTRACT_DIGEST="$(python3 - "$TASK" <<'PY'
import hashlib,json,sys
text=open(sys.argv[1],encoding='utf-8',errors='surrogateescape').read()
header,sep,body=text.partition('\n\n')
keys={'id','project_id','pilot_id','goal_id','goal','engine','verifier_engine','repo',
       'acceptance','workdir',
       'allowed_paths','non_goals','worker_class','model_alias','verifier_model_alias',
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
  python3 "$ROOT/scripts/runtime-state.py" lock-acquire "$WSLOCK" "$$" >>"$LOG" 2>&1
}
project_lock_release(){
  [ "$WSLOCK_HELD" = 1 ] || return 0
  if python3 "$ROOT/scripts/runtime-state.py" lock-release "$WSLOCK" "$$" >>"$LOG" 2>&1; then
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
  python3 "$ROOT/scripts/runtime-state.py" lock-acquire "$ROOT/state/.locks/ledger.lock" "$$" \
    >>"$LOG" 2>&1 || { log "ledger lock contended — refusing unlocked commit"; return 73; }
}
ledger_unlock(){
  python3 "$ROOT/scripts/runtime-state.py" lock-release "$ROOT/state/.locks/ledger.lock" "$$" \
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
  case "$DEADLINE_SECONDS:$VERIFIER_DEADLINE_SECONDS" in
    *[!0-9:]*|:*|*:) reject_preworker_contract deadline-invalid 2; exit $? ;;
  esac
  [ "$DEADLINE_SECONDS" -ge 1 ] && [ "$DEADLINE_SECONDS" -le "${RECURSION_WORKER_MAX_TIMEOUT_S:-86400}" ] \
    || { reject_preworker_contract deadline-invalid 2; exit $?; }
  [ "$VERIFIER_DEADLINE_SECONDS" -ge 1 ] && [ "$VERIFIER_DEADLINE_SECONDS" -le "${RECURSION_WORKER_MAX_TIMEOUT_S:-86400}" ] \
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
engine_is_builtin(){ case "$1" in none|claude|hermes|gate|deepseek|openrouter) return 0 ;; *) return 1 ;; esac; }
plugin_probe_ok(){ # plan-file: absent probe passes; a declared probe needs a 2xx from its URL.
  # Probe from inside the provider-only namespace when it exists, so "reachable" is measured
  # where the worker will actually run, not on the controller's unrestricted network.
  local plan="$1"
  local -a runner=(python3)
  if [ -e /run/netns/recursion-provider-only ] && command -v ip >/dev/null 2>&1; then
    runner=(ip netns exec recursion-provider-only python3)
  fi
  "${runner[@]}" - "$plan" <<'PY'
import json, os, sys, urllib.request
plan = json.load(open(sys.argv[1]))
probe = plan.get("availability_probe")
if not probe:
    raise SystemExit(0)
url = os.environ.get(probe.get("url_env", ""), "").strip()
if not url:
    raise SystemExit(1)
try:
    with urllib.request.urlopen(url, timeout=int(probe.get("timeout_s", 5))) as response:
        raise SystemExit(0 if 200 <= response.status < 300 else 1)
except Exception:
    raise SystemExit(1)
PY
}
engine_slot_release(){
  local slot
  for slot in ${ENGINE_SLOT_LOCKS[@]+"${ENGINE_SLOT_LOCKS[@]}"}; do
    python3 "$ROOT/scripts/runtime-state.py" lock-release "$slot" "$$" >>"$LOG" 2>&1 \
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
    if python3 "$ROOT/scripts/runtime-state.py" lock-acquire "$slot" "$$" >>"$LOG" 2>&1; then
      ENGINE_SLOT_LOCKS+=("$slot")
      return 0
    fi
  done
  return 1
}
plugin_resolve_plan(){ # role engine plan-path: emit the plan or log the refusal (rc 92)
  local role="$1" engine="$2" plan_path="$3" rc
  if python3 "$ROOT/scripts/resolve-engine.py" "$engine" --role "$role" \
      --emit-plan "$plan_path" 2>>"$LOG"; then
    rc=0
  else rc=$?; fi
  if [ "$rc" -ne 0 ]; then
    log "engine=$engine refused by the plugin resolver for the $role role rc=$rc (2=invalid manifest/role, 3=disabled or absent)"
    return 92
  fi
}
plugin_plan_checks(){ # plan resolved-model engine role: billing admission + model requirement
  local plan="$1" resolved_model="$2" engine="$3" role="$4" basis mreq
  basis="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["billing"]["basis"])' \
    "$plan" 2>>"$LOG")" || { log "engine plan billing could not be read"; return 95; }
  if [ "$basis" = metered ] && [ "${RECURSION_ALLOW_METERED:-0}" != 1 ]; then
    log "engine=$engine ($role) bills as metered and RECURSION_ALLOW_METERED is unset"
    return 93
  fi
  mreq="$(python3 -c 'import json,sys; print(1 if json.load(open(sys.argv[1]))["model_required"] else 0)' \
    "$plan" 2>>"$LOG")" || { log "engine plan model requirement could not be read"; return 95; }
  if [ "$mreq" = 1 ] && [ -z "$resolved_model" ]; then
    log "engine=$engine requires a model and the $role has none resolved"
    return 92
  fi
}
plugin_engine_gate(){ # engine plan: availability probe + witnessed capacity slot
  local engine="$1" plan="$2" cap rc
  cap="$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1]))["concurrency_cap"]; print("" if v is None else v)' \
    "$plan" 2>>"$LOG")" || { log "engine plan capacity could not be read"; return 95; }
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
  GATE_STRICT_ARGS=()
  [ -z "$PILOT_ID" ] || GATE_STRICT_ARGS+=(--strict-direct)
  GATE_ERR="$(mktemp "${TMPDIR:-/tmp}/validate-gate.XXXXXX")" || exit 74
  if GATE_CONTRACT="$(python3 "$ROOT/scripts/validate-gate.py" --acceptance "$ACCEPTANCE" \
      --allowed-paths "${ALLOWED_PATHS:-$WORKDIR}" --gate-files "$GATE_FILES" --repo "$REPO_DIR" \
      --controller-root "$ROOT" --controller-gate-files "$CONTROLLER_GATE_FILES" \
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
    approved=json.loads(sys.argv[1]); observed=json.loads(sys.argv[2])
except json.JSONDecodeError:
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
  if ! python3 "$ROOT/scripts/runtime-state.py" lock-acquire "$wtlock" "$$" >>"$LOG" 2>&1; then
    log "worktree lock contended — refusing unlocked worktree mutation"
    return 73
  fi
  git -C "$REPO_DIR" worktree remove --force "$wt" 2>/dev/null || true
  rm -rf "$wt" 2>/dev/null || true
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
  python3 "$ROOT/scripts/runtime-state.py" lock-release "$wtlock" "$$" >>"$LOG" 2>&1 \
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
  python3 -c 'import json,sys; print(json.loads(sys.argv[1])["content_sha256"])' "$result"
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
row=json.loads(sys.argv[1]); expected_paths=[p for p in sys.argv[5].split(',') if p]
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
  log "seed contract copied and verified ($(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("file_count",0))' "$result") files)"
}

verify_seed_integrity(){
  [ -n "$SEED" ] || return 0
  local observed
  observed="$(seed_digest "$1")" || { SCOPE_FAIL="declared seed integrity could not be measured"; return 1; }
  [ "$observed" = "${SEED_DIGEST_EXPECTED:-$SEED_DIGEST_BEFORE}" ] \
    || { SCOPE_FAIL="worker/gate mutated a declared read-only seed or oracle"; return 1; }
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
=========================== $HUMAN_GUIDANCE
===========================
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
run_model_supervised(){ # role worktree prompt resolved-model alias usage deadline run-id [commit] [implementer-run] [evidence-dir] [readonly]
  local role="$1" wt="$2" prompt="$3" resolved_model="$4" alias="$5" usage="$6"
  local deadline_s="$7" run_id="$8" commit_oid="${9:-}" implementer_run="${10:-}"
  local evidence_dir="${11:-}" readonly="${12:-0}" engine_plan=""
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
    ( umask 077; python3 - "$engine_plan" > "$usage" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1]))
print(json.dumps({"schema_version": 1, "provider": plan["billing"]["provider"],
                  "billing_basis": plan["billing"]["basis"], "tokens": 0, "cost_usd": 0,
                  "evidence_source": "engine-manifest-v1"}))
PY
    ) || return 98
  fi
  local -a cmd=(bash "$ROOT/scripts/worker-sandbox.sh" --engine "$active_engine" --worktree "$wt"
    --prompt "$prompt" --role "$role" --network-policy "$NETWORK_POLICY")
  [ -n "$resolved_model" ] && cmd+=(--model "$resolved_model")
  [ -z "$engine_plan" ] || cmd+=(--engine-plan "$engine_plan")
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
  local worker_pgid rc now_epoch deadline_epoch heartbeat_epoch log_path
  log_path="$ARMLOG"; [ "$role" != verifier ] || log_path="$ROOT/loop/logs/$DATE-verifier-$ID-$STAMP.out"
  RECURSION_VERIFICATION_COMMIT="$commit_oid" RECURSION_IMPLEMENTER_RUN_ID="$implementer_run" \
    setsid --wait "${cmd[@]}" >"$log_path" 2>&1 &
  worker_pgid=$!
  deadline_epoch="$(( $(date +%s) + deadline_s ))"; heartbeat_epoch=0
  LAST_RUN_ID="$run_id"; LAST_RUN_EVIDENCE="$ROOT/state/runs/$run_id.json"
  log "$role process group=$worker_pgid engine=$active_engine alias=${alias:-legacy} deadline=${deadline_s}s"
  if [ "$STRICT_PHASE5" = 1 ]; then
    mkdir -p "$ROOT/state/runs"
    python3 "$ROOT/scripts/worker-evidence.py" start --path "$LAST_RUN_EVIDENCE" --run-id "$run_id" \
      --task-id "$ID" --role "$role" --model-alias "$alias" --worktree "$wt" \
      --base-commit "$BASE_COMMIT" --pid "$worker_pgid" --process-group "$worker_pgid" \
      --deadline-epoch "$deadline_epoch" --log "${log_path#$ROOT/}" --network-policy "$NETWORK_POLICY" \
      ${SEED_EVIDENCE_FILE:+--seed-evidence "$SEED_EVIDENCE_FILE"} \
      ${implementer_run:+--implementer-run-id "$implementer_run"} ${commit_oid:+--commit-oid "$commit_oid"} \
      >>"$LOG" 2>&1 || { terminate_worker_group "$worker_pgid"; wait "$worker_pgid" 2>/dev/null || true; return 98; }
  fi
  while kill -0 "$worker_pgid" 2>/dev/null; do
    if [ -f "$ROOT/state/PAUSE" ]; then
      log "PAUSE observed with $role process group $worker_pgid in flight"
      terminate_worker_group "$worker_pgid" || true
      wait "$worker_pgid" 2>/dev/null || true
      kill -0 -- "-$worker_pgid" 2>/dev/null \
        && { log "worker group still alive after cancellation"; return 96; }
      [ "$STRICT_PHASE5" != 1 ] || python3 "$ROOT/scripts/worker-evidence.py" finish \
        "$LAST_RUN_EVIDENCE" cancelled pause 75 >>"$LOG" 2>&1 || true
      log "$role process group $worker_pgid terminated before gates/publication"
      return 75
    fi
    now_epoch="$(date +%s)"
    if [ "$STRICT_PHASE5" = 1 ] && [ "$now_epoch" -ge "$deadline_epoch" ]; then
      log "$role deadline expired; terminating process group $worker_pgid"
      terminate_worker_group "$worker_pgid" || true
      wait "$worker_pgid" 2>/dev/null || true
      kill -0 -- "-$worker_pgid" 2>/dev/null && return 96
      python3 "$ROOT/scripts/worker-evidence.py" finish "$LAST_RUN_EVIDENCE" failed timeout 124 \
        >>"$LOG" 2>&1 || true
      return 97
    fi
    if [ "$now_epoch" -ge "$heartbeat_epoch" ]; then
      heartbeat_epoch="$(( now_epoch + ${RECURSION_WORKER_HEARTBEAT_S:-5} ))"
      window_rc=0
      window_checkpoint "$role execution" || window_rc=$?
      if [ "$window_rc" -ne 0 ]; then
        log "pilot-window checkpoint stopped $role process group $worker_pgid in flight reason=$WINDOW_AUTHORITY_FAILURE"
        terminate_worker_group "$worker_pgid" || true
        wait "$worker_pgid" 2>/dev/null || true
        [ "$STRICT_PHASE5" != 1 ] || python3 "$ROOT/scripts/worker-evidence.py" finish \
          "$LAST_RUN_EVIDENCE" cancelled "$WINDOW_AUTHORITY_FAILURE" 75 >>"$LOG" 2>&1 || true
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
  return "$rc"
}

run_arm_supervised(){
  local wt="$1" attempt="${2:-1}" run_id
  run_id="$ID-impl-$STAMP-$$-a$attempt"
  run_model_supervised implementer "$wt" "$BUILD_PROMPT
EXACT BASE COMMIT: $BASE_COMMIT" "$RESOLVED_MODEL" "${MODEL_ALIAS:-legacy}" "$ARM_USAGE" \
    "$DEADLINE_SECONDS" "$run_id"
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
try: d=json.load(open(sys.argv[1],encoding='utf-8'))
except Exception: sys.exit(0)
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
  [ -z "$commit_oid" ] || args+=(--commit-oid "$commit_oid")
  [ ! -f "$SEED_EVIDENCE_FILE" ] || args+=(--seed-evidence "$SEED_EVIDENCE_FILE" --seed-verified-after)
  python3 "$ROOT/scripts/worker-evidence.py" "${args[@]}" >>"$LOG" 2>&1
}

phase5_validate_result(){
  [ "$STRICT_PHASE5" = 1 ] || return 0
  local result="$1/.recursion-result.json"
  VALIDATED_RESULT_FILE="$ROOT/state/runs/$LAST_RUN_ID-result.json"
  [ -f "$result" ] || { SCOPE_FAIL="required worker result is missing"; phase5_record_run failed missing-result 98 || true; return 1; }
  python3 "$ROOT/scripts/validate-worker-result.py" "$result" "$LAST_SCOPE_FILE" \
    --expected-change "$EXPECTED_CHANGE" >"$VALIDATED_RESULT_FILE" 2>"$VALIDATED_RESULT_FILE.err" \
    || { SCOPE_FAIL="$(cat "$VALIDATED_RESULT_FILE.err")"; phase5_record_run failed invalid-result 98 || true; return 1; }
  rm -f "$VALIDATED_RESULT_FILE.err"
  read_result "$1" || { SCOPE_FAIL="validated result could not be projected"; return 1; }
  phase5_record_run succeeded worker-complete 0 || return $?
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
    out="$(RECURSION_ROOT="$ROOT" RECURSION_HEAVY_TAG="$ID" RECURSION_HEAVY_PID="$$" \
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
  if ! RECURSION_ROOT="$ROOT" RECURSION_HEAVY_PID="$$" \
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
    && verify_seed_integrity "$1"
}

run_independent_verifier(){
  [ "$STRICT_PHASE5" = 1 ] || return 0
  local impl_run="$IMPLEMENTER_RUN_ID" impl_evidence="$LAST_RUN_EVIDENCE"
  local short verify_wt verifier_id evidence_dir verdict verifier_log
  short="${CANDIDATE_OID:0:12}"; verify_wt="$REPO_DIR/.worktrees/$ID-verifier-$short-$$"
  verifier_id="$ID-verify-$STAMP-$$"; evidence_dir="$ROOT/state/runs/$verifier_id-evidence"
  verdict="$evidence_dir/verdict.json"; verifier_log="$ROOT/loop/logs/$DATE-verifier-$ID-$STAMP.out"
  local prompt rc head dirty
  rm -rf "$evidence_dir"; mkdir -p "$evidence_dir"
  git -C "$REPO_DIR" worktree add --detach "$verify_wt" "$CANDIDATE_OID" >>"$LOG" 2>&1 || return 99
  if ! copy_seed_into_worktree "$verify_wt" || ! verify_seed_integrity "$verify_wt"; then
    git -C "$REPO_DIR" worktree remove --force "$verify_wt" >/dev/null 2>&1 || true
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
      "$verifier_id" "$CANDIDATE_OID" "$impl_run" "$evidence_dir" 1; then rc=0; else rc=$?; fi
  VERIFIER_RUN_ID="$LAST_RUN_ID"
  if [ "$rc" -ne 0 ]; then
    git -C "$REPO_DIR" worktree remove --force "$verify_wt" >/dev/null 2>&1 || true
    LAST_RUN_EVIDENCE="$impl_evidence"; IMPLEMENTER_RUN_ID="$impl_run"
    return "$rc"
  fi
  head="$(git -C "$verify_wt" rev-parse HEAD 2>/dev/null || true)"
  dirty="$(git -C "$verify_wt" status --porcelain --untracked-files=all 2>/dev/null || true)"
  if [ "$head" != "$CANDIDATE_OID" ] || [ -n "$dirty" ] || ! verify_seed_integrity "$verify_wt"; then
    python3 "$ROOT/scripts/worker-evidence.py" finish "$LAST_RUN_EVIDENCE" failed verifier-mutated-source 99 \
      --commit-oid "$CANDIDATE_OID" \
      ${SEED_EVIDENCE_FILE:+--seed-evidence "$SEED_EVIDENCE_FILE"} >>"$LOG" 2>&1 || true
    git -C "$REPO_DIR" worktree remove --force "$verify_wt" >/dev/null 2>&1 || true
    LAST_RUN_EVIDENCE="$impl_evidence"; IMPLEMENTER_RUN_ID="$impl_run"
    return 99
  fi
  if ! python3 "$ROOT/scripts/validate-verifier-result.py" "$verdict" --commit "$CANDIDATE_OID" \
      --implementer-run "$impl_run" >"$evidence_dir/validated-verdict.json" 2>>"$LOG"; then
    python3 "$ROOT/scripts/worker-evidence.py" finish "$LAST_RUN_EVIDENCE" failed invalid-verdict 99 \
      --commit-oid "$CANDIDATE_OID" >>"$LOG" 2>&1 || true
    git -C "$REPO_DIR" worktree remove --force "$verify_wt" >/dev/null 2>&1 || true
    LAST_RUN_EVIDENCE="$impl_evidence"; IMPLEMENTER_RUN_ID="$impl_run"
    return 99
  fi
  mv -f "$evidence_dir/validated-verdict.json" "$verdict"
  local -a finish_args=(finish "$LAST_RUN_EVIDENCE" succeeded verified 0
    --result-file "$verdict" --commit-oid "$CANDIDATE_OID")
  [ ! -f "$SEED_EVIDENCE_FILE" ] \
    || finish_args+=(--seed-evidence "$SEED_EVIDENCE_FILE" --seed-verified-after)
  [ -z "$LAST_USAGE_FILE" ] || [ ! -f "$LAST_USAGE_FILE" ] \
    || finish_args+=(--usage-file "$LAST_USAGE_FILE")
  python3 "$ROOT/scripts/worker-evidence.py" "${finish_args[@]}" >>"$LOG" 2>&1 || return 98
  python3 "$ROOT/scripts/worker-evidence.py" verify "$LAST_RUN_EVIDENCE" "$CANDIDATE_OID" \
    "$impl_run" "$verdict" >>"$LOG" 2>&1 || return 98
  python3 "$ROOT/scripts/worker-evidence.py" link-verifier "$impl_evidence" "$VERIFIER_RUN_ID" \
    "$CANDIDATE_OID" "$verdict" >>"$LOG" 2>&1 || return 98
  local verdict_value; verdict_value="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["verdict"])' "$verdict")"
  git -C "$REPO_DIR" worktree remove --force "$verify_wt" >/dev/null 2>&1 || true
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
  case "$arc" in
    90) msg="engine=$ENGINE CLI not logged in / unavailable on the box (for claude: run claude /login)" ;;
    91) msg="engine=$ENGINE has no file-writing tool (use an opencode engine manifest instead, D-015)" ;;
    92) msg="engine=$ENGINE is not runnable (unknown engine, or its admitted plugin plan is missing)" ;;
    93) msg="engine=$ENGINE is metered — a human sets RECURSION_ALLOW_METERED=1 in .env to allow it" ;;
    94) msg="hermes CLI unavailable" ;;
    95) msg="worker sandbox unavailable or dedicated runner not provisioned" ;;
    96) msg="worker process group could not be confirmed terminated" ;;
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
