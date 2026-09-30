#!/usr/bin/env bash
# plan.sh — turn the next roadmap item into a runnable task. The human sets DIRECTION (an ordered
# feature list); the loop mechanizes turning the next item into a task file the drainer can run.
# This keeps scope in human hands while the execution is autonomous.
#
#   plan.sh <workstream>        emit a task for the first unchecked roadmap item
#
# Roadmap file: state/roadmaps/<workstream>.md — a header of `key: value` task defaults, a blank
# line, then a checklist. Example:
#     engine: claude
#     repo: /opt/recursion
#     sshkey: ~/.ssh/id_deploy
#     workdir: apps/example
#     acceptance: cd apps/example && npm install && npm run build
#
# `automerge:` / `automerge_scope:` are NOT roadmap-grantable (esc-0001). A roadmap says what to
# build; it does not hand out default-branch write authority. Headers carrying them are dropped
# with a warning — see below.
#
#     - [x] Scaffold a minimal Next.js shell
#     - [ ] Add a landing page with a hero + CTA        <- plan.sh emits THIS, marks it [~]
set -euo pipefail
CODE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="${RECURSION_ROOT:-$(cd "$CODE_ROOT/.." && pwd -P)}"; cd "$ROOT"
require_repository_contract(){ python3 - "$CODE_ROOT" <<'PY'
import importlib
import sys
from pathlib import Path

code_root = Path(sys.argv[1])
sys.path.insert(0, str(code_root))
try:
    importlib.import_module("repository_contract")
except Exception as exc:
    missing = getattr(exc, "name", None) or "repository_contract"
    print(
        f"plan: required module 'repository_contract' could not be imported; "
        f"missing module '{missing}'; searched code root: {code_root}",
        file=sys.stderr,
    )
    raise SystemExit(74)
PY
}
require_repository_contract
fsync_dir(){ python3 - "$1" <<'PY' >/dev/null 2>&1
import os,sys
try:
  fd=os.open(sys.argv[1],os.O_RDONLY)
  try:
    os.fsync(fd)
  finally:
    os.close(fd)
except (ValueError,TypeError,OverflowError,RecursionError,UnicodeDecodeError,OSError):
  raise SystemExit(74)
PY
}
atomic_replace_checked(){ python3 - "$1" "$2" "$CODE_ROOT" <<'PY'
import os,stat,sys
from pathlib import Path

code_root = Path(sys.argv[3])
sys.path.insert(0, str(code_root))
try:
    import repository_contract as rc
except Exception as exc:
    missing = getattr(exc, "name", None) or "repository_contract"
    print(
        f"plan: required module 'repository_contract' could not be imported; "
        f"missing module '{missing}'; searched code root: {code_root}",
        file=sys.stderr,
    )
    raise SystemExit(74)

try:
    source = rc._lexical_absolute(sys.argv[1])
    target = rc._lexical_absolute(sys.argv[2])
    if source.parent != target.parent:
        raise ValueError("atomic replacement must stay in one directory")
    source_info = os.lstat(source)
    if rc._is_reparse_or_link(source_info) or not stat.S_ISREG(source_info.st_mode) \
            or getattr(source_info, "st_nlink", 1) != 1:
        raise ValueError("replacement source is not a private regular file")
    with rc._pinned_real_directory(target.parent) as parent_fd:
        if parent_fd is None:
            raise OSError("replacement directory was not pinned")
        current = os.lstat(source)
        if rc._is_reparse_or_link(current) or not stat.S_ISREG(current.st_mode) \
                or getattr(current, "st_nlink", 1) != 1 or not rc._same_file(source_info, current):
            raise OSError("replacement source changed while being pinned")
        if os.name == "nt":
            os.replace(source, target)
        else:
            os.replace(source.name, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
except (ValueError,TypeError,OverflowError,RecursionError,UnicodeDecodeError,OSError) as exc:
    print(f"plan: atomic replacement refused: {exc}", file=sys.stderr)
    raise SystemExit(74)
PY
}
PLANNER_LOCK_HELD=0; PLANNER_LOCK=""
planner_lock_acquire(){
  PLANNER_LOCK="$1"
  [ -x "$ROOT/scripts/runtime-state.py" ] \
    || { echo "plan: witnessed planner lock helper is absent" >&2; return 73; }
  python3 "$ROOT/scripts/runtime-state.py" lock-acquire "$PLANNER_LOCK" "$$" >/dev/null
}
planner_lock_release(){
  [ "$PLANNER_LOCK_HELD" = 1 ] || return 0
  if python3 "$ROOT/scripts/runtime-state.py" lock-release "$PLANNER_LOCK" "$$" >/dev/null; then
    PLANNER_LOCK_HELD=0
    return 0
  fi
  echo "plan: planner lock release failed closed: $PLANNER_LOCK" >&2
  return 74
}

if [ "${1:-}" = --complete ]; then
  PLANNED="${2:?usage: plan.sh --complete state/roadmaps/<workstream>.md:<line>}"
  case "$PLANNED" in state/roadmaps/*.md:[0-9]*) ;; *) echo "plan: invalid planned_from pointer" >&2; exit 2 ;; esac
  RM="${PLANNED%:*}"; LN="${PLANNED##*:}"; WS="$(basename "$RM" .md)"
  printf '%s' "$WS" | grep -qE '^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$' || exit 2
  [ -f "$RM" ] || { echo "plan: no roadmap for completion: $RM" >&2; exit 2; }
  mkdir -p state/.locks
  LOCK="state/.locks/planner-$WS.lock"
  planner_lock_acquire "$LOCK" \
    || { echo "plan: planner lock contended; refusing unlocked completion" >&2; exit 73; }
  PLANNER_LOCK_HELD=1
  trap 'planner_lock_release || true' EXIT
  current="$(sed -n "${LN}p" "$RM")"
  case "$current" in '- [x] '*) echo "roadmap already complete: $PLANNED"; exit 0 ;; '- [~] '*) : ;; *) echo "plan: roadmap row is not reserved: $PLANNED" >&2; exit 75 ;; esac
  tmp_rm="$(mktemp "$(dirname "$RM")/.$(basename "$RM").tmp.XXXXXX")"
  cp -p -- "$RM" "$tmp_rm"
  sed -i "${LN}s/^- \[~\] /- [x] /" "$tmp_rm"
  python3 - "$tmp_rm" <<'PY'
import os,sys
with open(sys.argv[1],'rb') as source: os.fsync(source.fileno())
PY
  mv -f -- "$tmp_rm" "$RM"
  fsync_dir "$(dirname "$RM")"
  echo "roadmap complete: $PLANNED"
  exit 0
fi

WS="${1:?usage: plan.sh <workstream>}"
printf '%s' "$WS" | grep -qE '^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$' \
  || { echo "plan: invalid workstream id" >&2; exit 2; }
RM="state/roadmaps/$WS.md"
[ -f "$RM" ] || { echo "no roadmap: $RM (create it: header of task defaults + a '- [ ]' checklist)"; exit 2; }
TASKDIR="state/tasks"; mkdir -p "$TASKDIR"

# Allocation and roadmap reservation are one critical section. The old read/allocate/write/sed
# sequence let two planners choose the same ID and checklist row. A contended lock never degrades
# to an unlocked write.
LOCKDIR="state/.locks"; mkdir -p "$LOCKDIR"
LOCK="$LOCKDIR/planner-$WS.lock"
planner_lock_acquire "$LOCK" \
  || { echo "plan: planner lock contended; refusing unlocked allocation" >&2; exit 73; }
PLANNER_LOCK_HELD=1
trap 'planner_lock_release || true' EXIT

# header defaults: read a key's value from the key:value lines above the first checklist item
hget(){ awk -v k="$1" -F': *' '/^- \[/{exit} $1==k{sub(/^[^:]+: */,"");print;exit}' "$RM"; }

# first UNCHECKED roadmap item (the goal)
GOAL_LINE="$(grep -nE '^- \[ \] ' "$RM" | head -1 || true)"
[ -n "$GOAL_LINE" ] || { echo "roadmap $WS: nothing unchecked — queue not refilled (all planned)."; exit 3; }
LN="${GOAL_LINE%%:*}"
GOAL="$(printf '%s' "$GOAL_LINE" | sed -E 's/^[0-9]+:- \[ \] //')"

# next id: <ws>-NNN (zero-padded, max existing + 1)
last="$(ls state/tasks/$WS-*.md 2>/dev/null | sed -E "s#.*/$WS-0*([0-9]+)\.md#\1#" | sort -n | tail -1 || true)"
next="$(printf '%03d' $(( ${last:-0} + 1 )))"
ID="$WS-$next"; TASK="state/tasks/$ID.md"

ENGINE="$(hget engine)";   REPO="$(hget repo)";     SSHKEY="$(hget sshkey)"
WORKDIR="$(hget workdir)"; ACC="$(hget acceptance)"; SEED="$(hget seed)"
AM="$(hget automerge)";    AMS="$(hget automerge_scope)"; MODEL="$(hget model)"
GOAL_ID="$(hget goal_id)"; DEPS="$(hget dependencies)"; ALLOWED="$(hget allowed_paths)"
NON_GOALS="$(hget non_goals)"; WORKER_CLASS="$(hget worker_class)"
GATE_COMMANDS="$(hget gate_commands)"
GATE_FILES="$(hget gate_files)"
CONTROLLER_GATE_FILES="$(hget controller_gate_files)"
GOAL_ID="${GOAL_ID:-goal-$WS}"
# Registered projects must bind the task to the exact current source tip. Disposable planner
# fixtures intentionally have no descriptor/checkout and retain their v1 compatibility path.
SOURCE_BRANCH=""; SOURCE_SHA=""; PROJECT_CONFIG=""; REPO_CANONICAL="$REPO"
for candidate_config in state/project-config/*.json; do
  [ -f "$candidate_config" ] || continue
  project_match="$(python3 - "$candidate_config" "$REPO" <<'PY'
import json,os,sys
from pathlib import Path
try:
    def reject_constant(value):
        raise ValueError("non-standard JSON constant")
    with open(sys.argv[1],encoding='utf-8') as stream:
        value=json.load(stream, parse_constant=reject_constant)
    if not isinstance(value,dict):
        print('invalid'); raise SystemExit(0)
    configured_raw=value.get('repo')
    if not isinstance(configured_raw,str) or not configured_raw.strip() \
       or any(char in configured_raw for char in '~%`*?'):
        print('invalid')
    else:
        configured=Path(os.path.abspath(configured_raw))
        requested=Path(os.path.abspath(str(sys.argv[2] or '')))
        print(value.get('status','invalid') if configured == requested else 'no')
except (json.JSONDecodeError,ValueError,TypeError,OverflowError,RecursionError,UnicodeDecodeError,OSError):
    print('invalid')
PY
)"
  case "$project_match" in
    active)
      [ -f "$CODE_ROOT/repository_contract.py" ] \
        || { echo "plan: required module 'repository_contract' not found; searched code root: $CODE_ROOT" >&2; exit 74; }
      python3 "$CODE_ROOT/repository_contract.py" validate-project "$candidate_config" >/dev/null \
        || { echo "plan: active project descriptor failed v2 validation: $candidate_config" >&2; exit 6; }
      PROJECT_CONFIG="$candidate_config"; break;;
    invalid)
      echo "plan: registered project descriptor is malformed: $candidate_config" >&2
      exit 6;;
    no) :;;
    *)
      echo "plan: registered project is not active (status=$project_match): $candidate_config" >&2
      exit 6;;
  esac
done
if [ -n "$PROJECT_CONFIG" ]; then
  [ -d "${REPO:-}/.git" ] || { echo "plan: registered project checkout is unavailable; exact source SHA cannot be admitted" >&2; exit 74; }
  observed_root="$(git -C "$REPO" rev-parse --show-toplevel 2>/dev/null || true)"
canonical_root="$(python3 - "$REPO" <<'PY'
import os,sys
try:
  print(os.path.abspath(sys.argv[1]))
except (ValueError,TypeError,OverflowError,OSError):
  raise SystemExit(74)
PY
)"
  [ -n "$observed_root" ] && [ "$canonical_root" = "$observed_root" ] \
    || { echo "plan: registered project path is not the exact Git worktree root" >&2; exit 74; }
  REPO_CANONICAL="$canonical_root"
SOURCE_BRANCH="$(python3 - "$PROJECT_CONFIG" <<'PY'
import json,sys
try:
  def reject_constant(value):
    raise ValueError("non-standard JSON constant")
  with open(sys.argv[1],encoding='utf-8') as stream:
    value=json.load(stream, parse_constant=reject_constant)
  if not isinstance(value,dict):
    raise ValueError("descriptor must be an object")
  print(value.get('default_branch',''))
except (json.JSONDecodeError,ValueError,TypeError,OverflowError,RecursionError,UnicodeDecodeError,OSError):
  raise SystemExit(74)
PY
)"
  SOURCE_SHA="$(git -C "$REPO" rev-parse --verify "refs/heads/$SOURCE_BRANCH^{commit}" 2>/dev/null || true)"
  printf '%s' "$SOURCE_SHA" | grep -qE '^[0-9a-f]{40,64}$' \
    || { echo "plan: registered project source branch is unavailable; exact source SHA cannot be admitted" >&2; exit 74; }
fi
# The planner does not grant automerge (esc-0001). Copying the header through is how a task
# acquired `automerge: yes` from a roadmap edit rather than a human decision. The fields are read
# only so the drop can be announced: a header that is silently ignored looks exactly like a
# header that was honored.
if [ -n "$AM" ] || [ -n "$AMS" ]; then
  echo "plan: REFUSED automerge from $RM (automerge='$AM' automerge_scope='$AMS') — not copied into $ID; publication stays review-branch-only. Remove it from the roadmap header." >&2
fi
printf '%s' "$GOAL_ID" | grep -qE '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$' \
  || { echo "plan: roadmap has an invalid goal_id" >&2; exit 2; }
if [ -f schemas/control-plane/v1/schema.json ] || [ -d state/goals ]; then
  [ -f "state/goals/$GOAL_ID.md" ] \
    || { echo "plan: parent goal does not exist: $GOAL_ID" >&2; exit 6; }
  [ "$(sed -n 's/^status: *//p' "state/goals/$GOAL_ID.md" | head -1)" = active ] \
    || { echo "plan: parent goal is not active: $GOAL_ID" >&2; exit 6; }
fi
# Bind the generated task's scope and exact source admission to the active descriptor before
# writing anything.  In particular, a permissive roadmap default (such as `allowed_paths: .`)
# must not widen a descriptor restricted to a subtree.
if [ -n "$PROJECT_CONFIG" ]; then
  python3 - "$CODE_ROOT" "$PROJECT_CONFIG" "$REPO_CANONICAL" "$ID" "$SOURCE_BRANCH" "$SOURCE_SHA" \
    "${ALLOWED:-${WORKDIR:-.}}" "$WORKDIR" "$ACC" "$GOAL" "$GATE_FILES" \
    "$CONTROLLER_GATE_FILES" <<'PY'
import json, re, sys
from pathlib import Path

code_root = Path(sys.argv[1])
sys.path.insert(0, str(code_root))
try:
    import repository_contract as rc
except Exception as exc:
    missing = getattr(exc, "name", None) or "repository_contract"
    print(
        f"plan: required module 'repository_contract' could not be imported; "
        f"missing module '{missing}'; searched code root: {code_root}",
        file=sys.stderr,
    )
    raise SystemExit(74)

_, descriptor_path, repo, task_id, source_branch, source_sha, allowed_raw, workdir, acceptance, goal, \
    gate_files_raw, controller_gate_files_raw = sys.argv[1:]
allowed = [item for item in re.split(r"[\s,]+", allowed_raw) if item]
gate_files = [item for item in re.split(r"[\s,]+", gate_files_raw) if item]
controller_gate_files = [item for item in re.split(r"[\s,]+", controller_gate_files_raw) if item]
task = {"id": task_id, "source_branch": source_branch, "source_sha": source_sha,
        "source_repository": repo, "allowed_paths": allowed, "workdir": workdir,
        "acceptance": acceptance, "goal": goal, "gate_files": gate_files,
        "controller_gate_files": controller_gate_files}
try:
    def reject_constant(value):
        raise ValueError("non-standard JSON constant")
    descriptor = json.loads(Path(descriptor_path).read_text(encoding="utf-8"),
                             parse_constant=reject_constant)
    if not isinstance(descriptor, dict):
        raise ValueError("descriptor must be an object")
    rc.admit_task(rc.validate_project_descriptor(descriptor), task, Path(repo), new_generation=True)
except (json.JSONDecodeError, ValueError, TypeError, OverflowError, RecursionError,
        UnicodeDecodeError, OSError) as exc:
    print(f"plan: generated task violates active repository contract: {exc}", file=sys.stderr)
    raise SystemExit(6)
PY
fi

tmp_task="$(mktemp "$TASKDIR/.${ID}.tmp.XXXXXX")"
{
  echo "schema_version: 1"
  echo "id: $ID"
  echo "workstream: $WS"
  echo "goal_id: $GOAL_ID"
  echo "dependencies: $DEPS"
  echo "goal: $GOAL"
  echo "engine: ${ENGINE:-claude}"
  echo "worker_class: ${WORKER_CLASS:-${ENGINE:-claude}}"
  [ -n "$REPO" ]    && echo "repo: ${REPO_CANONICAL:-$REPO}"
  [ -n "$SSHKEY" ]  && echo "sshkey: $SSHKEY"
  [ -n "$SEED" ]    && echo "seed: $SEED"
  [ -n "$SOURCE_BRANCH" ] && echo "source_branch: $SOURCE_BRANCH"
  [ -n "$SOURCE_SHA" ] && echo "source_sha: $SOURCE_SHA"
  [ -n "$MODEL" ]   && echo "model: $MODEL"
  echo "acceptance: ${ACC:?roadmap $WS is missing an 'acceptance:' default (no gate => no autonomy)}"
  echo "gate_files: ${GATE_FILES:-}"
  echo "controller_gate_files: ${CONTROLLER_GATE_FILES:-}"
  echo "workdir: ${WORKDIR:-.}"
  echo "allowed_paths: ${ALLOWED:-${WORKDIR:-.}}"
  echo "non_goals: $NON_GOALS"
  echo "autonomy: auto"
  echo "status: queued"
  echo "planned_from: $RM:$LN"
  echo
  echo "$GOAL"
  echo
  echo "(Auto-planned from the $WS roadmap. Fix the IMPLEMENTATION, not the test/fixtures. The gate"
  echo "is the acceptance command above; keep every currently-passing check green.)"
} > "$tmp_task"
python3 - "$tmp_task" <<'PY'
import os,sys
try:
  with open(sys.argv[1],'rb') as f:
    os.fsync(f.fileno())
except (ValueError,TypeError,OverflowError,RecursionError,UnicodeDecodeError,OSError):
  raise SystemExit(74)
PY
atomic_replace_checked "$tmp_task" "$TASK"
fsync_dir "$TASKDIR"

# mark the roadmap item as queued ([~]) so it is not re-emitted; it flips to [x] when the task passes.
tmp_rm="$(mktemp "$(dirname "$RM")/.$(basename "$RM").tmp.XXXXXX")"
cp -p -- "$RM" "$tmp_rm"
line="$(sed -n "${LN}p" "$tmp_rm")"
case "$line" in '- [ ] '*) ;; *) rm -f -- "$tmp_rm" "$TASK"; echo "plan: roadmap row changed during reservation" >&2; exit 75 ;; esac
sed -i "${LN}s/^- \[ \] /- [~] /" "$tmp_rm"
python3 - "$tmp_rm" <<'PY'
import os,sys
try:
  with open(sys.argv[1],'rb') as f:
    os.fsync(f.fileno())
except (ValueError,TypeError,OverflowError,RecursionError,UnicodeDecodeError,OSError):
  raise SystemExit(74)
PY
atomic_replace_checked "$tmp_rm" "$RM"
fsync_dir "$(dirname "$RM")"
echo "planned $ID -> $TASK"
echo "  goal: $GOAL"
