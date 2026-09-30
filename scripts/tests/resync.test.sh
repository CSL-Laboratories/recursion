#!/usr/bin/env bash
# resync.test.sh — freshness must be PROVEN, and "I could not check" must never read as "fine".
#
# WHY THESE TESTS. Two failures on this box motivated the whole branch, and both are the same
# shape: an operation that could not answer, answering "clean" anyway.
#   - `git ls-remote` with no usable key prints nothing and exits 0-ish; the caller reads that as
#     "this repo has no branches". Test 6 is that exact case and it must come back as a FAULT.
#   - work.sh fetched, failed, logged "building on local base", and built anyway — so the arm
#     built on a stale tree, the gate passed (it was testing the old tree), and merging the
#     result would revert whatever had been fixed upstream. Test 11 is that case.
#
# ★ Tests 1, 3c and 10 are CONTROLS. Without them every refusal here would also pass on a
#   resync.sh that refuses everything and a work.sh that builds nothing — which is the way this
#   suite would be worthless while scoring full marks.
#
# Hermetic: local bare repos as origins (file:// needs no credential), a temp registry, no
# network, no keys, no spend.
#
#   run: bash scripts/tests/resync.test.sh
set -uo pipefail
export LC_ALL=C

# This suite drives the live loop arms (flock, procfs, POSIX lock semantics) — off-box it can only
# produce noise, so it reports an explicit SKIP. The box baseline runs it for real.
if [ "$(uname -s)" != "Linux" ]; then
  echo "resync: SKIP (Linux loop runtime required — flock/procfs/POSIX lock semantics; certified in the box suite)"
  exit 0
fi

SRC="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
fails=0
ok(){ printf 'ok    %s\n' "$1"; }
no(){ printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }

command -v git >/dev/null 2>&1 || { echo "resync.test: git is required"; exit 3; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
R="$TMP/root"; mkdir -p "$R/scripts/tests" "$R/state/tasks" "$R/state/roadmaps" \
  "$R/state/pilot-windows" "$R/loop/logs" "$TMP/origins"
for s in assignment.py assignment_contract.py assist.sh author.sh board.sh capacity.sh conduct.sh digest.sh engine_contract.py git-scope.py \
         instruction_routing.py intake.sh jsonl_durable.py notify.sh phase5_safety.py pilot_instruction.py plan.sh project-pilot.py \
         project-reconcile.py project_authority.py provider-connect-proxy.py provider_contract.py provider-network.sh provider-egress.py publish.sh \
         remote_evidence.py resolve-engine.py resolve-model-alias.py resync.sh run_contract.py runtime-state.py \
         seed_contract.py taskctl.sh transition_receipt.py validate-assignment-result.py validate-control-state.py \
         validate-decision.py validate-gate.py validate-verifier-result.py validate-worker-result.py work.sh assignment-token.py \
         worker-evidence.py worker-sandbox.sh; do
  cp "$SRC/scripts/$s" "$R/scripts/"
done
chmod +x "$R"/scripts/*.sh 2>/dev/null
: > "$R/.env"; : > "$R/state/INBOX.md"; echo "anchor" > "$R/ANCHOR.md"
# A deliberately tiny controller stub keeps this suite focused on resync's interpretation of
# an already validated source pin.  The real controller and its Git ancestry proof are covered
# by a sample profile-promotion gate; malformed stub output still has to fail closed here.
cat > "$R/scripts/project-pilot.py" <<'PY'
#!/usr/bin/env python3
import json, pathlib, sys
if len(sys.argv) != 3 or sys.argv[1] != "active-source-pin":
    raise SystemExit(2)
project = sys.argv[2]
root = pathlib.Path(__file__).parents[1]
with (root / "state/controller-calls").open("a", encoding="utf-8") as calls:
    calls.write(project + "\n")
path = root / "state/test-source-pins" / project
source = path.read_text(encoding="utf-8").strip() if path.is_file() else "-"
if source == "!fail":
    print("project-pilot: bounded controller failure", file=sys.stderr)
    raise SystemExit(74)
if source == "!invalid":
    print("project-pilot: proved source-pin contradiction", file=sys.stderr)
    raise SystemExit(4)
if source == "!malformed":
    print("{malformed")
    raise SystemExit(0)
print(json.dumps({
    "schema": "recursion-project-source-pin/v1",
    "project_id": project,
    "source_head": source,
}, sort_keys=True, separators=(",", ":")))
PY
chmod +x "$R/scripts/project-pilot.py"
printf '{}\n' > "$R/state/pilot-windows/controller-trigger.json"
export RECURSION_NO_NOTIFY=1
unset RECURSION_SOURCE_LOCK_WAIT_SECONDS
GIT="git -c user.name=t -c user.email=t@t -c init.defaultBranch=main -c advice.detachedHead=false"

# --- fixtures: <name> gets an origin bare repo and a clone --------------------------------
mkrepo(){ # mkrepo <name>
  local n="$1" o="$TMP/origins/$n.git" c="$TMP/$n"
  $GIT init -q --bare "$o"
  $GIT init -q "$c"; ( cd "$c" && echo one > f.txt && $GIT add -A && $GIT commit -qm one \
    && $GIT branch -M main && $GIT remote add origin "$o" && $GIT push -q origin main \
    && $GIT branch --set-upstream-to=origin/main main ) >/dev/null 2>&1
  $GIT -C "$c" fetch -q origin
}
# advance the ORIGIN without the clone knowing (the normal "someone merged a PR" case)
advance_origin(){ # advance_origin <name> <msg>
  local n="$1" w="$TMP/.adv-$n"; rm -rf "$w"
  $GIT clone -q "$TMP/origins/$n.git" "$w" >/dev/null 2>&1
  ( cd "$w" && echo "$2" >> f.txt && $GIT add -A && $GIT commit -qm "$2" && $GIT push -q origin main ) >/dev/null 2>&1
  rm -rf "$w"
}
for n in insync behind lockahead pinned armrace badpin pinmoved pinfail pinmalformed diverged unreachable emptyremote; do mkrepo "$n"; done

# 'emptyremote' authenticates perfectly and simply answers with no heads. This is the trap:
# it is indistinguishable from a successful probe of a repo that has branches, unless someone
# decides in advance that zero refs means the probe failed.
$GIT init -q --bare "$TMP/origins/empty.git"
$GIT -C "$TMP/emptyremote" remote set-url origin "$TMP/origins/empty.git"
$GIT -C "$TMP/unreachable" remote set-url origin "$TMP/origins/does-not-exist.git"

reg(){ printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$TMP/$1" "file://$TMP/origins/$1.git" main - - - "$2"; }
{ printf '#project\tpath\tremote\tbranch\tsshkey\troadmap\ttopic\tstatus\n'
  reg insync active; reg behind active; reg lockahead active; reg pinned active; reg armrace active; reg badpin active; reg pinmoved active
  reg pinfail active; reg pinmalformed active; reg diverged active
  reg unreachable active; reg emptyremote active
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' notyet "$TMP/notyet" "file://$TMP/origins/notyet.git" main - - - pending
} > "$R/state/portfolio.tsv"
export RECURSION_PORTFOLIO="$R/state/portfolio.tsv"

rs(){ ( cd "$R" && RECURSION_ROOT="$R" \
    RECURSION_SOURCE_LOCK_WAIT_SECONDS="${RECURSION_SOURCE_LOCK_WAIT_SECONDS:-30}" \
    bash scripts/resync.sh "$@" ) >"$TMP/out" 2>&1; echo $?; }

# --- 1) ★ CONTROL: a repo that IS current reports fresh -------------------------------------
# The refusal text ("CANNOT PROVE FRESH") contains the word 'fresh', so every assertion about
# freshness matches the SUCCESS line's shape ("<project> fresh: origin/<branch> @ <sha>") rather
# than the bare word — an assertion that both outcomes satisfy is not an assertion.
rc=$(rs check insync)
if [ "$rc" = "0" ] && grep -q 'fresh: origin/' "$TMP/out"; then
  ok "★ CONTROL — an in-sync repo checks fresh (resync is not just refusing everything)"
else
  no "★ CONTROL FAILED — an in-sync repo did not check fresh (rc=$rc): $(cat "$TMP/out")"
fi

# --- 2) the registry is the single source of truth for repo -> deploy key -------------------
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' keyed "$TMP/insync" x main "$TMP/fake.key" - - active >> "$R/state/portfolio.tsv"
rc=$(rs key keyed)
[ "$rc" = "0" ] && grep -q 'fake.key' "$TMP/out" && ok "key: the registry answers repo -> deploy key" \
  || no "key: registry lookup failed (rc=$rc): $(cat "$TMP/out")"
rc=$(rs key nosuchproject)
[ "$rc" != "0" ] && ok "key: an unregistered repo has no key (and says so)" || no "key: invented a key for an unknown repo"

# --- 3) BEHIND: visible, non-blocking, and fast-forwardable ---------------------------------
advance_origin behind two
rc=$(rs check behind)
[ "$rc" = "0" ] && ok "a repo merely BEHIND still checks fresh (the build bases on origin/main)" \
  || no "a fast-forwardable repo was treated as unfit (rc=$rc)"
rc=$(rs status)
grep -q 'BEHIND' "$TMP/out" && ok "status names the drift instead of hiding it" || no "status did not report BEHIND"

# 3b) observe must fetch/report the real tip without moving a local branch or checked-out tree.
before="$($GIT -C "$TMP/behind" rev-parse main)"
tree_before="$(sha256sum "$TMP/behind/f.txt" | awk '{print $1}')"
heads_before="$($GIT -C "$TMP/behind" for-each-ref --format='%(refname) %(objectname)' refs/heads | sort)"
remote_tip="$($GIT --git-dir="$TMP/origins/behind.git" rev-parse refs/heads/main)"
rc=$(rs observe behind)
after="$($GIT -C "$TMP/behind" rev-parse main)"
tree_after="$(sha256sum "$TMP/behind/f.txt" | awk '{print $1}')"
heads_after="$($GIT -C "$TMP/behind" for-each-ref --format='%(refname) %(objectname)' refs/heads | sort)"
fetched="$($GIT -C "$TMP/behind" rev-parse origin/main)"
if [ "$rc" = "0" ] && [ "$before" = "$after" ] && [ "$tree_before" = "$tree_after" ] \
    && [ "$heads_before" = "$heads_after" ] && [ "$fetched" = "$remote_tip" ] \
    && grep -q 'BEHIND.*observed; PAUSE forbids fast-forward' "$TMP/out"; then
  ok "observe fetches/reports BEHIND without moving any local branch or the checked-out tree"
else
  no "observe mutated local state or hid drift (rc=$rc main=$before->$after tree=$tree_before->$tree_after fetched=$fetched remote=$remote_tip)"
fi

# 3c) ★ CONTROL: sync must actually fast-forward, or 'refuses to sync' below proves nothing.
before="$($GIT -C "$TMP/behind" rev-parse main)"
rc=$(rs sync behind)
after="$($GIT -C "$TMP/behind" rev-parse main)"
if [ "$before" != "$after" ] && [ "$($GIT -C "$TMP/behind" rev-parse main)" = "$($GIT -C "$TMP/behind" rev-parse origin/main)" ]; then
  ok "★ CONTROL — sync fast-forwards a behind repo onto origin/main"
else
  no "★ CONTROL FAILED — sync did not fast-forward (before=$before after=$after)"
fi

# 3d) An authenticated promoted source pin is an explicit exception to portfolio fast-forward.
# The remote is still fetched/measured, but the exact source branch and checkout must not move.
advance_origin pinned remote-ahead
mkdir -p "$R/state/test-source-pins"
pinned_head="$($GIT -C "$TMP/pinned" rev-parse main)"
printf '%s\n' "$pinned_head" > "$R/state/test-source-pins/pinned"
rc=$(rs sync pinned)
after="$($GIT -C "$TMP/pinned" rev-parse main)"
if [ "$rc" = "0" ] && [ "$after" = "$pinned_head" ] \
    && [ "$($GIT -C "$TMP/pinned" rev-parse origin/main)" != "$pinned_head" ] \
    && grep -q 'PINNED.*exact active pilot source' "$TMP/out"; then
  ok "an active promoted source pin measures remote drift without moving the exact source"
else
  no "promoted source pin did not preserve the exact source (rc=$rc before=$pinned_head after=$after)"
fi

rc=$(rs check pinned)
if [ "$rc" = "75" ] && grep -q 'exact active pilot source' "$TMP/out"; then
  ok "ordinary dispatch defers while a repository is held at an active pilot source"
else
  no "a non-pilot dispatch check did not preserve the active source-pin hold (rc=$rc)"
fi

digest_out="$(cd "$R" && RECURSION_ROOT="$R" bash scripts/digest.sh pinned --print 2>&1)"
if grep -q 'repo: .*PINNED.*exact active pilot source' <<<"$digest_out" \
    && ! grep -q 'NEEDS YOU.*exact active pilot source' <<<"$digest_out"; then
  ok "a healthy active source pin renders as intentional hold instead of an operator alarm"
else
  no "project digest misclassified an intentional active source pin: $digest_out"
fi

# 3e) Window arming and portfolio fast-forward share one lock. Hold that lock until resync has
# fetched the authenticated descendant, materialize the pin while it is waiting, then release.
# A pre-lock implementation races through the fast-forward and fails the unchanged-source check.
advance_origin armrace remote-ahead
armrace_source="$($GIT -C "$TMP/armrace" rev-parse main)"
armrace_remote="$($GIT --git-dir="$TMP/origins/armrace.git" rev-parse refs/heads/main)"
mkdir -p "$R/state/.locks"
exec {armrace_lock_fd}>"$R/state/.locks/phase8-window-armrace.lock"
flock -x "$armrace_lock_fd"
(
  cd "$R" || exit 1
  RECURSION_ROOT="$R" bash scripts/resync.sh sync armrace >"$TMP/armrace.out" 2>&1
  printf '%s\n' "$?" >"$TMP/armrace.rc"
) &
armrace_pid=$!
armrace_fetched=0
for _ in $(seq 1 500); do
  observed="$($GIT -C "$TMP/armrace" rev-parse --verify origin/main 2>/dev/null || true)"
  if [ "$observed" = "$armrace_remote" ] && kill -0 "$armrace_pid" 2>/dev/null; then
    armrace_fetched=1
    break
  fi
  kill -0 "$armrace_pid" 2>/dev/null || break
  sleep 0.01
done
printf '%s\n' "$armrace_source" > "$R/state/test-source-pins/armrace"
flock -u "$armrace_lock_fd"
exec {armrace_lock_fd}>&-
wait "$armrace_pid" 2>/dev/null || true
armrace_rc="$(cat "$TMP/armrace.rc" 2>/dev/null || printf 'missing')"
armrace_after="$($GIT -C "$TMP/armrace" rev-parse main)"
if [ "$armrace_fetched" = 1 ] && [ "$armrace_rc" = 0 ] \
    && [ "$armrace_after" = "$armrace_source" ] \
    && grep -q 'PINNED.*exact active pilot source' "$TMP/armrace.out"; then
  ok "window arming wins the shared lock before resync can fast-forward its source"
else
  no "resync crossed a concurrent window arm (fetched=$armrace_fetched rc=$armrace_rc source=$armrace_source after=$armrace_after)"
fi

# A well-shaped but wrong pin is not permission to move either the checkout or its branch.
advance_origin badpin remote-ahead
badpin_head="$($GIT -C "$TMP/badpin" rev-parse main)"
printf '%040d\n' 0 > "$R/state/test-source-pins/badpin"
rc=$(rs sync badpin)
after="$($GIT -C "$TMP/badpin" rev-parse main)"
if [ "$rc" != "0" ] && [ "$after" = "$badpin_head" ] \
    && grep -q 'FAULT.*source pin differs' "$TMP/out"; then
  ok "a mismatched promoted source pin fails closed without moving the checkout"
else
  no "a mismatched promoted source pin was accepted or moved the checkout (rc=$rc)"
fi

# Drift remains visible after a branch has already reached the fetched remote (behind=0).
advance_origin pinmoved remote-ahead
$GIT -C "$TMP/pinmoved" fetch -q origin
pinmoved_source="$($GIT -C "$TMP/pinmoved" rev-parse main)"
printf '%s\n' "$pinmoved_source" > "$R/state/test-source-pins/pinmoved"
$GIT -C "$TMP/pinmoved" reset -q --hard origin/main
pinmoved_tip="$($GIT -C "$TMP/pinmoved" rev-parse main)"
rc=$(rs sync pinmoved)
if [ "$rc" != "0" ] && [ "$($GIT -C "$TMP/pinmoved" rev-parse main)" = "$pinmoved_tip" ] \
    && grep -q 'FAULT.*source pin differs' "$TMP/out"; then
  ok "source-pin drift fails closed even after the branch is no longer behind"
else
  no "source-pin drift was hidden when the branch was already at the remote tip (rc=$rc)"
fi

# Controller and transport failures retain bounded diagnostics and never fast-forward.
printf '%s\n' '!fail' > "$R/state/test-source-pins/pinfail"
rc=$(rs sync pinfail)
if [ "$rc" != "0" ] && grep -q 'bounded controller failure' "$TMP/out"; then
  ok "a source-pin controller failure is diagnostic and fail-closed"
else
  no "a source-pin controller failure lost its diagnostic or returned success (rc=$rc)"
fi
printf '%s\n' '!malformed' > "$R/state/test-source-pins/pinmalformed"
rc=$(rs sync pinmalformed)
if [ "$rc" != "0" ] && grep -q 'active source authority could not be proved' "$TMP/out"; then
  ok "malformed source-pin output fails closed"
else
  no "malformed source-pin output was accepted (rc=$rc)"
fi
printf '%s\n' '!invalid' > "$R/state/test-source-pins/pinfail"
rc=$(rs check pinfail)
if [ "$rc" = "4" ] && grep -q 'NEEDS A HUMAN.*proved source-pin contradiction' "$TMP/out"; then
  ok "a proved source-pin contradiction remains distinct from proof unavailability"
else
  no "a proved source-pin contradiction lost exit-4 classification (rc=$rc)"
fi

# --- 4) DIVERGED needs a human, and sync must not "fix" it by rewriting anything -------------
advance_origin diverged remote-side
( cd "$TMP/diverged" && echo local-side >> f.txt && $GIT add -A && $GIT commit -qm local-side ) >/dev/null 2>&1
$GIT -C "$TMP/diverged" fetch -q origin
localtip="$($GIT -C "$TMP/diverged" rev-parse main)"
rc=$(rs check diverged)
[ "$rc" = "4" ] && ok "a DIVERGED repo needs a human (exit 4, distinct from 'cannot prove')" \
  || no "diverged repo returned $rc, expected 4"
rc=$(rs sync diverged)
if [ "$($GIT -C "$TMP/diverged" rev-parse main)" = "$localtip" ] && [ "$rc" != "0" ]; then
  ok "sync REFUSES to reconcile divergence and leaves local history untouched"
else
  no "sync altered a diverged branch (tip moved) or reported success"
fi

# --- 5) an unreachable remote cannot prove anything -----------------------------------------
rc=$(rs check unreachable)
if [ "$rc" = "3" ] && ! grep -q 'fresh: origin/' "$TMP/out"; then
  ok "an unreachable origin is 'cannot prove' (exit 3) and is never called fresh"
else
  no "unreachable origin returned $rc: $(cat "$TMP/out")"
fi

# --- 6) ★ THE TRAP: a remote that answers with ZERO refs is a failed probe, not an empty repo -
rc=$(rs check emptyremote)
if [ "$rc" = "3" ]; then
  ok "★ zero refs from origin is a FAULT, not a clean 'no branches' (the ls-remote trap)"
else
  no "★ TRAP OPEN — a zero-ref probe returned $rc; an auth failure will read as a clean result"
fi

# --- 7) an unregistered repo is not a pass ---------------------------------------------------
rc=$(rs check "$TMP/insync-unregistered")
[ "$rc" = "3" ] && ok "an unregistered repo cannot be proven fresh (exit 3, not 0)" \
  || no "an unregistered repo returned $rc"

# --- 8) status=pending is registered but not dispatchable ------------------------------------
rc=$(rs check notyet)
[ "$rc" = "4" ] && ok "a pending (not yet cloned) repo is refused for dispatch" || no "pending repo returned $rc"

# --- 9) repeated faults are ONE escalation with a sighting count, not a new item each run -----
rs sync unreachable >/dev/null 2>&1; rs sync unreachable >/dev/null 2>&1
n="$( ( cd "$R" && RECURSION_ROOT="$R" bash scripts/taskctl.sh esc-list open 2>/dev/null ) | grep -c 'resync-auth-unreachable' )"
c="$( ( cd "$R" && RECURSION_ROOT="$R" bash scripts/taskctl.sh esc-list open 2>/dev/null ) | awk -F'\t' '/resync-auth-unreachable/{print $6; exit}' )"
if [ "$n" = "1" ] && [ "${c:-0}" -ge 2 ]; then
  ok "a fault seen twice is ONE escalation with count=$c (dedup holds)"
else
  no "escalation dedup broke: $n item(s), count=${c:-?}"
fi

# The source-pin controller is a hard dependency only when durable window records exist.
mv "$R/scripts/project-pilot.py" "$R/scripts/project-pilot.py.saved"
rc=$(rs check insync)
if [ "$rc" = "3" ] && grep -q 'source-pin controller is absent' "$TMP/out"; then
  ok "a window record requires an explicit source-pin controller"
else
  no "a missing source-pin controller was not diagnosed with a window present (rc=$rc)"
fi
rm -f "$R/state/pilot-windows/controller-trigger.json"
rc=$(rs check insync)
if [ "$rc" = "3" ] && grep -q 'source-pin controller is absent' "$TMP/out"; then
  ok "an absent controller cannot turn an unproved window inventory into no pin"
else
  no "an absent controller granted an unproved empty window inventory (rc=$rc)"
fi
mv "$R/scripts/project-pilot.py.saved" "$R/scripts/project-pilot.py"
mkdir -p "$R/state/test-source-pins"
printf '%s\n' '!fail' > "$R/state/test-source-pins/insync"
rc=$(rs check insync)
if [ "$rc" = "75" ] && grep -q '^DEFERRED: .*bounded controller failure' "$TMP/out"; then
  ok "an empty window inventory still delegates typed controller unavailability"
else
  no "empty inventory bypassed controller unavailability (rc=$rc)"
fi
printf '%s\n' '!invalid' > "$R/state/test-source-pins/insync"
rc=$(rs check insync)
if [ "$rc" = "4" ] && grep -q 'proved source-pin contradiction' "$TMP/out"; then
  ok "an empty window inventory still delegates typed controller invalidity"
else
  no "empty inventory bypassed controller invalidity (rc=$rc)"
fi
rm -f "$R/state/test-source-pins/insync"
calls_before="$(wc -l < "$R/state/controller-calls")"
rc=$(rs check insync)
calls_after="$(wc -l < "$R/state/controller-calls")"
if [ "$rc" = "0" ] && grep -q 'fresh: origin/' "$TMP/out" \
    && [ "$calls_after" -eq $((calls_before + 1)) ]; then
  ok "the typed controller proves a genuinely empty window inventory"
else
  no "the controller could not prove a genuinely empty window inventory (rc=$rc calls=$calls_before->$calls_after)"
fi

# A human window action and ordinary resync share one project lock. Exercise the actual mutating
# sync path against a remote-ahead source: contention may fetch observation refs, but it must not
# move the local branch or checkout, and the same sync must fast-forward after lock release.
advance_origin lockahead remote-ahead
lock_path="$R/state/.locks/phase8-window-lockahead.lock"
mkdir -p "$(dirname "$lock_path")"
exec {contention_fd}>"$lock_path"
flock -x "$contention_fd"
before_lock_head="$($GIT -C "$TMP/lockahead" rev-parse refs/heads/main)"
before_lock_heads="$($GIT -C "$TMP/lockahead" for-each-ref --format='%(refname) %(objectname)' refs/heads | sort)"
before_lock_tree="$(sha256sum "$TMP/lockahead/f.txt" | awk '{print $1}')"
rc=$(RECURSION_SOURCE_LOCK_WAIT_SECONDS=0 rs sync lockahead)
after_lock_head="$($GIT -C "$TMP/lockahead" rev-parse refs/heads/main)"
after_lock_heads="$($GIT -C "$TMP/lockahead" for-each-ref --format='%(refname) %(objectname)' refs/heads | sort)"
after_lock_tree="$(sha256sum "$TMP/lockahead/f.txt" | awk '{print $1}')"
flock -u "$contention_fd"
exec {contention_fd}>&-
if [ "$rc" = "1" ] && grep -q '^DEFERRED  lockahead .* exact source/window lock is contended$' "$TMP/out" \
    && [ "$before_lock_head" = "$after_lock_head" ] \
    && [ "$before_lock_heads" = "$after_lock_heads" ] \
    && [ "$before_lock_tree" = "$after_lock_tree" ]; then
  ok "shared source-window lock contention defers the mutating sync without moving the source"
else
  no "shared lock contention was not a state-preserving sync deferral (rc=$rc)"
fi
rc=$(RECURSION_SOURCE_LOCK_WAIT_SECONDS=0 rs sync lockahead)
if [ "$rc" = "0" ] && grep -q '^FF        lockahead ' "$TMP/out" \
    && [ "$($GIT -C "$TMP/lockahead" rev-parse main)" = "$($GIT -C "$TMP/lockahead" rev-parse origin/main)" ]; then
  ok "the mutating source sync fast-forwards after the shared window lock is released"
else
  no "source sync did not recover after shared lock release (rc=$rc)"
fi

mkdir -p "$TMP/flock-fault"
cat > "$TMP/flock-fault/flock" <<'SH'
#!/usr/bin/env bash
exit 69
SH
chmod +x "$TMP/flock-fault/flock"
rc=$( ( cd "$R" && PATH="$TMP/flock-fault:$PATH" RECURSION_ROOT="$R" \
    RECURSION_SOURCE_LOCK_WAIT_SECONDS=0 bash scripts/resync.sh check insync \
    >"$TMP/out" 2>&1 ); echo $? )
if [ "$rc" = "3" ] && grep -q '^CANNOT PROVE FRESH: exact source/window lock is unavailable$' "$TMP/out"; then
  ok "an operational flock failure is unavailable, never misreported as contention"
else
  no "an operational flock failure was not typed as unavailable (rc=$rc)"
fi
printf '{}\n' > "$R/state/pilot-windows/controller-trigger.json"

# ============================================================================================
# work.sh integration — the actual defect this branch closes.
# ============================================================================================
mktask(){ # mktask <id> <repo-path>
  cat > "$R/state/tasks/$1.md" <<EOF
id: $1
workstream: resynctest
goal: prove the freshness precondition
engine: none
acceptance: test -s f.txt
repo: $2
workdir: .
autonomy: auto
status: queued
EOF
}
run_work(){ ( cd "$R" && RECURSION_ROOT="$R" RECURSION_NO_NOTIFY=1 \
    RECURSION_SOURCE_LOCK_WAIT_SECONDS="${RECURSION_SOURCE_LOCK_WAIT_SECONDS:-30}" \
    RECURSION_TEST_WORKER=logical bash scripts/work.sh "$1" ) >"$TMP/w.out" 2>&1; }
st(){ sed -n 's/^status: *//p' "$R/state/tasks/$1.md" | head -1 | tr -d '\r'; }

# --- 10) ★ CONTROL: against a FRESH repo the build proceeds normally --------------------------
mktask fresh-ok "$TMP/insync"
run_work fresh-ok
# work.sh removes the worktree when it finishes but keeps loop/work/<id> "for review", so the
# BRANCH is the durable evidence that a build ran — the directory is not.
built(){ $GIT -C "$1" rev-parse --verify --quiet "refs/heads/loop/work/$2" >/dev/null 2>&1 && echo yes || echo no; }
if [ "$(st fresh-ok)" = "passed" ] && [ "$(built "$TMP/insync" fresh-ok)" = "yes" ]; then
  ok "★ CONTROL — a task against a fresh repo builds (status=passed, worktree created)"
else
  no "★ CONTROL FAILED — a fresh repo did not build (status=$(st fresh-ok)); test 11 proves nothing"
  tail -5 "$TMP/w.out" | sed 's/^/        /'
fi

mktask lock-defer "$TMP/insync"
exec {work_lock_fd}>"$R/state/.locks/phase8-window-insync.lock"
flock -x "$work_lock_fd"
RECURSION_SOURCE_LOCK_WAIT_SECONDS=0 run_work lock-defer; lock_work_rc=$?
flock -u "$work_lock_fd"
exec {work_lock_fd}>&-
if [ "$lock_work_rc" = 75 ] && [ "$(st lock-defer)" = queued ] \
    && [ "$(built "$TMP/insync" lock-defer)" = no ] \
    && grep -q 'base freshness is temporarily deferred.*source/window lock is contended' "$TMP/w.out"; then
  ok "shared-lock contention defers work before task lease or worker launch"
else
  no "shared-lock contention did not preserve queued work (rc=$lock_work_rc status=$(st lock-defer))"
fi

mktask pin-io-defer "$TMP/insync"
printf '%s\n' '!fail' > "$R/state/test-source-pins/insync"
run_work pin-io-defer; pin_io_work_rc=$?
rm -f "$R/state/test-source-pins/insync"
if [ "$pin_io_work_rc" = 75 ] && [ "$(st pin-io-defer)" = queued ] \
    && [ "$(built "$TMP/insync" pin-io-defer)" = no ] \
    && grep -q 'base freshness is temporarily deferred.*bounded controller failure' "$TMP/w.out"; then
  ok "transient source-pin controller I/O defers before task lease or worker launch"
else
  no "transient source-pin I/O terminalized queued work (rc=$pin_io_work_rc status=$(st pin-io-defer))"
fi

# A healthy active pin is temporary authority, not a terminally bad base. It must stop before
# board/lease mutation so the exact queued task and its attempt budget remain retryable.
mktask pin-defer "$TMP/pinned"
run_work pin-defer; pin_work_rc=$?
if [ "$pin_work_rc" = 75 ] && [ "$(st pin-defer)" = queued ] \
    && [ "$(built "$TMP/pinned" pin-defer)" = no ] \
    && ! grep -q '^attempts: [1-9]' "$R/state/tasks/pin-defer.md"; then
  ok "a healthy active source pin defers before task lease or worker launch"
else
  no "an active source pin burned or terminalized ordinary work (rc=$pin_work_rc status=$(st pin-defer))"
fi

# --- 11) ★ the defect: a repo whose freshness cannot be proven must NOT be built on -----------
# Pre-change work.sh logged "fetch skipped/failed — building on local base" and carried on: the
# worktree was created and the arm ran against a stale tree. The two observable differences are
# that no worktree exists and the escalation names the BASE rather than the arm.
mktask stale-block "$TMP/unreachable"
run_work stale-block
wt_made="$(built "$TMP/unreachable" stale-block)"
if [ "$wt_made" = "no" ] && [ "$(st stale-block)" = "blocked" ]; then
  ok "★ a task against an unprovable base is BLOCKED before any build happens"
else
  no "★ THE DEFECT IS OPEN — built=$wt_made status=$(st stale-block); pre-change this task PASSED while its origin was unreachable"
fi
grep -qi 'current with origin\|cannot prove' "$R/state/INBOX.md" \
  && ok "the escalation names the base, not a downstream symptom" \
  || no "the escalation does not explain that the base could not be proven"

# --- 12) the off-switch is explicit, and says so ---------------------------------------------
mktask override "$TMP/unreachable"
( cd "$R" && RECURSION_ROOT="$R" RECURSION_NO_NOTIFY=1 RECURSION_REQUIRE_FRESH_BASE=0 \
    RECURSION_TEST_WORKER=logical bash scripts/work.sh override ) >"$TMP/w2.out" 2>&1
if [ "$(st override)" != "blocked" ] && grep -q 'FRESH-BASE CHECK DISABLED' "$R/loop/logs/"*.log 2>/dev/null; then
  ok "RECURSION_REQUIRE_FRESH_BASE=0 overrides the gate and LOGS that it did"
else
  no "the documented override did not work, or did not announce itself"
fi

echo
[ "$fails" -eq 0 ] && { echo "resync: ALL PASS"; exit 0; }
echo "resync: $fails FAILED"; exit 1
