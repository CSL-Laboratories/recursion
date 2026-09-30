#!/usr/bin/env bash
# worker-envelope.test.sh — the arm must stay inside its boundary, and the boundary must be
# enforced by code rather than by the prompt asking nicely.
#
# WHY. The envelope tells the arm its allowed paths, its non-goals, and to stop on ambiguity.
# A prompt is a request. These tests drive a STUB arm that deliberately misbehaves — writes a
# control-plane file, edits a test, wanders outside allowed_paths — and assert work.sh refuses
# the result. The acceptance gate structurally cannot catch any of it: the gate scores the
# OUTCOME, so a diff that quietly edits a fixture to reach that outcome still exits 0.
#
# ★ ORDER IS A PROPERTY UNDER TEST. The scope check must run BEFORE the acceptance gate. If it
# ran after, a tampered pass would already have been declared. Test 5 pins that by requiring the
# implementer run record to have no machine_gate entry.
#
# ★ The CONTROL (test 1) proves the gate is not simply refusing everything.
#
#   run: bash scripts/tests/worker-envelope.test.sh
set -uo pipefail
export LC_ALL=C

# This suite drives the live loop arms (flock, procfs, POSIX lock semantics) — off-box it can only
# produce noise, so it reports an explicit SKIP. The box baseline runs it for real.
if [ "$(uname -s)" != "Linux" ]; then
  echo "worker-envelope: SKIP (Linux loop runtime required — flock/procfs/POSIX lock semantics; certified in the box suite)"
  exit 0
fi

SRC="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
fails=0
ok(){ printf 'ok    %s\n' "$1"; }
no(){ printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"; mkdir -p "$BIN"

# stub arm: runs $STUB_ARM_SCRIPT inside the worktree (cwd), i.e. it writes whatever the case
# under test needs. Stands in for `claude -p` with no model call and no spend.
cat > "$BIN/claude" <<'STUB'
#!/usr/bin/env bash
[ -n "${STUB_ARM_SCRIPT:-}" ] && bash -c "$STUB_ARM_SCRIPT"
exit 0
STUB
chmod +x "$BIN/claude"

# disposable repo with a realistic shape (a protected tree and a product tree)
R="$TMP/repo"; mkdir -p "$R/scripts/tests" "$R/apps/example-ops" "$R/other" "$R/state/tasks" "$R/loop/logs"
cp "$SRC/scripts/work.sh" "$R/scripts/"
cp "$SRC/scripts/assignment-token.py" "$R/scripts/"
cp "$SRC/scripts/worker-sandbox.sh" "$R/scripts/"
cp "$SRC/scripts/phase5_safety.py" "$R/scripts/"
cp "$SRC/scripts/assignment_contract.py" "$R/scripts/"
chmod +x "$R/scripts/worker-sandbox.sh"
cp "$SRC/scripts/board.sh" "$R/scripts/"
cp "$SRC/scripts/notify.sh" "$R/scripts/"
cp "$SRC/scripts/taskctl.sh" "$R/scripts/"
cp "$SRC/scripts/jsonl_durable.py" "$R/scripts/"
cp "$SRC/scripts/transition_receipt.py" "$R/scripts/"
cp "$SRC/scripts/runtime-state.py" "$R/scripts/"
cp "$SRC/scripts/resync.sh" "$R/scripts/"
# resync's freshness gate shells the typed source-pin controller; without it (and its full
# import closure) every task blocks at CANNOT PROVE FRESH before the envelope is ever
# exercised. Hard cp: a silent skip here would reproduce exactly the failure this stages
# against.
# Everything below is also required by the reachable work/worker-sandbox closure: remote_evidence
# reaches assignment.py, the evidence validators reach run_contract.py, and provider-network.sh
# reaches provider-connect-proxy.py. These are hard copies deliberately; none is optional.
cp "$SRC/scripts/project-pilot.py" "$R/scripts/"
cp "$SRC/scripts/remote_evidence.py" "$R/scripts/"
cp "$SRC/scripts/assignment.py" "$R/scripts/"
cp "$SRC/scripts/assist.sh" "$R/scripts/"
cp "$SRC/scripts/capacity.sh" "$R/scripts/"
cp "$SRC/scripts/git-scope.py" "$R/scripts/"
cp "$SRC/scripts/plan.sh" "$R/scripts/"
cp "$SRC/scripts/project-reconcile.py" "$R/scripts/"
cp "$SRC/scripts/publish.sh" "$R/scripts/"
cp "$SRC/scripts/resolve-engine.py" "$R/scripts/"
cp "$SRC/scripts/resolve-model-alias.py" "$R/scripts/"
cp "$SRC/scripts/validate-assignment-result.py" "$R/scripts/"
cp "$SRC/scripts/validate-gate.py" "$R/scripts/"
cp "$SRC/scripts/validate-verifier-result.py" "$R/scripts/"
cp "$SRC/scripts/validate-worker-result.py" "$R/scripts/"
cp "$SRC/scripts/worker-evidence.py" "$R/scripts/"
cp "$SRC/scripts/provider-network.sh" "$R/scripts/"
cp "$SRC/scripts/provider-egress.py" "$R/scripts/"
cp "$SRC/scripts/provider-connect-proxy.py" "$R/scripts/"
cp "$SRC/scripts/provider_contract.py" "$R/scripts/"
cp "$SRC/scripts/run_contract.py" "$R/scripts/"
cp "$SRC/scripts/engine_contract.py" "$R/scripts/"
cp "$SRC/scripts/pilot_instruction.py" "$R/scripts/"
cp "$SRC/scripts/seed_contract.py" "$R/scripts/"
cp "$SRC/scripts/project_authority.py" "$R/scripts/"
printf 'seed\n' > "$R/apps/example-ops/seed.txt"
printf 'protected fixture\n' > "$R/scripts/tests/existing.test.cjs"
: > "$R/.env"
# The repo gets a real (local, bare) origin and a registry row, because work.sh will not build
# on a base whose freshness it cannot prove — see scripts/resync.sh. Giving the fixture a real
# upstream keeps these tests on the REAL dispatch path; setting RECURSION_REQUIRE_FRESH_BASE=0
# would have been one line shorter and would have quietly stopped testing what actually runs.
ORIGIN="$TMP/origin.git"
git -c init.defaultBranch=main init -q --bare "$ORIGIN"
( cd "$R" \
  && git -c init.defaultBranch=main init -q . \
  && git add -A >/dev/null 2>&1 \
  && git -c user.name=t -c user.email=t@t commit -qm init >/dev/null 2>&1 \
  && git branch -M main >/dev/null 2>&1 \
  && git remote add origin "$ORIGIN" \
  && git push -q origin main >/dev/null 2>&1 \
  && git fetch -q origin >/dev/null 2>&1 ) || true
printf '#project\tpath\tremote\tbranch\tsshkey\troadmap\ttopic\tstatus\n' > "$R/state/portfolio.tsv"
printf 'wtest\t%s\t%s\tmain\t-\t-\t-\tactive\n' "$R" "$ORIGIN" >> "$R/state/portfolio.tsv"

export RECURSION_MODEL_ALIAS_SONNET_ROUTINE=stub-sonnet
export RECURSION_MODEL_ALIAS_OPUS_REVIEW=stub-opus

mktask(){ # id allowed_paths acceptance [evidence_contract]
  local strict_fields=""
  if [ "${4:-}" = worker-result-v1 ]; then
    strict_fields='schema_version: 1
project_id: wtest
goal_id: wtest-goal
worker_class: claude
model_alias: sonnet-routine
verifier_model_alias: opus-review
deadline_seconds: 1
verifier_deadline_seconds: 2
verification: independent
expected_change: optional
evidence_contract: worker-result-v1'
  fi
  cat > "$R/state/tasks/$1.md" <<T
${strict_fields}
id: $1
workstream: wtest
goal: a bounded change
engine: claude
network_policy: none
repo: $R
acceptance: $3
workdir: .
allowed_paths: $2
autonomy: auto
status: queued

body
T
}

run_case(){ # id script
  ( cd "$R" && PATH="$BIN:$PATH" RECURSION_ROOT="$R" RECURSION_NO_NOTIFY=1 \
      RECURSION_GIT_FETCH=0 RECURSION_TEST_WORKER=logical STUB_ARM_SCRIPT="$2" \
      bash scripts/work.sh "$1" ) >"$TMP/out-$1" 2>&1
}
status_of(){ sed -n 's/^status: *//p' "$R/state/tasks/$1.md" | head -1 | tr -d '\r'; }
inbox_tail(){ tail -25 "$R/state/INBOX.md" 2>/dev/null; }
machine_gate_evidence(){
  python3 - "$R/state/runs" "$1" <<'PY'
import glob
import json
import sys

primary = None
for path in sorted(glob.glob(sys.argv[1] + "/" + sys.argv[2] + "-impl-*.json")):
    if path.endswith(("-result.json", "-scope.json")):
        continue
    try:
        with open(path, encoding="utf-8") as stream:
            record = json.load(stream)
    except (OSError, json.JSONDecodeError):
        continue
    if isinstance(record, dict) and record.get("role") == "implementer":
        primary = record
        break
if primary is None:
    print("missing")
elif "machine_gate" in primary:
    print("recorded")
else:
    print("absent")
PY
}

# --- 1) ★ CONTROL: an in-scope change with a green gate must PASS ---------------------------
mktask t-ok "apps/example-ops" "test -s apps/example-ops/new.txt"
run_case t-ok 'mkdir -p apps/example-ops && echo hello > apps/example-ops/new.txt'
if [ "$(status_of t-ok)" = "passed" ]; then
  ok "★ CONTROL — an in-scope change with a green gate PASSES (the gate is not refusing everything)"
else
  no "★ CONTROL FAILED — an in-scope change was rejected (status=$(status_of t-ok)); every result below is meaningless"
  sed 's/^/        /' "$TMP/out-t-ok" | tail -6
fi

# --- 2) writing a CONTROL-PLANE file is refused --------------------------------------------
mktask t-cp "apps/example-ops" "test -s apps/example-ops/a.txt"
run_case t-cp 'mkdir -p apps/example-ops && echo x > apps/example-ops/a.txt && echo "evil" > scripts/pwn.sh'
if [ "$(status_of t-cp)" != "passed" ] && inbox_tail | grep -q 'SCOPE VIOLATION'; then
  ok "control-plane write (scripts/) is REFUSED and escalated"
else
  no "control-plane write was accepted (status=$(status_of t-cp))"
fi

# --- 3) editing a TEST file is refused (the gate cannot see this) ---------------------------
mktask t-test "apps/example-ops" "test -s apps/example-ops/b.txt"
run_case t-test 'mkdir -p apps/example-ops && echo x > apps/example-ops/b.txt && echo "assert(true)" >> scripts/tests/existing.test.cjs'
if [ "$(status_of t-test)" != "passed" ]; then
  ok "editing a *.test.cjs is REFUSED (tamper path the acceptance gate cannot detect)"
else
  no "a test-file edit reached 'passed'"
fi

# --- 4) wandering outside allowed_paths is refused ------------------------------------------
mktask t-out "apps/example-ops" "test -s apps/example-ops/c.txt"
run_case t-out 'mkdir -p apps/example-ops other && echo x > apps/example-ops/c.txt && echo y > other/stray.txt'
if [ "$(status_of t-out)" != "passed" ] && inbox_tail | grep -qF 'other/stray.txt'; then
  ok "a write outside allowed_paths is REFUSED and names the offending path"
else
  no "out-of-scope write refusal/path assertion failed (status=$(status_of t-out)); relevant INBOX tail follows"
  inbox_tail | sed 's/^/        /'
fi

# --- 5) ★ ORDER: the scope check runs BEFORE the acceptance gate ----------------------------
# The predicate is valid and side-effect-free. On a scope violation it must never be recorded.
mktask t-order "apps/example-ops" "test -s apps/example-ops/d.txt" worker-result-v1
run_case t-order 'mkdir -p apps/example-ops && echo x > apps/example-ops/d.txt && echo evil > scripts/pwn2.sh'
order_gate_evidence="$(machine_gate_evidence t-order)"
if [ "$order_gate_evidence" = absent ] && [ "$(status_of t-order)" != "passed" ]; then
  ok "★ the scope check runs BEFORE the gate (implementer record has no machine_gate entry)"
elif [ "$order_gate_evidence" = missing ]; then
  no "★ scope-order proof is missing the primary implementer run record (gate non-execution is unproven)"
else
  no "★ the gate ran or scope refusal failed (status=$(status_of t-order) gate_evidence=$order_gate_evidence)"
fi

# --- 6) a `blocked` result escalates the arm's QUESTION and skips the gate -------------------
# A blocked result short-circuits before the gate: work.sh escalates the arm's own question and
# never runs the acceptance command. Two deliberate choices here:
#  - The gate is a valid non-vacuous predicate (apps/example-ops/seed.txt is never created in scope),
#    so it passes validate-gate.py's anti-vacuity check but WOULD FAIL if it ran.
#  - The task is intentionally NOT under the worker-result-v1 strict contract. A minimal blocked
#    result does not satisfy that schema, so requiring it would make the arm escalate on a
#    RESULT CONTRACT VIOLATION instead of exercising blocked handling at all.
# Because a completed gate reads `passed` or `failed`, a `blocked` status is itself proof the
# gate was skipped -- no separate run-record witness is needed for this case (unlike t-order,
# where status and the escalation reason cannot distinguish scope-before-gate from
# scope-after-gate, so the machine_gate witness is required there).
mktask t-blocked "apps/example-ops" "test -s apps/example-ops/seed.txt"
run_case t-blocked 'printf "{\"status\":\"blocked\",\"summary\":\"two readings\",\"blocker\":\"Which realm is the target?\"}" > .recursion-result.json'
if [ "$(status_of t-blocked)" = "blocked" ] && inbox_tail | grep -q 'Which realm is the target'; then
  ok "an ambiguity STOP is recorded as blocked and carries the arm's own question (blocked, not failed, so the gate was skipped)"
else
  no "a blocked result was not handled (status=$(status_of t-blocked))"
fi

# --- 7) the envelope actually reaches the arm -----------------------------------------------
# Capture the prompt the arm receives and assert the boundary is stated in it. A gate that
# enforces silently, with a prompt that never mentions the rule, teaches the arm nothing.
cat > "$BIN/claude" <<'STUB'
#!/usr/bin/env bash
prev=""
for a in "$@"; do
  [ "$prev" = "-p" ] && { printf '%s' "$a" > "$STUB_PROMPT_OUT"; break; }
  prev="$a"
done
exit 0
STUB
chmod +x "$BIN/claude"
mktask t-prompt "apps/example-ops" "test -s apps/example-ops/seed.txt"
export STUB_PROMPT_OUT="$TMP/prompt.txt"
( cd "$R" && PATH="$BIN:$PATH" RECURSION_ROOT="$R" RECURSION_NO_NOTIFY=1 RECURSION_GIT_FETCH=0 \
    RECURSION_TEST_WORKER=logical STUB_PROMPT_OUT="$TMP/prompt.txt" bash scripts/work.sh t-prompt ) >/dev/null 2>&1
miss=""
for phrase in "bounded executor" "ALLOWED PATHS" "PROTECTED" "ON AMBIGUITY" "recursion-result.json" "Hermes owns planning"; do
  grep -qF "$phrase" "$TMP/prompt.txt" 2>/dev/null || miss="$miss '$phrase'"
done
[ -z "$miss" ] && ok "the envelope reaches the arm (authority, paths, protections, stop rule, schema)" \
               || no "the arm prompt is missing:$miss"

echo
[ "$fails" -eq 0 ] && { echo "worker-envelope: ALL PASS"; exit 0; }
echo "worker-envelope: $fails FAILED"; exit 1
