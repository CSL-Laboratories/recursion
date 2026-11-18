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

On the live host only, three more checks run. ``plugin.config`` is the plugin config
folder. ``plugin.revocation`` is the revocation store (``check_store``, which does not
open it). ``plugin.installed`` is one row per installed plugin: its id, version, trust,
key id, registry label and the loader's developer checks (``PluginLoader.doctor``).
No plugin code runs. A plugin config folder that is not the operator's, or that group
or world can write, reports every plugin refused. A revocation store that is gone
after its folder exists reports every plugin refused, and this report does not create
a replacement store. The report adds no key to ``install.json`` and no manifest field,
and it does not change the daemon.

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


def run(probes=None) -> dict:
    """Build a doctor report. ``probes`` replaces every live check; None runs them.

    The plugin checks run only when ``probes`` is None. They report the live host and
    do not run plugin code.
    """

    checks = []
    for check_id in _IDS:
        checks.extend(_invoke(check_id, probes))
    if probes is None:
        for check_id in _PLUGIN_IDS:
            checks.extend(_invoke(check_id, None))
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


def _revocation():
    """``(path or None, state, detail, step)`` for the revocation store.

    ``path`` is set only when ``check_store`` passes. ``fail`` means the store folder
    exists and the store does not pass, so every installed plugin is reported refused.
    ``unknown`` means the store was never created; classification then uses no revoked keys.
    """

    folder = _state_store_dir()
    if folder is None:
        return None, "unknown", "XDG_STATE_HOME is not an absolute path", None
    path = os.path.join(folder, "revoked.sqlite3")
    if _denied(folder) or _denied(path):
        return None, "unknown", "the revocation store is not read", None
    if not os.path.lexists(path):
        try:
            present = os.path.isdir(folder) and not os.path.islink(folder)
        except OSError as exc:
            return None, "unknown", f"{type(exc).__name__}: {exc}", None
        if present:
            return (None, "fail",
                    "revocation_store_missing: the store is gone after its folder was installed",
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
    not pass after its folder exists, reports every plugin refused and does not read
    the plugin files.
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
        trust = TrustStore(install["trusted_keys"], revocation_store=rev_path)
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
    "plugin.config": _probe_plugin_config,
    "plugin.revocation": _probe_revocation,
    "plugin.installed": _probe_plugins,
}
