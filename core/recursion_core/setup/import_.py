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
paths of named project folders. The model passes the shared value rule
(``setup/value_rule.py``): a model-name shape (letters, digits and ``.`` ``_`` ``:``
``/`` ``-``, 64 characters at most), no other string value in that file, under any key,
of 4 characters or more, equal to it or contained in it (a Codex profile's copy of the
model is not counted), and no key shape (a credential prefix, a long part that starts
with one, or a random run). A key-shaped folder path is not copied. Environment values,
header values, command arguments, hooks and MCP commands are not copied. A file mode
without the owner-read bit is left unread, and so is a file the caller does not own or
that has a group- or world-write bit (the shared gate).
An alias (more than one link, or an inode that changes between the look and the read)
is not read. The real path must stay inside the home.
"""

from __future__ import annotations

import json
import os
import stat
import tomllib

from recursion_core.setup.deny import denied_paths, is_denied
from recursion_core.setup import value_rule
from recursion_core.setup.open_guard import inside_root, read_regular_file

_MAX_BYTES = 1024 * 1024
_PATH_CAP = 4096
_FOLDER_CAP = 64
# The model field does not consult _SECRET_KEYS: it passes the shared value rule.
# Folder paths still refuse a value stored under one of these keys (compared after
# case folding), and a key-shaped path.
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
    if (info.st_dev, info.st_ino) in denied_ids or not inside_root(path, home):
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
    """Read through the shared gate. A refusal raises and the caller copies nothing."""

    return read_regular_file(
        path, root=home, home=home, max_bytes=_MAX_BYTES, denied_ids=denied_ids,
    )


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
    if type(model) is str and not _model_blocked(parsed, model):
        found["model"] = model
    projects = parsed.get("projects")
    folders = []
    if type(projects) is dict:
        for key in sorted((key for key in projects if type(key) is str)):
            if not _folder_path(key) or _tainted(key, secrets) or value_rule.credential_shaped(key):
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


def _model_blocked(parsed: dict, model: str) -> bool:
    """True when the shared value rule withholds ``model`` (``setup/value_rule.py``).

    ``_SECRET_KEYS`` is not consulted. The model's own value is not an other value, nor
    is a Codex profile's copy of it. A string shorter than 4 characters does not block it.
    """

    others = value_rule.model_others(parsed, model)
    return value_rule.model_value(model, others) == value_rule.WITHHELD


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
