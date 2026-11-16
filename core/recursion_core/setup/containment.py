"""The containment setting (lane INSTALL): where the daemon may give an engine its route home.

The operator chooses it, and Recursion reads it from one file, ``.config/recursion/containment.json``
under the home the password database gives for this user, whatever ``HOME`` or ``XDG_CONFIG_HOME``
say (the Liaison's ruling on Q19, Nov 15): the daemon under the user manager and the operator's shell
can disagree about those variables, and a setting read from another folder would silently answer the
looser default. One place only: a ``containment.json`` under ``$XDG_CONFIG_HOME/recursion`` is never
read, and doctor names it (``doctor_checks``):

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
                            operator's, or writable by group or world; a link at or below the operator's
                            home (none is followed there); a link above the home that anyone but root
                            owns or could replace (the folder holding it, or one on the way to its
                            target, not root's alone), or more than 40 of them (the Liaison's ruling on
                            Q21); a folder above it another user could change (each root's or the
                            operator's, not writable by group or world unless sticky); or the password
                            database gives no absolute home for this user
    containment_unreadable  the file, or a folder on the way to it, cannot be looked up, opened, read or made
    containment_invalid     not UTF-8 JSON in the shape above (a half-written file included), or larger
                            than 64 KiB; or, for ``write``, a value that is not one of ``ENGINE_HOMES``
    containment_changed     the file changed while it was being read (rewritten where it is, or another
                            file renamed over it, or removed); run the command again
    containment_needs_engine_user   ``write`` only: outside_operator_home on a machine with no engine user
    containment_not_flushed  ``write`` only: the file holds the new value, but its folder could not be
                            flushed after the rename; run the command again

``read`` raises nothing else. Read once, pass the value: the daemon is to read the setting once when it
starts and pass the same ``allow_inside_operator_home`` to ``route_home.check`` (lane STOP, the
launcher) and to the subscription policy (lane ENGINE-E9), so the two never answer from two readings.
Nothing in the daemon reads it yet.

``write`` replaces the file whole: a new file, mode 0600, written, flushed, and renamed over the old one
in the same folder, which is then flushed, so a reader sees the old setting or the new one, never a mix
(or, reading while the rename happens, is refused ``containment_changed`` and runs again). Writers take
turns: each holds an exclusive ``flock`` on the folder from its read to its answer, so among writers that
take that lock (every ``write`` does) its answer is the file's value when it answers; a program that writes
the file without the lock (a hand edit) can change it after. It removes the temporary files a killed
writer left. A ``.config`` or ``recursion`` folder it makes is set to 0700 whatever the umask; one it
cannot make is ``containment_unreadable``, naming the folder it was to be made in. It refuses when the
file in place is refused by ``read`` (it never overwrites a file it could not read, one a newer Recursion
wrote included): the operator removes such a file by hand. A file-system error while writing (a full
disk) is raised as ``OSError``.

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
    "doctor_checks", "engine_user", "read", "write",
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

# The one text the operator reads about the default, wherever it is shown: recursiond's answers,
# doctor, the terminal app's and the page's settings screens, and the approval prompt for the looser
# value (Lucas, Nov 15: "make sure that the dangers are made clear to the operator"). A private folder
# separates an engine's settings; it is not isolation, and the text claims no protection beyond that.
OPERATOR_WORDS = (
    "Engines run as your user. Each engine's private folder inside your home "
    "(~/.local/state/recursion/homes/<engine>) keeps its settings and sign-ins separate, but does not "
    "prevent access to your other files, credentials or local services: an engine can read or change "
    "anything your user account can, your SSH keys and your browser and other app logins included. "
    "Choosing \"outside my home\" needs a separate engine user, so that engines do not run as you."
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


def config_folder() -> str:
    """``.config/recursion`` under the home the password database gives for this user, whatever ``HOME`` or
    ``XDG_CONFIG_HOME`` say. Refuses ``containment_untrusted`` when the database has no entry for this
    user or cannot be read, or its home is not an absolute path."""

    import pwd  # Linux only, as the launcher is

    try:
        home = pwd.getpwuid(os.geteuid()).pw_dir
    except (KeyError, OSError) as exc:
        raise ContainmentRefused("containment_untrusted",
                                 f"the password database gives no home for this user: {type(exc).__name__}") from None
    if not isinstance(home, str) or not home.startswith("/") or "\0" in home:
        raise ContainmentRefused("containment_untrusted", f"the operator's home {home!r} is not an absolute path")
    return os.path.normpath(os.path.join(home, ".config", FOLDER_NAME))


def read() -> Containment:
    """The setting, or the default when there is no file. See the module docstring for the refusals."""

    folder = config_folder()
    path = os.path.join(folder, FILE_NAME)
    fd = _open_folder(folder, create=False)
    if fd is None:
        return Containment(DEFAULT_ENGINE_HOMES, "default", path)
    try:
        value = _read_in(fd, path)
    finally:
        _close(fd)
    if value is None:
        return Containment(DEFAULT_ENGINE_HOMES, "default", path)
    return Containment(value, "file", path)


def write(engine_homes) -> tuple[Containment, bool]:
    """Set the value; answers the new reading and whether the file changed. See the module docstring."""

    if type(engine_homes) is not str or engine_homes not in ENGINE_HOMES:
        raise ContainmentRefused("containment_invalid", f"engine_homes must be one of {', '.join(ENGINE_HOMES)}")
    if engine_homes == OUTSIDE_OPERATOR_HOME and engine_user() is None:
        raise ContainmentRefused("containment_needs_engine_user", f"{OUTSIDE_OPERATOR_HOME} {NEEDS_ENGINE_USER}")
    folder = config_folder()
    path = os.path.join(folder, FILE_NAME)
    import fcntl  # Linux only, as the launcher is: not with the module, which every system imports

    fd = _open_folder(folder, create=True)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # writers take turns: read, compare and write alone (released at close)
        _sweep(fd)
        if _read_in(fd, path) == engine_homes:
            _flush_folder(fd, path, engine_homes)  # a run after containment_not_flushed makes it durable
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
        _flush_folder(fd, path, engine_homes)
    finally:
        _close(fd)
    return Containment(engine_homes, "file", path), True


def _flush_folder(fd: int, path: str, engine_homes: str) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        _refuse("containment_not_flushed", f"{path} holds {engine_homes} now, but its folder could not be flushed "
                f"({type(exc).__name__}), so the change may not survive a power loss; run the command again")


def _sweep(fd: int) -> None:
    """Remove the temporary files a writer killed before its rename left (none is being written: the
    caller holds the folder's lock)."""

    for name in os.listdir(fd):
        if name.startswith(f".{FILE_NAME}.") and name.endswith(".tmp"):
            try:
                os.unlink(name, dir_fd=fd)
            except OSError:  # gone, or not a file: left as it is
                pass


def _close(fd: int) -> None:
    """Close a descriptor opened for reading; an error there loses nothing that was read."""

    try:
        os.close(fd)
    except OSError:
        pass


def doctor_checks(environ=None) -> list[dict]:
    """The setting as checks of the doctor report (``doctor_report``'s ``checks`` entries, methods.v1 on
    SPINE's #368): its value, its level and what it means for the operator; and, when ``XDG_CONFIG_HOME``
    (``environ``'s, ``os.environ``'s by default) names another folder, a line that says the setting is
    not read from there, naming a ``containment.json`` found there."""

    checks = [_setting_check()]
    other = _elsewhere(os.environ if environ is None else environ)
    if other is not None:
        here = checks[0]["subject"] or "the home the password database gives"
        stray = os.path.join(other, FILE_NAME)
        try:
            found = os.path.lexists(stray)
        except ValueError:  # a NUL in XDG_CONFIG_HOME: no file can be there
            found = False
        if found:
            checks.append(_check("containment.config_folder", stray, "warn",
                                 f"{stray} is not read: the containment setting is read only from {here}, "
                                 "under the home the password database gives, whatever XDG_CONFIG_HOME says",
                                 f"set the value with `recursiond setup containment <value>`, then remove {stray}"))
        else:
            checks.append(_check("containment.config_folder", other, "ok",
                                 f"XDG_CONFIG_HOME names {other}; the containment setting is read only from "
                                 f"{here}, under the home the password database gives", None))
    return checks


def _elsewhere(env) -> str | None:
    """``$XDG_CONFIG_HOME/recursion`` when ``XDG_CONFIG_HOME`` is set and names another folder than the
    setting's own (compared as written, then with links followed); None otherwise."""

    xdg = env.get("XDG_CONFIG_HOME") or ""
    if not isinstance(xdg, str) or not xdg:
        return None
    other = os.path.join(xdg, FOLDER_NAME)
    try:
        here = config_folder()
    except ContainmentRefused:
        return other
    try:
        if os.path.normpath(other) == os.path.normpath(here) or os.path.realpath(other) == os.path.realpath(here):
            return None
    except (OSError, ValueError):
        pass
    return other


_SETTING = "containment.engine_homes"


def _setting_check() -> dict:
    subject = None
    try:
        subject = os.path.join(config_folder(), FILE_NAME)
        setting = read()
    except ContainmentRefused as exc:
        return _check(_SETTING, subject, "fail", f"the containment setting cannot be read ({exc.code}: {exc.detail})",
                      "make the file yours and not writable by others, or remove it, then run "
                      "`recursiond setup containment <value>`")
    said = f"engine_homes is {setting.engine_homes}{' (the default: no file)' if setting.source == 'default' else ''}"
    user = engine_user()
    if setting.engine_homes == INSIDE_OPERATOR_HOME:
        if user is None:  # no step the operator can take yet: said, not offered as a fix
            return _check(_SETTING, setting.path, "warn", f"{said}; level {setting.level}. {OPERATOR_WORDS} "
                          f"{OUTSIDE_OPERATOR_HOME} {NEEDS_ENGINE_USER}.", None)
        return _check(_SETTING, setting.path, "warn", f"{said}; level {setting.level}. {OPERATOR_WORDS}",
                      f"run `recursiond setup containment {OUTSIDE_OPERATOR_HOME}` to keep engines out of your home")
    if user is None:
        return _check(_SETTING, setting.path, "fail", f"{said}; level {setting.level}, which {NEEDS_ENGINE_USER}",
                      f"set up the engine user, or run `recursiond setup containment {INSIDE_OPERATOR_HOME}`")
    return _check(_SETTING, setting.path, "ok",
                  f"{said}; level {setting.level}: engines run as {user}, outside your home", None)


def _check(check_id: str, subject, state: str, detail: str, fix) -> dict:
    return {"id": check_id, "subject": subject, "state": state, "detail": detail, "fix": fix}


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
            os.chmod(path, 0o700)  # made here: 0700 whatever the umask (one can take the owner's own bits)
        except FileExistsError:  # made by another writer since: held to the same checks below
            pass
        except OSError as exc:  # its folder not usable by its owner, a full disk: said, naming that folder
            hint = (f"; {above} must be writable and searchable by its owner (`chmod 700` it)"
                    if isinstance(exc, PermissionError) else "")
            _refuse("containment_unreadable", f"{path} cannot be made in {above}: {type(exc).__name__}{hint}")
        fd = _open_existing_folder(path, flags)
    except OSError as exc:
        _folder_error(path, exc)
    try:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            _refuse("containment_unreadable", f"{path}: {type(exc).__name__}")
        if not stat.S_ISDIR(info.st_mode):
            _refuse("containment_untrusted", f"{path} is not a folder")
        if info.st_uid != os.geteuid():
            _refuse("containment_untrusted", f"{path} is not yours")
        if info.st_mode & 0o022:
            _refuse("containment_untrusted", f"{path} is writable by group or world; `chmod 700` it")
    except BaseException:
        _close(fd)
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


_MAX_LINKS = 40  # Linux's MAXSYMLINKS: the links one path lookup follows before ELOOP


def _walk_above(folder: str, *, create: bool) -> str | None:
    """``folder`` (``<home>/.config``, normalized) as the kernel reaches it, each folder root's or the operator's
    and not writable by group or world unless sticky. Above the operator's home a link is followed only when
    only root can change it (the Liaison's ruling on Q21): root owns the link, the folder holding it is root's
    and not writable by group or others (sticky or not), and the folders on the way to its target (a parent
    its ``..`` climbs to, and ``/`` for an absolute one, included), and on to the home, are root's and not
    writable by group or others, links among them held to the same rule, at most
    ``_MAX_LINKS`` in all. At or below the home no link is followed (the 3:46 PM ruling). None when a name on
    the way does not exist and ``create`` is False; with ``create``, a missing ``.config`` is made 0700, and
    nothing at or above the home is ever made."""

    trusted = (0, os.geteuid())
    home = os.path.dirname(folder)
    above = [name for name in os.path.dirname(home).split("/") if name]
    below = [name for name in home.split("/") if name][len(above):] + [os.path.basename(folder)]
    current, links, through = "/", 0, False
    _trusted_folder("/", _lstat("/"), trusted)
    while above:
        name = above.pop(0)
        if name == ".":
            continue
        if name == "..":
            current = os.path.dirname(current)  # current holds no link, so this is its parent
            if through:  # a parent a link's target climbs to is on the way too (Sol's C1 on #428)
                _root_alone(current, _lstat(current), "on the way through a link above your home,")
            continue
        candidate = os.path.join(current, name)
        info = _lstat(candidate)
        if info is None:
            if create:
                _refuse("containment_unreadable", f"{candidate} does not exist, so neither does the operator's home")
            return None
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid != 0:
                _refuse("containment_untrusted", f"{candidate} is a link above your home that root does not own")
            _root_alone(current, _lstat(current), f"which holds the link {candidate},")
            links += 1
            if links > _MAX_LINKS:
                _refuse("containment_untrusted", f"more than {_MAX_LINKS} links above your home")
            try:
                target = os.readlink(candidate)
            except OSError as exc:
                _refuse("containment_unreadable", f"{candidate}: {type(exc).__name__}")
            if target.startswith("/"):
                current = "/"
                _root_alone("/", _lstat("/"), "on the way through a link above your home,")
            above[:0] = [part for part in target.split("/") if part]
            through = True
            continue
        if not stat.S_ISDIR(info.st_mode):
            _refuse("containment_untrusted", f"{candidate} is not a folder")
        if through:
            _root_alone(candidate, info, "on the way through a link above your home,")
        else:
            _trusted_folder(candidate, info, trusted)
        current = candidate
    for index, name in enumerate(below):
        candidate = os.path.join(current, name)
        info = _lstat(candidate)
        if info is None:
            if not create:
                return None
            if index < len(below) - 1:
                _refuse("containment_unreadable", f"{candidate}, the operator's home, does not exist")
            try:
                os.mkdir(candidate, 0o700)
                os.chmod(candidate, 0o700)  # made here: 0700 whatever the umask
            except FileExistsError:
                pass
            except OSError as exc:
                _refuse("containment_unreadable", f"{candidate} cannot be made in {current}: {type(exc).__name__}")
            info = _lstat(candidate)
            if info is None:
                _refuse("containment_unreadable", f"{candidate} was removed while it was made")
        if stat.S_ISLNK(info.st_mode):
            _refuse("containment_untrusted", f"{candidate} is a link: the setting is not read through a link")
        if not stat.S_ISDIR(info.st_mode):
            _refuse("containment_untrusted", f"{candidate} is not a folder")
        _trusted_folder(candidate, info, trusted)
        current = candidate
    return current


def _root_alone(path: str, info: os.stat_result | None, said: str) -> None:
    """``path`` is root's and neither group nor others can write in it, sticky or not."""

    if info is None:
        _refuse("containment_unreadable", f"{path} does not exist")
    if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        _refuse("containment_untrusted",
                f"{path}, {said} is not root's alone (another user owns it or can write in it)")


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
        _close(fd)
    return _parse(data, path)


def _read_held(fd: int, path: str) -> tuple[bytes, os.stat_result]:
    try:
        info = os.fstat(fd)
    except OSError as exc:
        _refuse("containment_unreadable", f"{path}: {type(exc).__name__}")
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
