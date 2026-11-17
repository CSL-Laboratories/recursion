"""What Recursion read of local tool setup, with no secret values in the record.

``recursiond observe`` reports Claude, Codex, Cursor, git and the project instruction
files. Each file row is path, exists, size, sha256, an owner-only flag and a state of
``ok``, ``unknown`` or ``absent``. Allow-listed keys are the model, the approval mode,
the sandbox mode, and MCP server names and commands. Environment values, header values
and command arguments are not copied.

Sign-in stores named by ``deny.py`` are never opened, including through a symlink or a
hard link. ``~/.claude.json`` is not opened either: it can hold a sign-in beside settings,
so a present file is ``unknown``. A file mode without the owner-read bit (a mode-000
``config.toml``) is ``unknown``, not ``absent``. Git records whether ``user.name`` and
``user.email`` are present, not their values. Login is the exit code of ``claude auth
status`` or ``codex login status`` only; the vendor command's own output is discarded.
Cursor's sign-in command is not run: its store path is not established.

The record is one JSON object, mode 0600, at
``$XDG_STATE_HOME/recursion/observe/record.json`` (``$HOME/.local/state`` when the
variable is unset). ``run`` builds the report and writes nothing. ``command`` writes the
record and prints the same object.
"""

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tomllib

from recursion_core.setup.deny import denied_paths, is_denied

_MAX_BYTES = 1024 * 1024
_NAME_CAP = 200
_COMMAND_CAP = 1024
_LOGIN_TIMEOUT_S = 5
_LOGIN = {
    "claude": ["claude", "auth", "status"],
    "codex": ["codex", "login", "status"],
}
_RECORD_NAME = "record.json"


class ObserveRefused(Exception):
    """A refusal before any tool file is opened and before a record is written."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def run(*, home: str, project: str, runner=None) -> dict:
    """Build the report. ``runner`` receives a login argv and returns an exit code."""

    if runner is None:
        runner = _default_runner
    denied_ids = _denied_ids(home)
    sources = []
    for source_id, specs in _specs(home, project):
        files = [_inspect(path, kind, home, denied_ids) for path, kind in specs]
        sources.append(_source(source_id, files, runner))
    return {"generated_at": _now(), "sources": sources}


def command(out, err=None, *, home=None, project=None, state_home=None, runner=None, environ=None) -> int:
    """Write the owner-only record and print it. Refusals are one JSON object, exit 1.

    ``err`` is accepted so the console script can pass stderr through. Refusals are
    printed on ``out``, the same stream as the other ``recursiond`` commands.
    """

    environ = os.environ if environ is None else environ
    if home is None:
        home = environ.get("HOME") or _passwd_home()
    if project is None:
        project = os.getcwd()
    if state_home is None:
        chosen = environ.get("XDG_STATE_HOME") or ""
        if chosen:
            state_home = chosen
        elif type(home) is str and home:
            state_home = os.path.join(home, ".local", "state")
        else:
            state_home = ""
    if runner is None:
        runner = _default_runner
    try:
        _require(home, project, state_home)
        report = run(home=home, project=project, runner=runner)
        text = _persist(state_home, home, report)
    except ObserveRefused as refused:
        _emit(out, {"code": refused.code, "detail": refused.detail})
        return 1
    except OSError as exc:
        filename = exc.filename
        detail = type(exc).__name__ if not filename else f"{type(exc).__name__}: {filename}"
        _emit(out, {"code": "filesystem_error", "detail": detail})
        return 1
    out.write(text)
    return 0


def _require(home, project, state_home) -> None:
    if type(home) is not str or not os.path.isabs(home):
        raise ObserveRefused("home_unknown", "HOME is not an absolute path, so tool setup was not read")
    if type(project) is not str or not os.path.isabs(project):
        raise ObserveRefused("project_unknown", "the project path is not absolute, so tool setup was not read")
    if type(state_home) is not str or not os.path.isabs(state_home):
        raise ObserveRefused(
            "state_home_unknown",
            "XDG_STATE_HOME is not an absolute path, so no record was written",
        )
    record = os.path.join(state_home, "recursion", "observe", _RECORD_NAME)
    if is_denied(state_home, home) or is_denied(record, home):
        raise ObserveRefused(
            "state_home_unknown",
            "the state folder is a sign-in path, so no record was written",
        )


def _specs(home: str, project: str):
    return (
        ("claude", (
            (os.path.join(home, ".claude", "settings.json"), "json"),
            (os.path.join(home, ".claude.json"), "sealed"),
            (os.path.join(project, ".claude", "settings.json"), "json"),
        )),
        ("codex", (
            (os.path.join(home, ".codex", "config.toml"), "toml"),
        )),
        ("cursor", (
            (os.path.join(home, ".cursor", "mcp.json"), "json"),
            (os.path.join(project, ".cursor", "mcp.json"), "json"),
        )),
        ("git", (
            (os.path.join(home, ".gitconfig"), "git"),
            (os.path.join(home, ".config", "git", "config"), "git"),
            (os.path.join(project, ".git", "config"), "git"),
        )),
        ("project", (
            (os.path.join(project, "AGENTS.md"), "hash"),
            (os.path.join(project, "CLAUDE.md"), "hash"),
        )),
    )


def _source(source_id: str, files: list, runner) -> dict:
    body = {"files": files, "id": source_id}
    argv = _LOGIN.get(source_id)
    if argv is not None:
        body["login_exit_code"] = _exit_code(runner, argv)
    return body


def _exit_code(runner, argv: list):
    try:
        code = runner(list(argv))
    except (KeyboardInterrupt, SystemExit, AssertionError):
        raise
    except Exception:
        return None
    if type(code) is not int:
        return None
    return code


def _inspect(path: str, kind: str, home: str, denied_ids: set) -> dict:
    row = {
        "exists": False,
        "owner_only": None,
        "path": path,
        "sha256": None,
        "size": None,
        "state": "absent",
    }
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return row
    except OSError:
        row["state"] = "unknown"
        row["detail"] = "unreadable"
        return row
    row["exists"] = True
    row["size"] = info.st_size
    row["owner_only"] = (stat.S_IMODE(info.st_mode) & 0o077) == 0
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        row["state"] = "unknown"
        row["detail"] = "not_followed" if stat.S_ISLNK(info.st_mode) else "unreadable"
        return row
    if (info.st_dev, info.st_ino) in denied_ids or _path_blocked(path, home):
        row["state"] = "unknown"
        row["detail"] = "not_opened"
        return row
    # No owner-read bit covers mode 000. Size is already known from lstat; do not open.
    if kind == "sealed" or (info.st_mode & 0o400) == 0 or info.st_size > _MAX_BYTES:
        row["state"] = "unknown"
        row["detail"] = "not_opened" if kind == "sealed" else "unreadable"
        return row
    try:
        data = _read_bytes(path, home, denied_ids)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        row["state"] = "unknown"
        row["detail"] = "unreadable"
        return row
    row["sha256"] = hashlib.sha256(data).hexdigest()
    if kind == "hash":
        row["state"] = "ok"
        return row
    try:
        allow = _PARSERS[kind](data)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        allow = None
    if allow is None:
        row["state"] = "unknown"
        row["detail"] = "could_not_be_read"
        return row
    row["allow"] = allow
    row["state"] = "ok"
    return row


def _read_bytes(path: str, home: str, denied_ids: set) -> bytes:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PermissionError(path)
    if (info.st_dev, info.st_ino) in denied_ids or _path_blocked(path, home):
        raise PermissionError(path)
    if (info.st_mode & 0o400) == 0 or info.st_size > _MAX_BYTES:
        raise PermissionError(path)
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        held = os.fstat(fd)
        if not stat.S_ISREG(held.st_mode) or (held.st_dev, held.st_ino) in denied_ids:
            raise PermissionError(path)
        if held.st_size > _MAX_BYTES:
            raise OSError(errno.EFBIG, "file is too large", path)
        chunks = []
        total = 0
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            total += len(block)
            if total > _MAX_BYTES:
                raise OSError(errno.EFBIG, "file is too large", path)
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _path_blocked(path: str, home: str) -> bool:
    if is_denied(path, home):
        return True
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return True
    return is_denied(real, home)


def _denied_ids(home: str) -> set:
    found = set()
    if type(home) is not str or not home:
        return found
    for path in denied_paths(home):
        try:
            info = os.lstat(path)
        except OSError:
            continue
        found.add((info.st_dev, info.st_ino))
    return found


def _allow_json(data: bytes):
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if type(parsed) is not dict:
        return None
    return _allow_mapping(parsed)


def _allow_toml(data: bytes):
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    if type(parsed) is not dict:
        return None
    return _allow_mapping(parsed)


def _allow_mapping(parsed: dict) -> dict:
    allow = {}
    model = parsed.get("model")
    if type(model) is str and model and len(model) <= _NAME_CAP:
        allow["model"] = model
    approval = _first_str(parsed, ("approval_policy", "approval_mode", "approval"))
    if approval is not None:
        allow["approval"] = approval
    sandbox = _first_str(parsed, ("sandbox_mode", "sandbox"))
    if sandbox is not None:
        allow["sandbox"] = sandbox
    servers = _servers(parsed.get("mcpServers"))
    if not servers:
        servers = _servers(parsed.get("mcp_servers"))
    allow["mcp_servers"] = servers
    return allow


def _servers(raw) -> list:
    if type(raw) is not dict:
        return []
    rows = []
    for name in sorted(raw, key=str):
        if type(name) is not str or not name or len(name) > _NAME_CAP:
            continue
        command = None
        cfg = raw[name]
        if type(cfg) is dict:
            raw_command = cfg.get("command")
            if type(raw_command) is str and raw_command and len(raw_command) <= _COMMAND_CAP:
                command = raw_command
        rows.append({"command": command, "name": name})
    return rows


def _first_str(mapping: dict, keys: tuple) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if type(value) is str and value and len(value) <= _NAME_CAP:
            return value
    return None


def _allow_git(data: bytes):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    section = ""
    name_present = False
    email_present = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            header = stripped[1:-1].strip().lower()
            section = header.split(" ", 1)[0].split(".", 1)[0]
            continue
        if section != "user" or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip().lower()
        if key == "name":
            name_present = True
        elif key == "email":
            email_present = True
    return {"user_email_present": email_present, "user_name_present": name_present}


_PARSERS = {"json": _allow_json, "toml": _allow_toml, "git": _allow_git}


def _default_runner(argv: list) -> int:
    wanted = tuple(argv)
    if wanted not in {("claude", "auth", "status"), ("codex", "login", "status")}:
        raise OSError(errno.EINVAL, "not a login status command")
    binary = shutil.which(wanted[0])
    if not binary:
        raise FileNotFoundError(wanted[0])
    try:
        done = subprocess.run(
            [binary, *wanted[1:]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_LOGIN_TIMEOUT_S,
            check=False,
            shell=False,
            env=_login_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(wanted[0]) from exc
    return int(done.returncode)


def _login_env() -> dict:
    kept = {}
    for name in ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME", "TZ"):
        value = os.environ.get(name)
        if type(value) is str:
            kept[name] = value
    return kept


def _persist(state_home: str, home: str, report: dict) -> str:
    directory = os.path.join(state_home, "recursion", "observe")
    path = os.path.join(directory, _RECORD_NAME)
    if is_denied(path, home) or is_denied(directory, home):
        raise ObserveRefused("state_home_unknown", "the state folder is a sign-in path, so no record was written")
    _ensure_dir(state_home, tighten=False)
    _ensure_dir(os.path.join(state_home, "recursion"), tighten=False)
    _ensure_dir(directory, tighten=True)
    body = {
        "code": "ok",
        "generated_at": report["generated_at"],
        "record": path,
        "sources": report["sources"],
    }
    text = json.dumps(body, sort_keys=True) + "\n"
    _write_bytes(path, text.encode("ascii"))
    return text


def _ensure_dir(path: str, *, tighten: bool) -> None:
    if os.path.islink(path):
        raise OSError(errno.ELOOP, "link", path)
    if os.path.isdir(path):
        if tighten:
            os.chmod(path, 0o700)
        return
    if os.path.lexists(path):
        raise NotADirectoryError(errno.ENOTDIR, "not a directory", path)
    os.mkdir(path, 0o700)
    os.chmod(path, 0o700)


def _write_bytes(path: str, blob: bytes) -> None:
    if os.path.islink(path):
        raise OSError(errno.ELOOP, "link", path)
    temporary = path + ".tmp"
    if os.path.islink(temporary):
        raise OSError(errno.ELOOP, "link", temporary)
    if os.path.lexists(temporary):
        os.unlink(temporary)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(blob)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "short write", temporary)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(temporary, 0o600)
    try:
        os.replace(temporary, path)
    except OSError:
        if os.path.lexists(temporary) and not os.path.islink(temporary):
            os.unlink(temporary)
        raise
    os.chmod(path, 0o600)


def _now() -> str:
    moment = dt.datetime.now(dt.timezone.utc)
    millis = moment.microsecond // 1000
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


def _passwd_home() -> str:
    import pwd

    try:
        return pwd.getpwuid(os.geteuid()).pw_dir
    except (KeyError, OSError):
        return ""


def _emit(out, payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True), file=out)
