#!/bin/bash
# Evidence wrapper for a box with no systemd PID 1 and no logind (CSL-738).
#
# Run as the operator inside a private mount namespace that already has a private /run,
# $XDG_RUNTIME_DIR and a delegated cgroup v2 subtree. It starts a session bus and a real
# `systemd --user` manager, runs evidence_e2e.py against them, and stops what it started.
# Operator Linger cannot be read here (no logind), so the harness is told to stub it:
# "host precondition stubbed: operator Linger (no logind on box)". Product code is not
# changed by that; the stub is a file on the harness PATH only.
#
# Usage: evidence_ns.sh [output directory]
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-/dev/shm/nov11/out/evidence-S12}"
mkdir -p "$OUT"
: "${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR must be set}"

# The delegated cgroup this script was started in. systemd --user moves every process in it to
# init.scope and may enable controllers on it; that is undone on exit so the next run can enter.
ROOT_CG="/sys/fs/cgroup$(sed -n 's/^0:://p' /proc/self/cgroup)"

DBUS_PID="$(dbus-daemon --session --address="unix:path=$XDG_RUNTIME_DIR/bus" --fork --nopidfile --print-pid)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
/lib/systemd/systemd --user >"$OUT/systemd-user.log" 2>&1 &
SYSTEMD_PID=$!

stop_all() {
    systemctl --user exit >/dev/null 2>&1 || kill -TERM "$SYSTEMD_PID" 2>/dev/null
    for _ in $(seq 1 50); do kill -0 "$SYSTEMD_PID" 2>/dev/null || break; sleep 0.2; done
    kill -KILL "$SYSTEMD_PID" 2>/dev/null
    [ -n "$DBUS_PID" ] && kill -TERM "$DBUS_PID" 2>/dev/null
    for controller in $(cat "$ROOT_CG/cgroup.subtree_control" 2>/dev/null); do
        echo "-$controller" >"$ROOT_CG/cgroup.subtree_control" 2>/dev/null \
            || echo "note: could not disable $controller on $ROOT_CG" >&2
    done
}
trap stop_all EXIT

state=""
for _ in $(seq 1 150); do
    state="$(systemctl --user is-system-running 2>/dev/null)"
    [ "$state" = "running" ] && break
    sleep 0.2
done
if [ "$state" != "running" ]; then
    echo "systemd --user did not reach running (state: $state)" >&2
    exit 2
fi

# The output directory may be on a noexec mount; the stand-in engine and the stub go under the
# private runtime directory, which disappears with this namespace.
python3.13 -B "$HERE/evidence_e2e.py" --out "$OUT" --linger-stubbed \
    --exec-dir "$XDG_RUNTIME_DIR/recursion-evidence" 2>&1 | tee "$OUT/harness.log"
exit "${PIPESTATUS[0]}"
