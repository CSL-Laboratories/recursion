#!/usr/bin/env bash
# gate-collation.test.sh — guards the set-comparison soundness of sample-conformance-gate.sh.
#
# WHY THIS EXISTS. On 2026-08-06 the gate reported REGRESSION against a pass set that was a
# strict SUPERSET of the baseline — impossible by construction. Cause: under en_US.UTF-8,
# GNU `sort` and `comm` do not agree on ordering, so `comm -23` produced garbage while
# printing only a soft "not in sorted order" warning to stderr. The false REGRESSION is how
# it was caught; the false PASS is the dangerous direction, because a genuinely absent title
# could be skipped and a real regression blessed as green.
#
# ★ TEST 3 IS THE MUTATION TEST. A "fix" that makes the comparison never fail is not a fix.
# It removes a title and asserts the gate still names it. Without this, tests 1-2 could be
# satisfied by a gate that always returns empty.
#
#   run: bash scripts/tests/gate-collation.test.sh
set -uo pipefail

# This suite drives the live loop arms (flock, procfs, POSIX lock semantics) — off-box it can only
# produce noise, so it reports an explicit SKIP. The box baseline runs it for real.
if [ "$(uname -s)" != "Linux" ]; then
  echo "gate-collation: SKIP (Linux loop runtime required — flock/procfs/POSIX lock semantics; certified in the box suite)"
  exit 0
fi

ROOT="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
BASELINE="${SAMPLE_BASELINE:-$ROOT/state/sample/baseline-pass.txt}"
GATE="${SAMPLE_CONFORMANCE_GATE:-$ROOT/scripts/sample-conformance-gate.sh}"
fails=0
ok(){ printf 'ok    %s\n' "$1"; }
no(){ printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }

[ -f "$BASELINE" ] || { echo "FAIL  no baseline at $BASELINE"; exit 1; }
[ -f "$GATE" ]     || { echo "FAIL  no gate at $GATE"; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# The gate exports LC_ALL=C; these tests must compare under the SAME collation it uses, or
# they would validate a different program than the one that runs.
export LC_ALL=C

# --- 1) the baseline is C-collated and unique (the gate now hard-fails otherwise) ---------
if sort -c -u "$BASELINE" 2>/dev/null; then
  ok "baseline is C-collated and unique"
else
  no "baseline is NOT C-collated/unique — re-pin: LC_ALL=C sort -u <in> > $BASELINE"
fi

# --- 2) SUPERSET INVARIANT: baseline vs (baseline + new titles) must report nothing --------
# This is the exact comparison that failed. New titles must never look like removed ones.
{ cat "$BASELINE"
  printf '%s\n' \
    'zzz a brand new test title appended at the end' \
    'AAA a brand new test title that sorts first' \
    '★★ a new title starting with the symbol prefix the corpus uses' \
    'the lowercase-leading title class that tripped the original bug' \
    '  leading-whitespace title' \
    'punctuation: commas, dashes — and em-dashes'
} | sort -u > "$TMP/superset"
SPURIOUS="$(comm -23 "$BASELINE" "$TMP/superset" 2>"$TMP/err")"
if [ -z "$SPURIOUS" ] && ! grep -q 'not in sorted order' "$TMP/err"; then
  ok "superset invariant holds (no false regression, no collation warning)"
else
  no "FALSE REGRESSION — a superset reported missing titles:"
  printf '        %s\n' $SPURIOUS | head -5
  sed 's/^/        stderr: /' "$TMP/err" | head -3
fi

# --- 3) ★ MUTATION: a genuinely removed title MUST still be detected ------------------------
# Drop one real baseline line; the comparison has to name exactly that line.
VICTIM="$(sed -n '2p' "$BASELINE")"
grep -vxF -- "$VICTIM" "$BASELINE" | sort -u > "$TMP/mutated"
DETECTED="$(comm -23 "$BASELINE" "$TMP/mutated")"
if [ "$DETECTED" = "$VICTIM" ]; then
  ok "mutation detected — a removed title is still caught (gate can still fail)"
else
  no "MUTATION ESCAPED — removed '$VICTIM' but comparison reported: '${DETECTED:-<nothing>}'"
fi

# --- 4) the gate refuses a non-C-collated baseline instead of emitting a verdict -----------
# A baseline pinned under another locale makes every comparison unsound; that is a harness
# fault (exit 3), never a pass or a regression.
printf '%s\n' 'b title' 'A title' > "$TMP/badbaseline"   # not C-sorted: 'A' < 'b' in C
set +e
OUT="$(cd "$TMP" && SAMPLE_BASELINE="$TMP/badbaseline" bash "$GATE" --no-progress 2>&1)"; rc=$?
set -e
if [ "$rc" = "3" ] && printf '%s' "$OUT" | grep -q 'HARNESS'; then
  ok "non-C-collated baseline is rejected as a harness fault (exit 3)"
else
  no "expected exit 3 + HARNESS on a mis-collated baseline; got exit $rc: $(printf '%s' "$OUT" | head -1)"
fi

# --- 5) the gate still declares LC_ALL=C (guards against a well-meaning removal) -----------
if grep -qE '^export LC_ALL=C' "$GATE"; then
  ok "gate pins LC_ALL=C"
else
  no "gate no longer pins LC_ALL=C — the set comparison is unsound again"
fi

echo
[ "$fails" -eq 0 ] && { echo "gate-collation: ALL PASS"; exit 0; }
echo "gate-collation: $fails FAILED"; exit 1
