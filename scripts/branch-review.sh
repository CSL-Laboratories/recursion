#!/usr/bin/env bash
# branch-review.sh — the "what's waiting for your review" section of the daily digest.
# Enumerates the loop's review branches across every tracked repo, their merge status against the
# protected default branch, whether they are PUSHED (reviewable on GitHub) or still local, and a
# recommended review+merge order. The loop pushes branches; it NEVER merges main — you do, on GitHub.
#
# Read-only: fetch + inspect only. Emits markdown to stdout for the brief to include.
set -uo pipefail
HOME="${HOME:-/root}"

# Repos to enumerate — AUTO-DISCOVERED: every git repo under /opt with an origin remote, using its
# per-repo deploy key (~/.ssh/<name>_deploy) when one exists. The list used to be hardcoded to two
# repos, so a newly registered project was silently absent from the digest — the same class of bug
# as the hardcoded branch prefix below. RECURSION_REVIEW_REPOS overrides with explicit
# name|path|key rows (newline-separated). B6 replaces discovery with the portfolio registry; until
# then, discovery beats a list that goes stale the moment a project is added.
discover_repos(){
  local d name key
  for d in /opt/*/; do
    d="${d%/}"; name="$(basename "$d")"
    [ -d "$d/.git" ] || continue
    git -C "$d" remote get-url origin >/dev/null 2>&1 || continue
    key=""
    [ -f "$HOME/.ssh/${name}_deploy" ] && key="$HOME/.ssh/${name}_deploy"
    [ "$name" = "recursion" ] && key=""    # uses the default key
    printf '%s|%s|%s\n' "$name" "$d" "$key"
  done
}
if [ -n "${RECURSION_REVIEW_REPOS:-}" ]; then
  mapfile -t REPOS <<<"$RECURSION_REVIEW_REPOS"
else
  mapfile -t REPOS < <(discover_repos)
fi

# Branch namespaces that count as REVIEW branches. `loop/*` alone missed every branch the
# control-plane rebase produces (rebase/*, fix/*, re/*, prep/*): they were pushed to origin and
# still absent from this digest. Published but unsurfaced is indistinguishable from not published.
REVIEW_PREFIXES="${RECURSION_REVIEW_PREFIXES:-loop rebase fix re prep}"

gitfetch(){ # $1=path $2=key
  if [ -n "$2" ]; then
    GIT_SSH_COMMAND="ssh -i $2 -o IdentitiesOnly=yes -o BatchMode=yes" git -C "$1" fetch -q --prune origin 2>/dev/null || true
  else
    git -C "$1" fetch -q --prune origin 2>/dev/null || true
  fi
}

echo "## 🔀 Review branches — you merge on GitHub (the loop only pushes)"
echo ""
GRAND_OPEN=0
for entry in "${REPOS[@]}"; do
  IFS='|' read -r name path key <<<"$entry"
  [ -d "$path/.git" ] || continue
  gitfetch "$path" "$key"
  def="$(git -C "$path" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')"; def="${def:-main}"
  # all loop/* refs, remote (reviewable) and local (not yet pushed), deduped to branch names
  refargs=()
  for pfx in $REVIEW_PREFIXES; do refargs+=("refs/remotes/origin/$pfx" "refs/heads/$pfx"); done
  mapfile -t brs < <(git -C "$path" for-each-ref --format='%(refname:short)' \
      "${refargs[@]}" 2>/dev/null | sed 's#^origin/##' | sort -u)
  [ "${#brs[@]}" -eq 0 ] && continue

  # build sortable rows: <sortkey>\t<line>   (open first, then oldest-first within each group)
  rows=(); merged=0
  for br in "${brs[@]}"; do
    onorigin=0; git -C "$path" rev-parse --verify -q "origin/$br" >/dev/null 2>&1 && onorigin=1
    ref="origin/$br"; [ "$onorigin" -eq 1 ] || ref="$br"
    tip="$(git -C "$path" rev-parse --short "$ref" 2>/dev/null)"
    subj="$(git -C "$path" log -1 --format='%s' "$ref" 2>/dev/null | cut -c1-72)"
    ts="$(git -C "$path" log -1 --format='%ct' "$ref" 2>/dev/null || echo 0)"
    ago="$(git -C "$path" log -1 --format='%cr' "$ref" 2>/dev/null)"
    if git -C "$path" merge-base --is-ancestor "$ref" "origin/$def" 2>/dev/null; then
      # Already merged: nothing is waiting on it. Counted, not listed — widening the repo and
      # prefix scope surfaced ~30 historical merged branches, which would bury the handful that
      # actually need review and blow the brief's one-page cap (CLAUDE.md, bounded output).
      merged=$((merged+1)); continue
    elif [ "$onorigin" -eq 1 ]; then
      grp=0; tag="🟡 OPEN — review on GitHub"; GRAND_OPEN=$((GRAND_OPEN+1))
    else
      grp=1; tag="⚪ local only (not pushed yet — grant write key)"; GRAND_OPEN=$((GRAND_OPEN+1))
    fi
    rows+=("${grp} ${ts}	- [$tag] \`$br\` ($tip, $ago) — $subj")
  done
  # A repo with nothing open contributes one quiet line, not a heading and a wall of history.
  if [ "${#rows[@]}" -eq 0 ]; then
    [ "$merged" -gt 0 ] && echo "### $name" && echo "_nothing open ($merged merged, prunable)._" && echo ""
    continue
  fi
  echo "### $name"
  printf '%s\n' "${rows[@]}" | sort -k1,1n -k2,2n | cut -f2-
  [ "$merged" -gt 0 ] && echo "- _($merged already-merged branch(es) hidden — prunable)_"
  echo ""
done
if [ "$GRAND_OPEN" -eq 0 ]; then
  echo "_Nothing waiting — no open review branches._"
else
  echo "**$GRAND_OPEN branch(es) awaiting your review.** Recommended order: top-to-bottom within each repo (oldest-first; a dep lands before what builds on it)."
fi
