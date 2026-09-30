#!/usr/bin/env bash
# topics.sh — one forum topic per project in the operator surface, and the registry remembers which.
#
# The topic id is the routing key, so it belongs with the rest of what the control plane knows
# about a repo: state/portfolio.tsv column 7. Keeping it there rather than in .env means a new
# project gets a topic the same way it gets a deploy key or a roadmap, and `git log` shows when
# it happened.
#
# IDEMPOTENT BY CONSTRUCTION. `ensure` creates a topic only for an active project whose topic
# column is '-', and verifies the ones that are already set. Running it twice must not produce
# two topics per project — an "ensure" that is not idempotent is a duplicate factory, and
# Telegram will happily make ten topics with the same name.
#
# ★ THE TOKEN IS NEVER PRINTED. It is interpolated into a URL held in a local, curl's stderr is
# discarded (curl echoes the URL it was given, and the URL carries the token), and failures are
# reported with a redacted line.
#
#   topics.sh ensure [--dry-run]   create missing topics; write ids back to the registry
#   topics.sh list                 show project -> topic
#   topics.sh check                verify every recorded topic still exists
set -uo pipefail
export LC_ALL=C
ROOT="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"
[ -f .env ] && { set -a; . ./.env; set +a; }
# .env pins RECURSION_ROOT for the deployed checkout. Sourcing it must never RETARGET a
# caller who passed a different root: that silently made a "sandbox" enqueue ten decisions
# into production on 2026-08-12. ROOT and RECURSION_ROOT agree from here on.
RECURSION_ROOT="$ROOT"; export RECURSION_ROOT
REG="${RECURSION_PORTFOLIO:-$ROOT/state/portfolio.tsv}"
CHAT="${RECURSION_TELEGRAM_CHAT:-}"
CMD="${1:-list}"; shift 2>/dev/null || true

api(){ # api <method> <curl -d args...> ; prints the JSON body, never the URL
  local m="$1"; shift
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] || { echo '{"ok":false,"description":"no bot token configured"}'; return 1; }
  curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/$m" "$@" 2>/dev/null \
    || echo '{"ok":false,"description":"curl failed (details withheld: the URL carries the token)"}'
}
jget(){ python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: print(""); raise SystemExit
cur=d
for k in sys.argv[1].split("."):
    if isinstance(cur,dict): cur=cur.get(k)
    else: cur=None
print("" if cur is None else cur)' "$1"; }

rows(){ awk -F'\t' 'NF>=8 && $1 !~ /^#/ && $1 != ""' "$REG"; }

# verdict <json-body> -> EXISTS | MISSING | UNKNOWN
# ★ An error is not an absence. Telegram answers TOPIC_NOT_MODIFIED when you "edit" a topic to
# the name it already has — ok=false, and it PROVES the topic exists. Reading ok=false as "gone"
# reported all eight freshly-created topics as MISSING. Only TOPIC_ID_INVALID means absent;
# anything else (rights, network, rate limit) is UNKNOWN, because a probe that could not answer
# must not be allowed to answer.
verdict(){
  local body="$1" desc
  case "$(printf '%s' "$body" | jget ok)" in True|true) echo EXISTS; return 0 ;; esac
  desc="$(printf '%s' "$body" | jget description)"
  case "$desc" in
    *TOPIC_NOT_MODIFIED*)  echo EXISTS ;;
    *TOPIC_ID_INVALID*|*"thread not found"*|*THREAD_NOT_FOUND*) echo MISSING ;;
    *) echo UNKNOWN ;;
  esac
}

case "$CMD" in

list)
  printf '%-28s %-8s %s\n' PROJECT STATUS TOPIC
  rows | while IFS=$'\t' read -r p _ _ _ _ _ topic status; do
    printf '%-28s %-8s %s\n' "$p" "$status" "$topic"
  done ;;

check)
  [ -n "$CHAT" ] || { echo "topics: RECURSION_TELEGRAM_CHAT is not set — nothing to check against"; exit 3; }
  bad=0
  while IFS=$'\t' read -r p _ _ _ _ _ topic status; do
    [ "$topic" = "-" ] && continue
    # editForumTopic with no changes is the cheapest existence probe that needs no message.
    v="$(verdict "$(api editForumTopic -d chat_id="$CHAT" -d message_thread_id="$topic" --data-urlencode name="$p")")"
    case "$v" in
      EXISTS)  printf '  ok      %-26s topic %s\n' "$p" "$topic" ;;
      MISSING) printf '  MISSING %-26s topic %s no longer exists — re-run `topics.sh ensure` after clearing it to -\n' "$p" "$topic"; bad=1 ;;
      *)       printf '  UNKNOWN %-26s topic %s could not be checked (rights? network?) — NOT reported as deleted\n' "$p" "$topic"; bad=1 ;;
    esac
  done < <(rows)
  exit "$bad" ;;

ensure)
  DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
  [ -n "$CHAT" ] || { echo "topics: RECURSION_TELEGRAM_CHAT is not set."; echo "Set it in .env (a human flips .env — see state/PROPOSALS.md)."; exit 3; }
  # A non-forum chat cannot hold topics, and createForumTopic would fail once per project with a
  # different-looking error each time. Ask once, up front.
  isf="$(api getChat -d chat_id="$CHAT" | jget result.is_forum)"
  if [ "$isf" != "True" ] && [ "$isf" != "true" ]; then
    echo "topics: chat $CHAT is not a forum (is_forum=$isf) — enable Topics on the group first."; exit 3
  fi

  changed=0
  while IFS=$'\t' read -r p path remote branch key roadmap topic status; do
    [ "$status" = "active" ] || continue
    [ "$topic" = "-" ] || { echo "  keep    $p -> $topic"; continue; }
    if [ "$DRY" = "1" ]; then echo "  WOULD   create topic '$p'"; continue; fi
    body="$(api createForumTopic -d chat_id="$CHAT" --data-urlencode name="$p")"
    tid="$(printf '%s' "$body" | jget result.message_thread_id)"
    if [ -z "$tid" ]; then
      echo "  FAILED  $p — $(printf '%s' "$body" | jget description)"; continue
    fi
    # Write the id back into the registry, matching on the WHOLE row so a project whose name is
    # a prefix of another cannot be edited by accident.
    python3 - "$REG" "$p" "$tid" <<'PY'
import io,sys
reg,proj,tid=sys.argv[1],sys.argv[2],sys.argv[3]
out=[]
for line in io.open(reg,encoding='utf-8',newline='').read().split('\n'):
    c=line.split('\t')
    if len(c)>=8 and c[0]==proj and c[6]=='-':
        c[6]=tid; line='\t'.join(c)
    out.append(line)
io.open(reg,'w',encoding='utf-8',newline='\n').write('\n'.join(out))
PY
    echo "  created $p -> $tid"; changed=$((changed+1))
  done < <(rows)
  echo "topics: $changed created" ;;

# Exposed so scripts/tests/telegram-routing.test.sh can feed it the three responses captured
# live from the API, rather than re-implementing the classification and testing a copy.
verdict) verdict "${1:-}" ;;

*) sed -n '19,22p' "$0"; exit 2 ;;
esac
