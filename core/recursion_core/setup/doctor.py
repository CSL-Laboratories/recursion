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

Recursion installs no engine. A missing engine binary is unknown, not ok.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

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
_STATES = ("ok", "warn", "fail", "unknown")
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


def run(probes=None) -> dict:
    """Build a doctor report. ``probes`` replaces every live check; None runs them."""

    checks = []
    for check_id in _IDS:
        checks.extend(_invoke(check_id, probes))
    return {"generated_at": _now(), "checks": checks}


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
}
