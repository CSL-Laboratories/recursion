"""Pins for wrapper routes (CSL-786, candidate).

Lucas's 2026-11-09 call, beside the floor table on recursion #323 (CSL-732).
That table's Measured help column already records which builds have a measured
``--help``; this module does not repeat it.

* Pi, Hermes and Cursor run as beta only when the launcher script and every
  installed package file match a pin. A different script, a changed file, an
  extra file or a missing file refuses ``wrapper_pin_mismatch`` until the pin
  is replaced.
* A symlink in the package tree is followed and what it resolves to is hashed:
  a file link by the target's bytes, a directory link by walking the target
  tree. The link itself (its text and where it resolves) is in the manifest
  too. A link that resolves outside the install root refuses
  ``package_link_outside_root`` unless the caller lists the target in
  ``allowed_link_targets`` or passes ``full_access_acknowledged=True``. A
  dangling link refuses ``package_link_dangling``.
* The install root must be absolute (``install_root_not_absolute``), the
  manifest must hold at least one file (``package_manifest_empty``) and the
  launcher must resolve to a file under the install root
  (``launcher_outside_root``). These hold when measuring and when verifying.
* Antigravity CLI (``agy``) runs as beta when the version and the sha256 of
  the ``agy`` binary and of its launcher match a pin. Package files are not
  part of that pin. Antigravity CLI is the Google route; the Gemini CLI route
  was dropped by owner call (README).
* T3 Code is background only: no route and no pin.

Nothing here executes a launcher, binary or package file. Hashes are of
bytes on disk. No network call. ``allowed_link_targets`` and
``full_access_acknowledged`` are explicit arguments: nothing here reads a
config file, the environment, a keyring or a prompt.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass

__all__ = [
    "PACKAGE_WRAPPER_ENGINES",
    "AntigravityPin",
    "PackagePin",
    "WrapperInstall",
    "WrapperPinRefused",
    "check_link_requirements",
    "measure_antigravity_pin",
    "measure_package_pin",
    "verify_antigravity_pin",
    "verify_package_pin",
]

PACKAGE_WRAPPER_ENGINES = frozenset({"pi", "hermes", "cursor"})
_MAX_FILES = 100_000
# A sanity bound on one hashed file: a real agy 1.5.5 binary is 247,853,456 bytes.
_MAX_FILE_BYTES = 1024 * 1024 * 1024
_MAX_DEPTH = 200
_HEX64 = set("0123456789abcdef")


class WrapperPinRefused(Exception):
    """The installed wrapper does not match the pin, or the pin is not usable."""

    def __init__(self, cause: str, detail: str = "") -> None:
        super().__init__(cause + (f" ({detail})" if detail else ""))
        self.cause, self.detail = cause, detail


@dataclass(frozen=True)
class PackagePin:
    """sha256 of the launcher script, and of each installed package file.

    ``package_files`` is ``(relative posix path, sha256)`` sorted by path and
    is never empty. A regular file's row is the sha256 of its bytes. A file
    symlink's row covers the link text, where it resolves and the target's
    bytes. A directory symlink has a row for the link itself and one row for
    each file under the tree it resolves to. Callers build this with
    ``measure_package_pin`` rather than by hashing files themselves.
    """

    launcher_sha256: str
    package_files: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class WrapperInstall:
    """Where the launcher and the package tree are, plus the pin they must match."""

    launcher_path: str
    install_root: str
    pin: PackagePin


@dataclass(frozen=True)
class AntigravityPin:
    """Version plus sha256 of the ``agy`` binary and of its launcher.

    Package files are intentionally absent: a sibling file can change without
    retiring this pin. The version is the string the caller states; this
    module does not run ``agy`` to read it.
    """

    version: str
    binary_sha256: str
    launcher_sha256: str


def check_link_requirements(allowed_link_targets: object, full_access_acknowledged: object) -> tuple[str, ...]:
    """Validate the two caller inputs, fail closed, and return the allowed targets as a sorted tuple.

    ``allowed_link_targets`` is a tuple or frozenset of absolute, normalised real paths other than
    ``/``. ``full_access_acknowledged`` counts only as the bool ``True``; any non-bool refuses.
    """

    if not isinstance(full_access_acknowledged, bool):  # "true", 1 and the like are not an acknowledgment
        raise WrapperPinRefused("route_full_access_malformed", "")
    if not isinstance(allowed_link_targets, (tuple, frozenset)):
        raise WrapperPinRefused("requirements_malformed", "allowed_link_targets")
    for target in allowed_link_targets:
        if (not isinstance(target, str) or "\x00" in target or not os.path.isabs(target) or target == "/"
                or os.path.normpath(target) != target):
            raise WrapperPinRefused("requirements_malformed", "allowed_link_targets")
    return tuple(sorted(allowed_link_targets))


def measure_package_pin(
    launcher_path: str,
    install_root: str,
    *,
    allowed_link_targets: tuple[str, ...] | frozenset[str] = (),
    full_access_acknowledged: bool = False,
) -> PackagePin:
    """Hash the launcher and every file under ``install_root``. Does not execute them.

    Links are followed and their resolved contents hashed. A link that leaves the install root
    refuses unless its real target is, or is under, an entry of ``allowed_link_targets``, or
    ``full_access_acknowledged`` is ``True``.
    """

    allowed = check_link_requirements(allowed_link_targets, full_access_acknowledged)
    if not isinstance(install_root, str) or not os.path.isabs(install_root):
        raise WrapperPinRefused("install_root_not_absolute", "")
    files = _manifest(install_root, allowed, full_access_acknowledged is True)
    if not files:
        raise WrapperPinRefused("package_manifest_empty", "")
    return PackagePin(_hash_launcher(launcher_path, install_root), files)


def verify_package_pin(
    engine: str,
    install: WrapperInstall,
    *,
    allowed_link_targets: tuple[str, ...] | frozenset[str] = (),
    full_access_acknowledged: bool = False,
) -> PackagePin:
    """Raise ``WrapperPinRefused`` unless ``install`` matches a fresh measurement."""

    if engine not in PACKAGE_WRAPPER_ENGINES:
        raise WrapperPinRefused("wrapper_pin_not_applicable", str(engine))
    if not isinstance(install, WrapperInstall) or not isinstance(install.pin, PackagePin):
        raise WrapperPinRefused("wrapper_pin_missing", str(engine))
    _check_hex(install.pin.launcher_sha256, "launcher")
    if not isinstance(install.pin.package_files, tuple):
        raise WrapperPinRefused("wrapper_pin_malformed", "package_files")
    if not install.pin.package_files:
        raise WrapperPinRefused("package_manifest_empty", "")
    for row in install.pin.package_files:
        if not isinstance(row, tuple) or len(row) != 2:
            raise WrapperPinRefused("wrapper_pin_malformed", "path")
        rel, digest = row
        if not isinstance(rel, str) or not rel or rel.startswith("/") or ".." in rel.split("/"):
            raise WrapperPinRefused("wrapper_pin_malformed", "path")
        _check_hex(digest, rel)
    measured = measure_package_pin(
        install.launcher_path, install.install_root,
        allowed_link_targets=allowed_link_targets, full_access_acknowledged=full_access_acknowledged,
    )
    if measured.launcher_sha256 != install.pin.launcher_sha256.lower():
        raise WrapperPinRefused("wrapper_pin_mismatch", "launcher")
    if measured.package_files != install.pin.package_files:
        raise WrapperPinRefused("wrapper_pin_mismatch", _first_package_diff(measured.package_files, install.pin.package_files))
    return measured


def measure_antigravity_pin(version: str, binary_path: str, launcher_path: str) -> AntigravityPin:
    """Hash the ``agy`` binary and its launcher. Does not hash a package tree and does not run ``agy``."""

    return AntigravityPin(_version(version), _hash_target(binary_path, "binary"), _hash_target(launcher_path, "launcher"))


def verify_antigravity_pin(
    version: str, binary_path: str, launcher_path: str, pin: AntigravityPin
) -> AntigravityPin:
    """Raise ``WrapperPinRefused`` unless version, binary and launcher match ``pin``."""

    if not isinstance(pin, AntigravityPin):
        raise WrapperPinRefused("wrapper_pin_missing", "antigravity")
    _check_hex(pin.binary_sha256, "binary")
    _check_hex(pin.launcher_sha256, "launcher")
    measured = measure_antigravity_pin(version, binary_path, launcher_path)
    if measured.version != pin.version:
        raise WrapperPinRefused("wrapper_pin_mismatch", "version")
    if measured.binary_sha256 != pin.binary_sha256.lower():
        raise WrapperPinRefused("wrapper_pin_mismatch", "binary")
    if measured.launcher_sha256 != pin.launcher_sha256.lower():
        raise WrapperPinRefused("wrapper_pin_mismatch", "launcher")
    return measured


def _check_hex(digest: object, detail: str) -> None:
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in _HEX64 for char in digest):
        raise WrapperPinRefused("wrapper_pin_malformed", detail)


def _version(version: object) -> str:
    if not isinstance(version, str) or not version.strip() or any(char in version for char in "\n\r\x00"):
        raise WrapperPinRefused("wrapper_version_malformed", "")
    return version.strip()


def _hash_launcher(path: str, install_root: str) -> str:
    # Hash the script that would run: a symlink chain is followed and the resolved
    # file's bytes are hashed. That file must sit under the install root.
    digest = _hash_target(path, "launcher")
    if not _within(os.path.realpath(path), os.path.realpath(install_root)):
        raise WrapperPinRefused("launcher_outside_root", "")
    return digest


def _hash_target(path: str, detail: str) -> str:
    missing = "wrapper_launcher_missing" if detail == "launcher" else "wrapper_file_missing"
    if not isinstance(path, str) or not path or not os.path.lexists(path):
        raise WrapperPinRefused(missing, detail)
    real = os.path.realpath(path)
    if os.path.islink(real) or not os.path.isfile(real):
        raise WrapperPinRefused(missing, detail)
    return _hash_regular(real, detail)


def _hash_regular(path: str, detail: str) -> str:
    """Hash a regular file. ``_manifest`` resolves a symlink before it calls this."""

    missing = "wrapper_launcher_missing" if detail == "launcher" else "wrapper_file_missing"
    if not isinstance(path, str) or os.path.islink(path) or not os.path.isfile(path):
        raise WrapperPinRefused(missing, detail)
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_FILE_BYTES:
                raise WrapperPinRefused("wrapper_file_too_large", detail)
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(install_root: str, allowed: tuple[str, ...], full_access: bool) -> tuple[tuple[str, str], ...]:
    if not isinstance(install_root, str) or not os.path.isdir(install_root) or os.path.islink(install_root):
        raise WrapperPinRefused("wrapper_install_missing", "")
    real_root = os.path.realpath(install_root)
    rows: list[tuple[str, str]] = []
    count = 0
    # (directory to list, relative prefix, real paths of the directories being walked above it).
    # A directory link back to one of those is a cycle: the link is recorded and not entered,
    # because that directory's files are already being hashed by the walk it is part of.
    pending: list[tuple[str, str, tuple[str, ...]]] = [(real_root, "", (real_root,))]
    while pending:
        directory, prefix, above = pending.pop()
        if len(above) > _MAX_DEPTH:
            raise WrapperPinRefused("wrapper_tree_too_deep", prefix.rstrip("/"))
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            raise WrapperPinRefused("wrapper_tree_unreadable", prefix.rstrip("/")) from None
        for name in names:
            path = os.path.join(directory, name)
            rel = _relative_name(prefix, name)
            count += 1
            if count > _MAX_FILES:
                raise WrapperPinRefused("wrapper_tree_too_large", str(count))
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):
                pending.append((path, rel + "/", above + (os.path.realpath(path),)))
            elif stat.S_ISREG(mode):
                rows.append((rel, _hash_regular(path, rel)))
            elif stat.S_ISLNK(mode):
                real = os.path.realpath(path)
                try:
                    target_mode = os.stat(real).st_mode
                except OSError:  # nothing there, or a link loop
                    raise WrapperPinRefused("package_link_dangling", rel) from None
                inside = _within(real, real_root)
                if not inside and not full_access and not any(_within(real, target) for target in allowed):
                    raise WrapperPinRefused("package_link_outside_root", rel)
                # The link itself: its text and where it resolves. Retargeting it changes the pin
                # even when the new target holds the same bytes.
                where = "in:" + os.path.relpath(real, real_root) if inside else "out:" + real
                link = os.fsencode(os.readlink(path)) + b"\x00" + os.fsencode(where)
                if stat.S_ISDIR(target_mode):
                    if real in above:
                        rows.append((rel, _link_digest(b"link-cycle", link)))
                        continue
                    rows.append((rel, _link_digest(b"link-dir", link)))
                    pending.append((real, rel + "/", above + (real,)))
                elif stat.S_ISREG(target_mode):
                    rows.append((rel, _link_digest(b"link-file", link + b"\x00" + _hash_regular(real, rel).encode())))
                else:
                    raise WrapperPinRefused("wrapper_package_not_a_file", rel)
            else:
                raise WrapperPinRefused("wrapper_package_not_a_file", rel)
    rows.sort()
    return tuple(rows)


def _link_digest(kind: bytes, body: bytes) -> str:
    return hashlib.sha256(kind + b"\x00" + body).hexdigest()


def _within(real: str, root: str) -> bool:
    """True when ``real`` is ``root`` or inside it."""

    base = root.rstrip(os.sep)
    return real == base or real.startswith(base + os.sep)


def _relative_name(prefix: str, name: str) -> str:
    if not name or name in (".", "..") or "/" in name:
        raise WrapperPinRefused("wrapper_path_escapes", "")
    return prefix + name


def _first_package_diff(
    measured: tuple[tuple[str, str], ...], pinned: tuple[tuple[str, str], ...]
) -> str:
    got = dict(measured)
    want = dict(pinned)
    for path in sorted(set(got) | set(want)):
        if got.get(path) != want.get(path):
            return path
    return "package"
