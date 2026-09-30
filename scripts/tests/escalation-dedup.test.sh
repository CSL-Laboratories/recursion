#!/usr/bin/env bash
# escalation-dedup.test.sh — end-to-end: the real duplicate escalations must collapse to one.
#
# WHY NOT JUST THE UNIT TEST. taskctl.test.sh proves esc-record dedups when handed the same
# key. It cannot prove run.sh DERIVES the same key from two briefs — and that derivation is
# where the bug actually lived: each cycle stamped a fresh date/time into the heading, so a
# naive key would differ every time and dedup would never fire.
#
# ★ The fixtures below are the VERBATIM headings from state/INBOX.md on the live box — the
# ask that was appended four times in nine hours. A synthetic "same string twice" fixture
# would pass while the real thing still duplicated.
#
# Runs the real cycle.sh -> run.sh with a stub `hermes` on PATH. No model call, no Telegram.
#
#   run: bash scripts/tests/escalation-dedup.test.sh
set -uo pipefail
export LC_ALL=C

# This suite drives the live loop arms (flock, procfs, POSIX lock semantics) — off-box it can only
# produce noise, so it reports an explicit SKIP. The box baseline runs it for real.
if [ "$(uname -s)" != "Linux" ]; then
  echo "escalation-dedup: SKIP (Linux loop runtime required — flock/procfs/POSIX lock semantics; certified in the box suite)"
  exit 0
fi

SRC="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
fails=0
ok(){ printf 'ok    %s\n' "$1"; }
no(){ printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"; mkdir -p "$BIN"
R="$TMP/repo"; mkdir -p "$R"
tar -C "$SRC" -cf - scripts loop state ANCHOR.md CLAUDE.md 2>/dev/null | tar -C "$R" -xf - 2>/dev/null
mkdir -p "$R/state/briefs" "$R/loop/logs" "$R/state/tasks"
# state/PAUSE is ignored runtime state, so a test launched from the deployed checkout can inherit
# it through the broad state/ fixture copy above. This scenario is specifically proving one live
# notifier call; an inherited kill switch turns it into a PAUSE suppression test and passes no
# signal through the assertion. The disposable fixture owns this removal, never the source tree.
rm -f -- "$R/state/PAUSE"
# ★ NEUTRALISE THE PORTFOLIO. The tar above copies the live state/, including
# state/portfolio.tsv with ABSOLUTE paths to the real repos under /opt. This test runs cycle.sh
# three times, and cycle.sh resyncs the portfolio every run — so without this the test fetched
# eight real repos three times over. That cost 280s (measured) and, worse, reached outside its
# own fixture to touch Lucas's working copies. Third instance of that pattern today.
printf '#project	path	remote	branch	sshkey	roadmap	topic	status
' > "$R/state/portfolio.tsv"
rm -f "$R"/state/tasks/*.md "$R/state/escalations.tsv" "$R/state/events.jsonl"
: > "$R/.env"
printf '# Inbox\n\n_(empty)_\n' > "$R/state/INBOX.md"
( cd "$R" && git init -q . && git add -A >/dev/null 2>&1 \
  && git -c user.name=t -c user.email=t@t commit -qm init >/dev/null 2>&1 \
  && git checkout -qb loop/auto ) || true

export STUB_NOTIFY_LOG="$TMP/notify.log"; : > "$STUB_NOTIFY_LOG"

# The four REAL headings, verbatim from the live INBOX (2026-08-05/06). Three are the same
# ask with different stamps; the first is a genuinely different wording of the same topic and
# is EXPECTED to open its own escalation — dedup must not be so aggressive that it merges
# distinct asks.
FIX="$TMP/fixtures"; mkdir -p "$FIX"
cat > "$FIX/1" <<'F'
## 2026-08-06 00:15 — Approve the Example-game native-diff acceptance frontier
**Workstream:** example-game
**Pointer:** scripts/sample-nativediff-gate.sh
**Blocked on:** human approval of the numeric native-mesh error metric
F
cat > "$FIX/2" <<'F'
## 2026-08-06 03:15 — Approve the Example-game native-diff acceptance frontier
**Workstream:** example-game
**Pointer:** scripts/sample-nativediff-gate.sh
**Blocked on:** Human approval of the numeric native-mesh error metric
F
cat > "$FIX/3" <<'F'
## 2026-08-06 06:15 — Approve the Example-game native-diff acceptance frontier
**Workstream:** example-game
**Pointer:** state/roadmaps/sample-renderer.md
**Blocked on:** Human approval of the numeric native-mesh error metric
F
cat > "$FIX/4" <<'F'
## 2026-08-06 09:15 — Provision a clean proxy for the warp soak
**Workstream:** example-project
**Pointer:** docs/plans/warp.md
**Blocked on:** spend approval
F

run_with(){ # $1 = fixture file whose contents become the escalation block
  cat > "$BIN/hermes" <<STUB
#!/usr/bin/env bash
if [ "\${1:-}" = "send" ]; then shift; printf '%s\n' "NOTIFY \$*" >> "$STUB_NOTIFY_LOG"; exit 0; fi
usage=""; prompt=""
while [ \$# -gt 0 ]; do case "\$1" in -z) prompt="\$2"; shift 2;; --usage-file) usage="\$2"; shift 2;; *) shift;; esac; done
[ -n "\$usage" ] && printf '{"provider":"openai-codex","estimated_cost_usd":0.0}\n' > "\$usage"
if printf '%s' "\$prompt" | grep -q 'HEARTBEAT'; then echo "===HEARTBEAT: brief=== x"; else
  echo "# brief"; echo "===INBOX-START==="; cat "$1"; echo "===INBOX-END==="
fi
exit 0
STUB
  chmod +x "$BIN/hermes"
  # branch-review.sh predates portfolio-backed discovery and otherwise scans /opt/* even in
  # this disposable fixture. Pin its documented override to the fixture repository so this
  # test cannot fetch or rewrite FETCH_HEAD in any product checkout.
  ( cd "$R" && PATH="$BIN:$PATH" RECURSION_ROOT="$R" \
      RECURSION_REVIEW_REPOS="fixture|$R|" bash scripts/cycle.sh force ) >"$TMP/out" 2>&1
}

run_with "$FIX/1"
run_with "$FIX/2"
run_with "$FIX/3"

# --- 1) ★ the three identical asks collapse to ONE inbox entry -----------------------------
n="$(grep -c 'Approve the Example-game native-diff acceptance frontier' "$R/state/INBOX.md" || true)"
if [ "$n" = "1" ]; then
  ok "★ three cycles of the SAME ask -> 1 INBOX entry (was 4 on the live box)"
else
  no "★ the same ask appears $n times in INBOX — the duplication bug survives"
fi

# --- 2) it is recorded once, with a sighting count -----------------------------------------
rows="$(awk -F'\t' 'NR>1 && $9 ~ /native-diff acceptance frontier/' "$R/state/escalations.tsv" 2>/dev/null | wc -l)"
cnt="$(awk -F'\t' 'NR>1 && $9 ~ /native-diff acceptance frontier/ {print $6; exit}' "$R/state/escalations.tsv" 2>/dev/null)"
if [ "$rows" = "1" ] && [ "${cnt:-0}" = "3" ]; then
  ok "escalation index: 1 row, sighting count 3 (visible as a number, not as rows)"
else
  no "escalation index: rows=$rows count=${cnt:-none} (want 1 row / count 3)"
fi

# --- 3) the INBOX entry carries a stable id ------------------------------------------------
grep -qE '^## \[esc-[0-9]{4}\] ' "$R/state/INBOX.md" \
  && ok "INBOX entry carries a stable [esc-NNNN] id" \
  || no "INBOX entry has no escalation id — a reply cannot be correlated"

# --- 4) Telegram was pinged ONCE, not three times ------------------------------------------
pings="$(grep -c 'native-diff acceptance frontier' "$STUB_NOTIFY_LOG" || true)"
[ "$pings" = "1" ] && ok "notifier pinged once, not once per cycle" || no "notifier pinged $pings times (want 1)"

# --- 5) ★ CONTROL: a genuinely DIFFERENT ask still gets through -----------------------------
# Without this, a dedup that suppressed everything would pass all of the above.
run_with "$FIX/4"
if grep -q 'Provision a clean proxy for the warp soak' "$R/state/INBOX.md"; then
  ok "CONTROL — a different ask still reaches the INBOX (dedup is not swallowing)"
else
  no "CONTROL FAILED — dedup suppressed a genuinely new escalation"
fi

# --- 6) resolving it lets the same ask be raised again --------------------------------------
EID="$(awk -F'\t' 'NR>1 && $9 ~ /native-diff acceptance frontier/ {print $1; exit}' "$R/state/escalations.tsv")"
RECURSION_ROOT="$R" bash "$R/scripts/taskctl.sh" esc-resolve "$EID" "decided" >/dev/null 2>&1
run_with "$FIX/3"
n2="$(grep -c 'Approve the Example-game native-diff acceptance frontier' "$R/state/INBOX.md" || true)"
[ "$n2" = "2" ] && ok "after resolution the ask can be raised again (a real re-ask is not lost)" \
                || no "after resolution the ask reappeared $n2 times (want 2)"

echo
[ "$fails" -eq 0 ] && { echo "escalation-dedup: ALL PASS"; exit 0; }
echo "escalation-dedup: $fails FAILED"; exit 1
