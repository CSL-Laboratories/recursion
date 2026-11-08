"""Engine-user invariants for the later cross-uid path (CSL-738 B9, Lucas's Nov 8 call).

The engine user gets no linger and no systemd user manager; the operator keeps
linger. Nothing here enables, disables, masks or starts anything: the probe only
reads `loginctl`, `systemctl is-active` and the presence of one socket path.

This module does not choose the engine spawn path. B7 (CSL-734) picks it. It only
refuses launch wrappers that would open a PAM/logind session for the engine user
and so start `user@<engine uid>.service` again.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

# What this package actually does today (PR #324). Recorded, not chosen here.
SPAWN_PATH = {
    'path': ("same-uid transient unit in the operator's user manager: "
             "systemd-run --user --unit=recursion-run-<id>.service --service-type=exec "
             "--property=KillMode=control-group --property=Delegate=yes "
             "--property=RemainAfterExit=yes -- <argv>"),
    'engine_uid': 'none (RECURSION_ENGINE_USER is refused; Nov 11 is same-uid)',
    'pam_or_logind_session': 'none (no su, runuser, sudo, login or PAMName=)',
    'cross_uid_owner': 'B7 (CSL-734) picks the cross-uid spawn path; not implemented here',
}

USER = re.compile(r'[a-z_][a-z0-9_-]{0,31}')
# loginctl's answer when logind has no record of the user (not logged in, no linger).
NO_SESSION = ('is not logged in or lingering', 'org.freedesktop.login1.NoSuchUser')

# Launchers that run a PAM session stack (pam_systemd) or open a logind session, which
# would start the engine user's manager. Refused whatever their flags: which of them
# runs pam_systemd depends on the distribution's PAM config, so none is trusted.
SESSION_LAUNCHERS = frozenset({
    'su', 'sudo', 'sudoedit', 'runuser', 'login', 'machinectl', 'pkexec', 'doas',
    'ssh', 'sshd', 'systemd-run0', 'run0',
})
PAM_PROPERTY = re.compile(r'PAMName=', re.IGNORECASE)


class EngineManagerPresent(RuntimeError):
    """The engine user may have a user manager; refuse cross-uid runs."""


class SessionLaunchRefused(ValueError):
    """The launch would open a PAM/logind session."""


def refuse_session_launch(argv):
    """Refuse argv that would open a PAM/logind session (e.g. `su -`, `runuser -l`)."""
    if not argv or not all(isinstance(a, str) for a in argv):
        raise SessionLaunchRefused('argv must be a non-empty list of strings')
    name = os.path.basename(argv[0])
    if name in SESSION_LAUNCHERS:
        raise SessionLaunchRefused(f'{name} opens a PAM/logind session; refused')
    if name == 'systemd-run':
        # Inside systemd-run, only options before the command matter; a property
        # PAMName= opens a PAM session for the unit.
        head = argv[1:argv.index('--')] if '--' in argv else argv[1:]
        for arg in head:
            if PAM_PROPERTY.search(arg):
                raise SessionLaunchRefused(f'systemd-run {arg} opens a PAM session; refused')


def check_engine_no_user_manager(user, uid, *, operator_uid=None, call=subprocess.run,
                                 runtime_root=Path('/run/user')):
    """Read-only B9 pass probe for the engine user. Returns evidence; raises on doubt.

    Pass needs all three: `loginctl show-user <engine> -p Linger` says `no` or logind
    has no such user; `systemctl is-active user@<uid>.service` says `inactive`;
    `<runtime_root>/<uid>/systemd/private` does not exist. Anything else, including
    a probe that cannot run, fails closed. Masking user@<uid>.service is not part of
    the bar: it is proposed hardening, unmeasured.
    """
    if not isinstance(user, str) or not USER.fullmatch(user):
        raise ValueError('invalid engine user name')
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise ValueError('engine uid must be a positive int (never root)')
    operator_uid = os.getuid() if operator_uid is None else operator_uid
    if uid == operator_uid:
        raise EngineManagerPresent('engine uid equals the operator uid, which keeps linger')
    evidence = {}
    try:
        linger = call(['loginctl', 'show-user', user, '--property=Linger', '--value'],
                      check=False, text=True, capture_output=True, timeout=15)
        active = call(['systemctl', 'is-active', f'user@{uid}.service'],
                      check=False, text=True, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EngineManagerPresent(f'probe could not run: {type(exc).__name__}') from exc

    out, err = (linger.stdout or '').strip(), (linger.stderr or '').strip()
    if linger.returncode == 0 and out == 'no':
        evidence['linger'] = 'no'
    elif linger.returncode != 0 and any(marker in err for marker in NO_SESSION):
        evidence['linger'] = 'no such user session'
    else:
        raise EngineManagerPresent(f'engine Linger not confirmed off (rc={linger.returncode}, '
                                   f'out={out[:40]!r})')

    state = (active.stdout or '').strip()
    if state != 'inactive' or active.returncode == 0:
        raise EngineManagerPresent(f'user@{uid}.service is {state[:40]!r}, not inactive')
    evidence['user_manager'] = 'inactive'

    socket = Path(runtime_root) / str(uid) / 'systemd' / 'private'
    if os.path.lexists(socket):
        raise EngineManagerPresent(f'{socket} exists: an engine user manager is listening')
    evidence['manager_socket'] = 'absent'
    return evidence


# Where each engine keeps its own CLI login when no session D-Bus (Secret Service) is
# reachable. Recursion never opens, reads, copies, stores or ships these: the paths
# are recorded only where repository evidence names them, otherwise "unconfirmed".
SIGNIN_STORES = {
    'claude_code': {
        'store_path': 'unconfirmed',
        'store_evidence': 'none in the repository',
        'without_session_bus': 'unconfirmed',
    },
    'codex': {
        'store_path': '~/.codex/auth.json',
        'store_evidence': ('recursion #321 core/recursion_core/subscription_routes/policy.py '
                           'ENGINE_STORE_ENTRIES (cites CSL-712 H3: Hermes imports '
                           '~/.codex/auth.json)'),
        'without_session_bus': 'unconfirmed (whether Codex prefers a keyring when one is '
                               'reachable is not in repository evidence)',
    },
}
SIGNED_IN_RUN = 'pending founder-attended signed-in run (not performed; no provider call)'
SESSION_BUS_NAMES = ('DBUS_SESSION_BUS_ADDRESS', 'DBUS_STARTER_ADDRESS', 'DBUS_STARTER_BUS_TYPE')


def signin_without_session_bus(engine_env, uid, runtime_root=Path('/run/user')):
    """Offline B9 check: the engine env reaches no session bus and no user manager.

    It never opens a credential store and never reports sign-in success: the verdict
    is at best `pending`, because only a founder-attended signed-in run of each engine
    can confirm that its login still works with the bus absent.
    """
    leaked = sorted(name for name in SESSION_BUS_NAMES if name in engine_env)
    if leaked:
        raise EngineManagerPresent(f'engine env names a session bus: {", ".join(leaked)}')
    runtime = engine_env.get('XDG_RUNTIME_DIR')
    buses = [Path(runtime_root) / str(uid) / 'bus']
    if runtime:
        buses.append(Path(runtime) / 'bus')  # sd-bus falls back to $XDG_RUNTIME_DIR/bus
    for bus in buses:
        if os.path.lexists(bus):
            raise EngineManagerPresent(f'{bus} exists: a session bus is reachable for the engine')
    return {
        'session_bus': 'absent from engine env',
        'xdg_runtime_dir': 'unset' if runtime is None else 'set (not a bus address)',
        'engines': {name: dict(row, signed_in_run=SIGNED_IN_RUN)
                    for name, row in SIGNIN_STORES.items()},
        'verdict': 'pending',
    }
