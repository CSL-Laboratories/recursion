#!/usr/bin/env bash
# Positive architecture audit for Phase 8 observed project pilots.
set -euo pipefail
export LC_ALL=C
ROOT="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$ROOT"

bash -n scripts/conduct-hermes.sh scripts/hermes-actuator.sh scripts/hermes-decide.sh \
  scripts/assist.sh scripts/taskctl.sh scripts/work.sh scripts/notify.sh scripts/digest.sh \
  scripts/cycle.sh scripts/provider-network.sh scripts/worker-sandbox.sh loop/supervisor/run.sh \
  scripts/resync.sh scripts/tests/resync.test.sh \
  scripts/sample-conformance-gate.sh scripts/sample-conformance-gate-v2.sh \
  scripts/tests/gate-collation.test.sh \
  scripts/tests/sample-conformance-reporter.test.sh \
  scripts/tests/hermes-pilot-phase8-red.test.sh scripts/tests/provider-network-root.test.sh
python3 -m py_compile scripts/project-pilot.py scripts/project_authority.py scripts/pilot_instruction.py scripts/instruction_routing.py scripts/project-reconcile.py scripts/decision-queue.py \
  scripts/seed_contract.py scripts/tests/seed-contract.test.py \
  scripts/notification-outbox.py scripts/jsonl_durable.py scripts/transition_receipt.py scripts/validate-decision.py scripts/validate-control-state.py \
  scripts/validate-gate.py \
  scripts/runtime-state.py scripts/tests/hermes-pilot-phase8-red.py scripts/tests/hermes-phase8-routing-recovery-red.py \
  scripts/hermes-gateway-planned-stop.py scripts/tests/hermes-gateway-shutdown.test.py \
  scripts/tests/hermes-gateway-shutdown-systemd.test.py \
  scripts/tests/hermes-pilot-phase8-parallel.py \
  scripts/tests/hermes-pilot-phase8-legacy-journal-red.py \
  scripts/tests/hermes-pilot-phase8-race-red.py \
  scripts/tests/hermes-pilot-phase8-reject-red.py \
  scripts/tests/phase8-windows-harness.test.py \
  scripts/tests/hermes-pilot-phase8-notification-red.py scripts/tests/jsonl-durability.test.py \
  scripts/tests/decision-identity.test.py scripts/tests/runtime-state-locks.test.py \
  scripts/tests/runtime-state-recovery.test.py scripts/tests/reconcile-liveness.test.py \
  scripts/tests/transition-receipt.test.py scripts/tests/notification-transition-receipt.test.py \
  scripts/tests/phase8-notification-authentication.test.py \
  scripts/tests/pilot-instruction.test.py \
  scripts/tests/phase8-head-window-admission.test.py \
  scripts/tests/phase8-exact-instruction-turn.test.py \
  scripts/tests/project-pilot-status-fail-closed.test.py \
  scripts/provider-connect-proxy.py scripts/tests/provider-connect-proxy.test.py \
  scripts/sample-shade-tops-gate.py scripts/tests/sample-shade-tops-gate.test.py \
  scripts/tests/sample-profile-immutability.test.py \
  scripts/tests/sample-profile-promotion.test.py \
  scripts/tests/phase8-authority-availability.test.py \
  scripts/tests/validate-gate.test.py scripts/tests/allowed-path.test.py
python3 - <<'PY'
import json
json.load(open('schemas/control-plane/v1/schema.json',encoding='utf-8'))
PY
python3 scripts/tests/sample-profile-immutability.test.py
python3 scripts/tests/phase8-windows-harness.test.py
python3 scripts/tests/sample-profile-promotion.test.py
python3 scripts/tests/phase8-authority-availability.test.py
bash scripts/tests/resync.test.sh
python3 scripts/tests/hermes-gateway-shutdown.test.py
if [ "${RECURSION_ALLOW_SYSTEMD_SHUTDOWN_SKIP:-0}" = 1 ]; then
  echo "FAIL complete Phase 8 cannot skip the systemd shutdown contract" >&2
  exit 1
fi
python3 scripts/tests/hermes-gateway-shutdown-systemd.test.py
echo "ok    Phase 8 shell, Python, and schema artifacts parse"

for verb in pilot-plan-propose pilot-plan-approve pilot-window-open pilot-window-close; do
  grep -q "$verb" scripts/hermes-decide.sh
  grep -q "$verb" scripts/validate-decision.py
  grep -q "$verb" scripts/conduct-hermes.sh
done
grep -q 'canonical_digest(plan)' scripts/project-pilot.py
grep -q 'fresh pilot plan reuses an existing goal id' scripts/project-pilot.py
grep -q 'require_affirmative_command(body, verb' scripts/project-pilot.py
grep -q 'fail(f"Lucas {exc}")' scripts/project-pilot.py
grep -q 'instruction does not name the exact Phase 8 command and bounds' scripts/pilot_instruction.py
grep -q 'instruction is negated or ambiguous for the requested Phase 8 operation' scripts/pilot_instruction.py
echo "ok    fresh synthesis, schema bounds, collision refusal, and exact Lucas digest/window approval are mechanical"

grep -q 'project_required_non_goals' scripts/project-pilot.py
grep -q 'If the live project state contains required_gate_profile' scripts/hermes-decide.sh
grep -q 'repo_gate_contract' scripts/project-pilot.py
grep -q 'repository gate changed after proposal' scripts/project-pilot.py
grep -q 'recursion-project-gate-profile-selection/v1' scripts/project-pilot.py
grep -q 'no default or latest alias exists' scripts/project-pilot.py
grep -q 'gate_profile_promotions entry' scripts/hermes-decide.sh
grep -q 'gate_files are immutable grading code' scripts/hermes-decide.sh
grep -q -- '--network-policy none' scripts/work.sh
grep -q -- '--network-policy", "none"' scripts/project-reconcile.py
echo "ok    canonical project exclusions and immutable repo gates are rebound at every authority seam while gates stay offline"

grep -q 'build_pilot_brief' scripts/hermes-decide.sh
grep -q 'RECURSION_PILOT_MODEL' scripts/hermes-decide.sh
grep -q 'DECIDER_ACTOR="$route"' scripts/hermes-decide.sh
grep -q 'from instruction_routing import route_for' scripts/intake.sh
grep -q 'instruction_routing.py' scripts/hermes-decide.sh
grep -q '"head-object-command"' scripts/instruction_routing.py
grep -q 'PILOT_PLAN_INTENT' scripts/instruction_routing.py
echo "ok    event-invoked logical GPT pilots receive a scoped brief and shared boundary-safe authority routing"

grep -q 'project_window_open_terms' scripts/pilot_instruction.py
grep -q 'exact_head_window_decision' scripts/instruction_routing.py
grep -q 'instruction-reconciler)' scripts/conduct-hermes.sh
grep -q '"instruction-reconciler"' scripts/validate-control-state.py
grep -q '"instruction-reconciler"' schemas/control-plane/v1/schema.json
grep -q 'admission_rc' scripts/hermes-decide.sh
grep -q 'One failed action is NEVER this proof' scripts/hermes-decide.sh
grep -qF '^[A-Za-z0-9][A-Za-z0-9._/-]{0,80}$' scripts/hermes-decide.sh
echo "ok    exact authenticated project-aware window commands are shaped before model output while stale single failures and unsafe prose args stay non-authoritative"

grep -q 'a pilot window may be authorized only while PAUSE is observed' scripts/project-pilot.py
grep -q 'exact open pilot window authority is absent' scripts/project-reconcile.py
grep -q 'close-paused-windows' scripts/hermes-actuator.sh
grep -q 'check-work-authority' scripts/conduct-hermes.sh
grep -qF 'fresh_window_admission || exit $?' scripts/work.sh
grep -q 'check-work-authority' scripts/work.sh
echo "ok    fresh worker admission is remote-proven once while local heartbeats recheck revocation"

grep -q 'cannot enumerate status state' scripts/project-pilot.py
grep -q 'STATUS UNAVAILABLE' loop/hermes-skills/recursion-status/SKILL.md
grep -q 'restricted filesystem view' loop/hermes-skills/recursion-ops/SKILL.md
echo "ok    an unprivileged gateway cannot translate unreadable root-owned state into false absence"

# The machinery admitting six verbs under PAUSE is worthless if the prompt tells the model only
# one of them exists. It did, for four days: every decide run chose noop citing "no
# execution-capable verb is permitted", which is the RUNTIME/PAUSE block quoted back verbatim.
# A paused box must still be able to PROPOSE a plan -- that is the whole of gate 5.
# NOTE: there are two RUNTIME/PAUSE blocks. The first belongs to build_pilot_brief and is
# correct as written ("no worker may run"); this asserts only the Head Hermes one, anchored on
# text unique to it.
PAUSE_BLOCK="$(awk '/REMAIN EXECUTABLE while paused/{f=1} f{print} f&&/cat state\/PAUSE/{exit}' \
  scripts/hermes-decide.sh)"
[ -n "$PAUSE_BLOCK" ]
for verb in noop pilot-plan-propose pilot-plan-approve pilot-window-open pilot-window-close assist-answer; do
  printf '%s\n' "$PAUSE_BLOCK" | grep -q -- "$verb"
done
# Matched against the pre-fix bytes: the stale claim wrapped across two echo lines, so a grep
# spanning "the only executable verb" could never match and would have passed vacuously.
! grep -q "executable verb while paused is 'noop'" scripts/hermes-decide.sh
echo "ok    the paused Head Hermes prompt names every PAUSE-safe verb and claims no others"

# 'goal' is normalized into goal_id, which must be a canonical identifier. Every decision from
# 2026-08-07 to 2026-08-10 was discarded at normalize with "goal_id is not a canonical
# identifier" because the model wrote a purpose sentence there. The rejection is silent from the
# operator's side -- the run simply produces nothing -- so the constraint must be in the prompt.
grep -q 'goal.\+MUST BE AN IDENTIFIER, never a sentence' scripts/hermes-decide.sh
grep -qF '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$' scripts/hermes-decide.sh
echo "ok    the decision prompt states the goal identifier constraint that normalization enforces"

grep -q "ONLY THIS INSTRUCTION'S OWN DURABLE HISTORY COUNTS" scripts/hermes-decide.sh
grep -q 'instruction_noop_bind_evidence' scripts/conduct-hermes.sh
grep -qF 'project-pilot: instruction is not owned by this project pilot' scripts/conduct-hermes.sh
echo "ok    instruction-coupled noop requires exact same-instruction durable refusal evidence"

# The prompt is one double-quoted shell string, so anything the shell expands inside it is
# deleted from what the model reads. Backticks around two verb names silently emptied them:
# the model received "only Lucas can, on GitHub.  prepares a repo and  completes it". Guard the
# CLASS, not those two words -- the only substitution the prompt may contain is $(build_brief).
python3 - <<'PY'
import re, sys
lines = open('scripts/hermes-decide.sh', encoding='utf-8').read().split('\n')
start = next(i for i, l in enumerate(lines) if l.startswith('PROMPT="'))
end = next(i for i, l in enumerate(lines) if l.strip() == '$(build_brief)"')
body = lines[start:end + 1]
bad = [(start + i + 1, l) for i, l in enumerate(body) if '`' in l]
subs = [(start + i + 1, m) for i, l in enumerate(body) for m in re.findall(r'\$\([^)]*\)', l)]
unexpected = [(n, m) for n, m in subs if m != '$(build_brief)']
# Unescaped quotes around one word only delete the quotes. If they expose a shell separator,
# they end the assignment word and can turn the remaining prose into a command; bash -n accepts it.
stream = '\n'.join(body)[len('PROMPT="'):].rstrip()
in_q, split, i = True, [], 0
while i < len(stream):
    c = stream[i]
    if c == '\\':
        i += 2; continue
    if c == '"':
        in_q = not in_q
    elif not in_q and c in ' \t\n;&|()<>':
        split.append(start + 1 + stream.count('\n', 0, i))
    i += 1
split = sorted(set(split))
if bad or unexpected or split or in_q:
    for n, l in bad:
        print('FAIL backtick in prompt line %d: %s' % (n, l.strip()[:90]), file=sys.stderr)
    for n, m in unexpected:
        print('FAIL unexpected substitution in prompt line %d: %s' % (n, m), file=sys.stderr)
    for n in split:
        print('FAIL PROMPT assignment ends at unquoted whitespace, line %d: %s'
              % (n, lines[n - 1].strip()[:90]), file=sys.stderr)
    if in_q:
        print('FAIL PROMPT string is never closed', file=sys.stderr)
    raise SystemExit(1)
PY
echo "ok    the prompt string contains no shell expansion that would delete its own text"

# conduct-hermes.sh takes this same lock on fd 8 and the actuator runs it on a 2s poll, so a
# single non-blocking attempt loses roughly a third of all scheduled decision turns. Production
# measured the lock held for 18 of 60 half-second samples.
grep -qE '^flock -w "\$CONTROL_LOCK_WAIT_S" 9' scripts/hermes-decide.sh
! grep -qE '^flock -n 9' scripts/hermes-decide.sh
grep -q 'RECURSION_CONTROL_LOCK_WAIT_S' scripts/hermes-decide.sh
echo "ok    the decision turn waits for the shared control lock instead of failing on first contention"

# A systemd service inherits no HOME. Under `set -u` an unguarded $HOME is a fatal error on the
# FIRST line that mentions it, before any subcommand runs -- so the script is not "degraded", it
# is entirely dead, and only under systemd. intake.sh was dead this way for the whole life of
# hermes-actuator.service while every suite stayed green, because no test ran without HOME.
# Guard the class: branch-review.sh had already been fixed for this once, and resync.sh carried
# the same latent defect, which is the signature of something that comes back.
python3 - <<'PY'
import glob, re, sys
bad = []
# ONE glob: scripts/*.sh and scripts/**/*.sh overlap and reported every top-level script twice.
for path in sorted(set(glob.glob('scripts/**/*.sh', recursive=True))):
    text = open(path, encoding='utf-8', errors='replace').read()
    if not re.search(r'(?m)^set -[a-z]*u', text):
        continue
    uses = [n for n, line in enumerate(text.split('\n'), 1)
            if re.search(r'\$\{?HOME\b', line) and not line.lstrip().startswith('#')]
    if not uses:
        continue
    if not re.search(r'HOME="\$\{HOME:-', text):
        bad.append((path, uses[:3]))
for path, lines in bad:
    print('FAIL unguarded HOME under set -u: %s lines %s' % (path, lines), file=sys.stderr)
if bad:
    raise SystemExit(1)
PY
echo "ok    no set -u script dereferences HOME unguarded (systemd services have none)"

# THE PROMPT MUST TEACH WHAT THE VALIDATOR ENFORCES. Rehearsed against the live model: every
# rejected plan failed on a literal the prompt never stated. "complete Phase 6 budget" did not
# name the nine budget keys, so the model wrote prose. The prompt said "deadlines" and
# "independent verification" -- not the key names -- and the model copied them. Six fields are
# enum-constrained and the prompt listed no values, so the model guessed plausibly and wrongly
# every time (model_alias "claude", verifier_model_alias "codex", network_policy "offline-deny").
#
# Read the contract out of validate_plan's AST rather than spot-checking today's strings, so a
# field added to the validator fails this audit until the prompt teaches it.
python3 - <<'PY'
import ast, sys

src = open('scripts/project-pilot.py', encoding='utf-8').read()
fn = next(n for n in ast.walk(ast.parse(src))
          if isinstance(n, ast.FunctionDef) and n.name == 'validate_plan')

def strings(node):
    if isinstance(node, ast.Set):
        return {e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    return set()

required = set()
for node in ast.walk(fn):
    # required = {...} and required_budget = {...}
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in ('required', 'required_budget'):
                required |= strings(node.value)
    # task.get("field") != "value"   /   task.get("field") not in {"a", "b"}
    if isinstance(node, ast.Compare):
        left = node.left
        if (isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute)
                and left.func.attr == 'get'
                and isinstance(left.func.value, ast.Name) and left.func.value.id == 'task'
                and left.args and isinstance(left.args[0], ast.Constant)):
            required.add(left.args[0].value)
            for comparator in node.comparators:
                required |= strings(comparator)
        # "field" in task  (optional-key admission checks)
        if (isinstance(left, ast.Constant) and isinstance(left.value, str)
                and any(isinstance(op, ast.In) for op in node.ops)
                and any(isinstance(c, ast.Name) and c.id == 'task' for c in node.comparators)):
            required.add(left.value)
    # engine_contract helper shapes: pilot_engine_ok(task.get("k")) / alias_role_ok(task.get("k"), ...)
    if isinstance(node, ast.Call):
        func = node.func
        func_name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')
        if func_name in ('pilot_engine_ok', 'alias_role_ok') and node.args:
            first = node.args[0]
            if (isinstance(first, ast.Call) and isinstance(first.func, ast.Attribute)
                    and first.func.attr == 'get'
                    and isinstance(first.func.value, ast.Name) and first.func.value.id == 'task'
                    and first.args and isinstance(first.args[0], ast.Constant)):
                required.add(first.args[0].value)

# The enum values moved from validate_plan literals into engine_contract + the shipped alias
# registry, so seed them from those sources; an unreadable registry fails this audit closed.
import json
sys.path.insert(0, 'scripts')
from engine_contract import LEGACY_ALIASES, default_registry_path
required |= set(LEGACY_ALIASES) | {'claude'}
registry = json.load(open(default_registry_path('scripts'), encoding='utf-8'))
required |= set(registry.get('aliases') or {})

prompt = open('scripts/hermes-decide.sh', encoding='utf-8').read()
missing = sorted(term for term in required if term not in prompt)
if missing:
    print('FAIL the plan prompt never states %d enforced literal(s): %s'
          % (len(missing), ', '.join(missing)), file=sys.stderr)
    raise SystemExit(1)
print('    (%d enforced literals, all present in the prompt)' % len(required))
PY
echo "ok    the plan prompt states every key and enum value that validate_plan enforces"

# The validator requires non-empty prose in 'text' for every AUTHORING_TEXT_VERBS member, but the
# prompt's text rule named only one example verb. The model read "authoring" narrowly and omitted
# 'text' on pilot-plan-approve, 2/2, on 2026-08-12 -- a valid approval refused twice. The verb set
# is read from the validator's own AST; the check is anchored on the text-rule PARAGRAPH because
# most verb names already appear elsewhere in the prompt, which would make a whole-prompt scan
# pass vacuously against the broken bytes.
python3 - <<'PY8'
import ast, re, sys
tree = ast.parse(open('scripts/validate-decision.py', encoding='utf-8').read())
verbs = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == 'AUTHORING_TEXT_VERBS':
                for elt in ast.walk(node.value):
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        verbs.add(elt.value)
if not verbs:
    print('FAIL AUTHORING_TEXT_VERBS not found in validate-decision.py', file=sys.stderr)
    raise SystemExit(1)
lines = open('scripts/hermes-decide.sh', encoding='utf-8').read().splitlines()
starts = [i for i, l in enumerate(lines) if "is REQUIRED and must be NON-EMPTY" in l]
if len(starts) != 1:
    print('FAIL expected exactly one text-rule paragraph, found %d' % len(starts), file=sys.stderr)
    raise SystemExit(1)
i = starts[0]; para = []
while i < len(lines) and lines[i].strip():
    para.append(lines[i]); i += 1
tokens = set(re.findall(r'[A-Za-z0-9][A-Za-z0-9-]*', ' '.join(para)))
missing = sorted(v for v in verbs if v not in tokens)
if missing:
    print('FAIL the text rule paragraph never names %d text-requiring verb(s): %s'
          % (len(missing), ', '.join(missing)), file=sys.stderr)
    raise SystemExit(1)
print('    (%d authoring verbs, all named in the text-rule paragraph)' % len(verbs))
PY8
echo "ok    the prompt text rule names every verb the validator holds to it"

# A caller-supplied RECURSION_ROOT must survive .env. These scripts compute ROOT from
# ${RECURSION_ROOT:-<script dir>} and THEN source .env with `set -a`; production's .env pins
# RECURSION_ROOT=/opt/recursion, so from that line on the shell's ROOT and the exported
# RECURSION_ROOT disagree, and every python child reads the environment. On 2026-08-12 that made
# rehearsals run against a copy enqueue TEN decisions into production, with nothing to warn:
# the parent's own file writes went to the copy, so the split was invisible until the live queue
# was inspected. ROOT and RECURSION_ROOT must agree after .env is sourced.
python3 - <<'PY'
import glob, re, sys
# Assembled, not spelled: a checker that writes out its own pattern matches itself.
ENV_SRC = re.compile(r'^\[ -f \.env \]', re.M)
ROOT_FROM_ENV = '{RECURSION_ROOT:-'
REASSERT = 'RECURSION_ROOT="' + '$ROOT"; export RECURSION_ROOT'
bad = []
for path in sorted(set(glob.glob('scripts/**/*.sh', recursive=True))):
    text = open(path, encoding='utf-8', errors='replace').read()
    m = ENV_SRC.search(text)
    if not m or ROOT_FROM_ENV not in text:
        continue
    tail = text[m.end():]
    if REASSERT not in tail:
        bad.append(path)
for path in bad:
    print('FAIL %s sources .env without re-asserting the caller root after it' % path,
          file=sys.stderr)
if bad:
    raise SystemExit(1)
print('    (every .env-sourcing script re-asserts the caller root)')
PY
echo "ok    sourcing .env cannot retarget a caller-supplied root"

grep -q 'claim-pending --kind work' scripts/conduct-hermes.sh
grep -q 'RECURSION_PILOT_GLOBAL_CONCURRENCY' scripts/conduct-hermes.sh
grep -q -- '--exclude-project' scripts/conduct-hermes.sh
grep -q 'concurrency_limit' scripts/project-reconcile.py
grep -q 'max_inflight_workers' scripts/project-reconcile.py
echo "ok    the sole actuator enforces global and per-project worker capacity without a second executor"

grep -q 'resume-assist' scripts/assist.sh
grep -q 'resume_assist' scripts/project-reconcile.py
grep -q 'resume_event_id' scripts/project-reconcile.py
grep -q 'DURABLE_REPORTING' scripts/assist.sh
grep -q 'PILOT_ID' scripts/work.sh
grep -q 'assist-open' scripts/notification-outbox.py
grep -q 'question_snippet' scripts/assist.sh
grep -q 'assist-answer' scripts/notification-outbox.py
echo "ok    one answered assist reopens the same goal/task and all pilot notices use the Phase 7 outbox"

for state_path in state/decisions.jsonl state/decision-queue state/goals state/tasks state/assists \
  state/plans state/pilot-windows state/jsonl-quarantine; do
  grep -qF "\"$state_path\"" scripts/runtime-state.py
done
for ignored in state/plans state/pilot-windows state/jsonl-quarantine state/jsonl-index; do grep -qF "$ignored/" .gitignore; done
echo "ok    plan/window/queue/goal/task/assist state is backup-covered and runtime projections stay untracked"

grep -q 'read_jsonl_since' scripts/decision-queue.py
grep -q 'validate_projection_authority' scripts/decision-queue.py
grep -q 'INDEX_AUTHORITY = "jsonl-verification-index-v1"' scripts/jsonl_durable.py
echo "ok    decision/event journals use durable tail verification and claims rebind to journal authority"

for event in plan-proposed plan-approved pilot-window-armed pilot-window-opened pilot-window-closed \
  lucas-attention-required human-answer-received; do
  grep -q "$event" scripts/notification-outbox.py
done
grep -q 'source-plan-proposed-' scripts/project-pilot.py
grep -q 'source-assist-' scripts/project-reconcile.py
echo "ok    stable source-event identities feed exact-once Phase 7 delivery policy"

grep -q 'LEGACY_PRE_SCHEMA_NOOP_SHA256' scripts/decision-queue.py
grep -q 'reviewed-pre-schema-noop' scripts/decision-queue.py
grep -q 'decision journal import failed rc=' scripts/conduct-hermes.sh
grep -q 'PAUSE-safe queue pass failed rc=' scripts/hermes-actuator.sh
echo "ok    exact reviewed legacy noops are non-executable and journal failures are observable"

grep -q 'PAUSE asserted; transport refused' scripts/notify.sh
grep -q 'notify-event portfolio-brief' loop/supervisor/run.sh
grep -q 'notify-event project-digest' scripts/digest.sh
! grep -q 'notify.sh' scripts/digest.sh
grep -q 'paused: skipping per-project notification enqueue' scripts/cycle.sh
grep -q 'state/.board.tsv state/events.jsonl' scripts/cycle.sh
for event in portfolio-brief project-digest; do grep -q "$event" scripts/notification-outbox.py; done
echo "ok    PAUSE interlocks the sole sender and resumed brief/digest delivery enters the Phase 7 outbox"

bash scripts/tests/hermes-pilot-phase8-red.test.sh
bash scripts/tests/gate-collation.test.sh
SAMPLE_CONFORMANCE_GATE="$ROOT/scripts/sample-conformance-gate-v2.sh" \
  bash scripts/tests/gate-collation.test.sh
bash scripts/tests/sample-conformance-reporter.test.sh
python3 scripts/tests/seed-contract.test.py
python3 scripts/tests/hermes-pilot-phase8-notification-red.py
python3 scripts/tests/decision-identity.test.py
python3 scripts/tests/jsonl-durability.test.py
python3 scripts/tests/runtime-state-locks.test.py
python3 scripts/tests/runtime-state-recovery.test.py
python3 scripts/tests/reconcile-liveness.test.py
python3 scripts/tests/transition-receipt.test.py
python3 scripts/tests/notification-transition-receipt.test.py
python3 scripts/tests/phase8-notification-authentication.test.py
python3 scripts/tests/pilot-instruction.test.py
python3 scripts/tests/phase8-head-window-admission.test.py
python3 scripts/tests/provider-connect-proxy.test.py
python3 scripts/tests/sample-shade-tops-gate.test.py
python3 scripts/tests/validate-gate.test.py
python3 scripts/tests/allowed-path.test.py
echo "hermes-pilot-phase8-green: ALL PASS"
