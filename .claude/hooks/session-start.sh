#!/usr/bin/env bash
# Claude Code cloud-session bootstrap. Local sessions skip it.
# Toolchains come from the cloud environment's setup script; this installs the
# repo's own CI dependencies so they track the current branch.
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
set -uo pipefail
cd "$CLAUDE_PROJECT_DIR" || exit 0

# Latest remote state. tools/ci_gate.py diffs against a base commit, so it needs
# full history (CI checks out with fetch-depth: 0).
git fetch --prune --quiet origin || true
if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  git fetch --unshallow --quiet origin || true
fi

python3 -m pip install --quiet -r requirements-ci.txt || true

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PYTHONUTF8=1 PYTHONIOENCODING=utf-8" >> "$CLAUDE_ENV_FILE"
  base="$(git merge-base HEAD origin/master 2>/dev/null || true)"
  [ -n "$base" ] && echo "export CI_BASE_SHA=$base" >> "$CLAUDE_ENV_FILE"
fi

echo "recursion: origin/master $(git rev-parse --short origin/master), HEAD $(git rev-parse --short HEAD)"
exit 0
