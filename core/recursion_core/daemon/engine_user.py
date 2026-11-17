"""Engine-user invariants for the later cross-uid path (CSL-738 B9, Lucas's Nov 8 call).

The engine user gets no linger and no systemd user manager; the operator keeps
linger. Nothing here enables, disables, masks or starts anything: the probe only
reads `loginctl`, `systemctl is-active` and the presence of one socket path.

This module does not choose the daemon's spawn path. The recorded path stays the
same-uid transient unit (SPAWN_PATH). The daemon still refuses RECURSION_ENGINE_USER
and does not call spawn_as_engine_user: that refusal is the default until a real
engine user exists. The function drops uid, gid and supplementary groups only when
the caller injects an id switch; it never calls the operating system's setuid.
It also refuses launch wrappers that would open a PAM/logind session for the engine
user and so start `user@<engine uid>.service` again.

Same-uid operator runs (`Daemon.start`) are not filtered by these guards. The
daemon does not enforce engine-manager absence today.
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
    'cross_uid_owner': ('B7 (CSL-734) records the cross-uid spawn; spawn_as_engine_user '
                        'drops ids only when the caller injects a switch, and the daemon '
                        'still refuses RECURSION_ENGINE_USER'),
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


# Credential-shaped environment names. Matched by a whole '_' segment so a name such as
# MONKEY is kept and ANTHROPIC_API_KEY is not. The command environment is an allowlist;
# these names are refused even when the caller copied them into the engine identity.
_CREDENTIAL_PARTS = frozenset({
    'KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'PASSWD', 'CREDENTIAL', 'COOKIE',
})
_ENGINE_ENV_NAMES = ('PATH', 'LANG', 'LC_ALL', 'TERM', 'TZ', 'TMPDIR')


class EngineSpawnRefused(RuntimeError):
    """The cross-uid spawn did not start the command."""


class EngineUserUnconfigured(EngineSpawnRefused):
    """No engine user is configured. Refusal is the default."""


class EngineSwitchFailed(EngineSpawnRefused):
    """The id switch failed. The command was not started as the owner."""


def _credential_name(name):
    if name in SESSION_BUS_NAMES or name == 'SSH_AUTH_SOCK':
        return True
    return any(part in _CREDENTIAL_PARTS for part in name.split('_'))


def _positive_id(value, label):
    """A uid or gid the switch may target. Zero and booleans are refused."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value >= 2**32:
        raise ValueError(f'{label} must be a positive int (never root)')
    return value


def _group_tuple(value):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError('engine groups must be a list of positive ints')
    return tuple(_positive_id(item, 'supplementary group') for item in value)


def _parse_identity(identity):
    if not isinstance(identity, dict):
        raise ValueError('engine identity must be a mapping')
    user = identity.get('user')
    if not isinstance(user, str) or not USER.fullmatch(user):
        raise ValueError('invalid engine user name')
    home = identity.get('home')
    if (not isinstance(home, str) or not home.startswith('/') or home == '/'
            or home.endswith('/') or '\x00' in home):
        raise ValueError('engine home must be an absolute directory')
    environ = identity.get('environ', {})
    if environ is None:
        environ = {}
    if not isinstance(environ, dict):
        raise ValueError('engine environ must be a mapping')
    return {
        'user': user,
        'uid': _positive_id(identity.get('uid'), 'engine uid'),
        'gid': _positive_id(identity.get('gid'), 'engine gid'),
        'groups': _group_tuple(identity.get('groups')),
        'home': home,
        'environ': environ,
    }


def _path_inside(path, parent):
    if not isinstance(path, str) or not isinstance(parent, str) or not parent.startswith('/'):
        return False
    root = parent.rstrip('/') or '/'
    if root == '/':
        return path.startswith('/')
    return path == root or path.startswith(root + '/')


def _store_mode(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 0o777:
        raise ValueError('store mode must be a file mode')
    return value


def _store_readable(store, uid, gid, groups):
    """Unix read bit for one simulated store. No path is opened."""
    if not isinstance(store, dict):
        raise ValueError('store record must be a mapping')
    mode = _store_mode(store.get('mode'))
    owner_uid = _positive_id(store.get('owner_uid'), 'store owner uid')
    owner_gid = _positive_id(store.get('owner_gid'), 'store owner gid')
    if uid == owner_uid:
        return bool(mode & 0o400)
    if gid == owner_gid or owner_gid in groups:
        return bool(mode & 0o040)
    return bool(mode & 0o004)


def _store_map(stores, uid, gid, groups):
    if stores is None:
        return {}
    if isinstance(stores, (str, bytes)) or not isinstance(stores, (list, tuple)):
        raise ValueError('stores must be a list of records')
    reads = {}
    for store in stores:
        if not isinstance(store, dict):
            raise ValueError('store record must be a mapping')
        path = store.get('path')
        if not isinstance(path, str) or not path.startswith('/') or '\x00' in path:
            raise ValueError('store path must be an absolute path')
        reads[path] = _store_readable(store, uid, gid, groups)
    return reads


def _owner_secrets(owner_env):
    secrets = set()
    for name, value in owner_env.items():
        if isinstance(name, str) and isinstance(value, str) and value and _credential_name(name):
            secrets.add(value)
    return secrets


def _tainted(value, secrets, owner_home, store_paths):
    if (not isinstance(value, str) or value == '' or '\x00' in value
            or '\n' in value or '\r' in value):
        return True
    # Exact or embedded. Short values are not scanned, so LANG=C is kept.
    if any(secret and len(secret) >= 8 and secret in value for secret in secrets):
        return True
    if owner_home and _path_inside(value, owner_home):
        return True
    return any(path and path in value for path in store_paths)


def _engine_environment(identity, owner_env, owner_stores):
    """Allowlisted engine environment. Owner credential names and values are absent."""
    owner_env = owner_env or {}
    source = identity['environ']
    # Values copied onto the identity under a credential name are secrets too.
    secrets = _owner_secrets(owner_env) | _owner_secrets(source)
    owner_home = owner_env.get('HOME') if isinstance(owner_env.get('HOME'), str) else None
    store_paths = [store.get('path') for store in owner_stores or () if isinstance(store, dict)]
    env = {
        'HOME': identity['home'],
        'USER': identity['user'],
        'LOGNAME': identity['user'],
    }
    for name in _ENGINE_ENV_NAMES:
        if _credential_name(name):
            continue
        value = source.get(name)
        if _tainted(value, secrets, owner_home, store_paths):
            continue
        env[name] = value
    if 'PATH' not in env:
        env['PATH'] = '/usr/bin'
    return env


def _read_back(id_switch, uid, gid, groups):
    try:
        got_uid = id_switch.getuid()
        got_gid = id_switch.getgid()
        got_groups = id_switch.getgroups()
    except OSError as exc:
        raise EngineSwitchFailed('uid switch failed; command not started') from exc
    if isinstance(got_groups, (str, bytes)) or not isinstance(got_groups, (list, tuple)):
        raise EngineSwitchFailed('uid switch failed; command not started')
    same_groups = (
        len(got_groups) == len(groups)
        and all(not isinstance(item, bool) and isinstance(item, int) for item in got_groups)
        and set(got_groups) == set(groups))
    if (isinstance(got_uid, bool) or not isinstance(got_uid, int) or got_uid != uid
            or isinstance(got_gid, bool) or not isinstance(got_gid, int) or got_gid != gid
            or not same_groups):
        raise EngineSwitchFailed('uid switch failed; command not started')
    return got_uid, got_gid


def spawn_as_engine_user(argv, *, identity=None, id_switch=None, start=None,
                         owner_env=None, owner_uid=None, owner_stores=None,
                         own_stores=None):
    """Start argv as the engine user, or refuse without starting it.

    The default is a refusal: no identity, no injected id switch, or no start
    seam means the command does not run. The daemon does not call this. A real
    operating-system switch is not armed here; tests pass a simulated switch.

    Supplementary groups are set first, then the gid, then the uid. If any call
    fails, or the ids read back are not the engine ids, the command is not
    started and the owner identity is not used as a fallback. The environment
    passed to start is the engine allowlist, never the owner's credential names.
    An owner store the engine ids could read also refuses the start.
    """
    if identity is None or id_switch is None or start is None:
        raise EngineUserUnconfigured('no engine user is configured; cross-uid spawn refused')
    parsed = _parse_identity(identity)
    # A session launcher is refused before any id call, so su never becomes the switch.
    refuse_session_launch(argv)
    if owner_uid is not None and _positive_id(owner_uid, 'owner uid') == parsed['uid']:
        raise EngineSpawnRefused('engine uid equals the owner uid; command not started')
    owner_home = None
    if isinstance(owner_env, dict):
        home = owner_env.get('HOME')
        if isinstance(home, str):
            owner_home = home
    if owner_home and _path_inside(parsed['home'], owner_home):
        raise EngineSpawnRefused('engine home is inside the owner home; command not started')

    # setgroups and setgid must run before setuid: afterwards they are refused by the kernel.
    try:
        id_switch.setgroups(parsed['groups'])
    except OSError as exc:
        raise EngineSwitchFailed('uid switch failed; command not started') from exc
    try:
        id_switch.setgid(parsed['gid'])
    except OSError as exc:
        raise EngineSwitchFailed('uid switch failed; command not started') from exc
    try:
        id_switch.setuid(parsed['uid'])
    except OSError as exc:
        raise EngineSwitchFailed('uid switch failed; command not started') from exc

    uid, gid = _read_back(id_switch, parsed['uid'], parsed['gid'], parsed['groups'])
    env = _engine_environment(parsed, owner_env if isinstance(owner_env, dict) else {},
                              owner_stores)
    stores = _store_map(owner_stores, uid, gid, parsed['groups'])
    if any(stores.values()):
        raise EngineSpawnRefused('engine identity can read an owner store; command not started')
    own = _store_map(own_stores, uid, gid, parsed['groups'])
    creds = (uid, gid, parsed['groups'])
    start(list(argv), env, creds)
    return {
        'uid': uid,
        'gid': gid,
        'groups': parsed['groups'],
        'env': env,
        'stores': stores,
        'own_stores': own,
    }
