"""Engine-user invariants for the later cross-uid path (CSL-738 B9, Lucas's Nov 8 call).

The engine user gets no linger and no systemd user manager; the operator keeps
linger. Nothing here enables, disables, masks or starts anything: the probe only
reads `loginctl`, `systemctl is-active` and the presence of one socket path.

This module does not choose the engine spawn path. B7 (CSL-734) picks it. It only
provides a guard that refuses launch wrappers that would open a PAM/logind session
for the engine user and so start `user@<engine uid>.service` again.

Nothing in the daemon calls these yet: the cross-uid path is not implemented and
`RECURSION_ENGINE_USER` is refused, so the daemon does not enforce engine-manager
absence today. Same-uid operator runs (`Daemon.start`) are not filtered by them.
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


# systemd-run's options, from ``systemd-run --help`` on this host. A long name may
# be written as any unambiguous prefix, the way getopt_long accepts it. Short
# options cluster, and an option that takes an argument consumes the rest of
# the element (``-qpPAMName=login`` is ``-q`` then ``-p PAMName=login``).
_LONG_WITH_ARG = (
    'host', 'machine', 'unit', 'property', 'description', 'slice',
    'expand-environment', 'service-type', 'uid', 'gid', 'nice',
    'working-directory', 'setenv', 'path-property', 'socket-property',
    'on-active', 'on-boot', 'on-startup', 'on-unit-active', 'on-unit-inactive',
    'on-calendar', 'timer-property',
)
_LONG_FLAGS = (
    'help', 'version', 'no-ask-password', 'user', 'scope', 'slice-inherit',
    'no-block', 'remain-after-exit', 'wait', 'send-sighup', 'same-dir',
    'pty', 'pipe', 'quiet', 'collect', 'shell', 'on-timezone-change',
    'on-clock-change',
)
_SHORT_WITH_ARG = frozenset('HMupE')  # -H -M -u -p -E
_SHORT_FLAGS = frozenset('hrdtPqGS')
_LONG_NAMES = _LONG_WITH_ARG + _LONG_FLAGS


def _lines_of(arg: str) -> list[str]:
    """Split on LF and CR only. A later line of the same element is its own assignment."""
    lines: list[str] = []
    buf: list[str] = []
    for ch in arg:
        if ch in '\n\r':
            lines.append(''.join(buf))
            buf = []
        else:
            buf.append(ch)
    lines.append(''.join(buf))
    return lines


def _long_option(name: str) -> str | None:
    """The one long option ``name`` names, or None when it is unknown or ambiguous."""
    folded = name.lower()
    exact = [item for item in _LONG_NAMES if item == folded]
    if exact:
        return exact[0]
    matches = [item for item in _LONG_NAMES if item.startswith(folded) and folded]
    if len(matches) == 1:
        return matches[0]
    return None


def _assignment_is_pam(value: str) -> bool:
    """True when the property key, not a searched value, is PAMName."""
    return value.split('=', 1)[0].lower() == 'pamname'


def _element_sets_pam(text: str) -> bool:
    if text.startswith('--'):
        body = text[2:]
        name, sep, value = body.partition('=')
        resolved = _long_option(name)
        if resolved == 'property' and sep:
            return _assignment_is_pam(value)
        return False
    if text.startswith('-') and len(text) > 1 and text[1].isalpha():
        index = 1
        while index < len(text):
            letter = text[index]
            index += 1
            if letter in _SHORT_WITH_ARG:
                attached = text[index:]
                if letter == 'p' and attached:
                    return _assignment_is_pam(attached)
                return False
            if letter in _SHORT_FLAGS:
                continue
            return False
        return False
    if '=' in text:
        return _assignment_is_pam(text)
    return False


def _is_pam_name_property(arg: str) -> bool:
    """True when this argv element sets the unit property PAMName.

    The property key is compared, not searched. ``PAMName=`` inside another
    property's value (an ``Environment=`` assignment, for example) does not
    open a PAM session. A clustered short option, an unambiguous prefix of
    ``--property``, and a PAMName assignment on a later line of the same
    element do. The value is never copied into a refusal.
    """
    return any(_element_sets_pam(line) for line in _lines_of(arg) if line)


class EngineManagerPresent(RuntimeError):
    """The engine user may have a user manager; refuse cross-uid runs."""


class SessionLaunchRefused(ValueError):
    """The launch would open a PAM/logind session."""


def _runtime_path_present(path: Path) -> bool:
    """Whether ``path`` is present. Only ENOENT is absence.

    ``os.path.lexists`` turns EACCES into False, so a runtime directory the
    operator cannot search was reported as a missing manager socket or bus.
    Any other OSError fails closed: the path was not shown to be absent.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise EngineManagerPresent(
            f'runtime path cannot be inspected ({type(exc).__name__})') from exc
    return True


def refuse_session_launch(argv):
    """Refuse argv whose executable is a known session launcher (su, runuser -l, ...) or systemd-run with a PAM property.

    Wrappers such as env are not inspected: the check uses the base name of argv[0] only, after a trailing slash is stripped, so ``env su -`` is not a session launcher here.
    For the later cross-uid engine spawn (B7, CSL-734), not for same-uid operator runs.
    """
    if (not isinstance(argv, (list, tuple)) or not argv
            or not all(isinstance(a, str) for a in argv)):
        raise SessionLaunchRefused('argv must be a non-empty list of strings')
    # A trailing slash is not part of the launcher name (`su/` is `su`).
    name = os.path.basename(argv[0].rstrip('/\\'))
    if name in SESSION_LAUNCHERS:
        raise SessionLaunchRefused(f'{name} opens a PAM/logind session; refused')
    if name == 'systemd-run':
        # Inside systemd-run, only options before the command matter; a property
        # PAMName= opens a PAM session for the unit.
        head = argv[1:argv.index('--')] if '--' in argv else argv[1:]
        for arg in head:
            if _is_pam_name_property(arg):
                raise SessionLaunchRefused('systemd-run PAMName property opens a PAM session; refused')


def check_engine_no_user_manager(user, uid, *, operator_uid=None, call=subprocess.run,
                                 runtime_root=Path('/run/user')):
    """Read-only B9 pass probe for the engine user. Returns evidence; raises on doubt.

    Pass needs all three: `loginctl show-user <engine> -p Linger` says `no` or logind
    has no such user; `systemctl is-active user@<uid>.service` says `inactive` and exits 3;
    `<runtime_root>/<uid>/systemd/private` does not exist. Anything else, including
    a probe that cannot run, fails closed. Masking user@<uid>.service is not part of
    the bar: it is proposed hardening, unmeasured.
    """
    if not isinstance(user, str) or not USER.fullmatch(user):
        raise ValueError('invalid engine user name')
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise ValueError('engine uid must be a positive int (never root)')
    if operator_uid is None:
        if not hasattr(os, 'getuid'):  # Windows: no uid to compare, so no pass
            raise EngineManagerPresent('platform_unsupported: no os.getuid to name the operator uid')
        operator_uid = os.getuid()
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
    # `systemctl is-active` prints inactive and exits 3. A probe killed by a
    # signal (returncode -9, or 137 when a wrapper stores 128+signal) can leave
    # the same text and must not attest that the manager is absent.
    rc = active.returncode
    # 3.0 == 3, and bool is an int, so only a real int 3 attests inactive.
    if state != 'inactive' or type(rc) is not int or rc != 3:
        raise EngineManagerPresent(
            f'user@{uid}.service is {state[:40]!r} (exit {rc}), not inactive (exit 3)')
    evidence['user_manager'] = 'inactive'

    socket = Path(runtime_root) / str(uid) / 'systemd' / 'private'
    if _runtime_path_present(socket):
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
    """Offline B9 check: the engine env names no session bus and no bus socket exists; the user-manager check is check_engine_no_user_manager.

    It never opens a credential store and never reports sign-in success: the verdict
    is at best `pending`, because only a founder-attended signed-in run of each engine
    can confirm that its login still works with the bus absent.
    """
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise ValueError('engine uid must be a positive int (never root)')
    leaked = sorted(name for name in SESSION_BUS_NAMES if name in engine_env)
    if leaked:
        raise EngineManagerPresent(f'engine env names a session bus: {", ".join(leaked)}')
    runtime = engine_env.get('XDG_RUNTIME_DIR')
    buses = [Path(runtime_root) / str(uid) / 'bus']
    if runtime:
        buses.append(Path(runtime) / 'bus')  # sd-bus falls back to $XDG_RUNTIME_DIR/bus
    for bus in buses:
        if _runtime_path_present(bus):
            raise EngineManagerPresent(f'{bus} exists: a session bus is reachable for the engine')
    return {
        'session_bus': 'absent from engine env',
        'xdg_runtime_dir': 'unset' if runtime is None else 'set (not a bus address)',
        'engines': {name: dict(row, signed_in_run=SIGNED_IN_RUN)
                    for name, row in SIGNIN_STORES.items()},
        'verdict': 'pending',
    }
