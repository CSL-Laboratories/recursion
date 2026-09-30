#!/usr/bin/env bash
# plan-automerge.test.sh — the planner may not hand out automerge authority (esc-0001).
#
# WHY. `automerge:` on a task is the one field that can turn a review-branch loop into a
# default-branch writer, and it never came from a human decision — plan.sh COPIED it out of a
# roadmap header. That is how example-ops-001 and example-ops-003 ended up `status: passed` with
# `automerge: yes`: somebody edited a roadmap, and the planner promoted a scope note into a
# permission. Roadmaps say WHAT to build; they do not grant authority.
#
# So the field is dropped at the planner. Dropping it silently would be worse than copying it —
# a header that looks honored but is ignored is the same failure wearing the opposite mask — so
# the drop is announced on stderr, naming the roadmap the human has to edit.
#
# * Test 1 is the CONTROL: an ordinary roadmap must still plan, or every refusal below is
#   satisfied by a plan.sh that emits nothing at all.
# * Test 3 is the load-bearing one: the human must SEE that the field was dropped.
#
# Hermetic: a disposable fixture root. No network, no real state/, no spend.
#
#   run: bash scripts/tests/plan-automerge.test.sh
set -uo pipefail
export LC_ALL=C
SRC="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
fails=0
ok(){ printf 'ok    %s\n' "$1"; }
no(){ printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }
note(){ printf 'note  %s\n' "$1"; }

if python3 - "$SRC/scripts/plan.sh" <<'PY'
import re, sys
source = open(sys.argv[1], encoding="utf-8").read()
assert 're.split(r"[\\s,]+", allowed_raw)' in source
assert re.split(r"[\s,]+", "user-client/render, docs\ttools") == ["user-client/render", "docs", "tools"]
print("ok    planner allowed_paths parser preserves slash paths and splits spaces/tabs/commas")
PY
then
  :
else
  no "planner allowed_paths parser preserves slash paths and splits spaces/tabs/commas"
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
R="$TMP/root"
mkdir -p "$R/scripts" "$R/state/roadmaps" "$R/state/tasks" "$R/state/goals" "$R/state/.locks"
cp "$SRC/scripts/plan.sh" "$R/scripts/"
cp "$SRC/scripts/runtime-state.py" "$R/scripts/"
cp "$SRC/scripts/repository_contract.py" "$R/scripts/"
cp "$SRC/scripts/transition_receipt.py" "$R/scripts/"
cp "$SRC/scripts/jsonl_durable.py" "$R/scripts/"
chmod +x "$R"/scripts/plan.sh "$R"/scripts/runtime-state.py 2>/dev/null

# The planner lock witnesses the caller's process start time before it will hold a lock. Under
# MSYS `$$` is a shell PID that a Windows-native python cannot open, so the real helper cannot
# witness it and plan.sh refuses to allocate unlocked — correctly. Lock semantics have their own
# control (hermes-pilot-phase2-red.test.sh, test 3); this file is about header translation, so on
# such a host the fixture root gets a lock shim and the real helper is used everywhere else.
if ( cd "$R" && python3 scripts/runtime-state.py lock-acquire state/.locks/probe.lock $$ ) >/dev/null 2>&1; then
  ( cd "$R" && python3 scripts/runtime-state.py lock-release state/.locks/probe.lock $$ ) >/dev/null 2>&1 || true
else
  note "planner lock witness unavailable on this host — fixture lock shim in use"
  cat > "$R/scripts/runtime-state.py" <<'SHIM'
#!/usr/bin/env python3
"""Fixture-only stand-in for the planner lock. See scripts/tests/plan-automerge.test.sh."""
import os, shutil, stat, sys
command = sys.argv[1] if len(sys.argv) > 1 else ""
path = sys.argv[2] if len(sys.argv) > 2 else ""
def remove_lock(name):
    info = os.lstat(name)
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        if os.name == "nt" and stat.S_ISDIR(info.st_mode):
            os.rmdir(name)
        else:
            os.unlink(name)
    else:
        shutil.rmtree(name)
if command == "lock-acquire":
    try:
        os.makedirs(path)
    except FileExistsError:
        raise SystemExit(1)
elif command == "lock-release":
    remove_lock(path)
else:
    raise SystemExit(2)
SHIM
  chmod +x "$R/scripts/runtime-state.py"
fi

# Same story for durability. plan.sh fsyncs each file it writes and the directory it lands in —
# right on the Linux box, impossible here: Windows cannot _commit a read-only descriptor
# (EBADF) and cannot open a directory at all (EACCES), so every plan.sh run dies at its first
# fsync. Rather than edit the copy of plan.sh under test — which would stop it being plan.sh —
# the fixture neutralizes the two syscalls for the fixture's python only, and only where they
# are genuinely unavailable. Durability is covered on the host that can actually do it.
PYSHIM=""
if ! ( cd "$TMP" && : > probe.bin && python3 -c \
     'import os,sys; f=open("probe.bin","rb"); os.fsync(f.fileno())' ) >/dev/null 2>&1; then
  note "fsync unavailable on this host — fixture durability shim in use"
  PYSHIM="$TMP/pyshim"; mkdir -p "$PYSHIM"
  cat > "$PYSHIM/sitecustomize.py" <<'SHIM'
"""Fixture-only. See scripts/tests/plan-automerge.test.sh: this host has no usable fsync."""
import errno, os

_open, _fsync = os.open, os.fsync


def _fsync_optional(fd):
    try:
        return _fsync(fd)
    except OSError as exc:
        if exc.errno in (errno.EBADF, errno.EACCES, errno.EINVAL):
            return None
        raise


def _open_optional(path, flags, *args, **kwargs):
    try:
        return _open(path, flags, *args, **kwargs)
    except OSError:
        if os.path.isdir(path):
            return _open(os.devnull, os.O_RDONLY)
        raise


os.fsync = _fsync_optional
os.open = _open_optional
SHIM
fi

roadmap(){ # roadmap <workstream> <extra-header-lines...>
  local ws="$1"; shift
  printf 'status: active\n' > "$R/state/goals/goal-$ws.md"
  {
    echo "engine: claude"
    echo "repo: /opt/demo"
    echo "workdir: apps/demo"
    echo "acceptance: npm run build"
    for line in "$@"; do echo "$line"; done
    echo
    echo "- [ ] Add a landing page with a hero + CTA"
  } > "$R/state/roadmaps/$ws.md"
}
plan(){ # plan <workstream> -> rc; stdout in $TMP/out, stderr in $TMP/err
  ( cd "$R" && RECURSION_ROOT="$R" PYTHONPATH="${PYSHIM:-}${PYTHONPATH:+:$PYTHONPATH}" \
      bash scripts/plan.sh "$1" ) >"$TMP/out" 2>"$TMP/err"
  echo $?
}
header_of(){ awk 'NF==0{exit} {print}' "$R/state/tasks/$1.md" 2>/dev/null; }

# --- 0. a missing first-party module is diagnosed without a traceback -----------------------
mv -- "$R/scripts/repository_contract.py" "$TMP/repository_contract.py"
expected_code_root="$R/scripts"
if command -v cygpath >/dev/null 2>&1; then
  converted_code_root="$(cygpath -w "$expected_code_root" 2>/dev/null || true)"
  if [ -n "$converted_code_root" ]; then
    expected_code_root="$converted_code_root"
  fi
fi
rc="$(plan missing-module)"
if [ "$rc" != 0 ] && grep -q "required module 'repository_contract'" "$TMP/err" \
   && grep -Fq "searched code root: $expected_code_root" "$TMP/err" \
   && ! grep -q 'Traceback' "$TMP/err"; then
  ok "a missing planner module fails closed with its module and searched code root"
else
  no "missing planner module was not diagnosed cleanly (rc=$rc): $(cat "$TMP/err")"
fi
mv -- "$TMP/repository_contract.py" "$R/scripts/repository_contract.py"

# --- 1. CONTROL: an ordinary roadmap still plans -------------------------------------------
roadmap clean
rc="$(plan clean)"
if [ "$rc" = 0 ] && [ -f "$R/state/tasks/clean-001.md" ] \
   && header_of clean-001 | grep -q '^acceptance: npm run build$'; then
  ok "CONTROL: a roadmap without automerge still produces a task"
else
  no "CONTROL: plan.sh did not produce a usable task (rc=$rc): $(cat "$TMP/err")"
fi
if ! grep -qi automerge "$TMP/err"; then
  ok "a clean roadmap draws no automerge warning (the refusal stays meaningful)"
else
  no "plan.sh warned about automerge on a roadmap that has none: $(cat "$TMP/err")"
fi

# --- 1b. gate authority metadata survives planning ----------------------------------------
roadmap gated "gate_files: app/check.py" "controller_gate_files: scripts/controller-gate.py"
rc="$(plan gated)"
if [ "$rc" = 0 ] && [ -f "$R/state/tasks/gated-001.md" ] \
   && header_of gated-001 | grep -q '^gate_files: app/check.py$' \
   && header_of gated-001 | grep -q '^controller_gate_files: scripts/controller-gate.py$'; then
  ok "planner serializes gate_files and controller_gate_files into gate-evidence tasks"
else
  no "planner dropped gate authority metadata (rc=$rc): $(cat "$TMP/err")"
fi

# --- 2. the field does not reach the task --------------------------------------------------
roadmap grant "automerge: yes" "automerge_scope: apps/demo"
rc="$(plan grant)"
if [ "$rc" = 0 ] && [ -f "$R/state/tasks/grant-001.md" ]; then
  ok "a roadmap carrying automerge still plans (the item is not lost)"
else
  no "plan.sh failed on an automerge-carrying roadmap (rc=$rc): $(cat "$TMP/err")"
fi
if [ -f "$R/state/tasks/grant-001.md" ] && ! header_of grant-001 | grep -qi '^automerge'; then
  ok "* automerge:/automerge_scope: are NOT copied into the generated task"
else
  no "* automerge survived into the task: $(header_of grant-001 | grep -i '^automerge' | tr '\n' ' ')"
fi

# --- 3. the drop is announced, on stderr, naming the roadmap -------------------------------
if grep -qi automerge "$TMP/err"; then
  ok "* plan.sh prints a visible automerge refusal to stderr"
else
  no "* automerge was dropped SILENTLY — a human cannot tell the header was ignored"
fi
if grep -i automerge "$TMP/err" | grep -q 'state/roadmaps/grant.md'; then
  ok "the refusal names the roadmap the human has to edit"
else
  no "the refusal does not name the offending roadmap: $(cat "$TMP/err")"
fi
if ! grep -qi automerge "$TMP/out"; then
  ok "the refusal is not mixed into stdout (which the planner's callers parse)"
else
  no "an automerge line leaked onto stdout: $(cat "$TMP/out")"
fi

# --- 4. automerge_scope alone is the same grant, half-written ------------------------------
roadmap scoped "automerge_scope: apps/demo"
rc="$(plan scoped)"
if [ "$rc" = 0 ] && ! header_of scoped-001 | grep -qi '^automerge' && grep -qi automerge "$TMP/err"; then
  ok "a bare automerge_scope: is dropped and announced too"
else
  no "automerge_scope alone was copied through or passed unannounced (rc=$rc)"
fi

# --- 5. refusing the field does not break the reservation ----------------------------------
if grep -q '^- \[~\] ' "$R/state/roadmaps/grant.md"; then
  ok "the roadmap row is still reserved — dropping the field is not a planner failure"
else
  no "the roadmap row was not reserved after the automerge drop"
fi

# --- 6. a matching registered project may not fall through to the fixture path -------------
mkdir -p "$R/state/project-config" "$R/registered"
REG="$(cd "$R/registered" && pwd -P)"
cat > "$R/state/project-config/registered.json" <<JSON
{"id":"registered","repo":"$REG","status":"pending"}
JSON
{
  echo "repo: $REG"
  echo "workdir: ."
  echo "acceptance: git diff --check"
  echo
  echo "- [ ] This must not be planned while the project is pending"
} > "$R/state/roadmaps/registered.md"
rc="$(plan registered)"
if [ "$rc" = 6 ] && [ ! -f "$R/state/tasks/registered-001.md" ] \
   && [ ! -d "$R/state/.locks/planner-registered.lock" ]; then
  ok "a matching pending descriptor is refused without fixture task or lock residue"
else
  no "pending registered project fell through or left side effects (rc=$rc): $(cat "$TMP/err")"
fi

echo
[ "$fails" -eq 0 ] && { echo "plan-automerge: ALL PASS"; exit 0; }
echo "plan-automerge: $fails FAILED"; exit 1
