"""The held-folder walk on Linux and macOS, and the typed refusal everywhere else.

An ``Anchor`` holds one open folder. Every name below it is a single part, opened relative to the held
folder with ``O_NOFOLLOW``, so no part of a path is looked up again by its text after it was checked
and a link below the anchor is never followed, whoever owns it. Above the anchor, ``open_anchor``
follows a link only by the rule the caller names (``above``) and then checks that the folder it holds
is the folder it vetted (the same device and file number).

Three pieces are copies, not imports, because they live only in open drafts; each is named where it is
used: the trust store's owner check above a folder (``stores._check_ancestors``, ``_lstat`` and
``_trusted_folder``), the registry checker's typed root and its held tree (``registry._open_root`` and
``registry._Tree``), and the settings-file gate's walk and its platform check (``open_guard._open_walk``
and ``_walk_flags``). Standard library only; nothing is kept between two anchors.
"""

from __future__ import annotations

import errno
import os
import secrets
import shlex
import stat
import threading
from pathlib import Path

# One finite set. A later cause is appended; none is renamed or removed.
CAUSES = ("link", "not_a_folder", "not_regular", "owner", "writable", "too_many_links", "changed", "bad_name",
          "missing", "too_large", "platform_unsupported")
ABOVE = ("owner_trusted", "operator_root")

_UNSAFE_BITS = stat.S_IWGRP | stat.S_IWOTH
_MAX_LINKS = 40  # Linux's MAXSYMLINKS: the links one path lookup follows before ELOOP
_NO_COPY = ("an Anchor cannot be copied or pickled: it owns one open folder; open another anchor on the "
            "same path instead")


class AnchorRefused(Exception):
    """Nothing was opened, made, written, moved or removed. ``cause`` is one of ``CAUSES``.

    ``path`` is the absolute path of what was refused and ``way_out`` one plain sentence saying what to
    move or change, ending with running the command again. A caller maps ``cause`` to its own code.
    """

    def __init__(self, cause: str, path: str, way_out: str) -> None:
        if cause not in CAUSES:
            raise ValueError(f"not a cause of this module: {cause!r}")
        super().__init__(f"{cause}: {way_out}")
        self.cause = cause
        self.path = path
        self.way_out = way_out


def _shown(path) -> str:
    """``path`` as an absolute path, quoted for the shell, so a printed command runs as printed.

    Copied from the plugin installer's ``_shown`` (``plugin_security/install.py``, #537 at c10880e6).
    """
    return shlex.quote(str(Path(path).absolute()))


def _parent(path: str) -> str:
    return os.path.dirname(path.rstrip("/")) or "/"


def _refused(cause: str, path: str, *, name: str | None = None, limit: int | None = None) -> AnchorRefused:
    """The refusal with its way out. The link sentence is the plugin installer's ``_link_refusal``'s."""
    p, up = _shown(path), _shown(_parent(path))
    if cause == "link":
        text = (f"{p} is a link, and this command does not follow links. Move the link out of {up} and put the "
                f"file or folder it stands for in its place; then run the command again.")
    elif cause == "owner":
        text = (f"{p} is owned by another user, and this command uses only folders and files that root or you "
                f"own. Move it out of {up} and put one you own in its place; then run the command again.")
    elif cause == "writable":
        text = (f"{p} can be written by group or world. Take those write bits off with chmod go-w {p}; then "
                f"run the command again.")
    elif cause == "not_a_folder":
        text = f"{p} is not a folder. Move it out of {up} and put a folder in its place; then run the command again."
    elif cause == "not_regular":
        text = (f"{p} is not a regular file. Move it out of {up} and put a regular file in its place; then run "
                f"the command again.")
    elif cause == "too_many_links":
        text = (f"More than {_MAX_LINKS} links lead to {p}. Name it by a path with fewer links; then run the "
                f"command again.")
    elif cause == "changed":
        text = (f"{p} changed while it was being opened. Stop whatever is moving it; then run the command "
                f"again.")
    elif cause == "bad_name":
        text = (f"{name!r} is not a single name below {p}. Give one name with no separator that is not '.', "
                f"'..' or empty; then run the command again.")
    elif cause == "missing":
        text = f"{p} does not exist. Make it, or name one that exists; then run the command again."
    elif cause == "too_large":
        text = (f"{p} is larger than {limit} bytes, the most this command reads. Make it smaller or move it out "
                f"of {up}; then run the command again.")
    else:  # platform_unsupported
        text = (f"This system cannot open {p} without following links: it has no folder-relative open with "
                f"O_NOFOLLOW and O_DIRECTORY. Run the command on Linux or macOS.")
    return AnchorRefused(cause, path, text)


def _walk_flags():
    """The folder and file flags of the walk, or None where this platform lacks them.

    Copied from the settings-file gate (``setup/open_guard.py`` ``_walk_flags``, #533 at 5b0be1ee, :39-50),
    with the other folder-relative calls this module makes added to the check.
    """
    for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "O_CLOEXEC"):
        if not hasattr(os, name):
            return None
    if not hasattr(os, "geteuid") or not hasattr(os, "fchmod") or not hasattr(os, "fsync"):
        return None
    for call in (os.open, os.mkdir, os.rename, os.stat, os.unlink, os.rmdir):  # os.replace is os.rename's call
        if call not in os.supports_dir_fd:
            return None
    if os.scandir not in os.supports_fd:
        return None
    base = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    return base | os.O_DIRECTORY, base | os.O_NONBLOCK


def _kind_of(info: os.stat_result) -> str:
    # Copied from the registry checker (plugin_security/registry.py ``_kind_of``, #537 at 2d9005cb, :372-379).
    if stat.S_ISLNK(info.st_mode):
        return "link"
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    if stat.S_ISREG(info.st_mode):
        return "file"
    return "other"


def _same(held: os.stat_result, vetted: os.stat_result) -> bool:
    """The folder held is the folder vetted: the same device and the same file number."""
    return (held.st_dev, held.st_ino) == (vetted.st_dev, vetted.st_ino)


# -- above the anchor: the trust store's owner check ------------------------------------------------
# Copied from plugin_security/stores.py at #382 80b832f5 (``_check_ancestors`` :142-186, ``_lstat``
# :189-195, ``_trusted_folder`` :198-202), statement for statement. Two changes: a refusal is an
# ``AnchorRefused`` with its cause, and the check returns the link-free path it resolved to.

def _lstat(path: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        raise _refused("missing", path) from None
    except NotADirectoryError:
        raise _refused("not_a_folder", _parent(path)) from None


def _trusted_folder(path: str, info: os.stat_result, trusted: tuple) -> None:
    if info.st_uid not in trusted:
        raise _refused("owner", path)
    if info.st_mode & _UNSAFE_BITS and not info.st_mode & stat.S_ISVTX:
        raise _refused("writable", path)


def _check_ancestors(folder: str) -> str:
    """Refuse a path that another user could make name something else above ``folder``.

    Resolves the folders above ``folder`` from ``/`` one name at a time, as the kernel does. Each folder
    on the way must be root's or this user's, and one that group or world can write must carry the
    sticky bit (as ``/tmp`` does); the next name is then held to the same rule. A link on the way must be
    root's or ours; it is followed (``/home`` -> ``/var/home``) and its target is resolved the same way,
    at most ``_MAX_LINKS`` links. ``..`` goes up from where the resolution has got to, not from the
    text before it. Returns the folder above ``folder`` as a path that holds no link.
    """
    trusted = (0, os.geteuid())
    names = [name for name in os.path.dirname(folder).split("/") if name]
    current, links = "/", 0
    _trusted_folder("/", _lstat("/"), trusted)
    while names:
        # ``current`` holds no link, so ``current/..`` and ``current/.`` name what the kernel's own
        # resolution reaches there, and they are checked like any other name.
        candidate = os.path.join(current, names.pop(0))
        info = _lstat(candidate)
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid not in trusted:
                raise _refused("owner", candidate)
            links += 1
            if links > _MAX_LINKS:
                raise _refused("too_many_links", folder)
            try:
                target = os.readlink(candidate)
            except FileNotFoundError:  # removed since the lstat above
                raise _refused("missing", candidate) from None
            except OSError:  # replaced by something that is not a link
                raise _refused("changed", candidate) from None
            if target.startswith("/"):
                current = "/"
            names[:0] = [part for part in target.split("/") if part]
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise _refused("not_a_folder", candidate)
        _trusted_folder(candidate, info, trusted)
        current = candidate
    return current


# -- the anchor ---------------------------------------------------------------------------------------

def open_anchor(path, *, above: str = "owner_trusted") -> "Anchor":
    """Vet ``path``, open it, hold it, and check that the folder held is the folder vetted.

    ``above="owner_trusted"``: the folders above ``path`` pass the trust store's owner check (a link
    on the way is followed only when root or the user owns it), and ``path`` itself is a folder, not a
    link, that root or the user owns and that group and world cannot write unless it is sticky.
    ``above="operator_root"``: a root the operator typed for one command is followed as typed, once,
    then held (the registry checker's rule). Below the anchor no link is ever followed.
    On a system without folder-relative opens, ``O_NOFOLLOW`` or ``O_DIRECTORY`` (Windows) this refuses
    ``platform_unsupported`` before anything is opened.
    """
    if above not in ABOVE:
        raise ValueError(f"above must be one of {ABOVE}, not {above!r}")
    text = os.fspath(path)
    if type(text) is not str:
        raise TypeError("the anchor's path must be text")
    flags = _walk_flags()
    if flags is None:
        raise _refused("platform_unsupported", text)
    if not os.path.isabs(text) or "\0" in text:
        raise ValueError("the anchor's path must be absolute and hold no NUL")
    text = text.rstrip("/") or "/"
    if above == "operator_root":
        fd, shown = _open_root(text, flags[0])
        return Anchor(fd, shown, flags, trusted=None)
    return Anchor(_open_owner_trusted(text, flags[0]), text, flags, trusted=(0, os.geteuid()))


def _open_owner_trusted(path: str, dir_flags: int) -> int:
    trusted = (0, os.geteuid())
    above = _check_ancestors(path)
    last = os.path.basename(path.rstrip("/"))
    vetted_path = os.path.join(above, last) if last else above
    vetted = _lstat(vetted_path)
    if stat.S_ISLNK(vetted.st_mode):
        raise _refused("link", path)
    if not stat.S_ISDIR(vetted.st_mode):
        raise _refused("not_a_folder", path)
    _trusted_folder(path, vetted, trusted)
    try:
        # ``vetted_path`` holds no link, and its last name is opened without following one.
        fd = os.open(vetted_path, dir_flags)
    except OSError as exc:
        raise _classified(exc, path, vetted_path) from None
    try:
        held = os.fstat(fd)
        if not _same(held, vetted):
            raise _refused("changed", path)
        _trusted_folder(path, held, trusted)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_root(path: str, dir_flags: int) -> tuple[int, str]:
    # Copied from the registry checker (plugin_security/registry.py ``_Tree._open_root``, #537 at
    # 2d9005cb, :408-427). The operator chose this path, so the open of the root itself may follow it.
    try:
        fd = os.open(path, (dir_flags & ~os.O_NOFOLLOW) | os.O_CLOEXEC)
    except FileNotFoundError:
        raise _refused("missing", path) from None
    except NotADirectoryError:
        raise _refused("not_a_folder", path) from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _refused("too_many_links", path) from None
        raise
    try:
        held = os.fstat(fd)
        shown = os.path.realpath(path)
        named = os.stat(shown)
        if not _same(held, named):
            raise _refused("changed", path)
    except BaseException:
        os.close(fd)
        raise
    return fd, shown


def _classified(exc: OSError, shown: str, name: str, dir_fd: int | None = None) -> Exception:
    """A refusal for an open that failed: the link, the missing name or the wrong kind; else the error."""
    if exc.errno == errno.ENOENT:
        return _refused("missing", shown)
    if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR, errno.ENXIO):
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            return _refused("missing", shown)
        if stat.S_ISLNK(info.st_mode):
            return _refused("link", shown)
        return _refused("not_a_folder" if exc.errno == errno.ENOTDIR else "not_regular", shown)
    return exc


def _mode(mode: object) -> int:
    """``mode`` as given when it is a whole number with no bit outside 0o777; else ``ValueError``.

    Setuid, setgid and the sticky bit are refused, as are a negative number and anything that is not an
    ``int``. A group- or world-writable mode given on purpose is applied as given.
    """
    if type(mode) is not int or mode & ~0o777:
        raise ValueError("mode must be a whole number from 0 to 0o777, with no setuid, setgid or sticky bit")
    return mode


def _read_bounded(fd: int, limit: int, shown: str) -> bytes:
    # Copied from the registry checker (plugin_security/registry.py ``_read_bounded``, #537 at 2d9005cb, :382-393).
    chunks = []
    size = 0
    while size <= limit:
        chunk = os.read(fd, min(1 << 20, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    if size > limit:
        raise _refused("too_large", shown, limit=limit)
    return b"".join(chunks)


class Anchor:
    """One held folder. Every name is a single part below it; a link there is never followed.

    The tree walk is the registry checker's ``_Tree`` (plugin_security/registry.py, #537 at 2d9005cb,
    :396-535) and the settings-file gate's ``_open_walk`` (setup/open_guard.py, #533 at 5b0be1ee,
    :147-160), turned so that the caller holds each folder: ``a/b`` is ``child("a").child("b")``.
    Use it as a context manager, or call ``release``; an anchor from ``child`` or ``ensure_dir`` is its
    own and is released on its own. An anchor that is never released (the first one of
    ``child("a").child("b")``) closes its folder when it is collected, as a file object does.
    Release it only when no other thread is inside a call on it, and do not copy it (a copy or a pickle
    raises ``TypeError``). A descriptor handed to the constructor belongs to the anchor from then on.
    """

    def __init__(self, fd: int, path: str, flags: tuple, trusted: tuple | None) -> None:
        self._lock = threading.Lock()  # before ``_fd``: ``__del__`` releases only when ``_fd`` is set
        self._fd: int | None = fd
        self.path = path
        self._flags = flags
        self._trusted = trusted

    # -- the held descriptor -----------------------------------------------------------------------

    @property
    def fd(self) -> int:
        if self._fd is None:
            raise ValueError(f"the anchor on {self.path} was released")
        return self._fd

    def release(self) -> None:
        with self._lock:  # the descriptor is taken and cleared as one step: two releases shut it once
            fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def __enter__(self) -> "Anchor":
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def __del__(self) -> None:
        if getattr(self, "_fd", None) is not None:
            self.release()

    def __copy__(self):
        raise TypeError(_NO_COPY)

    def __deepcopy__(self, memo):
        raise TypeError(_NO_COPY)

    def __reduce_ex__(self, protocol):
        raise TypeError(_NO_COPY)

    def __repr__(self) -> str:
        return f"Anchor({self.path!r}{', released' if self._fd is None else ''})"

    def _name(self, name: object) -> str:
        if (type(name) is not str or name in ("", ".", "..") or "/" in name or "\0" in name
                or (os.sep != "/" and os.sep in name) or (os.altsep and os.altsep in name)):
            raise _refused("bad_name", self.path, name=name)
        return name

    def _below(self, name: str) -> str:
        return os.path.join(self.path, name)

    # -- folders -------------------------------------------------------------------------------------

    def child(self, name: str) -> "Anchor":
        """The folder ``name`` below this one, opened without following a link, as an anchor of its own."""
        name = self._name(name)
        parent = self.fd
        try:
            fd = os.open(name, self._flags[0], dir_fd=parent)
        except OSError as exc:
            raise _classified(exc, self._below(name), name, parent) from None
        return Anchor(fd, self._below(name), self._flags, self._trusted)

    def ensure_dir(self, name: str, mode: int = 0o700) -> "Anchor":
        """``child(name)``, making the folder first, owner-only, when it is missing.

        ``mode`` is a whole number with no bit outside 0o777 (``ValueError`` before anything is made).
        """
        name = self._name(name)
        mode = _mode(mode)
        try:
            os.mkdir(name, mode, dir_fd=self.fd)  # makes no folder through a link: a link here is EEXIST
        except FileExistsError:
            pass
        return self.child(name)

    def entries(self) -> list:
        """``(name, kind)`` for each entry, sorted by name; kind is ``dir``, ``file``, ``link`` or ``other``."""
        parent = self.fd
        found = []
        # A listing of its own: a read position shared with another listing on this anchor (another
        # thread's) would make either one short, doubled or empty.
        own = os.open(".", self._flags[0], dir_fd=parent)
        try:
            with os.scandir(own) as listing:
                names = sorted(entry.name for entry in listing)
        finally:
            os.close(own)
        for name in names:
            kind = self._kind(parent, name)
            if kind is not None:
                found.append((name, kind))
        return found

    def kind(self, name: str) -> str | None:
        """The kind of ``name`` as it is, a link not followed; None when it does not exist."""
        return self._kind(self.fd, self._name(name))

    @staticmethod
    def _kind(parent: int, name: str) -> str | None:
        try:
            return _kind_of(os.stat(name, dir_fd=parent, follow_symlinks=False))
        except FileNotFoundError:
            return None

    # -- files ---------------------------------------------------------------------------------------

    def read(self, name: str, limit: int) -> bytes:
        """The bytes of the regular file ``name``, at most ``limit``; the user's own, not group- or world-writable."""
        name = self._name(name)
        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a whole number of bytes, 0 or more")
        parent, shown = self.fd, self._below(name)
        try:
            fd = os.open(name, self._flags[1], dir_fd=parent)  # no link followed, no wait on a FIFO
        except OSError as exc:
            raise _classified(exc, shown, name, parent) from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _refused("not_regular", shown)
            if info.st_uid != os.geteuid():
                raise _refused("owner", shown)
            if info.st_mode & _UNSAFE_BITS:
                raise _refused("writable", shown)
            if info.st_size > limit:
                raise _refused("too_large", shown, limit=limit)
            return _read_bounded(fd, limit, shown)
        finally:
            os.close(fd)

    def write_atomic(self, name: str, data: bytes, mode: int = 0o600) -> None:
        """Write ``data`` as ``name``: a new temporary file beside it, synced, renamed into place, folder synced.

        ``mode`` is a whole number with no bit outside 0o777 (``ValueError`` before anything is made); the
        file gets it exactly, whatever the umask.
        """
        name = self._name(name)
        mode = _mode(mode)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes")
        parent = self.fd
        temp = f".{name}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(temp, flags, 0o600, dir_fd=parent)
        placed = False
        try:
            try:
                view = memoryview(data).cast("B")
                while view:
                    view = view[os.write(fd, view):]
                os.fchmod(fd, mode)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
            except IsADirectoryError:
                raise _refused("not_regular", self._below(name)) from None
            placed = True
            os.fsync(parent)
        finally:
            if not placed:
                try:
                    os.unlink(temp, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def rename(self, name: str, to: "Anchor", to_name: str) -> None:
        """Move ``name`` here to ``to_name`` in the held folder ``to``.

        A link seen at ``name`` is refused. A link put there while the call runs may itself be moved, so a
        caller must not take the moved entry for a file; what a link points at is never touched.
        """
        name = self._name(name)
        if not isinstance(to, Anchor):
            raise TypeError("to must be an Anchor")
        to_name = to._name(to_name)
        parent, target = self.fd, to.fd
        kind = self._kind(parent, name)
        if kind is None:
            raise _refused("missing", self._below(name))
        if kind == "link":
            raise _refused("link", self._below(name))
        os.replace(name, to_name, src_dir_fd=parent, dst_dir_fd=target)
        os.fsync(target)
        if target != parent:
            os.fsync(parent)

    def remove(self, name: str) -> None:
        """Remove the file or the empty folder ``name``.

        A link seen at ``name`` is refused. A link put there while the call runs may itself be removed;
        what a link points at is never touched, and nothing outside the held folder is.
        """
        name = self._name(name)
        parent = self.fd
        kind = self._kind(parent, name)
        if kind is None:
            raise _refused("missing", self._below(name))
        if kind == "link":
            raise _refused("link", self._below(name))
        if kind == "dir":
            os.rmdir(name, dir_fd=parent)
        else:
            os.unlink(name, dir_fd=parent)
