#!/usr/bin/env bash
# Claude Code cloud-session bootstrap. Local sessions skip it.
# Toolchains come from the cloud environment's setup script. This fetches the
# latest refs (HEAD is not moved) and installs the repo's CI dependencies.
# If a required step fails it exits 1: SessionStart then shows a hook error,
# and the session still starts.
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
set -uo pipefail
cd "$CLAUDE_PROJECT_DIR" || exit 1
export GIT_TERMINAL_PROMPT=0

fetch=ok deps=ok base=ok

# tools/ci_gate.py diffs against a base commit, so it needs full history (CI
# checks out with fetch-depth: 0) and origin/master, which a single-branch
# clone does not track.
if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  git fetch --quiet --unshallow origin || fetch=FAIL
fi
git fetch --quiet origin '+refs/heads/master:refs/remotes/origin/master' || fetch=FAIL
git fetch --quiet --prune origin || fetch=FAIL

python3 -m pip install --quiet -r requirements-ci.txt || deps=FAIL

base_sha="$(git merge-base HEAD origin/master 2>/dev/null)" || base=FAIL
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PYTHONUTF8=1 PYTHONIOENCODING=utf-8" >> "$CLAUDE_ENV_FILE"
  [ "$base" = ok ] && echo "export CI_BASE_SHA=$base_sha" >> "$CLAUDE_ENV_FILE"
fi

master="$(git rev-parse --short origin/master 2>/dev/null || echo unknown)"
[ "$fetch" = ok ] || master="$master (stale)"
status="recursion bootstrap: fetch=$fetch deps=$deps ci_base=$base; origin/master $master, HEAD $(git rev-parse --short HEAD 2>/dev/null || echo unknown) (not moved)"

if [ "$fetch$deps$base" != "okokok" ]; then
  echo "$status" >&2
  exit 1
fi
echo "$status"
exit 0
