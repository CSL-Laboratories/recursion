"""The containment setting (lane INSTALL): where the daemon may give an engine its route home.

The operator chooses it, and Recursion reads it from one file, ``containment.json`` in
``$XDG_CONFIG_HOME/recursion`` (``~/.config/recursion`` when ``XDG_CONFIG_HOME`` is unset or empty,
the folder the daemon reads ``routes.json`` from, ``daemon/control_adapter.py``):

    {"engine_homes": "inside_operator_home", "version": 1}

``engine_homes`` is one of:

    inside_operator_home    the default. Route homes are at ``~/.local/state/recursion/homes/<engine>``
                            and the engines run as the operator; only the operator's home itself is
                            refused as a route home (``setup.route_home.check``'s default). Level
                            ``same_user``: the words in ``OPERATOR_WORDS`` say what that lets an engine do.
    outside_operator_home   a route home inside the operator's home, or holding it, is refused
                            (``check(path, root, allow_inside_operator_home=False)``). It needs the
                            engines to run as their own user, which lane STOP sets up; until that
                            user exists, ``write`` refuses it (``NEEDS_ENGINE_USER``). Level ``engine_user``.

Further containment choices go in later as further keys of the same object. A reader refuses a key it
does not know, a version other than 1, a key written twice, or a value of the wrong type: a choice an
older Recursion cannot read stops it, never goes unread. No file (or no folder on the way to it) is
the default, and the answer says so (``source`` ``"default"``). Every other case is a typed refusal,
``ContainmentRefused(code, detail)``, never the default:

    containment_untrusted   the file is a link, not a regular file, not the operator's, or writable by
                            group or world; the ``recursion`` folder is a link, not a folder, not the
                            operator's, or writable by group or world; a folder above it another user
                            could change (each root's or the operator's, not writable by group or world
                            unless sticky; a link above it root's or the operator's, followed, at most
                            40 links); or the config folder is not an absolute path
    containment_unreadable  the file, or a folder on the way to it, cannot be looked up, opened or read
    containment_invalid     not UTF-8 JSON in the shape above (a half-written file included), or larger
                            than 64 KiB; or, for ``write``, a value that is not one of ``ENGINE_HOMES``
    containment_changed     the file changed while it was being read (rewritten where it is, or another
                            file renamed over it, or removed); run the command again
    containment_needs_engine_user   ``write`` only: outside_operator_home on a machine with no engine user

``read`` raises nothing else. Read once, pass the value: the daemon is to read the setting once when it
starts and pass the same ``allow_inside_operator_home`` to ``route_home.check`` (lane STOP, the
launcher) and to the subscription policy (lane ENGINE-E9), so the two never answer from two readings.
Nothing in the daemon reads it yet.

``write`` replaces the file whole: a new file, mode 0600, written, flushed, and renamed over the old one
in the same folder, so a reader sees the old setting or the new one, never a mix. It makes a missing
config folder and ``recursion`` folder 0700. It refuses when the file in place is refused by ``read``
(it never overwrites a file it could not read, one a newer Recursion wrote included): the operator
removes such a file by hand. A file-system error while writing (a full disk) is raised as ``OSError``.

The limit, as ``route_home.py``'s: code running as the operator, an engine under
``inside_operator_home`` included, can rewrite this file. The setting holds against other users and
against a mistaken file, not against the operator's own programs; ``OPERATOR_WORDS`` says so. POSIX
ACLs are not read. Linux only, as the launcher is.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import stat
from dataclasses import dataclass

__all__ = [
    "DEFAULT_ENGINE_HOMES", "ENGINE_HOMES", "FILE_NAME", "INSIDE_OPERATOR_HOME", "LEVELS", "NEEDS_ENGINE_USER",
    "OPERATOR_WORDS", "OUTSIDE_OPERATOR_HOME", "VERSION", "Containment", "ContainmentRefused", "config_folder",
    "doctor_check", "engine_user", "read", "write",
]

INSIDE_OPERATOR_HOME = "inside_operator_home"
OUTSIDE_OPERATOR_HOME = "outside_operator_home"
ENGINE_HOMES = (INSIDE_OPERATOR_HOME, OUTSIDE_OPERATOR_HOME)
DEFAULT_ENGINE_HOMES = INSIDE_OPERATOR_HOME
LEVELS = {INSIDE_OPERATOR_HOME: "same_user", OUTSIDE_OPERATOR_HOME: "engine_user"}
VERSION = 1
FOLDER_NAME = "recursion"
FILE_NAME = "containment.json"
MAX_BYTES = 64 * 1024
_KEYS = frozenset({"version", "engine_homes"})
_MAX_LINKS = 40  # Linux's MAXSYMLINKS

# The one text the operator reads about the default, wherever it is shown: recursiond's answers,
# doctor, the terminal app's and the page's settings screens, and the approval prompt for the looser
# value (Lucas, Nov 15: "make sure that the dangers are made clear to the operator").
OPERATOR_WORDS = (
    "Engines keep their sign-ins in a private folder inside your home "
    "(~/.local/state/recursion/homes/<engine>). They run as you, so an engine can read and change anything "
    "your user account can: your files, your SSH keys, your browser and other app logins. Recursion keeps "
    "each engine's folder private to you and never gives an engine your home folder as its own, but it "
    "cannot stop a program running as you from reaching the rest of your home. To keep engines out of your "
    "home, set up a separate engine user and choose \"outside my home\"."
)
NEEDS_ENGINE_USER = "needs the engine user, which this machine does not have"


class ContainmentRefused(Exception):
    """A typed refusal. ``code`` is stable; ``detail`` names the path or the key and is never secret."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Containment:
    """One reading of the setting: its value, where it came from (``"file"`` or ``"default"``) and the file."""

    engine_homes: str
    source: str
    path: str

    @property
    def allow_inside_operator_home(self) -> bool:
        """``route_home.check``'s keyword for this value: True for inside_operator_home only."""
        return self.engine_homes == INSIDE_OPERATOR_HOME

    @property
    def level(self) -> str:
        return LEVELS[self.engine_homes]


def engine_user() -> str | None:
    """The engines' own user, which lane STOP sets up. None: no machine has one yet."""

    return None


def config_folder(environ=None) -> str:
    """``$XDG_CONFIG_HOME/recursion``, or ``$HOME/.config/recursion`` (the password database's home when
    ``HOME`` is unset or empty). Refuses ``containment_untrusted`` unless the folder is absolute."""

    env = os.environ if environ is None else environ
    base = env.get("XDG_CONFIG_HOME") or ""
    if not base:
        home = env.get("HOME") or ""
        if not home:
            import pwd  # Linux only, as the launcher is

            try:
                home = pwd.getpwuid(os.geteuid()).pw_dir
            except (KeyError, OSError):
                home = ""
        base = os.path.join(home, ".config") if home else ""
    if not isinstance(base, str) or not base.startswith("/") or "\0" in base:
        raise ContainmentRefused("containment_untrusted", f"the config folder {base!r} is not an absolute path")
    return os.path.join(base, FOLDER_NAME)


def read(environ=None) -> Containment:
    """The setting, or the default when there is no file. See the module docstring for the refusals."""

    folder = config_folder(environ)
    path = os.path.join(folder, FILE_NAME)
    fd = _open_folder(folder, create=False)
    if fd is None:
        return Containment(DEFAULT_ENGINE_HOMES, "default", path)
    try:
        value = _read_in(fd, path)
    finally:
        os.close(fd)
    if value is None:
        return Containment(DEFAULT_ENGINE_HOMES, "default", path)
    return Containment(value, "file", path)


def write(engine_homes, environ=None) -> tuple[Containment, bool]:
    """Set the value; answers the new reading and whether the file changed. See the module docstring."""

    if type(engine_homes) is not str or engine_homes not in ENGINE_HOMES:
        raise ContainmentRefused("containment_invalid", f"engine_homes must be one of {', '.join(ENGINE_HOMES)}")
    if engine_homes == OUTSIDE_OPERATOR_HOME and engine_user() is None:
        raise ContainmentRefused("containment_needs_engine_user", f"{OUTSIDE_OPERATOR_HOME} {NEEDS_ENGINE_USER}")
    folder = config_folder(environ)
    path = os.path.join(folder, FILE_NAME)
    fd = _open_folder(folder, create=True)
    try:
        if _read_in(fd, path) == engine_homes:
            return Containment(engine_homes, "file", path), False
        data = (json.dumps({"engine_homes": engine_homes, "version": VERSION}, sort_keys=True) + "\n").encode()
        temp = f".{FILE_NAME}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
        try:
            try:
                os.fchmod(out, 0o600)
                view = memoryview(data)
                while view:
                    view = view[os.write(out, view):]
                os.fsync(out)
            finally:
                os.close(out)
            os.rename(temp, FILE_NAME, src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            try:
                os.unlink(temp, dir_fd=fd)
            except OSError:
                pass
            raise
        os.fsync(fd)
    finally:
        os.close(fd)
    return Containment(engine_homes, "file", path), True


def doctor_check(environ=None) -> dict:
    """The setting as one check of the doctor report (``doctor_report``'s ``checks`` entry, methods.v1 on
    SPINE's #368): its value, its level, and what it means for the operator."""

    subject = None
    try:
        subject = os.path.join(config_folder(environ), FILE_NAME)
        setting = read(environ)
    except ContainmentRefused as exc:
        return _check(subject, "fail", f"the containment setting cannot be read ({exc.code}: {exc.detail})",
                      "make the file yours and not writable by others, or remove it, then run "
                      "`recursiond setup containment <value>`")
    said = f"engine_homes is {setting.engine_homes}{' (the default: no file)' if setting.source == 'default' else ''}"
    if setting.engine_homes == INSIDE_OPERATOR_HOME:
        return _check(setting.path, "warn", f"{said}; level {setting.level}. {OPERATOR_WORDS}",
                      "set up a separate engine user, then run "
                      f"`recursiond setup containment {OUTSIDE_OPERATOR_HOME}`")
    user = engine_user()
    if user is None:
        return _check(setting.path, "fail", f"{said}; level {setting.level}, which {NEEDS_ENGINE_USER}",
                      f"set up the engine user, or run `recursiond setup containment {INSIDE_OPERATOR_HOME}`")
    return _check(setting.path, "ok", f"{said}; level {setting.level}: engines run as {user}, outside your home",
                  None)


def _check(subject, state: str, detail: str, fix) -> dict:
    return {"id": "containment.engine_homes", "subject": subject, "state": state, "detail": detail, "fix": fix}


def _refuse(code: str, detail: str):
    raise ContainmentRefused(code, detail)


def _open_folder(folder: str, *, create: bool) -> int | None:
    """An open descriptor of ``folder`` (the ``recursion`` folder) after its own checks and those of
    every folder above it; None when it, or a folder above it, does not exist and ``create`` is False."""

    above = _walk_above(os.path.dirname(folder), create=create)
    if above is None:
        return None
    path = os.path.join(above, os.path.basename(folder))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:  # made by another writer since: held to the same checks below
            pass
        fd = _open_existing_folder(path, flags)
    except OSError as exc:
        _folder_error(path, exc)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            _refuse("containment_untrusted", f"{path} is not a folder")
        if info.st_uid != os.geteuid():
            _refuse("containment_untrusted", f"{path} is not yours")
        if info.st_mode & 0o022:
            _refuse("containment_untrusted", f"{path} is writable by group or world; `chmod 700` it")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_existing_folder(path: str, flags: int) -> int:
    try:
        return os.open(path, flags)
    except OSError as exc:
        _folder_error(path, exc)


def _folder_error(path: str, exc: OSError):
    if exc.errno in (errno.ELOOP, errno.ENOTDIR):  # O_DIRECTORY|O_NOFOLLOW on a link is ENOTDIR on Linux
        try:
            link = stat.S_ISLNK(os.lstat(path).st_mode)
        except OSError:
            link = False
        _refuse("containment_untrusted", f"{path} is {'a link' if link else 'not a folder'}")
    _refuse("containment_unreadable", f"{path}: {type(exc).__name__}")


def _walk_above(folder: str, *, create: bool) -> str | None:
    """``folder`` (the config folder) with every link followed, each folder from ``/`` to it root's or
    the operator's and not writable by group or world unless sticky, each link root's or the operator's
    (at most ``_MAX_LINKS``). None when a name on the way does not exist and ``create`` is False; with
    ``create``, a missing folder is made 0700."""

    trusted = (0, os.geteuid())
    names = [name for name in folder.split("/") if name]
    current, links = "/", 0
    _trusted_folder("/", _lstat("/"), trusted)
    while names:
        name = names.pop(0)
        if name == ".":
            continue
        if name == "..":
            current = os.path.dirname(current)  # current holds no link, so this is its parent
            continue
        candidate = os.path.join(current, name)
        info = _lstat(candidate)
        if info is None:
            if not create:
                return None
            try:
                os.mkdir(candidate, 0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                _refuse("containment_unreadable", f"{candidate} cannot be made: {type(exc).__name__}")
            info = _lstat(candidate)
            if info is None:
                _refuse("containment_unreadable", f"{candidate} was removed while it was made")
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid not in trusted:
                _refuse("containment_untrusted", f"{candidate} is a link another user owns")
            links += 1
            if links > _MAX_LINKS:
                _refuse("containment_untrusted", f"more than {_MAX_LINKS} links on the way to {folder}")
            try:
                target = os.readlink(candidate)
            except OSError as exc:
                _refuse("containment_unreadable", f"{candidate}: {type(exc).__name__}")
            if target.startswith("/"):
                current = "/"
            names[:0] = [part for part in target.split("/") if part]
            continue
        if not stat.S_ISDIR(info.st_mode):
            _refuse("containment_untrusted", f"{candidate} is not a folder")
        _trusted_folder(candidate, info, trusted)
        current = candidate
    return current


def _lstat(path: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _refuse("containment_unreadable", f"{path}: {type(exc).__name__}")


def _trusted_folder(path: str, info: os.stat_result | None, trusted: tuple) -> None:
    if info is None:
        _refuse("containment_unreadable", f"{path} does not exist")
    if info.st_uid not in trusted:
        _refuse("containment_untrusted", f"{path} is owned by another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not info.st_mode & stat.S_ISVTX:
        _refuse("containment_untrusted", f"{path} is writable by group or world and not sticky")


def _read_in(folder_fd: int, path: str) -> str | None:
    """The value in ``FILE_NAME`` inside the open folder, or None when there is no file."""

    try:
        fd = os.open(FILE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=folder_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _refuse("containment_untrusted", f"{path} is a link")
        if exc.errno == errno.ENXIO:  # a socket
            _refuse("containment_untrusted", f"{path} is not a regular file")
        _refuse("containment_unreadable", f"{path}: {type(exc).__name__}")
    try:
        data, info = _read_held(fd, path)
        # Still the file's name, checked while the descriptor holds the inode (so it cannot be reused): a
        # file renamed over it, or removed, while it was read is a change, whatever the clock's grain.
        try:
            now = os.stat(FILE_NAME, dir_fd=folder_fd, follow_symlinks=False)
        except FileNotFoundError:
            now = None
        except OSError as exc:
            _refuse("containment_unreadable", f"{path}: {type(exc).__name__}")
        if now is None or (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
            _refuse("containment_changed", f"{path} changed while it was read; run the command again")
    finally:
        os.close(fd)
    return _parse(data, path)


def _read_held(fd: int, path: str) -> tuple[bytes, os.stat_result]:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        _refuse("containment_untrusted", f"{path} is not a regular file")
    if info.st_uid != os.geteuid():
        _refuse("containment_untrusted", f"{path} is not yours")
    if info.st_mode & 0o022:
        _refuse("containment_untrusted", f"{path} is writable by group or world; `chmod 600` it")
    if info.st_size > MAX_BYTES:
        _refuse("containment_invalid", f"{path} is larger than {MAX_BYTES} bytes")
    chunks, total = [], 0
    try:
        while True:
            chunk = os.read(fd, MAX_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_BYTES:
                _refuse("containment_invalid", f"{path} is larger than {MAX_BYTES} bytes")
        data = b"".join(chunks)
        after = os.fstat(fd)
        same = os.pread(fd, len(data) + 1, 0) == data
    except OSError as exc:
        _refuse("containment_unreadable", f"{path}: {type(exc).__name__}")
    if not same or len(data) != info.st_size or _version_of(after) != _version_of(info):
        _refuse("containment_changed", f"{path} changed while it was read; run the command again")
    return data, info


def _version_of(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class _Twice(Exception):
    pass


def _object(pairs):
    found = {}
    for key, value in pairs:
        if key in found:
            raise _Twice(key)
        found[key] = value
    return found


def _parse(data: bytes, path: str) -> str:
    try:
        text = data.decode("utf-8")
        document = json.loads(text, object_pairs_hook=_object)  # NaN and Infinity fail the type checks below
    except _Twice as exc:
        _refuse("containment_invalid", f"{path} has the key {_shown(exc.args[0])} twice")
    except (UnicodeDecodeError, ValueError, RecursionError):
        _refuse("containment_invalid", f"{path} is not a JSON object (empty, or half written?); "
                "run `recursiond setup containment <value>` after removing it")
    if type(document) is not dict:
        _refuse("containment_invalid", f"{path} is not a JSON object")
    unknown = sorted(set(document) - _KEYS)
    if unknown:
        shown = ", ".join(_shown(key) for key in unknown[:5]) + (", ..." if len(unknown) > 5 else "")
        _refuse("containment_invalid", f"{path} has a key this Recursion does not know ({shown}); a newer "
                "Recursion may have written it")
    version = document.get("version")
    if type(version) is not int or version != VERSION:
        _refuse("containment_invalid", f"{path} has version {_shown(version)}; this Recursion reads version {VERSION}")
    value = document.get("engine_homes")
    if type(value) is not str or value not in ENGINE_HOMES:
        _refuse("containment_invalid", f"{path} has engine_homes {_shown(value)}; it must be one of "
                f"{', '.join(ENGINE_HOMES)}")
    return value


def _shown(value) -> str:
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."
