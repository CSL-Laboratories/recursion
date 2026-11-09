"""Pins for wrapper routes (CSL-786, candidate).

Lucas's 2026-11-09 call, beside the floor table on recursion #323 (CSL-732).
That table's Measured help column already records which builds have a measured
``--help``; this module does not repeat it.

* Pi, Hermes and Cursor run as beta only when the launcher script and every
  installed package file match a pin. A different script, a changed file, an
  extra file or a missing file refuses ``wrapper_pin_mismatch`` until the pin
  is replaced.
* Antigravity CLI (``agy``) runs as beta when the version and the sha256 of
  the ``agy`` binary and of its launcher match a pin. Package files are not
  part of that pin. Gemini CLI consumer accounts stopped 2026-06-18; the
  subscription path is Antigravity CLI (Google's deprecation note and the
  Developers Blog post cited in the README).
* T3 Code is background only: no route and no pin.

Nothing here executes a launcher, binary or package file. Hashes are of
bytes on disk. No network call.
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
    "measure_antigravity_pin",
    "measure_package_pin",
    "verify_antigravity_pin",
    "verify_package_pin",
]

PACKAGE_WRAPPER_ENGINES = frozenset({"pi", "hermes", "cursor"})
_MAX_FILES = 100_000
_MAX_FILE_BYTES = 32 * 1024 * 1024
_HEX64 = set("0123456789abcdef")


class WrapperPinRefused(Exception):
    """The installed wrapper does not match the pin, or the pin is not usable."""

    def __init__(self, cause: str, detail: str = "") -> None:
        super().__init__(cause + (f" ({detail})" if detail else ""))
        self.cause, self.detail = cause, detail


@dataclass(frozen=True)
class PackagePin:
    """sha256 of the launcher script, and of each installed package file.

    ``package_files`` is ``(relative posix path, sha256)`` sorted by path.
    A symlink is hashed as ``symlink:`` plus the link text, not the target's
    bytes, and a directory symlink is not walked. Callers build this with
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


def measure_package_pin(launcher_path: str, install_root: str) -> PackagePin:
    """Hash the launcher and every file under ``install_root``. Does not execute them."""

    return PackagePin(_hash_launcher(launcher_path), _manifest(install_root))


def verify_package_pin(engine: str, install: WrapperInstall) -> PackagePin:
    """Raise ``WrapperPinRefused`` unless ``install`` matches a fresh measurement."""

    if engine not in PACKAGE_WRAPPER_ENGINES:
        raise WrapperPinRefused("wrapper_pin_not_applicable", str(engine))
    if not isinstance(install, WrapperInstall) or not isinstance(install.pin, PackagePin):
        raise WrapperPinRefused("wrapper_pin_missing", str(engine))
    _check_hex(install.pin.launcher_sha256, "launcher")
    for rel, digest in install.pin.package_files:
        if not isinstance(rel, str) or not rel or rel.startswith("/") or ".." in rel.split("/"):
            raise WrapperPinRefused("wrapper_pin_malformed", "path")
        _check_hex(digest, rel)
    measured = measure_package_pin(install.launcher_path, install.install_root)
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


def _hash_launcher(path: str) -> str:
    # Hash the script that would run. A symlink chain is followed only for the
    # hash; the walk of the package tree does not follow links.
    return _hash_target(path, "launcher")


def _hash_target(path: str, detail: str) -> str:
    missing = "wrapper_launcher_missing" if detail == "launcher" else "wrapper_file_missing"
    if not isinstance(path, str) or not path or not os.path.lexists(path):
        raise WrapperPinRefused(missing, detail)
    real = os.path.realpath(path)
    if os.path.islink(real) or not os.path.isfile(real):
        raise WrapperPinRefused(missing, detail)
    return _hash_regular(real, detail)


def _hash_regular(path: str, detail: str) -> str:
    """Hash a regular file. Symlinks are hashed by ``_manifest``, not here."""

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


def _manifest(install_root: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(install_root, str) or not os.path.isdir(install_root) or os.path.islink(install_root):
        raise WrapperPinRefused("wrapper_install_missing", "")
    rows: list[tuple[str, str]] = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(install_root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        for name in list(dirnames):
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                continue
            dirnames.remove(name)
            rows.append(_link_row(install_root, path))
            count += 1
            if count > _MAX_FILES:
                raise WrapperPinRefused("wrapper_tree_too_large", str(count))
        for name in filenames:
            path = os.path.join(dirpath, name)
            rel = _relative(install_root, path)
            if os.path.islink(path):
                rows.append(_link_row(install_root, path))
            else:
                mode = os.lstat(path).st_mode
                if not stat.S_ISREG(mode):
                    raise WrapperPinRefused("wrapper_package_not_a_file", rel)
                rows.append((rel, _hash_regular(path, rel)))
            count += 1
            if count > _MAX_FILES:
                raise WrapperPinRefused("wrapper_tree_too_large", str(count))
    rows.sort()
    return tuple(rows)


def _link_row(root: str, path: str) -> tuple[str, str]:
    rel = _relative(root, path)
    text = os.readlink(path).encode()
    return rel, hashlib.sha256(b"symlink:" + text).hexdigest()


def _relative(root: str, path: str) -> str:
    rel = os.path.relpath(path, root).replace(os.sep, "/")
    parts = rel.split("/")
    if not rel or rel.startswith("/") or ".." in parts or any(not part or part == "." for part in parts):
        raise WrapperPinRefused("wrapper_path_escapes", rel)
    return rel


def _first_package_diff(
    measured: tuple[tuple[str, str], ...], pinned: tuple[tuple[str, str], ...]
) -> str:
    got = dict(measured)
    want = dict(pinned)
    for path in sorted(set(got) | set(want)):
        if got.get(path) != want.get(path):
            return path
    return "package"
