"""Propose a Recursion config from a local tool, and write nothing.

``recursiond import --from codex|claude|cursor --dry-run`` prints one JSON object.
The default engine follows the tool: ``claude`` to ``claude_code``, ``codex`` to ``codex``.
Cursor is not a route-contract engine, so its default engine is left unset. Folder trust
is the user's choice on every source and is never copied from the tool, including when
the tool marks a folder trusted.

``--apply`` is not a mode of this command. Omitting ``--dry-run`` refuses
``dry_run_required`` and writes nothing. No file is written under the profile, under
``$XDG_CONFIG_HOME``, or under ``$XDG_STATE_HOME``.

Sign-in stores named by ``deny.py`` are never opened, including through a symlink or a
hard link. ``~/.claude.json`` is not opened either: it can hold a sign-in beside
settings. A settings file is read only to copy an allow-listed model string and the
paths of named project folders. Environment values, header values, command arguments,
hooks and MCP commands are not copied. A file mode without the owner-read bit is left
unread.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib

from recursion_core.setup.deny import denied_paths, is_denied

_MAX_BYTES = 1024 * 1024
_PATH_CAP = 4096
_FOLDER_CAP = 64
_MODEL = re.compile(r"^[\x21-\x7e]{1,128}$")
# Values stored under these keys are never copied, and a picked string that contains
# one of those values is dropped. Compared after case folding.
_SECRET_KEYS = frozenset({
    "access_token", "api_key", "apikey", "args", "arguments", "auth", "authorization",
    "credentials", "env", "headers", "hook", "hooks", "http_headers", "id_token",
    "key", "password", "refresh_token", "secret", "secrets", "token", "tokens",
})
# Engine ids match the route contract. Cursor has no engine id there.
_SOURCES = {
    "claude": ("claude_code", ((".claude/settings.json", "json"),)),
    "codex": ("codex", ((".codex/config.toml", "toml"),)),
    "cursor": (None, ((".cursor/mcp.json", "json"),)),
}


class ImportRefused(Exception):
    """A refusal before a tool file is opened. Nothing is written."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def command(out, err=None, *, source, dry_run=False, home=None, environ=None) -> int:
    """Print the proposal. Refusals are one JSON object on ``out``, exit 1.

    ``err`` is accepted so the console script can pass stderr through. Refusals are
    printed on ``out``, the same stream as the other ``recursiond`` commands.
    """

    del err  # the proposal and the refusals share stdout
    environ = os.environ if environ is None else environ
    try:
        if dry_run is not True:
            raise ImportRefused(
                "dry_run_required",
                "this command prints a proposal and writes nothing",
            )
        if home is None:
            home = _home_from(environ)
        if type(source) is not str or source not in _SOURCES:
            raise ImportRefused("source_unknown", "from is codex, claude or cursor")
        _require_home(home)
        proposal = _propose(home, source)
    except ImportRefused as refused:
        _emit(out, {"code": refused.code, "detail": refused.detail})
        return 1
    except (KeyboardInterrupt, SystemExit):
        raise
    except OSError as exc:
        _emit(out, {"code": "filesystem_error", "detail": type(exc).__name__})
        return 1
    _emit(out, proposal)
    return 0


def _home_from(environ) -> str:
    if "HOME" in environ:
        chosen = environ.get("HOME")
        return chosen if type(chosen) is str else ""
    return _passwd_home()


def _require_home(home) -> None:
    if type(home) is not str or not os.path.isabs(home):
        raise ImportRefused(
            "home_unknown",
            "HOME is not an absolute path, so no tool setup was read",
        )


def _propose(home: str, source: str) -> dict:
    engine, files = _SOURCES[source]
    denied_ids = _denied_ids(home)
    picked: dict = {}
    for relative, kind in files:
        found = _load(os.path.join(home, *relative.split("/")), home, denied_ids, kind)
        if found:
            picked = found
            break
    proposal = {"default_engine": engine, "folder_trust": "user_choice"}
    if "model" in picked:
        proposal["model"] = picked["model"]
    if picked.get("folders"):
        proposal["folders"] = picked["folders"]
    return {
        "action": "import",
        "code": "ok",
        "dry_run": True,
        "from": source,
        "proposal": proposal,
        "wrote": False,
    }


def _load(path: str, home: str, denied_ids: set, kind: str):
    if _blocked(path, home, denied_ids):
        return None
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    if (info.st_dev, info.st_ino) in denied_ids or not _inside(path, home):
        return None
    if (info.st_mode & 0o400) == 0 or info.st_size > _MAX_BYTES:
        return None
    try:
        data = _read_bytes(path, home, denied_ids)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return None
    return _parse(data, kind)


def _read_bytes(path: str, home: str, denied_ids: set) -> bytes:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PermissionError(path)
    if (info.st_dev, info.st_ino) in denied_ids or _blocked(path, home, denied_ids):
        raise PermissionError(path)
    if not _inside(path, home) or (info.st_mode & 0o400) == 0 or info.st_size > _MAX_BYTES:
        raise PermissionError(path)
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        held = os.fstat(fd)
        if (not stat.S_ISREG(held.st_mode) or (held.st_dev, held.st_ino) in denied_ids
                or held.st_size > _MAX_BYTES):
            raise PermissionError(path)
        chunks = []
        total = 0
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            total += len(block)
            if total > _MAX_BYTES:
                raise OSError("file is too large")
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _parse(data: bytes, kind: str):
    try:
        text = data.decode("utf-8")
        if kind == "json":
            parsed = json.loads(text)
        elif kind == "toml":
            parsed = tomllib.loads(text)
        else:
            return None
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return None
    if type(parsed) is not dict:
        return None
    return _pick(parsed)


def _pick(parsed: dict) -> dict:
    secrets = set()
    _collect_secrets(parsed, False, secrets)
    found = {}
    model = parsed.get("model")
    if type(model) is str and _MODEL.fullmatch(model) and not _tainted(model, secrets):
        found["model"] = model
    projects = parsed.get("projects")
    folders = []
    if type(projects) is dict:
        for key in sorted((key for key in projects if type(key) is str)):
            if not _folder_path(key) or _tainted(key, secrets):
                continue
            folders.append({"path": key, "trust": "user_choice"})
            if len(folders) == _FOLDER_CAP:
                break
    if folders:
        found["folders"] = folders
    return found


def _collect_secrets(node, secret: bool, found: set) -> None:
    if type(node) is dict:
        for key, value in node.items():
            hidden = secret or (type(key) is str and key.lower() in _SECRET_KEYS)
            _collect_secrets(value, hidden, found)
        return
    if type(node) is list:
        for item in node:
            _collect_secrets(item, secret, found)
        return
    if secret and type(node) is str and node:
        found.add(node)


def _tainted(text: str, secrets: set) -> bool:
    if text in secrets:
        return True
    for secret in secrets:
        if len(secret) >= 12 and secret in text:
            return True
    return False


def _folder_path(key: str) -> bool:
    if not key.startswith("/") or len(key) > _PATH_CAP:
        return False
    return "\n" not in key and "\r" not in key and "\x00" not in key


def _blocked(path: str, home: str, denied_ids: set) -> bool:
    if is_denied(path, home) or os.path.basename(path) == ".claude.json":
        return True
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if (info.st_dev, info.st_ino) in denied_ids:
        return True
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return True
    return is_denied(real, home) or os.path.basename(real) == ".claude.json"


def _inside(path: str, home: str) -> bool:
    try:
        real_home = os.path.realpath(home)
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    if real == real_home:
        return False
    prefix = real_home if real_home.endswith(os.sep) else real_home + os.sep
    return real.startswith(prefix)


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
    sealed = os.path.join(home, ".claude.json")
    try:
        info = os.lstat(sealed)
    except OSError:
        return found
    found.add((info.st_dev, info.st_ino))
    return found


def _passwd_home() -> str:
    import pwd

    try:
        return pwd.getpwuid(os.geteuid()).pw_dir
    except (KeyError, OSError):
        return ""


def _emit(out, payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True), file=out)
