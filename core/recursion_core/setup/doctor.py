"""A machine-readable report of this machine, with no daemon required.

``run(probes)`` returns a ``doctor_report``: ``generated_at`` and ``checks``. Each check is
``id``, ``subject``, ``state`` (ok, warn, fail or unknown), ``detail`` and ``fix``. Unknown
is never reported as ok. A probe that raises is unknown. ``KeyboardInterrupt`` and
``SystemExit`` propagate. A second caller gets its own report.

``probes`` is a mapping of check id to a callable, or None for the live host. A callable
returns one check body, or a list of them (``engine.binary``, one per engine). A body is
``{"state", "detail", "fix", "subject"}``. The live checks are ``platform.linux``,
``interpreter.free_threaded``, ``systemd.user_manager``, ``systemd.linger`` (via
``daemon.check_operator_linger``), ``daemon.unit``, ``daemon.socket``, ``store.ledger``,
``store.breaker`` (via ``plugin_security.stores.check_store``, which does not open the
file) and ``engine.binary`` (``shutil.which`` and ``--version`` with a timeout, no model
call, and an empty home so the command is not pointed at a sign-in file).

Two checks report the event store's ground. ``store.sqlite`` is the SQLite library this
interpreter loaded (``sqlite3.sqlite_version``): fail on a version with the WAL reset bug, as
SQLite's own pages name it (3.51.2 and older, except 3.44.6 and later in 3.44 and 3.50.7 and
later in 3.50, and the withdrawn 3.52.0), ok on every other. ``store.filesystem`` is
the file-system type of the state folder (after ``XDG_STATE_HOME``, links followed), read
from ``/proc/self/mountinfo`` by the longest mount point that holds it: fail on 9p, drvfs,
v9fs, overlay, nfs, nfs4, cifs or smb3, or when the folder lies under the WSL automount
root ``/mnt/``; unknown when the type cannot be read. They run on the live host, and under
a probe map only when the map names them. They report; nothing is refused because of them.

On the live host only, three more checks run. ``plugin.config`` is the plugin config
folder. ``plugin.revocation`` is the revocation store (``check_store``, which does not
open it). ``plugin.installed`` is one row per installed plugin: its id, version, trust,
key id, registry label and the loader's developer checks (``PluginLoader.doctor``).
No plugin code runs. A plugin config folder that is not the operator's, or that group
or world can write, reports every plugin refused. A revocation store is reported lost
only when a side file of ``revoked.sqlite3`` remains; the breaker directory alone is
not that signal, and this report does not create a replacement store. A legacy
revocation store is reported ``revocation_store_legacy`` and is not rewritten. A
symlinked ``plugin-security`` directory is refused. The report adds no key to
``install.json`` and no manifest field, and it does not change the daemon.

Recursion installs no engine. A missing engine binary is unknown, not ok.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import os
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

# None: today's account probes. A string: those probes read this directory and
# do not ask the password database, loginctl or systemctl.
_ROOT = contextvars.ContextVar("recursion_doctor_root", default=None)

_IDS = (
    "platform.linux",
    "interpreter.free_threaded",
    "systemd.user_manager",
    "systemd.linger",
    "daemon.unit",
    "daemon.socket",
    "store.ledger",
    "store.breaker",
    "engine.binary",
)
# The event store's ground. Live, and under a probe map only when the map names them, so a
# map written for ``_IDS`` gives the report it gave before.
_GROUND_IDS = ("store.sqlite", "store.filesystem")
_STATES = ("ok", "warn", "fail", "unknown")
# The WAL reset bug, as SQLite's own pages describe it: present from 3.7.0 to 3.51.2, repaired
# in 3.51.3 and later and in the backports 3.44.6 and 3.50.7. Per line (major, minor): the first
# patch release that carries the repair, or None when no release of that line is said to carry
# it; a line newer than every row carries it, any other line has the bug. Append-only: a later
# backport is a new row.
_SQLITE_WAL_RESET_REPAIRED = {
    (3, 44): 6,
    (3, 50): 7,
    (3, 51): 3,
    # 3.52.0 was withdrawn; SQLite's pages do not say it carries the repair (3.53.0 lists it).
    (3, 52): None,
}
# The first SQLite with WAL mode. An older one cannot have the WAL reset bug, and SQLite's pages
# name none of them unaffected.
_SQLITE_FIRST_WAL = (3, 7, 0)
_MOUNTINFO = "/proc/self/mountinfo"
# The WSL automount root. The base has no WSL probe module to read it from.
_WSL_AUTOMOUNT = "/mnt/"
_UNSAFE_FILESYSTEMS = frozenset({"9p", "drvfs", "v9fs", "overlay", "nfs", "nfs4", "cifs", "smb3"})
_GROUND_STEP = "set XDG_STATE_HOME to a folder on a local Linux file system such as ext4"
_UNIT_NAME = "recursion-daemon.service"
_STORE_DIR = "plugin-security"
_LEDGER_FILE = "ledger.sqlite3"
_BREAKER_FILE = "breaker.sqlite3"
_MANAGER_TIMEOUT_S = 5
_VERSION_TIMEOUT_S = 3
# Command names for the engines in routes.contract.ENGINES. An engine with no name
# is reported unknown, so a later engine is not silently treated as present.
_COMMANDS = {
    "claude_code": "claude",
    "codex": "codex",
    "pi": "pi",
    "hermes": "hermes",
    "opencode": "opencode",
}
_ENGINE_NEXT = "install the engine the operator configured; Recursion installs no engine"
# Live host only. A probe map stands in for ``_IDS`` and does not invent plugin rows.
_PLUGIN_IDS = ("plugin.config", "plugin.revocation", "plugin.installed")
_TRUST_CODES = frozenset({
    "signature_valid_trusted_key",
    "signature_invalid",
    "signer_not_on_trusted_list",
    "signer_key_revoked",
})
_FOLDER_STEP = "run `chmod go-w` on the plugin config folder"
_FILE_STEP = "the plugin file must be this user's regular file and not writable by group or world"
_STORE_STEP = "restore the revocation store; a missing store after install refuses every plugin"
_VERIFY_STEP = "install the signature backend named in plugin_security/README.md"


def run(probes=None, *, root=None) -> dict:
    """Build a doctor report. ``probes`` replaces every live check; None runs them.

    The plugin checks run only when ``probes`` is None. They report the live host and
    do not run plugin code.

    ``root`` is an optional absolute directory the live account probes read. None
    keeps today's probes: the unit file comes from the password database, and linger
    and the user manager ask this account. A root confines those three probes to
    that directory.
    """

    if root is None:
        return _collect(probes)
    token = _ROOT.set(root)
    try:
        return _collect(probes)
    finally:
        _ROOT.reset(token)


def _collect(probes) -> dict:
    checks = []
    for check_id in _IDS:
        checks.extend(_invoke(check_id, probes))
    for check_id in _GROUND_IDS:
        if probes is None or check_id in probes:
            checks.extend(_invoke(check_id, probes))
    if probes is None:
        for check_id in _PLUGIN_IDS:
            checks.extend(_invoke(check_id, None))
    return {"generated_at": _now(), "checks": checks}


def _injected_home():
    """``(asked, home)``. ``asked`` is true when ``run`` was given a root.

    A root that is not an absolute path does not fall through to the account.
    """

    root = _ROOT.get()
    if root is None:
        return False, None
    if type(root) is not str or not os.path.isabs(root):
        return True, None
    return True, root


def exit_code(report: dict) -> int:
    """1 when any check is fail, otherwise 0. Unknown and warn do not by themselves."""

    checks = report.get("checks") if isinstance(report, dict) else None
    if type(checks) is not list:
        return 1
    for check in checks:
        if isinstance(check, dict) and check.get("state") == "fail":
            return 1
    return 0


def text(report: dict) -> str:
    """One human line per check, plus a ``next:`` line when the check names a step."""

    lines = []
    checks = report.get("checks") if isinstance(report, dict) else None
    for check in checks or []:
        if not isinstance(check, dict):
            continue
        subject = check.get("subject")
        name = check.get("id", "")
        if isinstance(subject, str) and subject:
            name = f"{name} ({subject})"
        lines.append(f"{name}: {check.get('state')}  {check.get('detail')}")
        step = check.get("fix")
        if isinstance(step, str) and step:
            lines.append(f"  next: {step}")
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _invoke(check_id: str, probes) -> list:
    try:
        if probes is None:
            raw = _LIVE[check_id]()
        else:
            raw = probes[check_id]()
    except Exception as exc:
        return [_row(check_id, "unknown", f"{type(exc).__name__}: {exc}", None, None)]
    rows = raw if type(raw) is list else [raw]
    return [_coerce(check_id, item) for item in rows]


def _coerce(check_id: str, item) -> dict:
    if type(item) is not dict:
        return _row(check_id, "unknown", "the probe returned no check", None, None)
    state = item.get("state")
    detail = item.get("detail")
    subject = item.get("subject", None)
    step = item.get("fix", None)
    if state not in _STATES:
        state = "unknown"
    if type(detail) is not str:
        state = "unknown"
        detail = "the probe returned no detail"
    if subject is not None and type(subject) is not str:
        state = "unknown"
        subject = None
    if step is not None and type(step) is not str:
        state = "unknown"
        step = None
    return _row(check_id, state, detail, subject, step)


def _row(check_id: str, state: str, detail: str, subject, step) -> dict:
    if state not in _STATES:
        state = "unknown"
    return {"id": check_id, "subject": subject, "state": state, "detail": detail, "fix": step}


def _body(state: str, detail: str, step=None, subject=None) -> dict:
    return {"state": state, "detail": detail, "fix": step, "subject": subject}


def _now() -> str:
    moment = dt.datetime.now(dt.timezone.utc)
    millis = moment.microsecond // 1000
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


def _one_line(text: str, limit: int = 200) -> str:
    return " ".join(text.split())[:limit]


def _probe_linux():
    if sys.platform.startswith("linux"):
        return _body("ok", f"sys.platform is {sys.platform}")
    return _body("fail", f"this system is {sys.platform}", "recursiond runs on Linux")


def _probe_interpreter():
    free = (sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 14)
            and bool(sysconfig.get_config_var("Py_GIL_DISABLED")))
    gil = "GIL off" if free else "with the GIL"
    shown = f"{sys.implementation.name} {sys.version_info[0]}.{sys.version_info[1]} {gil}"
    if free:
        return _body("ok", shown)
    return _body("fail", shown, "install free-threaded CPython 3.14 (python3.14t) and run its recursiond")


def _probe_user_manager():
    asked, home = _injected_home()
    if asked:
        if home is None:
            return _body("unknown", "the doctor root is not an absolute path")
        path = os.path.join(home, ".config", "systemd", "user")
        try:
            os.lstat(path)
        except FileNotFoundError:
            return _body("unknown", "systemd user manager is not in the doctor root")
        except OSError as exc:
            return _body("unknown", f"{type(exc).__name__}: {exc}")
        return _body("unknown", "systemd user manager was not asked; the doctor root is set")
    try:
        done = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True, text=True, timeout=_MANAGER_TIMEOUT_S,
            stdin=subprocess.DEVNULL, check=False,
        )
    except FileNotFoundError:
        return _body("fail", "systemctl is not on PATH", "install systemd and start a user manager")
    except subprocess.TimeoutExpired:
        return _body("unknown", "systemctl --user is-system-running timed out")
    except OSError as exc:
        return _body("unknown", f"{type(exc).__name__}: {exc}")
    lines = (done.stdout or "").strip().splitlines()
    word = lines[0].strip() if lines else ""
    if word == "running":
        return _body("ok", "systemd user manager is running")
    if word == "degraded":
        return _body("warn", "systemd user manager is degraded", "see `systemctl --user --failed`")
    if word in ("maintenance", "stopping", "offline"):
        return _body("fail", f"systemd user manager is {word}",
                     "start a user session; `loginctl enable-linger` keeps the user manager")
    if word in ("initializing", "starting"):
        return _body("unknown", f"systemd user manager is {word}")
    err = (done.stderr or "").strip().splitlines()
    shown = word or (err[0] if err else "no answer")
    return _body("unknown", f"systemd user manager: {_one_line(shown)}")


def _probe_linger():
    asked, home = _injected_home()
    if asked:
        if home is None:
            return _body("unknown", "the doctor root is not an absolute path")
        try:
            os.lstat(home)
        except OSError as exc:
            return _body("unknown", f"{type(exc).__name__}: {exc}")
        return _body("unknown", "linger was not asked; the doctor root is set")
    import getpass

    from recursion_core.daemon import check_operator_linger

    try:
        user = getpass.getuser()
    except Exception as exc:
        return _body("unknown", f"{type(exc).__name__}: {exc}")
    try:
        check_operator_linger(user)
    except RuntimeError as exc:
        text = str(exc)
        if "linger probe failed" in text:
            return _body("unknown", _one_line(text))
        return _body("fail", _one_line(text), f"run `loginctl enable-linger {user}`")
    except Exception as exc:
        return _body("unknown", f"{type(exc).__name__}: {exc}")
    return _body("ok", f"Linger=yes for {user}")


def _account_home():
    import pwd

    try:
        home = pwd.getpwuid(os.geteuid()).pw_dir
    except (KeyError, OSError):
        return None
    if type(home) is not str or not os.path.isabs(home):
        return None
    return home


def _probe_unit():
    asked, home = _injected_home()
    if asked:
        if home is None:
            return _body("unknown", "the doctor root is not an absolute path")
    else:
        home = _account_home()
        if home is None:
            return _body("unknown", "the password database gives no home for this user")
    path = os.path.join(home, ".config", "systemd", "user", _UNIT_NAME)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return _body("fail", f"{path} is not there",
                     "run `recursiond install`, then the systemctl --user commands it prints")
    except OSError as exc:
        return _body("unknown", f"{type(exc).__name__}: {exc}")
    if stat.S_ISLNK(info.st_mode):
        return _body("fail", f"{path} is a symlink", "replace it with the unit `recursiond install` writes")
    if not stat.S_ISREG(info.st_mode):
        return _body("fail", f"{path} is not a regular file",
                     "replace it with the unit `recursiond install` writes")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        return _body("fail", f"{path} is owned by another user", "run `recursiond install` as this user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return _body("fail", f"{path} is writable by group or world", "run `chmod go-w` on the unit")
    return _body("ok", path)


def _probe_socket():
    if not sys.platform.startswith("linux"):
        return _body("fail", f"no control socket on {sys.platform}", "recursiond runs on Linux")
    try:
        from recursion_core.control.client import socket_path
        path = socket_path(os.environ)
    except Exception as exc:
        return _body("unknown", f"{type(exc).__name__}: {exc}")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return _body("fail", f"{path} is not there",
                     "the daemon is not running; its control socket appears when it is")
    except OSError as exc:
        return _body("unknown", f"{type(exc).__name__}: {exc}")
    if not stat.S_ISSOCK(info.st_mode):
        return _body("fail", f"{path} is not a socket", "remove it and start the daemon")
    import socket
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        try:
            sock.connect(path)
        except BlockingIOError:
            return _body("unknown", f"{path} did not answer")
        except (ConnectionRefusedError, FileNotFoundError):
            return _body("fail", f"nothing is listening at {path}", "the daemon is not running")
        except OSError as exc:
            return _body("unknown", f"{type(exc).__name__}: {exc}")
    finally:
        sock.close()
    return _body("ok", path)


def _state_store_dir():
    raw = os.environ.get("XDG_STATE_HOME") or ""
    if raw:
        if not os.path.isabs(raw):
            return None
        base = raw
    else:
        base = str(Path.home() / ".local" / "state")
    return os.path.join(base, "recursion", _STORE_DIR)


def _probe_store(filename: str, magic: str):
    folder = _state_store_dir()
    if folder is None:
        return _body("unknown", "XDG_STATE_HOME is not an absolute path")
    path = os.path.join(folder, filename)
    from recursion_core.plugin_security.errors import Refused
    from recursion_core.plugin_security.stores import check_store
    try:
        check_store(path, magic)
    except Refused as exc:
        return _body("fail", f"{exc.code}: {exc.detail}",
                     "the installer creates this store; run `recursiond install`")
    except Exception as exc:
        return _body("unknown", f"{type(exc).__name__}: {exc}")
    return _body("ok", path)


def _probe_ledger():
    from recursion_core.plugin_security.stores import LEDGER_MAGIC
    return _probe_store(_LEDGER_FILE, LEDGER_MAGIC)


def _probe_breaker():
    from recursion_core.plugin_security.stores import BREAKER_MAGIC
    return _probe_store(_BREAKER_FILE, BREAKER_MAGIC)


def _probe_sqlite():
    import sqlite3

    found = sqlite3.sqlite_version
    try:
        version = tuple(int(part) for part in found.split("."))
    except (AttributeError, TypeError, ValueError):
        return _body("unknown", f"the SQLite version {found!r} could not be read")
    line, patch = version[:2], (version + (0, 0))[2]
    if line in _SQLITE_WAL_RESET_REPAIRED:
        first = _SQLITE_WAL_RESET_REPAIRED[line]
        repaired = first is not None and patch >= first
    else:
        repaired = line > max(_SQLITE_WAL_RESET_REPAIRED)
    if repaired:
        return _body("ok", f"SQLite {found} is loaded")
    withdrawn = _SQLITE_WAL_RESET_REPAIRED.get(line, 0) is None
    before_wal = line + (patch,) < _SQLITE_FIRST_WAL
    if withdrawn or before_wal:
        # Only versions that pass, named from the table: the lines newer than every row, then each
        # line's first repaired release; no range that holds a version this row fails.
        top = max(_SQLITE_WAL_RESET_REPAIRED)
        passing = [f"{top[0]}.{top[1] + 1}.0 or later"] + [
            f"{major}.{minor}.{first} or later in {major}.{minor}"
            for (major, minor), first in sorted(_SQLITE_WAL_RESET_REPAIRED.items(), reverse=True)
            if first is not None]
        way_out = f"{', '.join(passing[:-1])}, or {passing[-1]}"
        if withdrawn:
            reason = "a withdrawn release that SQLite's pages do not say is repaired"
        else:
            reason = (f"older than the first SQLite with WAL ({'.'.join(map(str, _SQLITE_FIRST_WAL))}) and older "
                      "than every version SQLite names as unaffected")
        return _body("fail", f"SQLite {found} is loaded and is {reason}; the event store needs {way_out}",
                     f"run recursiond on an interpreter built with SQLite {way_out}")
    # Every repaired version, named from the table: the newest line first, then each backport.
    rows = sorted((line + (first,) for line, first in _SQLITE_WAL_RESET_REPAIRED.items() if first is not None),
                  reverse=True)
    newest, backports = ".".join(map(str, rows[0])), [".".join(map(str, row)) for row in rows[1:]]
    needs = [f"{newest} or later"] + [f"{name} or later in {name.rsplit('.', 1)[0]}" for name in backports]
    return _body("fail",
                 f"SQLite {found} is loaded and has the WAL reset bug; the event store needs "
                 f"{', '.join(needs[:-1])}, or {needs[-1]}",
                 f"run recursiond on an interpreter built with SQLite {newest} or later, or with "
                 + " or ".join(backports))


def _unescape_mount(field: str) -> str:
    """A mountinfo field with its octal escapes (``\\040`` for a space) turned back into characters."""

    import re

    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), field)


def _mount_type(path: str):
    """The type of the longest mount point holding ``path`` in ``_MOUNTINFO``, or None.

    Of two mounts on the same point, the later line is the one on top.
    """

    try:
        with open(_MOUNTINFO, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    longest = -1
    kind = None
    for line in lines:
        fields = line.split(" ")
        try:
            dash = fields.index("-", 6)
            point = _unescape_mount(fields[4])
            fstype = fields[dash + 1]
        except (ValueError, IndexError):
            continue
        if not point.startswith("/") or not fstype:
            continue
        holds = path == point or path.startswith(point.rstrip("/") + "/")
        if holds and len(point) >= longest:
            longest = len(point)
            kind = fstype
    return kind


def _probe_filesystem():
    store = _state_store_dir()
    if store is None:
        return _body("unknown", "XDG_STATE_HOME is not an absolute path")
    folder = os.path.realpath(os.path.dirname(store))
    kind = _mount_type(folder)
    shown = kind if kind else "a type that could not be read"
    automount = _WSL_AUTOMOUNT.rstrip("/") + "/"
    if folder.startswith(automount):
        return _body("fail", f"{folder} lies under the WSL automount root {automount} ({shown})",
                     _GROUND_STEP)
    if kind is None:
        return _body("unknown", f"the file-system type of {folder} could not be read from {_MOUNTINFO}")
    if kind in _UNSAFE_FILESYSTEMS:
        return _body("fail", f"{folder} is on {kind}", _GROUND_STEP)
    return _body("ok", f"{folder} is on {kind}")


def _denied(path) -> bool:
    """True when ``path`` is a sign-in path this report must not open."""

    if type(path) is not str:
        return False
    try:
        home = str(Path.home())
    except (OSError, RuntimeError):
        return False
    from recursion_core.setup.deny import is_denied

    return is_denied(path, home)


def _config_root():
    raw = os.environ.get("XDG_CONFIG_HOME") or ""
    if raw:
        if not os.path.isabs(raw):
            return None
        return raw
    try:
        base = str(Path.home() / ".config")
    except (OSError, RuntimeError):
        return None
    if not os.path.isabs(base):
        return None
    return base


def _child_names(folder):
    """Directory and link names in ``folder``, or None when the folder cannot be listed.

    Symlinks are names only. Their targets are not opened.
    """

    names = []
    try:
        with os.scandir(folder) as scan:
            for entry in scan:
                try:
                    directory = entry.is_dir(follow_symlinks=False)
                    link = entry.is_symlink()
                except OSError:
                    continue
                if directory or link:
                    names.append(entry.name)
    except OSError:
        return None
    names.sort()
    return names


def _folder_status():
    """The plugin config folder: absent, ok, bad (untrusted) or unknown.

    Returns ``(kind, path, detail, step, names)``. ``names`` is the plugin directory
    names when the folder itself could be listed, and None when it could not.
    """

    root = _config_root()
    if root is None:
        return "unknown", None, "the plugin config folder could not be named", None, None
    folder = os.path.join(root, "recursion", "plugins")
    if _denied(folder):
        return "unknown", folder, "the plugin config folder is not read", None, None
    try:
        info = os.lstat(folder)
    except FileNotFoundError:
        return "absent", folder, "no plugin config folder; no plugin is installed", None, None
    except OSError as exc:
        return "unknown", folder, f"{type(exc).__name__}: {exc}", None, None
    if stat.S_ISLNK(info.st_mode):
        return ("bad", folder, "plugin_directory_untrusted: the plugin config folder is a symlink",
                _FOLDER_STEP, None)
    if not stat.S_ISDIR(info.st_mode):
        return ("bad", folder, "plugin_directory_untrusted: the plugin config folder is not a directory",
                _FOLDER_STEP, None)
    names = _child_names(folder)
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        return ("bad", folder, "plugin_directory_untrusted: the plugin config folder is owned by another user",
                _FOLDER_STEP, names)
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return ("bad", folder,
                "plugin_directory_untrusted: the plugin config folder is writable by group or world",
                _FOLDER_STEP, names)
    if names is None:
        return "unknown", folder, "the plugin config folder could not be listed", None, None
    return "ok", folder, folder, None, names


_REVOCATION_FILE = "revoked.sqlite3"
# The same suffixes ``stores._SIDE_FILES`` checks beside a store. A leftover one is
# evidence the revocation file itself existed. ``breaker.sqlite3`` is not.
_REVOCATION_SIDES = ("-wal", "-shm", "-journal")


def _revocation_side_present(folder) -> bool:
    for suffix in _REVOCATION_SIDES:
        side = os.path.join(folder, _REVOCATION_FILE + suffix)
        try:
            if os.path.lexists(side):
                return True
        except OSError:
            continue
    return False


def _legacy_refusal(path):
    """The legacy refusal, or None when this open is not that case.

    Read-only. A store that is merely unreadable is left to the plugin rows,
    which already report that refusal. This never migrates and never creates.
    """

    from recursion_core.plugin_security.errors import Refused
    from recursion_core.plugin_security.manifest import TrustStore

    try:
        trust = TrustStore({}, revocation_store=path, read_only=True)
    except Refused as exc:
        if exc.code == "revocation_store_legacy":
            return exc
        return None
    trust.close()
    return None


def _revocation():
    """``(path or None, state, detail, step)`` for the revocation store.

    ``path`` is set only when the store can be classified. ``fail`` means every
    installed plugin is reported refused: the directory is a symlink, a side file
    shows the revocation file is gone, the store is legacy, or ``check_store``
    refused it. ``unknown`` means the revocation file was never there. The doctor
    does not create a store and does not migrate one.
    """

    folder = _state_store_dir()
    if folder is None:
        return None, "unknown", "XDG_STATE_HOME is not an absolute path", None
    path = os.path.join(folder, _REVOCATION_FILE)
    if _denied(folder) or _denied(path):
        return None, "unknown", "the revocation store is not read", None
    if not os.path.lexists(path):
        try:
            info = os.lstat(folder)
        except FileNotFoundError:
            return None, "unknown", "revocation store is not there yet", None
        except OSError as exc:
            return None, "unknown", f"{type(exc).__name__}: {exc}", None
        if stat.S_ISLNK(info.st_mode):
            return (None, "fail",
                    "revocation_store_untrusted: plugin-security is a symlink",
                    _STORE_STEP)
        if stat.S_ISDIR(info.st_mode) and _revocation_side_present(folder):
            return (None, "fail",
                    "revocation_store_missing: revoked.sqlite3 is gone and a side file of it remains",
                    _STORE_STEP)
        return None, "unknown", "revocation store is not there yet", None
    from recursion_core.plugin_security.errors import Refused
    from recursion_core.plugin_security.stores import REVOCATION_MAGIC, check_store

    try:
        check_store(path, REVOCATION_MAGIC)
    except Refused as exc:
        detail = exc.code if not exc.detail else f"{exc.code}: {exc.detail}"
        return None, "fail", detail, _STORE_STEP
    except Exception as exc:
        return None, "unknown", f"{type(exc).__name__}: {exc}", None
    legacy = _legacy_refusal(path)
    if legacy is not None:
        detail = legacy.code if not legacy.detail else f"{legacy.code}: {legacy.detail}"
        return None, "fail", detail, _STORE_STEP
    return path, "ok", path, None


def _refused_rows(names, text, step):
    if not names:
        return [_body("fail", text, step, None)]
    return [_body("fail", text, step, name) for name in names]


def _peek_plugin(source, plugins, parse_manifest, refused):
    """Plugin id and version from the manifest bytes, or the directory name. No code runs."""

    name = os.path.basename(source)
    try:
        if not plugins._owned_directory(source):
            return name, "unknown"
        raw = plugins._read_owned(Path(source) / "manifest.json", plugins.MAX_MANIFEST_BYTES)
        parsed = parse_manifest(raw)
    except (OSError, refused, plugins.PluginRefused, ValueError, TypeError):
        return name, "unknown"
    return parsed.plugin_id, parsed.version


def _one_plugin(loader, source, plugins, parse_manifest, refused, key_id):
    name = os.path.basename(source)
    try:
        if not plugins._owned_directory(source):
            raise plugins.PluginRefused(
                "plugin_directory_untrusted",
                "must be an operator-owned directory, not group or world writable")
        if _denied(source):
            return _body("unknown", "the plugin directory is not read", None, name)
        manifest = plugins._read_owned(Path(source) / "manifest.json", plugins.MAX_MANIFEST_BYTES)
        artifact = plugins._read_owned(Path(source) / "plugin.py", plugins.MAX_ARTIFACT_BYTES)
        signature = plugins._read_owned(Path(source) / "manifest.sig", 4096, missing_ok=True)
    except plugins.PluginRefused as exc:
        step = _FOLDER_STEP if exc.code == "plugin_directory_untrusted" else _FILE_STEP
        detail = exc.code if not exc.detail else f"{exc.code}: {exc.detail}"
        return _body("fail", f"refused: {detail}", step, name)
    signature = None if signature is None else signature.decode("ascii", "replace").strip()
    findings = loader.doctor(manifest, signature, artifact, None)
    version = "unknown"
    plugin_id = name
    developer_key = None
    try:
        parsed = parse_manifest(manifest)
    except refused:
        parsed = None
    else:
        version = parsed.version
        plugin_id = parsed.plugin_id
        developer_key = parsed.developer_key
    trust = "unknown"
    label = "unlisted"
    codes = []
    refused_code = ""
    warned = False
    for finding in findings:
        if type(finding) is not dict:
            continue
        code = finding.get("code")
        if type(code) is str:
            codes.append(code)
        severity = finding.get("severity")
        if severity == "refuse" and type(code) is str:
            refused_code = code
        elif severity == "warn":
            warned = True
        if code == "registry_label":
            shown = finding.get("detail")
            if type(shown) is str and shown:
                label = shown.split(" ", 1)[0]
        if code in _TRUST_CODES:
            shown = finding.get("detail")
            if type(shown) is str and shown:
                trust = shown
    key = "none"
    if trust == "signed" and type(developer_key) is str:
        try:
            key = key_id(developer_key)
        except refused:
            key = "none"
    state = "fail" if refused_code else "warn" if warned else "ok"
    detail = f"version {version}; trust {trust}; key_id {key}; label {label}; checks {','.join(codes)}"
    step = None
    if refused_code:
        detail = f"refused: {refused_code}; {detail}"
        step = "the loader's checks refused this plugin; no plugin code ran"
    return _body(state, _one_line(detail, 500), step, plugin_id)


def _probe_plugin_config():
    kind, _path, detail, step, _names = _folder_status()
    if kind in ("absent", "ok"):
        return _body("ok", detail)
    if kind == "bad":
        return _body("fail", detail, step)
    return _body("unknown", detail, step)


def _probe_revocation():
    _path, state, detail, step = _revocation()
    return _body(state, detail, step)


def _probe_plugins():
    """One row per installed plugin. Reads manifests and calls ``PluginLoader.doctor``.

    Plugin code is not run. An untrusted config folder, or a revocation store that does
    not pass, reports every plugin refused and does not read the plugin files. The
    revocation store is opened read-only.
    """

    kind, folder, detail, step, names = _folder_status()
    if kind in ("absent", "unknown"):
        return []
    if kind == "bad":
        return _refused_rows(names, f"refused: {detail}", step)
    if not names:
        return []
    import recursion_core.daemon.plugins as plugins
    from recursion_core.plugin_security import CoreHealth, PluginLoader, Refused, TrustStore, parse_manifest
    from recursion_core.plugin_security.signing import backend_available, key_id

    rev_path, rev_state, rev_detail, rev_step = _revocation()
    if rev_state == "fail":
        return _refused_rows(names, f"refused: {rev_detail}", rev_step)
    sources = [os.path.join(folder, name) for name in names]
    if not backend_available():
        rows = []
        for source in sources:
            plugin_id, version = _peek_plugin(source, plugins, parse_manifest, Refused)
            rows.append(_body(
                "fail",
                "refused: plugin_verification_unavailable: "
                f"{plugins.CRYPTOGRAPHY_MISSING}; version {version}",
                _VERIFY_STEP,
                plugin_id,
            ))
        return rows
    try:
        install = plugins.read_install(folder)
    except plugins.PluginRefused as exc:
        shown = exc.code if not exc.detail else f"{exc.code}: {exc.detail}"
        return _refused_rows(names, f"refused: {shown}", _FILE_STEP)
    try:
        trust = TrustStore(install["trusted_keys"], revocation_store=rev_path, read_only=True)
    except Refused as exc:
        shown = exc.code if not exc.detail else f"{exc.code}: {exc.detail}"
        return _refused_rows(names, f"refused: {shown}", _STORE_STEP)
    loader = PluginLoader(trust, install["grants"], install["reserved"], CoreHealth())
    try:
        rows = []
        for source in sources:
            try:
                rows.append(_one_plugin(loader, source, plugins, parse_manifest, Refused, key_id))
            except Exception as exc:
                rows.append(_body("unknown", f"{type(exc).__name__}: {exc}", None, os.path.basename(source)))
        return rows
    finally:
        trust.close()


def _probe_engines():
    from recursion_core.routes.contract import ENGINES

    rows = []
    for engine in ENGINES:
        command = _COMMANDS.get(engine)
        if command is None:
            rows.append(_body("unknown", "no command name is known for this engine", None, engine))
            continue
        found = shutil.which(command)
        if not found:
            rows.append(_body("unknown", "binary not found on PATH", _ENGINE_NEXT, engine))
            continue
        rows.append(_version(engine, found))
    return rows


def _version(engine: str, binary: str):
    scratch = tempfile.mkdtemp(prefix="recursion-doctor-")
    try:
        os.chmod(scratch, 0o700)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": scratch,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        try:
            done = subprocess.run(
                [binary, "--version"],
                capture_output=True, text=True, timeout=_VERSION_TIMEOUT_S,
                stdin=subprocess.DEVNULL, env=env, check=False,
            )
        except subprocess.TimeoutExpired:
            return _body("unknown", f"{binary} --version timed out", _ENGINE_NEXT, engine)
        except OSError as exc:
            return _body("unknown", f"{type(exc).__name__}: {exc}", None, engine)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if done.returncode != 0:
        return _body("unknown", f"{binary} --version exited {done.returncode}", _ENGINE_NEXT, engine)
    lines = (done.stdout or done.stderr or "").strip().splitlines()
    shown = _one_line(lines[0]) if lines else "no version text"
    return _body("ok", shown or "no version text", None, engine)


_LIVE = {
    "platform.linux": _probe_linux,
    "interpreter.free_threaded": _probe_interpreter,
    "systemd.user_manager": _probe_user_manager,
    "systemd.linger": _probe_linger,
    "daemon.unit": _probe_unit,
    "daemon.socket": _probe_socket,
    "store.ledger": _probe_ledger,
    "store.breaker": _probe_breaker,
    "engine.binary": _probe_engines,
    "store.sqlite": _probe_sqlite,
    "store.filesystem": _probe_filesystem,
    "plugin.config": _probe_plugin_config,
    "plugin.revocation": _probe_revocation,
    "plugin.installed": _probe_plugins,
}
