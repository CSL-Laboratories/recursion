"""Install, upgrade and uninstall the per-user daemon's systemd user unit and its stores.

``install`` creates the breaker store and the revocation store where the daemon opens them, then
writes the unit; run again, it finds both stores and the same unit bytes and changes nothing.
The ledger step binds the authority ledger to an anchor key. ``recursiond install`` (``make_anchor``)
makes the install's own Ed25519 key, owner-only under ``~/.config/recursion/authority/``, and binds the
new ledger to its public half; run again with key and ledger agreeing, it writes nothing, and a key and a
ledger that disagree are refused, typed, before anything is written. A caller that passes neither a key
nor ``make_anchor`` gets ``needs the anchor key, which recursiond install makes`` and no ledger.
``upgrade`` does the same for an existing unit and rewrites it (a new interpreter, or a hand-placed
unit from before the installer). ``uninstall`` removes the unit it wrote and nothing else: not the
folders around it, which other units share, and not the daemon's state folder with the run
registry and the stores in it (lane INSTALL's question 3).

Before any store is made, ``install`` and ``upgrade`` read the file-system type of the folder the stores
go in from the kernel's mount table (``filesystem_type``) and refuse 9p, overlay and network disks
(``store_filesystem_unsupported``, ``check_store_ground``), where SQLite's write-ahead log can damage a
database; a table that cannot be read refuses nothing and the plan says the type was not read. The folder
is read as the kernel resolves it, from opened folders (``store_ground``); every store is made through the
folder opened when its disk was read, by its descriptor, and every store made or found is held to that folder
(``store_ground_changed``). A step that fails after it began to make the stores removes what it made.

Each returns a ``Plan`` with the ``systemctl --user`` commands that apply the change. This module
runs none of them: it starts no process, opens no socket and touches no system unit.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass

from .errors import InstallRefused
from .layout import (BREAKER_FILE, LEDGER_FILE, REVOCATION_FILE, STATE_FOLDER, STORE_FOLDER, Owned, Roots,
                     check_dirs, check_private_dir, check_state_root, default_state_home, ensure_dir,
                     ensure_state_root, file_untrusted, held_regular, owned_paths, read_regular_and_stat,
                     replace_regular, still_holds, still_names, unlink_regular, write_exclusive)
from .layout import _MAX_LINKS, _existing_folder, _folders_below, _fsync_dir, _nearest_trusted
from .unit import MARKER, UNIT_NAME, render_unit, state_elsewhere

_UNIT_MODE = 0o644
_DIR_MODE = 0o700
_KEY_MODE = 0o600
# The whole detail of a ledger step that has no anchor to bind. The installer does not choose one.
NEEDS_ANCHOR = "needs the anchor key, which recursiond install makes"
# The whole detail of the ledger step of ``make_anchor`` where this interpreter cannot import
# ``cryptography`` (the optional ``plugins`` extra): no key can be made, so no ledger is made either.
NEEDS_BACKEND = "needs the cryptography package to make this install's authority key (csl-recursion[plugins])"
# Key ids (``signing.key_id``) on CSL's published list: such a key is refused as an anchor
# (``authority.py``: a profile is signed on the user's machine, never by CSL). None is published yet.
CSL_KEY_IDS: frozenset = frozenset()
# The one line ``recursiond install`` prints when it made the key.
ANCHOR_MADE = ("Made this install's own authority key (owner-only, {path}); it signs your order permissions on "
               "this machine and never leaves it. Losing it means making new permissions; it is not recoverable.")
# The file-system kinds the stores are not made on: SQLite's write-ahead log can damage a database there. The
# same kinds as the doctor's store.filesystem row; append-only (a kind added later is appended in both places).
REFUSED_FILESYSTEMS = ("9p", "v9fs", "drvfs", "overlay", "nfs", "nfs4", "cifs", "smb3")
# The kernel's mount table the type is read from.
_MOUNTINFO = "/proc/self/mountinfo"
# The values ``check_store_ground`` takes for a refused kind: refuse (the default) or go on, the risk accepted.
_FILESYSTEM_UNSAFE = ("refuse", "accept_risk")
# The whole detail of ``store_filesystem_unsupported``. {folder} is the folder whose type was read (the store
# folder, or its nearest folder that is there, resolved as the kernel resolves it, so the findmnt command runs
# as printed); {quoted} is that folder quoted for a POSIX shell (``shlex.quote``), so the pasted command reads
# back exactly it; a folder holding a control character is shown in one escaped form in both (``_shown``).
# {link} is empty, or one sentence on a link on the way whose target is not there (``_link_words``). The XDG_STATE_HOME way is
# for install: an upgrade whose unit names another state folder is still refused ``unit_state_elsewhere``, with
# that refusal's own ways out.
_STORE_GROUND = ("Recursion does not create its stores in {folder}: it is on {kind}, where SQLite's write-ahead log "
                 "can damage a database. Put a local disk or volume at {folder} (in a container: mount a volume "
                 "there) and run recursiond install again, or run it with XDG_STATE_HOME set to an absolute folder "
                 "on a local disk.{link} Check a folder with: findmnt -no FSTYPE -T {quoted}")
# The whole detail of ``store_ground_undecided``: the disk under a store folder cannot be told.
_UNDECIDED = ("Recursion cannot tell which disk {folder} is on, so it creates no store there: {why}. Make "
              "every folder on the way to it a folder you can open, with no loop of links, or run recursiond "
              "install with XDG_STATE_HOME set to an absolute folder on a local disk, then run it again.")
# The whole details of ``store_ground_changed``: a folder read was moved or replaced before, or while, the
# stores were made.
_CHANGED_BEFORE = ("{where} was moved or replaced after its disk was read and before the stores were made, so "
                   "Recursion made nothing. Leave the folders on the way to it as they are while the command "
                   "runs, and run it again.")
_CHANGED_AFTER = ("{path} does not lead to the store in {where}, the folder whose disk was read ({kind}): a "
                  "folder on the way was moved or replaced while the stores were made, so Recursion removes "
                  "what this run made. Leave the folders on the way to it as they are while the command runs, "
                  "and run it again. See where it leads now with: findmnt -no FSTYPE -T {quoted}")
# How long a second install or upgrade of one profile waits for the first to end, in seconds: 0 (the default)
# refuses it at once, None waits as long as the first runs. A module value of the draft: a settings row to come.
INSTALL_LOCK_WAIT: float | None = 0.0
# How often a waiting install or upgrade looks again whether the first has ended, in seconds; a settings row to come.
INSTALL_LOCK_POLL = 0.05
# The whole detail of ``install_running``: another install or upgrade of the same profile holds its lock.
_RUNNING = ("another install of this profile is running (recursiond install or upgrade for {home}), so this one made "
            "nothing. Let it end, then run the command again.")
# The plan's line when the profile cannot be locked here (a disk that keeps no such locks): the step goes on.
_UNLOCKED = ("{home} could not be locked for this step ({why}), so it ran without the profile's lock: run one "
             "install or upgrade of this profile at a time")


@dataclass(frozen=True)
class Plan:
    action: str
    changed: bool
    unit: str
    commands: tuple[tuple[str, ...], ...]
    stores: tuple[tuple[str, str, bool], ...] = ()  # (kind, path, created now), install and upgrade
    # (path, created now, detail). Detail is ``needs the anchor key, which recursiond install makes`` when the step had no
    # key and the ledger was not there. None on uninstall, which does not touch the ledger.
    ledger: tuple[str, bool, str] | None = None
    # install and upgrade with a chosen place: the daemon's state folder in use, and one plain sentence when
    # the default's plugin-security folder holds a store, which is left where it is and not used. "" otherwise.
    state: str = ""
    left: str = ""
    # make_anchor: (the key's path, made now). None when no key was asked for, or none can be made here.
    anchor: tuple[str, bool] | None = None
    # install and upgrade: one line per store folder whose file-system type was not read, or whose refused
    # kind was accepted (``check_store_ground``). () when every type was read and none is refused.
    filesystem: tuple[str, ...] = ()


def plan_commands(action: str) -> tuple[tuple[str, ...], ...]:
    """The user-manager commands that apply ``action``. Never ``sudo``, never a system unit.

    install: reload, then ``enable`` without ``--now`` (it starts at the next login; ``up`` starts
    it now). upgrade: reload, then ``try-restart`` (restarts it only if it is running). uninstall:
    ``stop``, then reload (``uninstall`` refuses while the unit is still enabled).
    """

    if action == "install":
        return (("systemctl", "--user", "daemon-reload"), ("systemctl", "--user", "enable", UNIT_NAME))
    if action == "upgrade":
        return (("systemctl", "--user", "daemon-reload"), ("systemctl", "--user", "try-restart", UNIT_NAME))
    if action == "uninstall":
        return (("systemctl", "--user", "stop", UNIT_NAME), ("systemctl", "--user", "daemon-reload"))
    raise InstallRefused("action", repr(action))


# A folder of the walk is opened as a folder and never through a link at its own name: a link is read
# (``readlink``) and followed by the walk itself, as the kernel follows it.
_FOLDER_OPEN = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0))


@dataclass(frozen=True)
class Ground:
    """What ``store_ground`` read for one folder of the stores.

    ``where`` is the folder's nearest folder that is there, as the kernel names the folder the walk opened for it;
    ``below`` the names under it that are not there yet (``ensure_dir`` makes them); ``kind`` its file-system type
    (None: the mount table was not read); ``link`` and ``target`` a link on the way whose target is not there, and
    that target ("" when none); ``device`` and ``inode`` the opened folder's, which the stores made are held to.
    ``fd`` is the opened folder while the caller's ``held`` stack holds it, else -1."""

    folder: str
    where: str
    below: tuple[str, ...]
    kind: str | None
    link: str = ""
    target: str = ""
    device: int = 0
    inode: int = 0
    fd: int = -1


def _undecided(folder: str, why: str) -> InstallRefused:
    """``store_ground_undecided``: the disk under ``folder`` cannot be told, so nothing is made on it."""

    return InstallRefused("store_ground_undecided", _UNDECIDED.format(folder=_shown(folder), why=why))


def _walk(folder: str) -> tuple[int, str, tuple[str, ...], str, str]:
    """Open ``folder``'s nearest folder that is there, resolved as the kernel resolves it, from opened folders.

    From ``/``, one name at a time, each opened relative to the folder opened before it (``dir_fd``): a link is
    read and its target walked in its place (from ``/`` when the target is absolute), at most ``_MAX_LINKS``
    links, the kernel's own limit; ``..`` goes up from the folder the walk has reached, never from the text before
    it, so ``<link>/..`` is the parent of where the link leads. The first name that is not there ends the walk:
    the names from it on are read as the folders ``ensure_dir`` makes read them (below a folder that is not there
    no link can stand), and a ``..`` among them walks on from the folder reached. Returns the opened folder (the
    caller closes it), its path as the walk reached it, the names below it, and the link whose target was not
    there with that target ("" and "" when none)."""

    if type(folder) is not str or not folder.startswith("/") or "\0" in folder:
        raise InstallRefused("store_ground_undecided", f"a store folder is an absolute path, not {folder!r}")
    names = [(name, "") for name in folder.split("/") if name]
    targets: dict[str, str] = {}
    links = 0
    fd, path = os.open("/", _FOLDER_OPEN), "/"
    try:
        while names:
            name, via = names.pop(0)
            if name == ".":
                continue
            if name == "..":
                if path != "/":
                    up = os.open("..", _FOLDER_OPEN, dir_fd=fd)
                    os.close(fd)
                    fd, path = up, os.path.dirname(path)
                continue
            try:
                below = os.open(name, _FOLDER_OPEN, dir_fd=fd)
            except FileNotFoundError:
                rest = [name] + [later for later, _ in names if later != "."]
                if ".." in rest:
                    names = [(part, via) for part in os.path.normpath("/".join(rest)).split("/")]
                    continue
                return fd, path, tuple(rest), via, targets.get(via, "")
            except OSError as exc:
                here = os.path.join(path, name)
                if exc.errno not in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                    raise _undecided(folder, f"{_shown(here)} cannot be opened ({errno.errorcode.get(exc.errno, exc.errno)})") from None
                try:
                    target = os.readlink(name, dir_fd=fd)
                except OSError:
                    raise _undecided(folder, f"{_shown(here)} is not a folder") from None
                links += 1
                if links > _MAX_LINKS:
                    raise _undecided(folder, f"more than {_MAX_LINKS} links on the way") from None
                targets[here] = target
                if target.startswith("/"):
                    root = os.open("/", _FOLDER_OPEN)
                    os.close(fd)
                    fd, path = root, "/"
                names[:0] = [(part, here) for part in target.split("/") if part]
                continue
            os.close(fd)
            fd, path = below, os.path.join(path, name)
        return fd, path, (), "", ""
    except BaseException:
        os.close(fd)
        raise


def _kernel_name(fd: int, path: str, folder: str) -> str:
    """The path the kernel gives the opened folder (``/proc/self/fd``), else the walk's own; a folder removed
    since it was opened is ``store_ground_undecided``."""

    try:
        name = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return path
    if name.endswith(" (deleted)"):
        raise _undecided(folder, f"{_shown(path)} was removed while its disk was read")
    return name if name.startswith("/") else path


def _point(field: str) -> str | None:
    """A mount point of the table, normalised once: unescaped, a run of slashes read as one, ``.`` and a trailing
    slash dropped, ``..`` taken back, so a point of only slashes is ``/``. None for an empty point or one that is
    not absolute: it holds no folder. The kernel writes the plain form; only a hand-made table writes others."""

    point = _unescape(field)
    if not point.startswith("/"):
        return None
    return os.path.normpath("/" + point.lstrip("/"))


def _mount_type(where: str) -> str | None:
    """The type of the LONGEST normalised mount point that holds ``where`` (of two equal ones the later line: a
    mount over a mount), or None when the table cannot be read or names no mount for it. Only ``store_ground``
    calls this: ``where`` is always the kernel's name of a folder it opened."""

    try:
        # As the kernel writes it: only a newline ends a line; a carriage return is part of a field (newline="").
        with open(_MOUNTINFO, encoding="utf-8", errors="surrogateescape", newline="") as table:
            lines = table.read().split("\n")
    except (OSError, ValueError):
        return None
    best, kind = -1, None
    for line in lines:
        fields = line.split(" ")
        try:
            dash = fields.index("-", 6)
            point, fstype = _point(fields[4]), fields[dash + 1]
        except (ValueError, IndexError):
            continue
        if point is None:
            continue
        holds = point == "/" or where == point or where.startswith(point + "/")
        if holds and len(point) >= best:
            best, kind = len(point), fstype
    return kind


def store_ground(folder: str, held: contextlib.ExitStack | None = None) -> Ground:
    """THE ONE READING of the disk a store folder is on: every caller in the installer goes through it.

    The folder is walked as the kernel resolves it, from opened folders (``_walk``: links first, then ``..``),
    to its nearest folder that is there; that folder is opened, and the type is read for the name the kernel
    gives the opened folder, never for the text written. So every spelling of one place (a trailing or doubled
    slash, ``.``, ``..`` after a link, a link of the person's own) gives one answer. With ``held``, the opened
    folder stays open in it (``Ground.fd``) so the stores made can be held to it (``_still_on_ground``,
    ``_made_on_ground``); without, it is closed before the answer. What cannot be told (a loop of links, a
    folder on the way that cannot be searched, a name that is not a folder) is ``store_ground_undecided``."""

    fd, path, below, link, target = _walk(folder)
    try:
        info = os.fstat(fd)
        where = _kernel_name(fd, path, folder)
        ground = Ground(folder, where, below, _mount_type(where), link, target, info.st_dev, info.st_ino,
                        fd if held is not None else -1)
    except BaseException:
        os.close(fd)
        raise
    if held is None:
        os.close(fd)
    else:
        held.callback(os.close, fd)
    return ground


def _unescape(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _needs_escape(text: str) -> bool:
    """True when ``text`` holds a character that is not a plain printable one (``str.isprintable``): a control
    character of either range, a line or paragraph separator, a format character, a byte that is not text."""

    return not text.isprintable()


def _escaped(text: str) -> str:
    """``text`` in a shell's ``$'...'`` form: every character that is not a plain printable one as an escape
    (``\\n``, ``\\t``; ``\\xHH`` for a control character below 128 and for a byte that is not text; ``\\uHHHH``
    or ``\\UHHHHHHHH`` for any other), so the words stay one line whatever the folder's name holds."""

    out = []
    for c in text:
        code = ord(c)
        if c in "\\'":
            out.append("\\" + c)
        elif c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        elif 0xDC80 <= code <= 0xDCFF:
            out.append(f"\\x{code - 0xDC00:02x}")
        elif code < 128 and not c.isprintable():
            out.append(f"\\x{code:02x}")
        elif not c.isprintable():
            out.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
        else:
            out.append(c)
    return "$'" + "".join(out) + "'"


def _shown(path: str) -> str:
    """A folder as the words name it: as it is, or, when it holds a character that is not a plain printable one
    (a newline, a tab, a line separator, a byte that is not text), in one escaped form (``_escaped``), the same in the sentence and in the command, so the
    words stay one line and name the folder exactly."""

    return _escaped(path) if _needs_escape(path) else path


def _quoted(path: str) -> str:
    """The folder as the findmnt command takes it: ``shlex.quote``, or the escaped form ``_shown`` uses."""

    return _escaped(path) if _needs_escape(path) else shlex.quote(path)


def _link_words(ground: Ground) -> str:
    if not ground.link:
        return ""
    return (f" {_shown(ground.link)} is a link to {_shown(ground.target)}, which is not there: what is made through "
            f"it would be made in {_shown(ground.where)}, so that folder's disk was read.")


def filesystem_type(folder: str) -> str | None:
    """The file-system type of ``folder`` as the kernel's mount table (``/proc/self/mountinfo``) gives it, or
    None when the table cannot be read or names no mount for it: ``store_ground(folder).kind``, read for the
    nearest folder of it that is there, resolved as the kernel resolves it. Standard library only; nothing is
    written."""

    return store_ground(folder).kind


def _judge(folders, filesystem_unsafe: str = "refuse",
           held: contextlib.ExitStack | None = None) -> tuple[tuple[str, ...], tuple[Ground, ...]]:
    """``check_store_ground``, and the grounds read (each through ``store_ground``, held open in ``held``)."""

    if filesystem_unsafe not in _FILESYSTEM_UNSAFE:
        raise InstallRefused("filesystem_unsafe", f"filesystem_unsafe is {filesystem_unsafe!r}, not refuse or accept_risk")
    lines, grounds, seen = [], [], set()
    for folder in dict.fromkeys(folders):
        ground = store_ground(folder, held)
        grounds.append(ground)
        if (ground.device, ground.inode, ground.below) in seen:
            continue  # the same place written another way: one answer, said once
        seen.add((ground.device, ground.inode, ground.below))
        shown = _shown(ground.where)
        if ground.kind is None:
            lines.append(f"the file-system type of {shown} was not read{_link_words(ground)}")
        elif ground.kind in REFUSED_FILESYSTEMS:
            if filesystem_unsafe != "accept_risk":
                detail = _STORE_GROUND.format(folder=shown, kind=ground.kind, quoted=_quoted(ground.where),
                                              link=_link_words(ground))
                raise InstallRefused("store_filesystem_unsupported", detail)
            lines.append(f"{shown} is on {ground.kind}, where SQLite's write-ahead log can damage a database; the risk "
                         f"was accepted{_link_words(ground)}")
    return tuple(lines), tuple(grounds)


def check_store_ground(folders, filesystem_unsafe: str = "refuse") -> tuple[str, ...]:
    """Refuse to make the stores on a refused kind of disk (``REFUSED_FILESYSTEMS``), before anything is made.

    For each folder: its ground is read (``store_ground``: the nearest folder that is there, resolved as the kernel
    resolves it, and its type); a refused kind is ``store_filesystem_unsupported``, the detail naming the folder
    read, the kind and the ways out (and a link on the way whose target is not there), unless
    ``filesystem_unsafe`` is ``accept_risk``: then the step goes on and one line names the folder, the kind and
    that the risk was accepted. A type that cannot be read refuses nothing: one line says it was not read. A
    ground that cannot be told is ``store_ground_undecided``. Returns those lines."""

    return _judge(folders, filesystem_unsafe)[0]


def _same(info: os.stat_result, ground: Ground) -> bool:
    return (info.st_dev, info.st_ino) == (ground.device, ground.inode)


def _still_on_ground(grounds: tuple[Ground, ...]) -> None:
    """Before the stores are made: each folder read still names the folder opened for it (``store_ground``).
    One moved or replaced since (a link put in its place, another folder renamed over it) is
    ``store_ground_changed``, and nothing is made."""

    for ground in grounds:
        try:
            same = _same(os.stat(ground.where), ground)
        except OSError:
            same = False
        if not same:
            raise InstallRefused("store_ground_changed", _CHANGED_BEFORE.format(where=_shown(ground.where)))


def _lock_profile(paths: Owned, held: contextlib.ExitStack) -> str:
    """One install or upgrade of a profile at a time: the profile's home folder is opened and locked (``flock``)
    for the whole step, from its first judgment to its last link, and let go at the answer (``held``). A second
    step of the same profile waits ``INSTALL_LOCK_WAIT`` seconds for the first to end, looking again every
    ``INSTALL_LOCK_POLL`` seconds, and is then ``install_running`` with nothing made. Returns "" when locked, or the
    plan's line saying why the home could not be locked (a platform or disk that keeps no such locks): the step
    then goes on, and that line says to run one at a time."""

    try:
        import fcntl
    except ImportError:
        return _UNLOCKED.format(home=_shown(paths.home), why="this platform has no flock")
    try:
        fd = os.open(paths.home, _FOLDER_OPEN | getattr(os, "O_NONBLOCK", 0))  # the resolved home, never a link
    except OSError as exc:
        return _UNLOCKED.format(home=_shown(paths.home), why=errno.errorcode.get(exc.errno, str(exc.errno)))
    deadline = None if INSTALL_LOCK_WAIT is None else time.monotonic() + INSTALL_LOCK_WAIT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if deadline is not None and time.monotonic() >= deadline:
                os.close(fd)
                raise InstallRefused("install_running", _RUNNING.format(home=_shown(paths.home))) from None
            time.sleep(INSTALL_LOCK_POLL if deadline is None else max(0.0, min(INSTALL_LOCK_POLL,
                                                                                deadline - time.monotonic())))
            continue
        except OSError as exc:
            os.close(fd)
            return _UNLOCKED.format(home=_shown(paths.home), why=errno.errorcode.get(exc.errno, str(exc.errno)))
        held.callback(os.close, fd)  # closing the folder lets the lock go, after everything else the step holds
        return ""


def _store_folders(paths: Owned) -> tuple[str, ...]:
    """The folders of every store the step makes: the breaker and revocation stores' and the ledger's."""

    return (os.path.dirname(paths.breaker_store), os.path.dirname(paths.revocation_store), os.path.dirname(paths.ledger))


def _made_on_ground(grounds: tuple[Ground, ...], run: "_Run") -> None:
    """After the stores are made: every store this step made or found is in the folder opened when its disk was read.

    Each store's entry, opened from that folder's descriptor one name at a time and never through a link, must be
    on the opened folder's device and be the very file at the store's path, and a store made now must still be the
    file this step made and holds open (``_Run.proven``). A store elsewhere (a folder on the way moved or replaced
    meanwhile) is ``store_ground_changed``, naming it, and the step removes what it made (``_Run.undo``)."""

    for path, fd, name, mine in run.stores:
        run.proven(path, fd, name, mine)


# The step (install or upgrade) whose stores are being made: ``ensure_stores`` and ``ensure_ledger`` called inside
# it make and find the stores through the folders it judged (``_Run``).
_RUN: contextvars.ContextVar = contextvars.ContextVar("recursion_install_run", default=None)
# The folders of a step (those made through an opened folder, and the private folder a store is built in) are
# made by this one call, bound when the module is loaded.
_mkdir = os.mkdir
# The sentence added to a failure after which this step could not remove all it made.
_NOT_CLEANED = (" This run could not remove what it made at {paths}: remove {it} once nothing uses {it}, then run "
                "the command again.")
# The sentence added to a failure when the key this step made is no longer at its name, so it was not removed.
_KEY_MOVED = (" The authority key this run made at {path} was moved or replaced while the command ran, so this run "
              "could not remove it: what is at {path} now is left as it is, and the key moved away is not bound to a "
              "ledger; remove it wherever it was moved, then run the command again.")


def _remove_entry(fd: int, name: str, mine: tuple[int, int]) -> None:
    """Remove ``name`` in the opened folder ``fd`` while it is the file ``mine``; anything else there is kept."""

    with contextlib.suppress(OSError):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if (info.st_dev, info.st_ino) == mine:
            os.unlink(name, dir_fd=fd)


def _take_away(fd: int, name: str, mine: tuple[int, int]) -> tuple[str, str]:
    """Remove the file ``name`` of the opened folder ``fd`` only if, at the moment it is removed, it is the very
    file ``mine`` this run made: the entry is first renamed to a fresh name of this run in the same folder, where
    no other program puts a file, and the file at that name is compared with ``mine``; only then is it unlinked. A
    file of another program found so is put back at its name (never over a file put there since) and kept.
    Returns ("removed", ""), ("gone", "") when nothing is at the name, ("other", "") when another file is there,
    or ("left", the name the run's file is at) when it could not be removed."""

    aside = f".{name}.{secrets.token_hex(8)}.gone"
    try:
        os.rename(name, aside, src_dir_fd=fd, dst_dir_fd=fd)
    except FileNotFoundError:
        return "gone", ""
    except OSError:
        return "left", name
    try:
        info = os.stat(aside, dir_fd=fd, follow_symlinks=False)
    except OSError:
        return "left", aside
    if (info.st_dev, info.st_ino) == mine:
        try:
            os.unlink(aside, dir_fd=fd)
        except FileNotFoundError:
            return "gone", ""
        except OSError:
            return "left", aside
        return "removed", ""
    try:  # another program's file: back at its name, never over one put there since
        os.link(aside, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.unlink(aside, dir_fd=fd)
    except OSError:
        with contextlib.suppress(OSError):
            os.stat(name, dir_fd=fd, follow_symlinks=False)
            return "other", ""
        with contextlib.suppress(OSError):
            os.rename(aside, name, src_dir_fd=fd, dst_dir_fd=fd)
    return "other", ""


def _open_store(path: str, magic: str, *, check_only: bool = False) -> None:
    """``plugin_security.stores``' own check (``check_store``) or open (``open_store``) of a store, its refusal
    kept as the installer's."""

    from recursion_core.plugin_security import Refused, stores

    try:
        if check_only:
            stores.check_store(path, magic)
        else:
            stores.open_store(path, magic).close()
    except Refused as refusal:
        raise InstallRefused(refusal.code, f"{path}: {refusal.detail}") from None
    except (OSError, sqlite3.Error) as exc:
        raise InstallRefused("store_not_created", f"{path}: {type(exc).__name__}: {exc}") from None


def _built(path: str, create) -> bytes:
    """A new store's bytes: ``create`` (``plugin_security.stores``' own create, which builds, checks and links it)
    run in a private temporary folder of this process, read back, and that folder removed."""

    from recursion_core.plugin_security import Refused

    build = os.path.join(tempfile.gettempdir(), f".recursion-store-{secrets.token_hex(8)}")
    try:
        _mkdir(build, 0o700)
    except OSError as exc:
        raise InstallRefused("store_not_created", f"{path}: no private folder to build it in: {type(exc).__name__}: "
                             f"{exc}") from None
    try:
        built = os.path.join(build, os.path.basename(path))
        try:
            create(built)
        except Refused as refusal:
            if refusal.code.endswith("_invalid"):
                # The helper's refusal of a new store it could not build or put in place: a store not made.
                raise InstallRefused("store_not_created", f"{path}: {refusal.code}: {refusal.detail}") from None
            raise InstallRefused(refusal.code, f"{path}: {refusal.detail}") from None
        except (OSError, sqlite3.Error) as exc:
            raise InstallRefused("store_not_created", f"{path}: {type(exc).__name__}: {exc}") from None
        with open(built, "rb") as source:
            return source.read()
    finally:
        shutil.rmtree(build, ignore_errors=True)


def _checked_way(paths: Owned) -> None:
    """The rule ``ensure_state_root`` and ``ensure_dir`` hold a folder that is there to (a link, a file, or a folder
    another user owns or can write is refused instead of being used), for each folder from the state root to the
    store folder that is there, read only: the folders that are not there are made by ``_Run.folder``."""

    root = _nearest_trusted(paths.state_root) if paths.chosen else paths.state_root
    for current in _folders_below(root, paths.store_dir):
        if not _existing_folder(current):
            return


class _Run:
    """What one step makes of the stores, through the folders it judged (``store_ground``), and how it is undone.

    A folder below a judged folder is opened from the judged folder's descriptor, one name at a time and never
    through a link, and made there (0700) when it is not there. A new store is built by ``plugin_security.stores``
    in a private temporary folder (``_built``); its bytes are written through the opened store folder under a
    temporary name (``O_EXCL``, ``O_NOFOLLOW``, held open until the answer) and linked to the store's name
    exclusively, so a store already at that name is never taken for the one just made. Every store, made or found,
    is held to the opened folder by the kernel's identity of the file (``proven``). ``undo`` removes what the step
    made, newest first, each only while its name still holds the file or folder made, and the key made now with
    the ledger, and returns the paths it could not remove."""

    def __init__(self, grounds: tuple[Ground, ...], held: contextlib.ExitStack) -> None:
        self.grounds = grounds
        self.by_folder = {ground.folder: ground for ground in grounds}
        self.held = held
        self.folders: dict[str, tuple[int, Ground]] = {}
        # (what, opened folder or -1, name or path, (device, inode), path), in the order made
        self.made: list[tuple[str, int, str, tuple[int, int], str]] = []
        self.stores: list[tuple[str, int, str, tuple[int, int]]] = []
        self.notes: list[str] = []  # sentences of ``undo`` beside the paths it could not remove
        # The key made now: its path, the bytes made (None: no key made), whether it was linked, and its folders
        # as opened before and after it was written (``key_folder``).
        self.key_path, self.pem, self.key_written = "", None, False
        self.key_dirs: list[int] = []

    def covers(self, paths: Owned) -> bool:
        return all(folder in self.by_folder for folder in _store_folders(paths))

    def folder(self, folder: str) -> int:
        """The store folder ``folder``, opened (and the folders below its judged folder made) from the judged
        folder's descriptor; held open until the answer."""

        if folder in self.folders:
            return self.folders[folder][0]
        ground = self.by_folder[folder]
        changed = InstallRefused("store_ground_changed", _CHANGED_BEFORE.format(where=_shown(ground.where)))
        if ground.fd < 0:
            raise changed
        fd, path = ground.fd, ground.where
        for name in ground.below:
            path = os.path.join(path, name)
            made = False
            try:
                try:
                    below = os.open(name, _FOLDER_OPEN, dir_fd=fd)
                except FileNotFoundError:
                    try:
                        _mkdir(name, _DIR_MODE, dir_fd=fd)
                        made = True
                    except FileExistsError:
                        pass  # another caller made it since the look: held to the same rules below
                    below = os.open(name, _FOLDER_OPEN, dir_fd=fd)
            except OSError:
                raise changed from None
            self.held.callback(os.close, below)
            info = os.fstat(below)
            if made:
                self.made.append(("folder", fd, name, (info.st_dev, info.st_ino), path))
            if info.st_dev != ground.device:
                raise changed
            if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o022):
                raise InstallRefused("folder_untrusted", f"{_shown(path)} is another user's or writable by others, so "
                                     "no store is made in it: `chmod 700` it, or remove it, then run the command again")
            fd = below
        self.folders[folder] = (fd, ground)
        return fd

    def there(self, path: str) -> bool:
        fd = self.folder(os.path.dirname(path))
        try:
            os.stat(os.path.basename(path), dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise InstallRefused("store_not_created", f"{path}: {type(exc).__name__}: {exc}") from None
        return True

    def store(self, path: str, magic: str, create) -> bool:
        """Find the store at ``path`` in its judged folder, or make it there with ``create``. True when made now."""

        from recursion_core.plugin_security import stores

        prefix = stores.KINDS[magic]
        fd, name = self.folder(os.path.dirname(path)), os.path.basename(path)
        if self.there(path):
            self.proven(path, fd, name, None)  # before any open: the path leads into the folder judged
            _open_store(path, magic)
            self.stores.append((path, fd, name, self.proven(path, fd, name, None)))
            return False
        if create is None:
            raise InstallRefused(f"{prefix}_missing", f"{path}: {name}")
        for suffix in ("-wal", "-shm", "-journal"):  # a leftover side file would be replayed into the new store
            try:
                os.stat(name + suffix, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise InstallRefused(f"{prefix}_exists", f"{path}: {name}{suffix}")
        data = _built(path, create)
        part = f".{name}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            out = os.open(part, flags, 0o600, dir_fd=fd)
        except OSError as exc:
            raise InstallRefused("store_not_created", f"{path}: {type(exc).__name__}: {exc}") from None
        self.held.callback(os.close, out)  # the file made stays open until the answer: its identity is not reused
        info = os.fstat(out)
        mine = (info.st_dev, info.st_ino)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(out, view):]
            os.fsync(out)
            try:
                os.link(part, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                raise InstallRefused(f"{prefix}_exists", f"{path}: {name}") from None
            except OSError as exc:
                raise InstallRefused("store_not_created", f"{path}: cannot link the new store: {type(exc).__name__}: "
                                     f"{exc}") from None
            self.made.append(("file", fd, name, mine, path))
            os.fsync(fd)
        except OSError as exc:
            raise InstallRefused("store_not_created", f"{path}: {type(exc).__name__}: {exc}") from None
        finally:
            _remove_entry(fd, part, mine)
        self.proven(path, fd, name, mine)
        _open_store(path, magic, check_only=True)  # the store and the folders above it, by the stores' own rules
        self.stores.append((path, fd, name, mine))
        return True

    def key_folder(self, folder: str) -> None:
        """Open the key's folder (never through a link at its own name) and hold it until the answer, so ``undo``
        finds the key this step made in it wherever the folder's path leads by then. Called before the key is
        written and after."""

        with contextlib.suppress(OSError):
            fd = os.open(folder, _FOLDER_OPEN)
            self.held.callback(os.close, fd)
            self.key_dirs.append(fd)

    def _key_by_path(self) -> tuple[bool, list[str]]:
        """The key at its path, while the path still names the file read (``held_regular``) and that file holds the
        bytes this step made: removed through its folder, opened now, renamed aside and compared first
        (``_take_away``). Returns whether it was removed, and the path it could not remove."""

        folder, name = os.path.dirname(self.key_path), os.path.basename(self.key_path)
        with contextlib.suppress(OSError, InstallRefused):
            fd = os.open(folder, _FOLDER_OPEN)
            try:
                with held_regular(self.key_path) as (data, info):
                    entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if data != self.pem or (entry.st_dev, entry.st_ino) != (info.st_dev, info.st_ino):
                        return False, []  # another file at the path now: kept
                    status, at = _take_away(fd, name, (info.st_dev, info.st_ino))
                    return status == "removed", [os.path.join(folder, at)] if status == "left" else []
            finally:
                os.close(fd)
        return False, []

    def _key_in(self, fd: int) -> tuple[bool, list[str]]:
        """In an opened key folder the key's path no longer leads to: each file named as the key or as a temporary
        file of it that holds the bytes this step made (which no other caller holds) is removed (``_take_away``).
        Returns whether one was removed, and the paths it could not remove. A folder the path still leads to is
        left to ``_key_by_path``."""

        name = os.path.basename(self.key_path)
        try:
            here, there = os.fstat(fd), os.stat(os.path.dirname(self.key_path))
            if (here.st_dev, here.st_ino) == (there.st_dev, there.st_ino):
                return False, []
        except OSError:
            pass
        try:
            names = os.listdir(fd)
        except OSError:
            return False, []
        try:
            where = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            where = os.path.dirname(self.key_path)
        removed, left = False, []
        for entry in names:
            if entry != name and not (entry.startswith(f".{name}.") and entry.endswith(".tmp")):
                continue
            try:
                handle = os.open(entry, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                                 | getattr(os, "O_CLOEXEC", 0), dir_fd=fd)
            except OSError:
                continue
            try:
                info = os.fstat(handle)
                data = os.read(handle, len(self.pem) + 1) if stat.S_ISREG(info.st_mode) else b""
            except OSError:
                data = b""
            finally:
                os.close(handle)
            if data != self.pem:
                continue
            status, at = _take_away(fd, entry, (info.st_dev, info.st_ino))
            if status == "removed":
                removed = True
            elif status == "left":
                left.append(os.path.join(where, at))
        return removed, left

    def proven(self, path: str, fd: int, name: str, mine: tuple[int, int] | None) -> tuple[int, int]:
        """The store's entry in its opened folder is on that folder's device, is the file at ``path``, and, made now
        or seen before (``mine``), is that same file; else ``store_ground_changed``."""

        ground = self.folders[os.path.dirname(path)][1]
        try:
            entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            there = os.stat(path, follow_symlinks=False)
            same = ((entry.st_dev, entry.st_ino) == (there.st_dev, there.st_ino) and entry.st_dev == ground.device
                    and (mine is None or (entry.st_dev, entry.st_ino) == mine))
        except OSError:
            same = False
        if not same:
            raise InstallRefused("store_ground_changed", _CHANGED_AFTER.format(
                path=_shown(path), where=_shown(ground.where), kind=ground.kind or "a type that was not read",
                quoted=_quoted(os.path.dirname(path))))
        return (entry.st_dev, entry.st_ino)

    def made_at(self, path: str) -> None:
        """A folder made by path in this step (``ensure_dir``): removed by ``undo`` while it is that folder."""

        with contextlib.suppress(OSError):
            info = os.stat(path, follow_symlinks=False)
            self.made.append(("path", -1, path, (info.st_dev, info.st_ino), path))

    def undo(self) -> list[str]:
        """Remove what this step made, newest first; the paths it could not remove."""

        left = []
        for what, fd, name, mine, path in reversed(self.made):
            if what == "file":  # proven to be the very file made at the moment it is removed
                status, at = _take_away(fd, name, mine)
                if status == "left":
                    left.append(os.path.join(os.path.dirname(path), at))
                continue
            try:
                info = os.stat(name, dir_fd=fd if fd >= 0 else None, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                left.append(path)
                continue
            if (info.st_dev, info.st_ino) != mine:
                continue  # another file or folder at the name now: not this step's, left as it is
            try:  # a folder: rmdir removes it only while it is empty
                os.rmdir(name, dir_fd=fd if fd >= 0 else None)
            except FileNotFoundError:
                pass
            except OSError:
                left.append(path)
        if self.pem is not None:  # the key made now, with the ledger made for it (removed above, newest first)
            removed, kept = self._key_by_path()
            left += kept
            for fd in self.key_dirs:
                found, kept = self._key_in(fd)
                removed = removed or found
                left += [where for where in kept if where not in left]
            if self.key_written and not removed:
                self.notes.append(_KEY_MOVED.format(path=_shown(self.key_path)))
        return left

    def undone(self, exc: BaseException) -> BaseException:
        """``exc`` after ``undo``: an ``InstallRefused`` with one sentence naming what could not be removed."""

        left = self.undo()
        words = "".join(self.notes)
        if left:
            words = _NOT_CLEANED.format(paths=", ".join(_shown(path) for path in left),
                                        it="it" if len(left) == 1 else "them") + words
        if not words:
            return exc
        if isinstance(exc, InstallRefused):
            return InstallRefused(exc.kind, exc.detail + words)
        if hasattr(exc, "add_note"):
            exc.add_note(words.strip())
        return exc


@contextlib.contextmanager
def _in_run(paths: Owned):
    """The step ``ensure_stores`` and ``ensure_ledger`` make the stores in: the caller's (install, upgrade), or,
    called on their own, one of their own over the store folders (each judged, ``accept_risk``: they refuse no kind
    of disk themselves), whose stores are proven at its end and undone on a failure."""

    run = _RUN.get()
    if run is not None and run.covers(paths):
        yield run
        return
    with contextlib.ExitStack() as held:
        _lock_profile(paths, held)  # one step of this profile at a time
        run = _Run(_judge(_store_folders(paths), "accept_risk", held)[1], held)
        token = _RUN.set(run)
        try:
            yield run
            _made_on_ground(run.grounds, run)
        except BaseException as exc:
            refusal = run.undone(exc)
            if refusal is exc:
                raise
            raise refusal from None
        finally:
            _RUN.reset(token)


def ensure_stores(paths: Owned) -> tuple[tuple[str, str, bool], ...]:
    """Create the breaker store and the revocation store where the daemon opens them, or check them.

    A missing store is made by ``plugin_security.stores`` (built under a temporary name, linked into
    place exclusively, then checked with ``open_store``). A store already there must pass
    ``open_store`` and is left as it is. A refusal keeps the store's own code
    (``breaker_store_untrusted``, ``revocation_store_invalid``, ``breaker_store_exists`` for a
    leftover ``-wal`` beside a missing store, ...); a store that cannot be made at all (a full disk,
    a umask that takes the owner's own write bit, an I/O error at the final link: the helper's
    ``<store>_invalid`` for a new store, its code kept in the detail) is ``store_not_created``. Returns
    ``(kind, path, created now)`` for each. The ledger is not one of these: ``ensure_ledger`` is the
    step that would create it, and it has no anchor key unless the caller passes one. Each store is found or
    made through the folder its disk was read for (``_Run``: inside ``install`` or ``upgrade``, theirs; called
    on its own, one of its own that refuses no kind of disk), and a failure removes what was made.
    """

    # Imported here, not with this module: ``recursiond --help`` builds its parser from this package.
    from recursion_core.plugin_security import stores

    if type(paths) is not Owned:
        raise InstallRefused("paths", "ensure_stores takes owned_paths(roots)")
    with _in_run(paths) as run:
        _checked_way(paths)
        done = []
        for path, magic, create in ((paths.breaker_store, stores.BREAKER_MAGIC, stores.create_breaker_store),
                                    (paths.revocation_store, stores.REVOCATION_MAGIC, stores.create_revocation_store)):
            done.append((stores.KINDS[magic], path, run.store(path, magic, create)))
    return tuple(done)


def ensure_ledger(paths: Owned, anchor_public_key: str | None) -> tuple[str, bool, str]:
    """The ledger step. Returns ``(path, created now, detail)``.

    ``anchor_public_key`` is the key ``authority.create_ledger`` binds the new ledger to:
    ``install`` and ``upgrade`` pass the install's own key with ``make_anchor`` (``find_anchor``),
    or the key their caller passed. None, or an empty string, reports
    ``needs the anchor key, which recursiond install makes`` when the ledger is not there and creates nothing. A
    caller that has the key creates a missing ledger through ``plugin_security.stores.create_ledger``
    (that calls ``authority.create_ledger``). A ledger already there is checked with
    ``open_store`` and left as it is, whether or not a key was passed.
    """

    return _ensure_ledger(paths, anchor_public_key, create=True)


def _ensure_ledger(paths: Owned, anchor_public_key: str | None, *, create: bool) -> tuple[str, bool, str]:
    """``ensure_ledger``. Without ``create`` (a key found with its ledger) a ledger not there now is
    ``authority_anchor_disagrees`` and nothing is created: a key that had a ledger never gets a new one."""

    from recursion_core.plugin_security import Refused, stores

    if type(paths) is not Owned:
        raise InstallRefused("paths", "ensure_ledger takes owned_paths(roots)")
    if anchor_public_key is not None and type(anchor_public_key) is not str:
        raise InstallRefused("anchor_public_key", "the anchor is the installer's public key, or absent")
    with _in_run(paths) as run:  # found or made through the folder its disk was read for
        _checked_way(paths)
        path = paths.ledger
        if not run.there(path):
            if not anchor_public_key:
                return (path, False, NEEDS_ANCHOR)
            if not create:
                raise InstallRefused("authority_anchor_disagrees", _ledger_gone_detail(paths))
            run.store(path, stores.LEDGER_MAGIC, lambda built: stores.create_ledger(built, anchor_public_key))
            return (path, True, "")
        run.store(path, stores.LEDGER_MAGIC, None)
        return (path, False, "")


def _open_ledger(path: str) -> None:
    from recursion_core.plugin_security import Refused, stores

    try:
        stores.open_store(path, stores.LEDGER_MAGIC).close()
    except Refused as refusal:
        raise InstallRefused(refusal.code, f"{path}: {refusal.detail}") from None
    except (OSError, sqlite3.Error) as exc:
        raise InstallRefused("store_not_created", f"{path}: {type(exc).__name__}: {exc}") from None


@dataclass(frozen=True)
class _Found:
    """What ``find_anchor`` read: the backend's classes, and the key's public half (None: no key yet)."""

    backend: object
    public: str | None


def find_anchor(paths: Owned) -> _Found | None:
    """Read the install's own key, the ledger's binding and ``install.json``'s ``authority_anchor``, and
    refuse unless they agree. Reads only; called before anything is made. None where this interpreter
    cannot import ``cryptography``: then nothing is read here and no key can be made.

    Agreeing: no key, no ledger and no ``authority_anchor`` (a fresh profile: ``make_anchor_key`` makes
    the key), or a key whose public half the ledger is bound to, with ``authority_anchor`` null or that
    same key. Any other state is ``authority_anchor_disagrees``, its detail naming which part: a key
    without a ledger, a ledger without a key, a ledger bound to another key, an ``authority_anchor``
    without a key or naming another key. A new key is never made over a ledger: it would orphan the
    ledger's spend history. A key or an ``authority_anchor`` on CSL's published list is
    ``anchor_is_csl_key``. The key file must be a regular file (a link is refused, ``not_regular``, never
    followed), the person's and open to nobody else (``anchor_key_untrusted``), in a folder open to
    nobody else (``folder_untrusted``), holding an unencrypted Ed25519 PKCS#8 PEM key
    (``anchor_key_invalid``). No detail names the key's bytes."""

    from recursion_core.plugin_security.signing import key_id

    if type(paths) is not Owned:
        raise InstallRefused("paths", "find_anchor takes owned_paths(roots)")
    backend = _ed25519()
    if backend is None:
        return None
    configured = _configured_anchor(paths)
    if configured is not None and _key_id(configured, "install.json's authority_anchor") in CSL_KEY_IDS:
        raise InstallRefused("anchor_is_csl_key", f"install.json's authority_anchor ({configured}) is on CSL's "
                             "published list; an install's anchor is its own key")
    ledger_there = os.path.lexists(paths.ledger)
    check_dirs(paths.home, paths.authority_dir)  # a link, or a folder another user could change, on the way
    if os.path.lexists(paths.authority_dir):
        _check_key_dir(paths.authority_dir)
    if not os.path.lexists(paths.anchor_key):
        if ledger_there:
            raise InstallRefused("authority_anchor_disagrees", _ledger_alone_detail(paths))
        if configured is not None:
            raise InstallRefused("authority_anchor_disagrees", f"install.json's authority_anchor is {configured} "
                                 f"and no key is at {paths.anchor_key}; nothing was written")
        return _Found(backend, None)
    public = _read_anchor_key(paths.anchor_key, backend)
    if key_id(public) in CSL_KEY_IDS:
        raise InstallRefused("anchor_is_csl_key", f"the key at {paths.anchor_key} is on CSL's published list; "
                             "an install's anchor is its own key")
    if not ledger_there:
        raise InstallRefused("authority_anchor_disagrees", _key_alone_detail(paths))
    bound = _ledger_anchor(paths.ledger)
    if bound != key_id(public):
        raise InstallRefused("authority_anchor_disagrees", f"{paths.ledger} is bound to another key ({bound}) "
                             f"than the one at {paths.anchor_key} ({key_id(public)}); nothing was written")
    if configured is not None and configured != public:
        raise InstallRefused("authority_anchor_disagrees", f"install.json's authority_anchor is {configured}, not "
                             f"the public half of the key at {paths.anchor_key} ({public}); nothing was written")
    return _Found(backend, public)


def make_anchor_key(paths: Owned, found: _Found) -> str:
    """Make the install's own Ed25519 key pair and write its private half, owner-only, to
    ``paths.anchor_key`` (folder 0700 under the home's checks, file 0600, a fresh temporary name made with
    ``O_EXCL`` and ``O_NOFOLLOW``, then linked into place exclusively: anything there meanwhile is
    ``already_present``). PKCS#8 PEM, unencrypted (the same-uid limit is stated in ``authority.py``, not
    solved). Returns the public half, 64 lowercase hex. Never printed, logged or answered; no copy is made.
    A failure after the key was linked (the temporary name's removal, or the folder's fsync) removes it
    again: the key file is removed only while it holds the bytes this call wrote."""

    return _make_key(paths, found)[0]


def _make_key(paths: Owned, found: _Found, run: "_Run | None" = None) -> tuple[str, bytes]:
    """``make_anchor_key``, also returning the private key's PEM so a failure later in the same run can
    tell the key it made from any other. Inside a step (``run``), the key's folder is opened before the key is
    written and after (``_Run.key_folder``), and the step's undo removes the key by its path (``_Run._key_by_path``)
    or, where the path leads elsewhere by then, by its bytes through those folders (``_Run._key_in``)."""

    if found.public is not None:
        raise InstallRefused("anchor_key", "the key is there already")
    private_class, serialization = found.backend
    if ensure_dir(paths.home, paths.authority_dir, mode=_DIR_MODE):
        _sync_parents(paths.home, paths.authority_dir)
    _check_key_dir(paths.authority_dir)
    key = private_class.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    if run is not None:  # the step's undo finds the key by its bytes in the folders opened for it
        run.key_path, run.pem = paths.anchor_key, pem
        run.key_folder(paths.authority_dir)
    try:
        write_exclusive(paths.anchor_key, pem, _KEY_MODE)
    except BaseException:
        _remove_key_made(paths.anchor_key, pem)  # linked before the failure; a key another linked first stays
        raise
    if run is not None:
        run.key_written = True
        run.key_folder(paths.authority_dir)
    return public, pem


def _sync_parents(root: str, path: str) -> None:
    """After folders were made on the way to ``path``: fsync the parent of each folder from ``path`` up to
    ``root``, so the new folders' own entries are on disk before the key is written into the last one
    (``write_exclusive`` syncs the key's entry in ``path`` itself). Only where the platform has a directory
    sync (``O_DIRECTORY``)."""

    if not hasattr(os, "O_DIRECTORY"):
        return
    cur = path
    while cur != root:
        parent = os.path.dirname(cur)
        if parent == cur:
            return
        _fsync_dir(parent)
        cur = parent


def _remove_key_made(path: str, pem: bytes) -> bool:
    """Remove the key file at ``path`` if it holds ``pem``, the private key this run made (no other caller
    can hold those bytes), and only while the path still names the file read: it is held open, and its
    device and inode are compared with the path's (``still_names``) immediately before the unlink, so a
    key moved onto the path after the read is kept. One window is left: a file moved there between that
    comparison and the unlink (``unlink_regular``'s own lookup and its unlink). Called on a failure: a
    second failure here leaves the first to be raised. True when it removed the key."""

    with contextlib.suppress(OSError, InstallRefused):
        with held_regular(path) as (data, info):
            if data == pem:
                still_names(path, info)  # another file at the path now: unit_changed, and it is kept
                return unlink_regular(path)
    return False


def _bound_to(ledger: str, public: str) -> bool:
    """True when a ledger is at ``ledger`` and bound to ``public``. Reads only; any failure is False."""

    from recursion_core.plugin_security.signing import key_id

    try:
        return os.path.lexists(ledger) and _ledger_anchor(ledger) == key_id(public)
    except (InstallRefused, OSError, sqlite3.Error):
        return False


def _key_alone_detail(paths: Owned) -> str:
    return (f"the key at {paths.anchor_key} is there and no ledger is at {paths.ledger}, so the two do not agree; "
            "this run wrote nothing. A key is left without its ledger when an install stops between making "
            "the key and making the ledger, or when the ledger was made in another state folder. If it was, "
            "run the command again with XDG_STATE_HOME naming that folder (unset for the default one). To "
            f"start again with a new key, move {paths.anchor_key} out of {paths.authority_dir} and run the "
            "command again: a new key and a new ledger are made, and permissions the old key signed are not "
            "accepted by them")


def _ledger_alone_detail(paths: Owned) -> str:
    return (f"{paths.ledger} is there and no key is at {paths.anchor_key}. The ledger holds the spend history "
            "of the permissions its key signed, so no new key is made over it; this run wrote nothing. If the "
            f"key was moved, put it back at {paths.anchor_key} (the person's own, mode 0600) and run the "
            "command again. If it is lost, the permissions it signed cannot be used again; the ledger is left "
            f"as it is, and a new key is made only when no ledger is at {paths.ledger}")


def _ledger_gone_detail(paths: Owned) -> str:
    return (f"{paths.ledger} was there with the key at {paths.anchor_key} when this run looked, and is not there "
            "now. A key that had a ledger is never given a new ledger, which would take the place of that "
            "ledger's spend history, so none was made and the key is left as it is. Put the ledger back at "
            f"{paths.ledger} and run the command again")


def _ed25519():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        return None
    return (Ed25519PrivateKey, serialization)


def _key_id(value: object, where: str) -> str:
    from recursion_core.plugin_security import Refused
    from recursion_core.plugin_security.signing import key_id

    try:
        return key_id(value)
    except Refused:
        raise InstallRefused("authority_anchor_disagrees", f"{where} is not a public key (64 lowercase hex)") from None


def _configured_anchor(paths: Owned) -> str | None:
    """``install.json``'s ``authority_anchor`` through the daemon's own reader (one parser); None when the file
    or the value is absent. Read only: this step never writes ``install.json``."""

    from recursion_core.daemon.plugins import PLUGINS_DIR, PluginRefused, read_install

    try:
        return read_install(os.path.join(paths.config, PLUGINS_DIR))["authority_anchor"]
    except PluginRefused as refusal:
        raise InstallRefused(refusal.code, f"install.json: {refusal.detail}") from None


def _check_key_dir(path: str) -> None:
    info = os.lstat(path)
    if info.st_mode & 0o077 or (info.st_mode & stat.S_IRWXU) != stat.S_IRWXU:
        raise InstallRefused("folder_untrusted", f"{path} holds the install's authority key and must be open to "
                             "nobody else and usable by its owner: `chmod 700` it")


def _read_anchor_key(path: str, backend) -> str:
    private_class, serialization = backend
    data, info = read_regular_and_stat(path)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise InstallRefused("anchor_key_untrusted", f"{path} must be the person's and open to nobody else: "
                             "`chmod 600` it")
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except Exception:  # a malformed, encrypted or unsupported key; its bytes are never named
        key = None
    if not isinstance(key, private_class):
        raise InstallRefused("anchor_key_invalid", f"{path} does not hold an unencrypted Ed25519 PKCS#8 PEM key")
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def _ledger_anchor(path: str) -> str | None:
    from recursion_core.plugin_security import Refused, stores

    try:
        db = stores.open_store(path, stores.LEDGER_MAGIC)
    except Refused as refusal:
        raise InstallRefused(refusal.code, f"{path}: {refusal.detail}") from None
    except (OSError, sqlite3.Error) as exc:
        raise InstallRefused("store_not_created", f"{path}: {type(exc).__name__}: {exc}") from None
    try:
        row = db.execute("SELECT v FROM meta WHERE k='anchor'").fetchone()
    except sqlite3.Error:
        raise InstallRefused("ledger_invalid", f"{path}: not a readable ledger") from None
    finally:
        db.close()
    return None if row is None else row[0]


def _found(paths: Owned, anchor_public_key: str | None, make_anchor: bool) -> _Found | None:
    if not make_anchor:
        return None
    if anchor_public_key is not None:
        raise InstallRefused("anchor_public_key", "pass the anchor key or make_anchor, not both")
    return find_anchor(paths)


def _anchor_and_ledger(paths: Owned, anchor_public_key: str | None, found: _Found | None,
                       make_anchor: bool, on_anchor_made=None) -> tuple[tuple[str, bool, str], tuple[str, bool] | None]:
    """The ledger step, after the stores. With ``make_anchor``: a fresh profile gets its key, then the
    ledger bound to it. Inside ``install`` and ``upgrade`` the key's folder is opened for the step
    (``_Run.key_folder``) and a failure of the step removes the key with the ledger made for it (``_Run.undo``);
    ``on_anchor_made`` is then called by the step only when it ended made (``_kept``). Outside a step, a
    failure from the key's link to the ledger's leaves the two agreeing or neither: a ledger already bound to
    the key just made keeps the key with it (and ``on_anchor_made(path)`` is called); with no ledger there,
    the key is removed again (``_remove_key_made``); a ledger there that is not bound to it, or cannot be
    read, keeps the key too. A process ended between the two can still leave the key alone, which the next
    run refuses with the ways on. A key that was found (with its ledger, ``find_anchor``) never gets a new
    ledger: one gone since the look is ``authority_anchor_disagrees``, the key and whatever moved the ledger
    left as they are."""

    if not make_anchor:
        return ensure_ledger(paths, anchor_public_key), None
    if found is None:  # no cryptography here: no key, so no ledger
        ledger = ensure_ledger(paths, None)
        return (ledger[0], ledger[1], NEEDS_BACKEND if ledger[2] == NEEDS_ANCHOR else ledger[2]), None
    if found.public is not None:  # found with its ledger: a ledger gone since the look is refused, never made anew
        return _ensure_ledger(paths, found.public, create=False), (paths.anchor_key, False)
    run = _RUN.get()
    if run is not None and run.covers(paths):  # the step removes the key with the ledger on a failure
        public, pem = _make_key(paths, found, run)
        return ensure_ledger(paths, public), (paths.anchor_key, True)
    public, pem = _make_key(paths, found)
    try:
        ledger = ensure_ledger(paths, public)
    except BaseException:
        if _bound_to(paths.ledger, public):  # made and bound before the failure: both stay, agreeing
            if on_anchor_made is not None:
                on_anchor_made(paths.anchor_key)
        elif not os.path.lexists(paths.ledger):
            _remove_key_made(paths.anchor_key, pem)
        raise
    if on_anchor_made is not None:
        on_anchor_made(paths.anchor_key)
    return ledger, (paths.anchor_key, True)


def install(roots: Roots, exec_args: tuple[str, ...], *, anchor_public_key: str | None = None,
            make_anchor: bool = False, on_anchor_made=None) -> Plan:
    """Create the stores and the route homes' custody root, then write the unit. The same bytes
    already there change nothing; other bytes, a unit another user could change (another user's, or
    writable by group or world) and a custody root open to anyone else or unusable by its owner are
    refused before anything is made; the root as made (or as another caller made it meanwhile) is held
    to the same rule before the unit is written. A unit found is held open until the answer and checked against
    its bytes and then its path last (``still_holds``, then ``still_names``): one removed, replaced or rewritten
    in place meanwhile is ``unit_missing`` or ``unit_changed``. A failure
    after the stores are begun (that, or a store refused after the other was made) removes what this run
    made (the stores, the folders made for them, a key made now) and names any path it could not remove;
    the stores found are left as they are. ``anchor_public_key``
    is passed to ``ensure_ledger``; the default None is the command, which has no key. A platform
    with no ``os.geteuid`` is refused ``platform_unsupported`` (one line naming the platform) before
    any path is read or written. With ``make_anchor`` (``recursiond install``) the ledger is bound to the
    install's own key, made on a fresh profile (``find_anchor``, ``make_anchor_key``); key, ledger and
    ``install.json``'s ``authority_anchor`` that disagree are refused before anything is made.
    ``on_anchor_made(path)``, if given, is called once when a key made now is kept: when the install ends
    made."""

    paths = owned_paths(roots)
    unit = render_unit(exec_args, paths.state_home if paths.chosen else None).encode()
    check_dirs(paths.home, paths.unit_dir)
    with contextlib.ExitStack() as held:  # a unit found stays open until the answer: its inode is not reused
        unlocked = _lock_profile(paths, held)  # one install or upgrade of this profile at a time, to the answer
        present = os.path.lexists(paths.unit)
        if present:
            read = held.enter_context(held_regular(paths.unit))  # checked against the path while open
            current, info = read
            if current != unit:
                raise InstallRefused("unit_differs", f"{paths.unit} holds another unit; `recursiond upgrade` replaces it")
            why = file_untrusted(paths.unit, info)
            if why:
                raise InstallRefused("unit_untrusted", f"{why}; `recursiond upgrade` writes it again, ours and 0644")
        check_state_root(paths)  # a chosen place: before anything is made
        check_private_dir(paths.state_root, paths.homes)  # before anything is made
        # before anything is made or opened; each folder read stays open until the answer (``store_ground``)
        ground, grounds = _judge(_store_folders(paths), held=held)
        ground += (unlocked,) if unlocked else ()
        found = _found(paths, anchor_public_key, make_anchor)  # before anything is made
        _still_on_ground(grounds)  # the folders read still name the folders opened: nothing swapped since
        run = _Run(grounds, held)  # the stores made through the folders opened, and undone on a failure
        token = _RUN.set(run)
        try:
            made = ensure_stores(paths)  # first: a unit is never left naming a daemon whose stores are missing
            ledger, anchor = _anchor_and_ledger(paths, anchor_public_key, found, make_anchor, on_anchor_made)
            _made_on_ground(grounds, run)  # each store made or found is in the folder whose disk was read
            homes = ensure_dir(paths.state_root, paths.homes, mode=_DIR_MODE)
            if homes:
                run.made_at(paths.homes)
            check_private_dir(paths.state_root, paths.homes, required=True)  # as made, or as another caller made it
            if present:
                still_holds(paths.unit, read)  # the bytes read, first: an in-place rewrite shows here
                still_names(paths.unit, info)  # last: the path still names the file that was read
            else:
                ensure_dir(paths.home, paths.unit_dir, mode=_DIR_MODE)
                write_exclusive(paths.unit, unit, _UNIT_MODE)
        except BaseException as exc:
            refusal = run.undone(exc)  # nothing this run made is left; what could not be removed is named
            if refusal is exc:
                raise
            raise refusal from None
        finally:
            _RUN.reset(token)
        _kept(paths, anchor, on_anchor_made)
        return _plan("install", not present or _any_created(made) or homes or ledger[1], paths, made, ledger, anchor,
                     ground)


def upgrade(roots: Roots, exec_args: tuple[str, ...], *, anchor_public_key: str | None = None,
            make_anchor: bool = False, on_anchor_made=None) -> Plan:
    """Make any missing store and the route homes' custody root, and rewrite the unit to what
    ``install`` would write now. A missing unit, and a custody root open to anyone else or unusable by
    its owner, are refused before anything is made; the root as made is held to the same rule before
    the unit is rewritten. A unit whose daemon may keep its state in another folder (``state_elsewhere``:
    its own ``XDG_STATE_HOME``, an ``EnvironmentFile``, the manager's value passed through) is refused
    ``unit_state_elsewhere`` before anything is made: replacing it would point the daemon at another
    state folder, without the revocations and breaker trips kept in its own. The same bytes are not
    written again unless
    another user could change the file; a unit kept is held open until the answer and checked
    against its bytes and then its path last, as in ``install``, whose words on a refusal after the stores
    apply. A platform with no ``os.geteuid`` is refused
    ``platform_unsupported`` (one line naming the platform) before any path is read or written.
    ``make_anchor`` and ``on_anchor_made`` as in ``install``: a profile installed before the key existed
    gets it here."""

    paths = owned_paths(roots)
    unit = render_unit(exec_args, paths.state_home if paths.chosen else None).encode()
    check_dirs(paths.home, paths.unit_dir)
    if not os.path.lexists(paths.unit):
        raise InstallRefused("unit_missing", f"no unit at {paths.unit}; `recursiond install` writes it")
    with contextlib.ExitStack() as held:  # the unit read is held open until the answer
        unlocked = _lock_profile(paths, held)  # one install or upgrade of this profile at a time, to the answer
        read = held.enter_context(held_regular(paths.unit))
        data, info = read
        current = data == unit and not file_untrusted(paths.unit, info)
        why = None if data == unit else state_elsewhere(data, paths.home, paths.state_home)
        if why:  # the S6 sweep's F1 on #410: before anything is made, the unit left as it is
            raise InstallRefused("unit_state_elsewhere", _elsewhere_detail(paths, data, why))
        check_state_root(paths)  # a chosen place: before anything is made
        check_private_dir(paths.state_root, paths.homes)  # before anything is made
        # before anything is made or opened; each folder read stays open until the answer (``store_ground``)
        ground, grounds = _judge(_store_folders(paths), held=held)
        ground += (unlocked,) if unlocked else ()
        found = _found(paths, anchor_public_key, make_anchor)  # before anything is made
        _still_on_ground(grounds)  # the folders read still name the folders opened: nothing swapped since
        run = _Run(grounds, held)  # the stores made through the folders opened, and undone on a failure
        token = _RUN.set(run)
        try:
            made = ensure_stores(paths)
            ledger, anchor = _anchor_and_ledger(paths, anchor_public_key, found, make_anchor, on_anchor_made)
            _made_on_ground(grounds, run)  # each store made or found is in the folder whose disk was read
            homes = ensure_dir(paths.state_root, paths.homes, mode=_DIR_MODE)
            if homes:
                run.made_at(paths.homes)
            check_private_dir(paths.state_root, paths.homes, required=True)  # as made, or as another caller made it
            if current:
                still_holds(paths.unit, read)  # the bytes read, first: an in-place rewrite shows here
                still_names(paths.unit, info)  # last: the path still names the file that was read
            else:
                replace_regular(paths.unit, unit, _UNIT_MODE)
        except BaseException as exc:
            refusal = run.undone(exc)  # nothing this run made is left; what could not be removed is named
            if refusal is exc:
                raise
            raise refusal from None
        finally:
            _RUN.reset(token)
        _kept(paths, anchor, on_anchor_made)
        return _plan("upgrade", not current or _any_created(made) or homes or ledger[1], paths, made, ledger, anchor,
                     ground)


def _elsewhere_detail(paths: Owned, data: bytes, why: str) -> str:
    """The text of ``unit_state_elsewhere``: what the unit does, and only ways out that work.

    With a place the person chose, or a unit this installer wrote (which names a chosen place when it is
    refused), the two ways that work: run ``recursiond upgrade`` again with ``XDG_STATE_HOME`` naming the
    folder the unit names (unset where the unit names the default) to keep the state where it is, or
    the daemon stopped, ``recursiond uninstall``, the state folder the unit names moved to the folder this
    command would use,
    then ``recursiond install`` to start there: the ledger moves with the stores, since a key with no ledger
    is refused (``find_anchor``). A unit
    placed by hand, while the default applies, is still told to move the state folder and take its line
    out: that unit has no chosen place of this installer's in it."""

    said = (f"{paths.unit} {why}: the daemon it starts may keep its state (the revocation and breaker stores, the "
            f"run registry) there, and the unit `recursiond upgrade` writes puts it in {paths.state}. ")
    ours = data.split(b"\n", 1)[0] == MARKER.encode()
    if not ours and not paths.chosen:
        return said + (f"Stop the daemon, move that state folder to {paths.state}, take the line out of the unit, "
                       "then run `recursiond upgrade` again")
    keep = "unset" if state_elsewhere(data, paths.home) is None else "set to the folder that unit names"
    now = f"set to {paths.state_home}" if paths.chosen else "unset"
    start = ("run `recursiond uninstall`" if ours else
             f"remove {paths.unit}, which `recursiond install` did not write")
    return said + (f"To keep the state where it is, run `recursiond upgrade` again with XDG_STATE_HOME {keep}. "
                   f"To start in {paths.state} instead, stop the daemon, {start}, move the state folder that unit "
                   f"names (with the ledger in it) to {paths.state}, where nothing may be yet, then "
                   "run `recursiond install` with "
                   f"XDG_STATE_HOME {now}: the ledger moves with the stores, since a key with no ledger is refused")


def uninstall(roots: Roots) -> Plan:
    """Remove the unit this installer wrote. Refused while it is enabled, or if another wrote it. The unit
    is removed while the file read is still held open, after the path was checked to name it."""

    paths = owned_paths(roots)
    check_dirs(paths.home, paths.unit_dir)
    if os.path.lexists(paths.enabled):
        raise InstallRefused("unit_enabled", f"run `systemctl --user disable --now {UNIT_NAME}` first")
    if not os.path.lexists(paths.unit):
        return _plan("uninstall", False, paths)
    try:
        with held_regular(paths.unit) as (data, _):  # checked against the path while held; removed while held
            if data.split(b"\n", 1)[0] != MARKER.encode():
                raise InstallRefused("unit_not_ours", f"{paths.unit} was not written by `recursiond install`; it is left as it is")
            return _plan("uninstall", unlink_regular(paths.unit), paths)
    except InstallRefused as refusal:
        if refusal.kind != "unit_missing":
            raise
        return _plan("uninstall", False, paths)  # removed since the lexists above: nothing is left to remove


def leftovers(roots: Roots) -> tuple[str, ...]:
    """The files ``uninstall`` removes that are present now: the unit. The stores stay (question 3)."""

    paths = owned_paths(roots)
    return tuple(path for path in (paths.unit,) if os.path.lexists(path))


def _kept(paths: Owned, anchor: tuple[str, bool] | None, on_anchor_made) -> None:
    """At the end of a step that ended made: ``on_anchor_made`` for a key made now, which is kept."""

    if anchor is not None and anchor[1] and on_anchor_made is not None:
        on_anchor_made(paths.anchor_key)


def _any_created(made: tuple[tuple[str, str, bool], ...]) -> bool:
    return any(created for _, _, created in made)


def _plan(action: str, changed: bool, paths: Owned, stores: tuple[tuple[str, str, bool], ...] = (),
          ledger: tuple[str, bool, str] | None = None, anchor: tuple[str, bool] | None = None,
          ground: tuple[str, ...] = ()) -> Plan:
    chosen = paths.chosen and action != "uninstall"
    return Plan(action=action, changed=changed or bool(anchor and anchor[1]), unit=paths.unit,
                commands=plan_commands(action), stores=stores, ledger=ledger, state=paths.state if chosen else "",
                left=_default_stores_left(paths) if chosen else "", anchor=anchor, filesystem=ground)


def _default_stores_left(paths: Owned) -> str:
    """One plain sentence when the default's plugin-security folder holds a store while the person chose
    another place; "" otherwise. Nothing there is read, moved or removed: only whether a name is present."""

    folder = os.path.join(default_state_home(paths.home), STATE_FOLDER, STORE_FOLDER)
    found = [name for name in (BREAKER_FILE, REVOCATION_FILE, LEDGER_FILE) if os.path.lexists(os.path.join(folder, name))]
    if not found:
        return ""
    return (f"{folder} holds {', '.join(found)} from the default state folder; they are left where they are "
            f"and not used, since XDG_STATE_HOME chose {paths.state_home}")
