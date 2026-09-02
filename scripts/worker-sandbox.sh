#!/usr/bin/env bash
# Launch one model worker as a hardened transient service. systemd constructs the private
# mount namespace as root, then drops to the dedicated runner uid before executing the arm.
set -euo pipefail

ROOT="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENGINE=""; WT=""; PROMPT=""; MODEL=""; USAGE_FILE=""; ROLE="implementer"
NETWORK_POLICY="provider-only"; READ_ONLY_WORKTREE=0; EVIDENCE_DIR=""
GATE_COMMAND=""; WORKDIR_PATH="."; READ_ONLY_PATHS=(); ENGINE_PLAN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --engine) ENGINE="${2:-}"; shift 2 ;;
    --engine-plan) ENGINE_PLAN="${2:-}"; shift 2 ;;
    --worktree) WT="${2:-}"; shift 2 ;;
    --prompt) PROMPT="${2:-}"; shift 2 ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    --usage-file) USAGE_FILE="${2:-}"; shift 2 ;;
    --role) ROLE="${2:-}"; shift 2 ;;
    --network-policy) NETWORK_POLICY="${2:-}"; shift 2 ;;
    --read-only-worktree) READ_ONLY_WORKTREE=1; shift ;;
    --read-only-path) READ_ONLY_PATHS+=("${2:-}"); shift 2 ;;
    --evidence-dir) EVIDENCE_DIR="${2:-}"; shift 2 ;;
    --gate-command) GATE_COMMAND="${2:-}"; shift 2 ;;
    --workdir) WORKDIR_PATH="${2:-}"; shift 2 ;;
    *) echo "worker-sandbox: unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$ENGINE" in
  claude|hermes|gate)
    [ -z "$ENGINE_PLAN" ] || { echo "worker-sandbox: builtin engines take no engine plan" >&2; exit 92; } ;;
  none|deepseek|openrouter|"") echo "worker-sandbox: invalid engine" >&2; exit 92 ;;
  *)
    # Plugin engines (D-015) run only from a controller-admitted plan file, and the name is
    # revalidated here because it is used as a root-written filename below.
    [ -n "$ENGINE_PLAN" ] || { echo "worker-sandbox: invalid engine" >&2; exit 92; }
    [[ "$ENGINE" =~ ^[a-z][a-z0-9-]{1,31}$ ]] \
      || { echo "worker-sandbox: invalid engine" >&2; exit 92; } ;;
esac
PLAN_EXE=""; PLAN_ARGV=(); PLAN_MODEL_SUFFIX=(); PLAN_ENV_PASS=(); PLAN_OK=0; PLAN_MREQ=0
ENGINE_ENV_FILE=""
if [ -n "$ENGINE_PLAN" ]; then
  [ -f "$ENGINE_PLAN" ] || { echo "worker-sandbox: engine plan is missing" >&2; exit 92; }
  while IFS= read -r plan_line; do
    plan_line="${plan_line%$'\r'}"   # a Windows-native python3 emits CRLF; the box's does not
    case "$plan_line" in
      EXE=*)  PLAN_EXE="${plan_line#EXE=}" ;;
      ARG=*)  PLAN_ARGV+=("${plan_line#ARG=}") ;;
      MSFX=*) PLAN_MODEL_SUFFIX+=("${plan_line#MSFX=}") ;;
      ENVP=*) PLAN_ENV_PASS+=("${plan_line#ENVP=}") ;;
      MREQ=1) PLAN_MREQ=1 ;;
      OK=1)   PLAN_OK=1 ;;
    esac
  done < <(python3 - "$ENGINE_PLAN" "$ENGINE" <<'PY' || true
import json, re, sys
try:
    plan = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(92)
engine = plan.get("engine")
if plan.get("schema_version") != 1 or engine != sys.argv[2] \
        or not isinstance(engine, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", engine):
    raise SystemExit(92)
exe = plan.get("executable"); argv = plan.get("argv")
if not isinstance(exe, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", exe):
    raise SystemExit(92)
if not isinstance(argv, list) or not argv or argv[0] != "{{EXE}}" \
        or argv.count("{{EXE}}") != 1 or argv.count("{{PROMPT}}") != 1 \
        or argv.count("{{MODEL}}") > 1:
    raise SystemExit(92)
suffix = plan.get("model_argv_suffix") or []
if not isinstance(suffix, list) or suffix.count("{{MODEL}}") > 1:
    raise SystemExit(92)
for element in list(argv) + list(suffix):
    if not isinstance(element, str) or any(ord(ch) < 32 for ch in element):
        raise SystemExit(92)
    if element.startswith("{{") and element.endswith("}}") \
            and element not in ("{{EXE}}", "{{PROMPT}}", "{{MODEL}}"):
        raise SystemExit(92)
for element in suffix:
    if element in ("{{EXE}}", "{{PROMPT}}"):
        raise SystemExit(92)
if plan.get("model_required"):
    print("MREQ=1")
print("EXE=" + exe)
for element in argv:
    print("ARG=" + element)
for element in suffix:
    print("MSFX=" + element)
for name in plan.get("env_passthrough") or []:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", name):
        raise SystemExit(92)
    print("ENVP=" + name)
print("OK=1")
PY
)
  [ "$PLAN_OK" = 1 ] || { echo "worker-sandbox: engine plan is invalid" >&2; exit 92; }
  # A group/other-writable plan can be rewritten between resolve and launch by any local
  # process; refuse it in every mode, logical included. Root ownership is asserted only on
  # the real-run path below — a /tmp logical controller cannot own files as uid 0.
  plan_mode_any="$(stat -c %a "$ENGINE_PLAN" 2>/dev/null || echo 777)"
  [ $((8#$plan_mode_any & 022)) -eq 0 ] \
    || { echo "worker-sandbox: engine plan must not be group/other-writable" >&2; exit 95; }
  # model.required is a launch precondition, not advice: without a model the engine would run
  # on its own configured default, off-alias and off-evidence.
  [ "$PLAN_MREQ" != 1 ] || [ -n "$MODEL" ] \
    || { echo "worker-sandbox: engine plan requires a model and none was given" >&2; exit 92; }
fi
build_plan_argv(){ # resolved-exe -> PLAN_RUN: exact-element placeholder substitution only
  local exe="$1" element
  PLAN_RUN=()
  for element in "${PLAN_ARGV[@]}"; do
    case "$element" in
      '{{EXE}}')    PLAN_RUN+=("$exe") ;;
      '{{PROMPT}}') PLAN_RUN+=("$PROMPT") ;;
      '{{MODEL}}')  PLAN_RUN+=("$MODEL") ;;
      *)            PLAN_RUN+=("$element") ;;
    esac
  done
  if [ -n "$MODEL" ]; then
    for element in "${PLAN_MODEL_SUFFIX[@]}"; do
      case "$element" in
        '{{MODEL}}') PLAN_RUN+=("$MODEL") ;;
        *)           PLAN_RUN+=("$element") ;;
      esac
    done
  fi
}
case "$ROLE" in implementer|verifier|gate) ;; *) echo "worker-sandbox: invalid worker role" >&2; exit 92;; esac
case "$NETWORK_POLICY" in provider-only|none) ;; *) echo "worker-sandbox: invalid network policy" >&2; exit 92;; esac
[ "$ENGINE:$ROLE" = gate:gate ] || { [ "$ENGINE" != gate ] && [ "$ROLE" != gate ]; } \
  || { echo "worker-sandbox: gate engine and role must be paired" >&2; exit 92; }
[ "$ENGINE" != gate ] || [ -n "$GATE_COMMAND" ] \
  || { echo "worker-sandbox: gate command is missing" >&2; exit 92; }
[ "$ROLE" != gate ] || [ "$NETWORK_POLICY" = none ] \
  || { echo "worker-sandbox: acceptance gates are offline; provider-only is invalid" >&2; exit 96; }
[ -d "$WT" ] || { echo "worker-sandbox: worktree is missing" >&2; exit 95; }
WT="$(realpath -e "$WT")"
HOST_CWD="$(realpath -e "$WT/${WORKDIR_PATH:-.}" 2>/dev/null || true)"
case "$HOST_CWD" in
  "$WT"|"$WT"/*) ;;
  *) echo "worker-sandbox: working directory escapes the worktree" >&2; exit 95 ;;
esac
SANDBOX_CWD="/workspace${HOST_CWD#$WT}"
READ_ONLY_HOSTS=(); READ_ONLY_DESTS=()
for item in "${READ_ONLY_PATHS[@]}"; do
  case "$item" in ''|/*|*..*) echo "worker-sandbox: invalid read-only worktree path" >&2; exit 95 ;; esac
  host="$(realpath -e "$WT/$item" 2>/dev/null || true)"
  case "$host" in
    "$WT"/*) READ_ONLY_HOSTS+=("$host"); READ_ONLY_DESTS+=("/workspace/${item#./}") ;;
    *) echo "worker-sandbox: read-only path escapes or is absent: $item" >&2; exit 95 ;;
  esac
done
[ -z "$EVIDENCE_DIR" ] || { mkdir -p "$EVIDENCE_DIR"; EVIDENCE_DIR="$(realpath -e "$EVIDENCE_DIR")"; }
export RECURSION_WORKER_ROLE="$ROLE"
[ -z "$EVIDENCE_DIR" ] || export RECURSION_EVIDENCE_DIR="$EVIDENCE_DIR"

case "${RECURSION_TEST_WORKER:-}" in
  logical)
    # Existing hermetic envelope tests exercise the logical scope gate, not the OS boundary.
    # This bypass is accepted only for disposable /tmp controllers and is never valid on /opt.
    case "$(realpath -m "$ROOT")" in /tmp/*) ;; *) echo "worker-sandbox: test bypass refused" >&2; exit 95;; esac
    if [ "$NETWORK_POLICY" = provider-only ] \
       && [ "${RECURSION_TEST_PROVIDER_NETWORK:-}" != logical ]; then
      echo "worker-sandbox: provider-only test capability is absent" >&2; exit 96
    fi
    case "$ENGINE" in
      gate)
        cd "$HOST_CWD"
        env -i HOME=/tmp USER=nobody LOGNAME=nobody LANG=C.UTF-8 \
          PATH=/usr/local/bin:/usr/bin:/bin bash -c "$GATE_COMMAND"
        ;;
      claude)
        command -v claude >/dev/null 2>&1 || exit 90
        cd "$WT"; claude -p "$PROMPT" --permission-mode acceptEdits \
          --allowedTools 'Bash(node:*)' ${MODEL:+--model "$MODEL"}
        ;;
      hermes)
        command -v hermes >/dev/null 2>&1 || exit 94
        cd "$WT"; hermes -z "$PROMPT" -m "${MODEL:-gpt-5.6-sol}" -t file,terminal --yolo \
          ${USAGE_FILE:+--usage-file "$USAGE_FILE"}
        ;;
      *)
        command -v "$PLAN_EXE" >/dev/null 2>&1 || exit 90
        # Declare the passthrough set so hermetic controls can verify the plan->env plumbing;
        # the real --setenv/EnvironmentFile wiring is exercised only on the box.
        for plan_env_name in "${PLAN_ENV_PASS[@]}"; do
          echo "worker-sandbox: env-passthrough $plan_env_name" >&2
        done
        build_plan_argv "$(command -v "$PLAN_EXE")"
        cd "$WT"; "${PLAN_RUN[@]}"
        ;;
    esac
    exit $?
    ;;
  ""|sandbox) ;;
  *) echo "worker-sandbox: invalid test mode" >&2; exit 95 ;;
esac

# provider-only is not a descriptive label. It requires a root-provisioned namespace whose
# firewall/routing policy is owned outside worker authority. There is deliberately no host-network
# fallback: absent capability means no worker.
PROVIDER_NETNS=/run/netns/recursion-provider-only
if [ "$NETWORK_POLICY" = provider-only ]; then
  [ -e "$PROVIDER_NETNS" ] && mountpoint -q "$PROVIDER_NETNS" \
    || { echo "worker-sandbox: provider-only network namespace capability is absent" >&2; exit 96; }
  netns_owner="$(stat -c %u "$PROVIDER_NETNS" 2>/dev/null || true)"
  netns_mode="$(stat -c %a "$PROVIDER_NETNS" 2>/dev/null || true)"
  [ "$netns_owner" = 0 ] && [ -n "$netns_mode" ] \
    && [ $((8#$netns_mode & 022)) -eq 0 ] \
    || { echo "worker-sandbox: provider network capability is not root-owned/read-only" >&2; exit 96; }
fi

[ "$(id -u)" = 0 ] || { echo "worker-sandbox: controller must be root" >&2; exit 95; }
command -v systemd-run >/dev/null 2>&1 \
  || { echo "worker-sandbox: systemd-run unavailable" >&2; exit 95; }
if [ -n "$ENGINE_PLAN" ]; then
  plan_owner="$(stat -c %u "$ENGINE_PLAN" 2>/dev/null || echo x)"
  plan_mode="$(stat -c %a "$ENGINE_PLAN" 2>/dev/null || echo 777)"
  [ "$plan_owner" = 0 ] && [ $((8#$plan_mode & 022)) -eq 0 ] \
    || { echo "worker-sandbox: engine plan must be root-owned and not group/other-writable" >&2; exit 95; }
fi
if [ "$NETWORK_POLICY" = provider-only ]; then
  bash "$ROOT/scripts/provider-network.sh" check >/dev/null \
    || { echo "worker-sandbox: provider-only capability probe failed" >&2; exit 96; }
fi

RUNNER_USER="${RECURSION_RUNNER_USER:-recursion-runner}"
if getent passwd "$RUNNER_USER" >/dev/null 2>&1; then
  RUNNER_UID="$(id -u "$RUNNER_USER")"; RUNNER_GID="$(id -g "$RUNNER_USER")"
  RUNNER_HOME="$(getent passwd "$RUNNER_USER" | cut -d: -f6)"
else
  # Disposable controls run before sysusers.d is deployed but still use a real non-root uid.
  case "$(realpath -m "$ROOT")" in
    /tmp/*) RUNNER_USER=nobody; RUNNER_UID="$(id -u nobody)"; RUNNER_GID="$(id -g nobody)"; RUNNER_HOME="" ;;
    *) echo "worker-sandbox: dedicated recursion-runner account is missing" >&2; exit 95 ;;
  esac
fi
[ "$RUNNER_UID" != 0 ] && [ "$RUNNER_GID" != 0 ] \
  || { echo "worker-sandbox: runner identity is privileged" >&2; exit 95; }

case "$ENGINE" in
  gate) EXE=""; EXE_REAL="" ;;
  claude) EXE="$(command -v claude 2>/dev/null || true)"; [ -n "$EXE" ] || exit 90 ;;
  hermes) EXE="$(command -v hermes 2>/dev/null || true)"; [ -n "$EXE" ] || exit 94 ;;
  *) EXE="$(command -v "$PLAN_EXE" 2>/dev/null || true)"; [ -n "$EXE" ] || exit 90 ;;
esac
[ "$ENGINE" = gate ] || EXE_REAL="$(realpath -e "$EXE")"

# The disposable worktree is the only writable project mount. A test stub outside /usr is copied
# in as one read-only executable and removed before the controller evaluates the diff.
chown -R "$RUNNER_UID:$RUNNER_GID" "$WT"
INNER_EXE="$EXE"
SANDBOX_BIN="$WT/.recursion-sandbox"
case "$ENGINE:$EXE_REAL" in
  gate:) ;;
  *:/usr/*) ;;
  *)
    rm -rf -- "$SANDBOX_BIN"
    install -d -o 0 -g "$RUNNER_GID" -m 0710 "$SANDBOX_BIN"
    install -o 0 -g "$RUNNER_GID" -m 0550 "$EXE_REAL" "$SANDBOX_BIN/$ENGINE"
    INNER_EXE="/workspace/.recursion-sandbox/$ENGINE"
    ;;
esac

safe_engine="${ENGINE//[^A-Za-z0-9_.-]/-}"
UNIT="recursion-worker-${safe_engine}-$$-${RANDOM}.service"
client_pid=""
stop_unit(){
  systemctl kill --kill-whom=all --signal=TERM "$UNIT" >/dev/null 2>&1 || true
  local i
  for i in $(seq 1 50); do
    systemctl is-active --quiet "$UNIT" 2>/dev/null || return 0
    sleep 0.1
  done
  systemctl kill --kill-whom=all --signal=KILL "$UNIT" >/dev/null 2>&1 || true
  systemctl stop "$UNIT" >/dev/null 2>&1 || true
  for i in $(seq 1 50); do
    systemctl is-active --quiet "$UNIT" 2>/dev/null || return 0
    sleep 0.1
  done
  echo "worker-sandbox: worker cgroup did not terminate" >&2
  return 1
}
cleanup(){
  stop_unit || true
  rm -rf -- "$SANDBOX_BIN"
  [ -z "$ENGINE_ENV_FILE" ] || rm -f -- "$ENGINE_ENV_FILE"
  chown -R 0:0 "$WT" 2>/dev/null || true
}
trap 'cleanup; exit 143' TERM INT
trap cleanup EXIT

# ProtectHome=yes makes /home inaccessible and prevents BindPaths under it (systemd.exec).
# When we bind RUNNER_HOME -> /home/runner, use tmpfs so the bind is visible.
PROTECT_HOME=yes
if [ "$ENGINE" != gate ] && [ -n "$RUNNER_HOME" ] && [ -d "$RUNNER_HOME" ]; then
  PROTECT_HOME=tmpfs
fi
RUN=(systemd-run --quiet --wait --collect --pipe --service-type=exec --unit "$UNIT"
  --uid "$RUNNER_USER" --gid "$RUNNER_GID" --working-directory "$SANDBOX_CWD"
  --property NoNewPrivileges=yes --property CapabilityBoundingSet=
  --property AmbientCapabilities= --property PrivateTmp=yes --property PrivateDevices=yes
  --property ProtectSystem=strict --property "ProtectHome=$PROTECT_HOME"
  --property ProtectKernelTunables=yes --property ProtectKernelModules=yes
  --property ProtectKernelLogs=yes --property ProtectControlGroups=yes
  --property RestrictSUIDSGID=yes --property LockPersonality=yes
  --property UMask=0077 --property KillMode=control-group
  --property "InaccessiblePaths=-/workspace/.env -/workspace/state -/workspace/CONTROL_PLANE.md -/workspace/ANCHOR.md -/workspace/CLAUDE.md /run /var/run"
  --property "ReadOnlyPaths=-/workspace/scripts -/workspace/.github"
  --setenv HOME=/home/runner --setenv USER="$RUNNER_USER" --setenv LOGNAME="$RUNNER_USER"
  --setenv LANG=C.UTF-8 --setenv PATH=/usr/local/bin:/usr/bin:/bin
  --setenv RECURSION_WORKER_ROLE="$ROLE")

# Model workers need only their checkout and provider executable. Gates additionally need the
# repository's read-only git metadata and controller-owned gate scripts/baselines, so they retain
# a read-only view of /opt while still running non-root under ProtectSystem=strict.
# /run and /var/run carry host Unix sockets (systemd's private bus, Docker, journald) and other
# confused deputies. Masking them per-arm here (and in the shared base above) keeps every arm from
# reaching host sockets; Claude's provider egress is the TCP loopback CONNECT proxy, never a socket.
if [ "$ENGINE" = gate ]; then
  # Git must remain readable to acceptance suites, but the suite is worker-authored code.
  # Protect the worktree pointer itself; ProtectSystem=strict separately keeps its controller
  # gitdir/refs read-only when that pointer resolves outside /workspace.
  RUN+=(--property "ReadOnlyPaths=-/workspace/.git")
elif [ "$ROLE" = verifier ]; then
  # Phase-5 independent verifier must inspect base..candidate via git, but must not reach /opt
  # controller gates (machine-gate evidence is already recorded). Keep .git read-only.
  RUN+=(--property "InaccessiblePaths=/opt /srv /run /var/run")
  RUN+=(--property "ReadOnlyPaths=-/workspace/.git")
else
  # Implementer and other model workers: no git, no /opt.
  RUN+=(--property "InaccessiblePaths=/opt /srv -/workspace/.git /run /var/run")
fi

if [ "$READ_ONLY_WORKTREE" = 1 ]; then
  RUN+=(--property "BindReadOnlyPaths=$WT:/workspace" --property ReadOnlyPaths=/workspace)
else
  RUN+=(--property "BindPaths=$WT:/workspace" --property ReadWritePaths=/workspace)
fi
for ((i=0; i<${#READ_ONLY_HOSTS[@]}; i++)); do
  RUN+=(--property "BindReadOnlyPaths=${READ_ONLY_HOSTS[$i]}:${READ_ONLY_DESTS[$i]}")
done
if [ -n "$EVIDENCE_DIR" ]; then
  chown -R "$RUNNER_UID:$RUNNER_GID" "$EVIDENCE_DIR"
  RUN+=(--property "BindPaths=$EVIDENCE_DIR:/evidence" --property ReadWritePaths=/evidence
        --setenv RECURSION_EVIDENCE_DIR=/evidence)
fi
if [ "$NETWORK_POLICY" = none ]; then
  RUN+=(--property PrivateNetwork=yes --property IPAddressDeny=any)
else
  RUN+=(--property "NetworkNamespacePath=$PROVIDER_NETNS")
fi
case "$ENGINE" in
  claude|cursor)
    # Provider request is the only intentional egress.  The namespace has no route or
    # non-loopback interface, so clearing these variables cannot fall back to the host network.
    RUN+=(--setenv HTTPS_PROXY=http://127.0.0.1:18080
          --setenv HTTP_PROXY=http://127.0.0.1:18080)
    if [ "$ENGINE" = claude ]; then
      RUN+=(--setenv CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1)
    fi
    ;;
esac
for name in RECURSION_VERIFICATION_COMMIT RECURSION_IMPLEMENTER_RUN_ID; do
  value="${!name:-}"; [ -z "$value" ] || RUN+=(--setenv "$name=$value")
done

if [ "$ENGINE" != gate ] && [ -n "$RUNNER_HOME" ] && [ -d "$RUNNER_HOME" ]; then
  RUN+=(--property "BindReadOnlyPaths=$RUNNER_HOME:/home/runner")
  # Cursor needs writable ~/.cursor (projects/cache). Keep auth via RO home (.config),
  # then override ~/.cursor with a WT-owned writable dir (later BindPaths wins).
  if [ "$ENGINE" = cursor ]; then
    # Keep Cursor CLI state OUT of the worktree so path-scope checks do not see it as
    # an out-of-boundary modify. Host path under recursion-runner-writable; bind over ~/.cursor.
    CURSOR_STATE="/var/lib/recursion-runner-writable/cursor-home"
    install -d -o "$RUNNER_UID" -g "$RUNNER_GID" -m 700 "$CURSOR_STATE"
    install -d -o "$RUNNER_UID" -g "$RUNNER_GID" -m 700 "$CURSOR_STATE/projects"
    install -d -o "$RUNNER_UID" -g "$RUNNER_GID" -m 700 "$CURSOR_STATE/projects/workspace"
    if [ -f "$RUNNER_HOME/.cursor/cli-config.json" ] && [ ! -f "$CURSOR_STATE/cli-config.json" ]; then
      install -o "$RUNNER_UID" -g "$RUNNER_GID" -m 600 "$RUNNER_HOME/.cursor/cli-config.json" "$CURSOR_STATE/cli-config.json"
    fi
    RUN+=(--property "BindPaths=$CURSOR_STATE:/home/runner/.cursor")
  fi
fi
if [ "${RECURSION_TEST_WORKER:-}" = sandbox ]; then
  case "$(realpath -m "$ROOT")" in /tmp/*) ;; *) echo "worker-sandbox: sandbox test env refused" >&2; exit 95;; esac
  [ -n "${STUB_ARM_SCRIPT:-}" ] && RUN+=(--setenv STUB_ARM_SCRIPT="$STUB_ARM_SCRIPT")
  for name in STUB_SECRET STUB_LEAK STUB_OUTSIDE STUB_UIDLOG STUB_PUBLISHER STUB_PUBLOG STUB_VERIFIER_SCRIPT; do
    value="${!name:-}"; [ -n "$value" ] && RUN+=(--setenv "$name=$value")
  done
  if [ -n "${STUB_STARTED:-}" ]; then
    : > "$STUB_STARTED"
    RUN+=(--setenv STUB_STARTED=/tmp/worker-started)
  fi
fi

case "$ENGINE" in
  gate)
    RUN+=(/usr/bin/env -i HOME=/tmp USER="$RUNNER_USER" LOGNAME="$RUNNER_USER" LANG=C.UTF-8 \
      PATH=/usr/local/bin:/usr/bin:/bin /bin/bash -c "$GATE_COMMAND")
    ;;
  claude)
    RUN+=(/usr/bin/env "$INNER_EXE" -p "$PROMPT" --permission-mode acceptEdits --allowedTools 'Bash(node:*)')
    [ -n "$MODEL" ] && RUN+=(--model "$MODEL")
    ;;
  hermes)
    # The mount target must not live under /run: this arm masks /run and /var/run as
    # InaccessiblePaths (twice, base set + non-gate set), and systemd drops any mount entry
    # shadowed by an inaccessible path — the bind would be silently discarded and hermes
    # could never write the file. PrivateTmp=yes gives the unit its own writable /tmp, so
    # the usage file is bound there instead.
    if [ -n "$USAGE_FILE" ]; then
      : > "$USAGE_FILE"; chown "$RUNNER_UID:$RUNNER_GID" "$USAGE_FILE"
      RUN+=(--property "BindPaths=$USAGE_FILE:/tmp/recursion-worker-usage.json")
    fi
    RUN+=(/usr/bin/env "$INNER_EXE" -z "$PROMPT" -m "${MODEL:-gpt-5.6-sol}" -t file,terminal --yolo)
    [ -n "$USAGE_FILE" ] && RUN+=(--usage-file /tmp/recursion-worker-usage.json)
    ;;
  *)
    # Plugin engines (D-015): exec exactly the admitted plan argv; no shell, no interpolation.
    # Passthrough values ride a root-owned 0600 EnvironmentFile, never the systemd-run command
    # line, so credentials stay out of /proc/*/cmdline and `systemctl show`.
    if [ "${#PLAN_ENV_PASS[@]}" -gt 0 ]; then
      ENGINE_ENV_FILE="$(mktemp /tmp/recursion-engine-env.XXXXXX)" || exit 95
      chmod 0600 "$ENGINE_ENV_FILE"
      for plan_env_name in "${PLAN_ENV_PASS[@]}"; do
        plan_env_value="${!plan_env_name:-}"
        [ -n "$plan_env_value" ] || continue
        case "$plan_env_value" in
          *$'\n'*|*$'\r'*) echo "worker-sandbox: engine env value contains a newline" >&2; exit 92 ;;
        esac
        printf '%s=%s\n' "$plan_env_name" "$plan_env_value" >> "$ENGINE_ENV_FILE"
      done
      RUN+=(--property "EnvironmentFile=$ENGINE_ENV_FILE")
    fi
    build_plan_argv "$INNER_EXE"
    RUN+=(/usr/bin/env "${PLAN_RUN[@]}")
    ;;
esac

"${RUN[@]}" & client_pid=$!
if wait "$client_pid"; then rc=0; else rc=$?; fi
client_pid=""
exit "$rc"
