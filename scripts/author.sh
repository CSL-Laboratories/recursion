#!/usr/bin/env bash
# author.sh — the only way Hermes may WRITE, and a deliberately small one.
#
# Intake gave Hermes Lucas's words. Until now it could do nothing with most of them: the verb
# whitelist was noop/plan/work/conduct/publish/escalate/resolve-esc/reap, every one of which
# operates on work somebody else had already written down. "Set up example-site" could only ever
# come back as `escalate`. That is the gap between a scheduler and a manager.
#
# ★ THE BOUND, because this is an expansion of authority and should read like one:
#   - It writes ONLY under state/roadmaps/, state/projects/, state/project-config/, state/pilots/,
#     state/portfolio.tsv and state/instructions/. Never scripts/, .env, code, gates, or tests.
#   - Roadmaps and notes are APPEND-ONLY. A model that can rewrite a roadmap can delete the plan,
#     and the deletion would look exactly like a plan. Appending is bounded and reviewable, and
#     everything lands on loop/auto where `git log` shows who wrote what.
#   - Names are validated against the registry. No traversal, no invention of paths.
#   - ★ THE HUMAN GATE IS THE CREDENTIAL. `portfolio-add` can only create a PENDING row.
#     Activating one requires a deploy key at the path it names, and only Lucas can provision a
#     deploy key on GitHub. So Hermes can do all the wiring and still cannot bring a repo into the
#     portfolio on its own. That gate is a fact about the world, not a flag someone can flip.
#
#   author.sh roadmap-add <workstream> <item>     append a plannable checklist item
#   author.sh roadmap-new <workstream> <project>  create a roadmap wired to a registered repo
#   author.sh portfolio-add <project> <remote> <charter>  prepare a PENDING project/pilot
#   author.sh project-topic-bind <project> <topic>        bind an existing Telegram topic
#   author.sh register <project>                  activate only after every proof passes
#   author.sh note <project> <text>               append to the project's state file
set -uo pipefail
export LC_ALL=C
ROOT="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT" 2>/dev/null || { echo "author: RECURSION_ROOT '$ROOT' is not a directory" >&2; exit 3; }
[ -f .env ] && { set -a; . ./.env; set +a; }
# .env pins RECURSION_ROOT for the deployed checkout. Sourcing it must never RETARGET a
# caller who passed a different root: that silently made a "sandbox" enqueue ten decisions
# into production on 2026-08-12. ROOT and RECURSION_ROOT agree from here on.
RECURSION_ROOT="$ROOT"; export RECURSION_ROOT
REG="${RECURSION_PORTFOLIO:-$ROOT/state/portfolio.tsv}"
TASKCTL="$ROOT/scripts/taskctl.sh"
CMD="${1:-help}"; shift 2>/dev/null || true
now(){ date -u +%FT%TZ; }
event(){ [ -x "$TASKCTL" ] && RECURSION_ROOT="$ROOT" bash "$TASKCTL" event "$1" "$2" "$3" >/dev/null 2>&1 || true; }

# A name becomes a FILENAME, so it is validated like an injection boundary, not like a label.
ok_name(){ case "$1" in ''|*/*|*..*|.*|*' '*) return 1 ;; esac
  printf '%s' "$1" | grep -qE '^[a-z0-9][a-z0-9._-]{0,60}$'; }
row_for(){ awk -F'\t' -v q="$1" '$1 !~ /^#/ && NF>=8 && $1==q {print; exit}' "$REG"; }
fld(){ printf '%s' "$1" | cut -f"$2"; }

case "$CMD" in

roadmap-add)
  WS="${1:?usage: author.sh roadmap-add <workstream> <item>}"; shift; ITEM="$*"
  ok_name "$WS" || { echo "author: '$WS' is not a valid workstream name" >&2; exit 2; }
  F="$ROOT/state/roadmaps/$WS.md"
  [ -f "$F" ] || { echo "author: no roadmap $WS — use roadmap-new first (it needs a repo to build in)" >&2; exit 2; }
  [ -n "${ITEM//[[:space:]]/}" ] || { echo "author: refusing to add an empty roadmap item" >&2; exit 2; }
  # One line, appended. plan.sh turns the first unchecked box into a task, so this is precisely
  # "give the loop something to do" and nothing more.
  case "$ITEM" in *$'\n'*) echo "author: a roadmap item is one line" >&2; exit 2 ;; esac
  printf -- '- [ ] %s\n' "$ITEM" >> "$F"
  event roadmap-add "" "$WS: $ITEM"
  echo "added to $WS: $ITEM" ;;

roadmap-new)
  WS="${1:?usage: author.sh roadmap-new <workstream> <project>}"; PROJ="${2:?project}"
  ok_name "$WS" || { echo "author: invalid workstream name" >&2; exit 2; }
  F="$ROOT/state/roadmaps/$WS.md"
  [ -f "$F" ] && { echo "author: roadmap $WS already exists" >&2; exit 2; }
  ROW="$(row_for "$PROJ")"
  [ -n "$ROW" ] || { echo "author: '$PROJ' is not in the portfolio — add and register it first" >&2; exit 2; }
  [ "$(fld "$ROW" 8)" = "active" ] || { echo "author: '$PROJ' is not active (status=$(fld "$ROW" 8)) — it has no clone to build in" >&2; exit 4; }
  # The header is the task template plan.sh stamps onto everything it creates, so it is built
  # from the REGISTRY rather than typed: a roadmap that names the wrong repo or key silently
  # builds in the wrong place.
  { echo "engine: claude"
    echo "repo: $(fld "$ROW" 2)"
    echo "sshkey: $(fld "$ROW" 5)"
    echo "workdir: ."
    echo "acceptance: true"
    echo
    echo "<!-- Created by author.sh $(now) on Lucas's instruction. The header above is stamped"
    echo "     onto every task planned from this roadmap; it is derived from state/portfolio.tsv,"
    echo "     not typed, because a roadmap naming the wrong repo builds in the wrong place."
    echo "     acceptance: true is a PLACEHOLDER — no gate, no autonomy. Replace it with a real"
    echo "     machine-checkable command before expecting work.sh to build anything here. -->"
    echo
    echo "- [ ] define the first machine-checkable acceptance command for $PROJ"
  } > "$F"
  event roadmap-new "" "$WS -> $PROJ"
  echo "created state/roadmaps/$WS.md wired to $PROJ ($(fld "$ROW" 2))" ;;

portfolio-add)
  PROJ="${1:?usage: author.sh portfolio-add <project> <remote> <charter>}"; REMOTE="${2:?remote}"; shift 2
  CHARTER="$*"
  ok_name "$PROJ" || { echo "author: invalid project name" >&2; exit 2; }
  [ -n "$(row_for "$PROJ")" ] && { echo "author: '$PROJ' is already in the portfolio" >&2; exit 2; }
  [ -n "${CHARTER//[[:space:]]/}" ] \
    || { echo "author: portfolio preparation needs a bounded charter" >&2; exit 2; }
  [ -x "$ROOT/scripts/project-pilot.py" ] \
    || { echo "author: project-pilot registry helper is absent" >&2; exit 4; }
  RECURSION_ROOT="$ROOT" python3 "$ROOT/scripts/project-pilot.py" prepare "$PROJ" "$REMOTE" "$CHARTER" \
    >/dev/null || exit $?
  event portfolio-add "" "$PROJ pending"
  echo "added $PROJ as PENDING. It needs a deploy key at ~/.ssh/${PROJ}_deploy (Lucas provisions it on GitHub), then: recursion author register $PROJ" ;;

project-topic-bind)
  PROJ="${1:?usage: author.sh project-topic-bind <project> <topic>}"; TOPIC="${2:?topic id}"
  [ -x "$ROOT/scripts/project-pilot.py" ] || { echo "author: project-pilot registry helper is absent" >&2; exit 4; }
  RECURSION_ROOT="$ROOT" python3 "$ROOT/scripts/project-pilot.py" bind-topic "$PROJ" "$TOPIC" >/dev/null || exit $?
  event project-topic-bind "" "$PROJ -> $TOPIC"
  echo "bound pending project $PROJ to Telegram topic $TOPIC" ;;

register)
  PROJ="${1:?usage: author.sh register <project>}"
  ROW="$(row_for "$PROJ")"; [ -n "$ROW" ] || { echo "author: '$PROJ' is not in the portfolio" >&2; exit 2; }
  [ "$(fld "$ROW" 8)" = "pending" ] || { echo "author: '$PROJ' is status=$(fld "$ROW" 8), not pending" >&2; exit 2; }
  [ -x "$ROOT/scripts/project-pilot.py" ] \
    || { echo "author: project-pilot registry helper is absent" >&2; exit 4; }
  RECURSION_ROOT="$ROOT" python3 "$ROOT/scripts/project-pilot.py" activate "$PROJ" >/dev/null || exit $?
  ROW="$(row_for "$PROJ")"; P="$(fld "$ROW" 2)"; BR="$(fld "$ROW" 4)"
  event register "" "$PROJ active"
  echo "registered $PROJ -> $P (active, branch $BR)" ;;

note)
  PROJ="${1:?usage: author.sh note <project> <text>}"; shift; TEXT="$*"
  ok_name "$PROJ" || { echo "author: invalid project name" >&2; exit 2; }
  [ -n "${TEXT//[[:space:]]/}" ] || { echo "author: refusing an empty note" >&2; exit 2; }
  F="$ROOT/state/projects/$PROJ.md"
  [ -f "$F" ] || printf '# %s — STATE\n' "$PROJ" > "$F"
  { echo; echo "### $(now)"; printf '%s\n' "$TEXT"; } >> "$F"
  event note "" "$PROJ"
  echo "noted on $PROJ" ;;

*) sed -n '20,26p' "$0"; exit 2 ;;
esac
