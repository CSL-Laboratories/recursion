"""Whole-package verification (lane PLUGIN-SIGN).

``verify_package(directory, trust)`` reads a plugin package once, through one open directory,
and returns the bytes of every file whose SHA-256 is the one its manifest names, together with
the signature's verdict. A changed, added or removed file is not accepted: the result is
unsigned (``package_files_changed``) and those bytes are not returned. What cannot be read
safely is refused: a symbolic link, a device, a pipe or socket, a file or folder another user
owns or others can write, too many or too large files, an unreadable or malformed manifest.

The bytes returned are the ones that run: the daemon hands them to the loader, which compiles
the entry and every listed module a plugin imports from them and never from the folder again.
After its verdict and before it returns, ``verify_package`` lists the folder again through the
same open directory and compares the identity of every file and folder it read (device, inode,
type, size, change and modification times) with what it saw before the read. Anything that
differs, appeared or went away is ``changed_while_checked``: the result is unsigned
(``package_files_changed``) and the daemon loads nothing of it. ``package_changes`` makes the
same comparison again by path, just before the load.

``require_whole_package`` is the non-paper rule. Paper passes for any package. Any other
``scope.environment`` needs a version-1 manifest (it names every file), every file intact, and
a signature by a key on this install's list.

POSIX only: the reads go through ``dir_fd``. Elsewhere ``package_platform_unsupported``.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from types import MappingProxyType

from .errors import Refused
from .manifest import ENTRY_FILE, MANIFEST_SCHEMA_V1, MAX_MANIFEST_BYTES, Manifest, TrustStore, parse_manifest

__all__ = [
    "MANIFEST_FILE",
    "PAPER_ENVIRONMENT",
    "SIGNATURE_FILE",
    "VerifiedPackage",
    "files_digest",
    "package_changes",
    "require_whole_package",
    "verify_package",
]

# Paper is the only environment ``charge`` allows without a whole-package verification.
PAPER_ENVIRONMENT = "paper"
MANIFEST_FILE, SIGNATURE_FILE = "manifest.json", "manifest.sig"
MAX_SIGNATURE_BYTES = 4096
# The daemon's bound on plugin.py, applied to every file of the package.
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_FILES = 4096  # manifest version 1's own bound on ``files``
MAX_DEPTH = 32
# O_DIRECTORY and O_NOFOLLOW are what make an opened folder a folder and not a link to one.
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
# O_NONBLOCK: a pipe swapped in after the listing must not hang the read; fstat refuses it.
_FILE_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
               | getattr(os, "O_NONBLOCK", 0))
_UNSAFE = "a symbolic link, device, pipe or socket"
# The key of the package folder itself in ``VerifiedPackage.identity``.
_TOP = "."


@dataclass(frozen=True)
class VerifiedPackage:
    """What ``verify_package`` found. ``files`` holds only bytes that match the manifest."""

    manifest: Manifest
    trust: str                        # "signed" or "unsigned"
    signer_key_id: str | None         # set only when signed
    reason: str                       # classify's reason, or "package_files_changed"
    files: MappingProxyType          # path -> bytes; only files that match the manifest
    signature: str | None = None      # the signature file's text; None when absent
    changed: tuple = ()               # listed, present, other bytes
    added: tuple = ()                 # present, not listed
    missing: tuple = ()               # listed, not present
    changed_while_checked: tuple = ()  # read, then changed, added or removed before the check ended
    identity: tuple = ()              # ((path, (device, inode, type, size, mtime_ns, ctime_ns)), ...)
    entry: bytes | None = None        # plugin.py as it was read, matching its digest or not

    @property
    def intact(self) -> bool:
        """True when nothing listed was changed or missing, nothing extra is present, and
        nothing changed while the package was read and checked."""
        return not (self.changed or self.added or self.missing or self.changed_while_checked)

    @property
    def files_sha256(self) -> str:
        """``files_digest(files)``: one digest of every verified path and its bytes."""
        return files_digest(self.files)


def files_digest(files: object) -> str:
    """SHA-256 over each ``path NUL sha256(bytes) LF`` of ``files`` in path order.

    The same files give the same digest wherever they are held, so the digest of what was
    verified and the digest of what the loader serves can be compared.
    """
    outer = hashlib.sha256()
    for path in sorted(files):
        outer.update(path.encode("utf-8", "surrogateescape") + b"\0"
                     + hashlib.sha256(files[path]).hexdigest().encode("ascii") + b"\n")
    return outer.hexdigest()


def require_whole_package(result: object, environment: object) -> None:
    """Refuse a non-paper environment unless ``result`` is a whole signed package.

    Paper passes even when ``result`` is missing. Anything else needs a version-1 manifest
    (``names_every_file``), every file intact, and ``trust == "signed"``. A result that is
    not a ``VerifiedPackage`` is not a whole-package verification.
    """
    if type(environment) is str and environment == PAPER_ENVIRONMENT:
        return
    if type(result) is not VerifiedPackage:
        raise Refused(
            "package_not_signed_whole",
            "non-paper authority needs every file of the package verified and signed by a trusted key")
    if not result.manifest.names_every_file:
        raise Refused(
            "plugin_py_only_signature",
            "a signature that names only plugin.py is not enough for non-paper authority; "
            "sign the whole package as csl-plugin-manifest/1")
    if result.trust != "signed" or not result.intact:
        raise Refused(
            "package_not_signed_whole",
            "non-paper authority needs every file of the package verified and signed by a trusted key")


def _owned(info: os.stat_result, name: str) -> None:
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise Refused(
            "package_entry_untrusted",
            "%s: must be owned by this user and not writable by group or others" % name)


def _identity_of(info: os.stat_result) -> tuple:
    """What tells one file apart from its replacement: a new inode, a new type, a write."""
    if stat.S_ISDIR(info.st_mode):
        return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_file(dir_fd: int, name: str, shown: str, limit: int) -> bytes:
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise Refused("package_entry_unreadable", "%s: %s" % (shown, type(exc).__name__)) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused("package_entry_untrusted", "%s: not a regular file" % shown)
        _owned(info, shown)
        chunks: list[bytes] = []
        size = 0  # bounded by what is read, not by st_size: a file can grow meanwhile
        while True:
            chunk = os.read(fd, min(1 << 20, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise Refused("package_file_too_large", shown)
        return b"".join(chunks)
    finally:
        os.close(fd)


class _Walk:
    """One pass over a package folder. With ``read`` False it only takes identities (the second
    listing); nothing is read, so nothing it finds can reach the loader."""

    def __init__(self, read: bool = True) -> None:
        self.read = read
        self.files: dict[str, bytes] = {}
        self.unlistable: list[str] = []
        self.identity: dict[str, tuple] = {}
        self.total = 0
        self.listed = 0  # entries other than folders seen by the second listing

    def folder(self, dir_fd: int, prefix: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise Refused("package_too_deep", prefix.rstrip("/"))
        try:
            with os.scandir(dir_fd) as entries:
                names = sorted(entry.name for entry in entries)
        except OSError as exc:
            raise Refused("package_entry_unreadable", "%s: %s" % (prefix or ".", type(exc).__name__)) from None
        for name in names:
            path = prefix + name
            shown = ascii(path)[1:-1]
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError as exc:
                if not self.read:
                    continue  # gone since the scan: it is missing from the second listing
                raise Refused("package_entry_unreadable", "%s: %s" % (shown, type(exc).__name__)) from None
            if not self.read and not stat.S_ISDIR(info.st_mode):
                self.listed += 1
                if self.listed > MAX_FILES:
                    raise Refused("package_too_many_files", "more than %d" % MAX_FILES)
            if prefix == "" and name in (MANIFEST_FILE, SIGNATURE_FILE):
                if self.read:
                    # Their identities were taken before they were read (verify_package); one
                    # that appears only now was not there when the signature was read.
                    self.identity.setdefault(path, ("appeared after its read",))
                else:
                    self.identity[path] = _identity_of(info)
                continue
            # The identity is taken BEFORE the read: a file replaced or written between this
            # stat and the end of the check is seen by the second listing.
            self.identity[path] = _identity_of(info)
            if stat.S_ISDIR(info.st_mode):
                try:
                    fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
                except OSError as exc:
                    if not self.read:
                        continue
                    raise Refused("package_entry_unreadable", "%s: %s" % (shown, type(exc).__name__)) from None
                try:
                    opened = os.fstat(fd)
                    if self.read:
                        _owned(opened, shown)  # O_DIRECTORY | O_NOFOLLOW: this folder, not a link
                    if _identity_of(opened) != self.identity[path]:
                        self.identity[path] = ("replaced while opened",)
                    self.folder(fd, path + "/", depth + 1)
                finally:
                    os.close(fd)
            elif not self.read:
                continue
            elif stat.S_ISREG(info.st_mode):
                if len(self.files) + len(self.unlistable) >= MAX_FILES:
                    raise Refused("package_too_many_files", "more than %d" % MAX_FILES)
                data = _read_file(dir_fd, name, shown, MAX_FILE_BYTES)
                self.total += len(data)
                if self.total > MAX_PACKAGE_BYTES:
                    raise Refused("package_too_large", "more than %d bytes" % MAX_PACKAGE_BYTES)
                # A name the manifest grammar cannot list is still present: it counts as added.
                if path.isascii() and path == shown:
                    self.files[path] = data
                else:
                    self.unlistable.append(shown)
            else:
                raise Refused("package_entry_untrusted", "%s: %s" % (shown, _UNSAFE))


def _read_signature(dir_fd: int) -> str | None:
    try:
        os.stat(SIGNATURE_FILE, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    raw = _read_file(dir_fd, SIGNATURE_FILE, SIGNATURE_FILE, MAX_SIGNATURE_BYTES)
    return raw.decode("ascii", "replace").strip()


def _posix_reads() -> bool:
    return bool(
        os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd and os.scandir in os.supports_fd
        and getattr(os, "O_DIRECTORY", 0) and getattr(os, "O_NOFOLLOW", 0))


def _differences(before: dict, after: dict) -> tuple:
    """Every path whose identity differs, that appeared, or that went away, sorted."""
    return tuple(sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path)))


def _listing(top: int) -> dict:
    """The identities of everything under the open folder ``top``, the folder itself included."""
    again = _Walk(read=False)
    again.identity[_TOP] = _identity_of(os.fstat(top))
    try:
        again.folder(top, "", 0)
    except Refused:
        again.identity["(the folder could not be listed again)"] = ()
    return again.identity


def package_changes(directory: str | os.PathLike, result: VerifiedPackage) -> tuple:
    """The paths of ``directory`` that differ from what ``result`` was read from, by path now.

    Empty when the folder at ``directory`` is the folder that was verified and every file and
    folder in it has the identity it had when it was read. Nothing is read: an identity is not
    a content, so a change found here is a refusal, never a new verdict.
    """
    if type(result) is not VerifiedPackage or not result.identity:
        return (_TOP,)
    try:
        top = os.open(os.fspath(directory), _DIR_FLAGS)
    except OSError:
        return (_TOP,)
    try:
        return _differences(dict(result.identity), _listing(top))
    finally:
        os.close(top)


def verify_package(directory: str | os.PathLike, trust: TrustStore) -> VerifiedPackage:
    """Read ``directory`` once and return its verified files and the signature's verdict.

    Raises ``Refused`` for anything that cannot be read safely. A mismatch between the files
    and the manifest is an unsigned package (``package_files_changed``), not a signed one:
    a changed or added file's bytes are left out of ``files``. So is a package any file or
    folder of which changed between its read and the end of the check
    (``changed_while_checked``).
    """
    if not _posix_reads():
        raise Refused("package_platform_unsupported", "needs dir_fd, O_DIRECTORY and O_NOFOLLOW (POSIX)")
    if type(trust) is not TrustStore:
        raise Refused("package_trust_invalid", "verify_package takes this install's TrustStore")
    try:
        top = os.open(os.fspath(directory), _DIR_FLAGS)
    except OSError as exc:
        raise Refused("package_directory_unreadable", type(exc).__name__) from None
    try:
        opened = os.fstat(top)
        _owned(opened, "the package folder")
        walk = _Walk()
        walk.identity[_TOP] = _identity_of(opened)
        try:
            walk.identity[MANIFEST_FILE] = _identity_of(os.stat(MANIFEST_FILE, dir_fd=top, follow_symlinks=False))
        except FileNotFoundError:
            raise Refused("package_manifest_missing", MANIFEST_FILE) from None
        raw = _read_file(top, MANIFEST_FILE, MANIFEST_FILE, MAX_MANIFEST_BYTES)
        manifest = parse_manifest(raw)
        try:
            walk.identity[SIGNATURE_FILE] = _identity_of(os.stat(SIGNATURE_FILE, dir_fd=top, follow_symlinks=False))
        except FileNotFoundError:
            pass
        signature = _read_signature(top)
        walk.folder(top, "", 0)
        listed = dict(manifest.files) if manifest.schema == MANIFEST_SCHEMA_V1 else {ENTRY_FILE: manifest.artifact_sha256}
        present = walk.files
        changed = tuple(sorted(
            path for path in listed
            if path in present and hashlib.sha256(present[path]).hexdigest() != listed[path]))
        missing = tuple(sorted(path for path in listed if path not in present))
        added = tuple(sorted([path for path in present if path not in listed] + walk.unlistable))
        verified = MappingProxyType({
            path: present[path] for path in sorted(listed) if path in present and path not in changed})
        if changed or added or missing:
            verdict, signer, reason = "unsigned", None, "package_files_changed"
        else:
            verdict, signer, reason = trust.classify(manifest, signature)
        # The second listing, after the verdict, through the same open folder; then the path
        # itself must still name that folder.
        moved = _differences(walk.identity, _listing(top))
        try:
            still = _identity_of(os.stat(os.fspath(directory), follow_symlinks=False)) == walk.identity[_TOP]
        except OSError:
            still = False
        if not still and _TOP not in moved:
            moved = tuple(sorted(moved + (_TOP,)))
    finally:
        os.close(top)
    identity = tuple(sorted(walk.identity.items()))
    if moved:
        verdict, signer, reason = "unsigned", None, "package_files_changed"
    return VerifiedPackage(manifest, verdict, signer, reason, verified, signature, changed, added, missing,
                           moved, identity, present.get(ENTRY_FILE))
