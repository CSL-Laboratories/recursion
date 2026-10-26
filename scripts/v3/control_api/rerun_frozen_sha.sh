#!/usr/bin/env bash
# Frozen-SHA rerun for the Recursion spine (WP16, Mon Nov 9).
#
#   scripts/v3/control_api/rerun_frozen_sha.sh <40-hex-sha> [--out DIR] [--steps a,b,c] [--dry-run]
#
# Checks out exactly <sha> in a throwaway detached worktree, runs the provider-free
# gate and the recorded fake-driver run against that tree, and writes one evidence
# file naming the full 40-hex commit, each command, its exit code and log digest.
# Exit 0 only if every step passed. A branch name, an abbreviated or upper-case
# SHA, or a SHA that is not a commit in this clone is refused (exit 2) so the
# evidence cannot describe a moving ref.
#
# Steps (default: all):  gate   python tools/ci_gate.py
#                        record python -m scripts.v3.control_api.record --check
#                        scope  python tools/check_public_scope.py
#
# No provider, network or credential is used. Linux only for a real run: the
# recorded evidence for the Nov 11 demo is the Linux run.
set -euo pipefail

ALL_STEPS="gate,record,scope"
sha="" out="spine-rerun-evidence" steps="$ALL_STEPS" dry=0

die() { printf '%s\n' "rerun_frozen_sha: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --out) [ $# -ge 2 ] || die "--out needs a directory"; out="$2"; shift 2 ;;
    --steps) [ $# -ge 2 ] || die "--steps needs a list"; steps="$2"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *) [ -z "$sha" ] || die "only one SHA is accepted"; sha="$1"; shift ;;
  esac
done

[ -n "$sha" ] || die "a full 40-hex commit SHA is required"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die "not a full lower-case 40-hex SHA"

IFS=',' read -r -a step_list <<< "$steps"
[ "${#step_list[@]}" -gt 0 ] || die "no steps"
for step in "${step_list[@]}"; do
  case "$step" in gate|record|scope) ;; *) die "unknown step: $step" ;; esac
done

repo="$(git rev-parse --show-toplevel 2>/dev/null)" || die "run inside a git clone"
resolved="$(git -C "$repo" rev-parse --verify --quiet "${sha}^{commit}" || true)"
[ "$resolved" = "$sha" ] || die "not a commit in this clone (try: git fetch origin $sha)"

if [ "$dry" -eq 1 ]; then
  printf 'dry-run ok\nsha=%s\nsteps=%s\nout=%s\n' "$sha" "$steps" "$out"
  exit 0
fi

[ "$(uname -s)" = "Linux" ] || die "a real run is Linux only (uname: $(uname -s))"
command -v python3 >/dev/null || die "python3 is required"

mkdir -p "$out"
out="$(cd "$out" && pwd)"
work="$(mktemp -d)"
tree="$work/tree"
cleanup() {
  git -C "$repo" worktree remove --force "$tree" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

git -C "$repo" worktree add --detach "$tree" "$sha" >/dev/null 2>&1 || die "could not check out $sha"
[ "$(git -C "$tree" rev-parse HEAD)" = "$sha" ] || die "checked-out HEAD differs from $sha"

export PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1
unset CI_BASE_SHA GITHUB_OUTPUT || true

tsv="$out/$sha.steps.tsv"
: > "$tsv"
started_la="$(TZ=America/Los_Angeles date '+%Y-%m-%d %H:%M:%S %Z')"
overall=0

run_step() {
  local name="$1"; shift
  local log="$out/$sha.$name.log" begin end code
  begin="$(date +%s)"
  set +e
  ( cd "$tree" && "$@" ) > "$log" 2>&1
  code=$?
  set -e
  end="$(date +%s)"
  printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$*" "$code" "$((end - begin))" "$(basename "$log")" >> "$tsv"
  [ "$code" -eq 0 ] || overall=1
}

for step in "${step_list[@]}"; do
  case "$step" in
    gate)   run_step gate python3 -X utf8 -B tools/ci_gate.py ;;
    record) run_step record python3 -X utf8 -B -m scripts.v3.control_api.record --check ;;
    scope)  run_step scope python3 -X utf8 -B tools/check_public_scope.py ;;
  esac
done

finished_la="$(TZ=America/Los_Angeles date '+%Y-%m-%d %H:%M:%S %Z')"
tree_dirty="$(git -C "$tree" status --porcelain | wc -l | tr -d ' ')"

OUT_DIR="$out" SHA="$sha" TSV="$tsv" STARTED="$started_la" FINISHED="$finished_la" \
TREE_DIRTY="$tree_dirty" OVERALL="$overall" python3 -X utf8 -B - <<'PY'
import hashlib, json, os, platform, subprocess, sys
from importlib import metadata

out, sha = os.environ["OUT_DIR"], os.environ["SHA"]
steps = []
for line in open(os.environ["TSV"], encoding="utf-8"):
    name, command, code, seconds, log = line.rstrip("\n").split("\t")
    digest = hashlib.sha256(open(os.path.join(out, log), "rb").read()).hexdigest()
    steps.append({"step": name, "command": command, "exit_code": int(code),
                  "seconds": int(seconds), "log": log, "log_sha256": digest})
try:
    jsonschema = metadata.version("jsonschema")
except metadata.PackageNotFoundError:
    jsonschema = None
evidence = {
    "type": "SpineFrozenShaRerun@0",
    "candidate_sha": sha,
    "passed": os.environ["OVERALL"] == "0" and all(s["exit_code"] == 0 for s in steps),
    "steps": steps,
    "tree_dirty_files_after": int(os.environ["TREE_DIRTY"]),
    "started_america_los_angeles": os.environ["STARTED"],
    "finished_america_los_angeles": os.environ["FINISHED"],
    "environment": {"system": platform.system(), "release": platform.release(),
                    "machine": platform.machine(), "python": platform.python_version(),
                    "jsonschema": jsonschema},
    "provider_calls": 0,
    "note": "Fake driver only. An implementer's pass is not acceptance; an independent verifier reruns this SHA.",
}
path = os.path.join(out, f"spine-rerun-{sha}.json")
with open(path, "w", encoding="utf-8", newline="\n") as handle:
    json.dump(evidence, handle, indent=2, sort_keys=True)
    handle.write("\n")
print(json.dumps({"evidence": path, "passed": evidence["passed"], "candidate_sha": sha}))
sys.exit(0 if evidence["passed"] else 1)
PY
