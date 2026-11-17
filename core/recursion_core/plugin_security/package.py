"""Whole-package verification (lane PLUGIN-SIGN).

``verify_package(directory, trust)`` reads a plugin package once, through one open directory,
and returns the bytes of every file whose SHA-256 is the one its manifest names, together with
the signature's verdict. A changed, added or removed file is not accepted: the result is
unsigned (``package_files_changed``) and those bytes are not returned. What cannot be read
safely is refused: a symbolic link, a device, a pipe or socket, a file or folder another user
owns or others can write, too many or too large files, an unreadable or malformed manifest.

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

    @property
    def intact(self) -> bool:
        """True when nothing listed was changed or missing and nothing extra is present."""
        return not (self.changed or self.added or self.missing)


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
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.unlistable: list[str] = []
        self.total = 0

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
            if prefix == "" and name in (MANIFEST_FILE, SIGNATURE_FILE):
                continue
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError as exc:
                raise Refused("package_entry_unreadable", "%s: %s" % (shown, type(exc).__name__)) from None
            if stat.S_ISDIR(info.st_mode):
                try:
                    fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
                except OSError as exc:
                    raise Refused("package_entry_unreadable", "%s: %s" % (shown, type(exc).__name__)) from None
                try:
                    _owned(os.fstat(fd), shown)  # O_DIRECTORY | O_NOFOLLOW: this folder, not a link
                    self.folder(fd, path + "/", depth + 1)
                finally:
                    os.close(fd)
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


def verify_package(directory: str | os.PathLike, trust: TrustStore) -> VerifiedPackage:
    """Read ``directory`` once and return its verified files and the signature's verdict.

    Raises ``Refused`` for anything that cannot be read safely. A mismatch between the files
    and the manifest is an unsigned package (``package_files_changed``), not a signed one:
    a changed or added file's bytes are left out of ``files``.
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
        _owned(os.fstat(top), "the package folder")
        try:
            os.stat(MANIFEST_FILE, dir_fd=top, follow_symlinks=False)
        except FileNotFoundError:
            raise Refused("package_manifest_missing", MANIFEST_FILE) from None
        raw = _read_file(top, MANIFEST_FILE, MANIFEST_FILE, MAX_MANIFEST_BYTES)
        manifest = parse_manifest(raw)
        signature = _read_signature(top)
        walk = _Walk()
        walk.folder(top, "", 0)
    finally:
        os.close(top)
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
        return VerifiedPackage(
            manifest, "unsigned", None, "package_files_changed", verified, signature, changed, added, missing)
    verdict, signer, reason = trust.classify(manifest, signature)
    return VerifiedPackage(manifest, verdict, signer, reason, verified, signature)
