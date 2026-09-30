#!/usr/bin/env bash
# digest.sh — each project's own status, in its own topic.
#
# WHY THIS AND NOT ONE BIG BRIEF. The daily brief is a portfolio view for the supervisor, and it
# is the right shape for "what should happen next across everything". It is the wrong shape for
# "how is a project doing", which is the question actually asked in a project's thread.
#
# ★ AND IT IS WHAT MAKES A TOPIC INTO A PILOT. The hermes gateway keys a conversational session
# per (chat, thread) — verified: agent:main:telegram:group:-1003894008732:1. So every project
# topic ALREADY gets its own session with its own history. What it lacks is any idea which
# project it belongs to: it is the generic assistant, with no roadmap, no gates, no branches.
# Posting this digest into the topic puts those facts INTO that session's history, so when Lucas
# asks a question in a project's thread the answer is grounded in that project's state.
# The identity comes from the thread's contents, which costs one message instead of a second
# agent per project.
#
#   digest.sh <project>   render and post one project's digest to its topic
#   digest.sh --all       every active project
#   digest.sh <project> --print   render to stdout, post nothing
set -uo pipefail
export LC_ALL=C
ROOT="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT" 2>/dev/null || { echo "digest: RECURSION_ROOT '$ROOT' is not a directory" >&2; exit 3; }
[ -f .env ] && { set -a; . ./.env; set +a; }
# .env pins RECURSION_ROOT for the deployed checkout. Sourcing it must never RETARGET a
# caller who passed a different root: that silently made a "sandbox" enqueue ten decisions
# into production on 2026-08-12. ROOT and RECURSION_ROOT agree from here on.
RECURSION_ROOT="$ROOT"; export RECURSION_ROOT
REG="${RECURSION_PORTFOLIO:-$ROOT/state/portfolio.tsv}"

row_for(){ awk -F'\t' -v q="$1" '$1 !~ /^#/ && NF>=8 && $1==q {print; exit}' "$REG"; }
fld(){ printf '%s' "$1" | cut -f"$2"; }
tf(){ sed -n "s/^$1: *//p" "$2" 2>/dev/null | head -1 | tr -d '\r'; }

render(){ # render <project>
  local proj="$1" row path key roadmap ws n
  row="$(row_for "$proj")"; [ -n "$row" ] || { echo "digest: '$proj' is not in the portfolio" >&2; return 2; }
  path="$(fld "$row" 2)"; key="$(fld "$row" 5)"; roadmap="$(fld "$row" 6)"
  # workstream = the roadmap's stem; tasks carry `workstream:`, the registry carries the roadmap.
  ws=""; [ "$roadmap" != "-" ] && ws="$(basename "$roadmap" .md)"

  echo "📋 $proj"

  # FRESHNESS first: everything below is a claim about a repo, and a claim about a repo you
  # cannot reach is not a claim.
  if [ -x scripts/resync.sh ]; then
    local out rc; out="$(bash scripts/resync.sh check "$proj" 2>&1)"; rc=$?
    case "$rc" in
      0) echo "repo: $(printf '%s' "$out" | sed 's/^[^:]*: *//')" ;;
      3) echo "repo: ⚠ CANNOT VERIFY — $(printf '%s' "$out" | head -c 120)" ;;
      4|75)
        case "$out" in
          *"is held at an exact active pilot source; ordinary dispatch is refused"*)
            echo "repo: 🔒 PINNED — exact active pilot source; ordinary dispatch is intentionally held" ;;
          *) echo "repo: 🛑 NEEDS YOU — $(printf '%s' "$out" | head -c 120)" ;;
        esac ;;
      *) echo "repo: 🛑 NEEDS YOU — $(printf '%s' "$out" | head -c 120)" ;;
    esac
  fi

  # WAITING ON LUCAS leads, because it is the only section he can act on right now.
  if [ -x scripts/assist.sh ]; then
    n="$(bash scripts/assist.sh list open 2>/dev/null | grep -c "^[a-z0-9]" || true)"
    if [ "${n:-0}" -gt 1 ]; then
      echo; echo "🙋 waiting on you:"
      bash scripts/assist.sh list open 2>/dev/null | tail -n +2 | while read -r line; do
        printf '  %s\n' "$(printf '%s' "$line" | cut -c1-110)"; done
    fi
  fi

  if [ -n "$ws" ] && [ -d state/tasks ]; then
    local q r b p f
    q=0 r=0 b=0 p=0 f=0
    for t in state/tasks/*.md; do
      [ -f "$t" ] || continue
      [ "$(tf workstream "$t")" = "$ws" ] || continue
      case "$(tf status "$t")" in
        queued) q=$((q+1)) ;; running) r=$((r+1)) ;; blocked|escalated) b=$((b+1)) ;;
        passed) p=$((p+1)) ;; failed) f=$((f+1)) ;;
      esac
    done
    echo; echo "tasks: $q queued · $r running · $b blocked · $p passed · $f failed"
  fi

  if [ -n "$roadmap" ] && [ "$roadmap" != "-" ] && [ -f "$roadmap" ]; then
    local next; next="$(grep -n '^- \[ \]' "$roadmap" 2>/dev/null | head -3 | sed 's/^[0-9]*://')"
    if [ -n "$next" ]; then echo; echo "next up:"; printf '%s\n' "$next" | sed 's/^- \[ \]/  ·/'; fi
  fi

  # Review branches: the actual output of the loop, and the thing that needs a human on GitHub.
  if [ -d "$path/.git" ]; then
    local def brs
    def="$(fld "$row" 4)"
    brs="$(git -C "$path" for-each-ref --format='%(refname:short)' "refs/remotes/origin" 2>/dev/null \
           | sed 's#^origin/##' | grep -vE "^(HEAD|origin|$def)$" | head -5)"
    # 'origin' is refs/remotes/origin/HEAD in short form, not a branch. Listing it put a work
    # item in front of Lucas that does not exist, and a digest that invents work gets ignored.
    if [ -n "$brs" ]; then
      echo; echo "awaiting your review on GitHub:"
      printf '%s\n' "$brs" | sed 's/^/  · /'
    fi
  fi
}

post(){ # post <project>
  local proj="$1" body
  body="$(render "$proj")" || return $?
  [ -x scripts/taskctl.sh ] || { echo "digest: canonical event writer is unavailable" >&2; return 3; }
  bash scripts/taskctl.sh notify-event project-digest "$proj" "$body" \
    >/dev/null || return $?
  printf '%s\n' "$body"
}

case "${1:-}" in
  --all)
    awk -F'\t' '$1 !~ /^#/ && NF>=8 && $8=="active" {print $1}' "$REG" | while read -r p; do
      post "$p" >/dev/null 2>&1 && echo "queued digest: $p" || echo "digest failed: $p"
    done ;;
  "") echo "usage: digest.sh <project> | --all | <project> --print"; exit 2 ;;
  *)
    if [ "${2:-}" = "--print" ]; then render "$1"; else post "$1"; fi ;;
esac
