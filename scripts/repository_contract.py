#!/usr/bin/env python3
"""Recursion v2 repository, branch, and candidate contracts.

This module is deliberately independent of the worker and publisher processes.  It admits a
task against one exact source commit, verifies an externally-produced candidate, imports that
candidate into a canonical task ref with compare-and-swap semantics, and produces a read-only
integration plan while holding a per-repository serialization lock.

No function in this module pushes a remote or updates a default/integration ref.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import hmac
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
SCHEMA = "recursion-project-repository-contract/v2"
_MODULE_ROOT = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))
_DEFAULT_CONTROLLER_KEY_PATH = _MODULE_ROOT / "state" / "secrets" / "verifier-receipt.key"
CONTROLLER_KEY_PATH = _DEFAULT_CONTROLLER_KEY_PATH
OUTCOMES = {
    "current-compatible", "stale-reviewable", "stale-rebase-required", "invalidated",
    "source-unavailable", "unauthorized-result",
}
_GATE_SHELL_EXPANSION = set("~%$`*?[")


class ContractError(ValueError):
    """A fail-closed contract refusal."""

    def __init__(self, message: str, code: str = "contract-invalid") -> None:
        super().__init__(message)
        self.code = code


_BOUNDARY_ERRORS = (json.JSONDecodeError, ValueError, TypeError, OverflowError,
                    RecursionError, UnicodeDecodeError, OSError, subprocess.TimeoutExpired)


def _native_handle_value(handle: Any) -> int:
    try:
        value = handle.value if hasattr(handle, "value") else int(handle)
        if not isinstance(value, int) or value <= 0:
            raise ValueError("invalid native handle")
        return value
    except _BOUNDARY_ERRORS as exc:
        raise OSError("invalid native handle") from exc


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is not permitted: {value}")


def _strict_json_loads(raw: str | bytes) -> Any:
    try:
        return json.loads(raw, parse_constant=_reject_json_constant)
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("JSON input is malformed", "contract-invalid") from exc


def _json_digest(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode("ascii")
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("JSON value cannot be canonically encoded", "contract-invalid") from exc
    return hashlib.sha256(encoded).hexdigest()


def _normalized_path(value: object, *, field: str = "path") -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise ContractError(f"{field} must be a non-empty path")
    if any(char in _GATE_SHELL_EXPANSION for char in value):
        raise ContractError(f"{field} contains shell expansion or glob syntax")
    if "\\" in value or value != value.strip() or value != "." and (
        value.startswith("./") or value.endswith("/") or "//" in value
    ):
        raise ContractError(f"{field} is not in canonical repository-relative form")
    if re.match(r"^[A-Za-z]:", value):
        raise ContractError(f"{field} must be repository-relative")
    path = PurePosixPath(value)
    raw_parts = value.split("/")
    if path.is_absolute() or value != "." and any(part in {".", ".."} for part in raw_parts):
        raise ContractError(f"{field} is unsafe")
    return "." if str(path) in {"", "."} else str(path)


def _normalized_paths(values: object, *, field: str) -> list[str]:
    if not isinstance(values, list) or not values:
        raise ContractError(f"{field} must be a non-empty string array")
    normalized = [_normalized_path(value, field=field) for value in values]
    if len(set(normalized)) != len(normalized):
        raise ContractError(f"{field} must contain unique normalized paths")
    return normalized


def _normalized_gate_files(values: object, *, field: str) -> list[str]:
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values) \
            or len(set(values)) != len(values):
        raise ContractError(f"{field} must be a unique string array")
    normalized = [_normalized_path(item, field=field) for item in values]
    if any(item == "." or any(char.isspace() or char == "," for char in item)
           for item in normalized):
        raise ContractError(f"{field} contains an unrepresentable gate file path")
    if len(set(normalized)) != len(normalized):
        raise ContractError(f"{field} must contain unique normalized paths")
    return normalized


def _path_subset(child: str, parent: str) -> bool:
    child_path = PurePosixPath(child)
    parent_path = PurePosixPath(parent)
    return parent == "." or child == parent or parent_path in child_path.parents


def _gate_digest(contract: dict[str, Any]) -> str:
    declared = contract.get("gate_digest")
    computed = _json_digest(contract["gate_commands"])
    if declared is not None:
        if not isinstance(declared, str) or not re.fullmatch(r"[0-9a-f]{64}", declared):
            raise ContractError("gate_digest must be a lowercase SHA-256 digest")
        if declared != computed:
            raise ContractError("gate_digest does not match gate_commands")
        return declared
    return computed


def _required_gate_profile(contract: dict[str, Any]) -> dict[str, Any] | None:
    profile = contract.get("required_gate_profile")
    if profile is None:
        return None
    required = {"acceptance", "gate_class", "gate_files", "controller_gate_files"}
    if not isinstance(profile, dict) or set(profile) != required:
        raise ContractError("required_gate_profile is malformed")
    acceptance = profile["acceptance"]
    if not isinstance(acceptance, str) or not acceptance.strip():
        raise ContractError("required_gate_profile acceptance is invalid")
    if not isinstance(profile["gate_class"], str) or profile["gate_class"] not in {"light", "heavy"}:
        raise ContractError("required_gate_profile gate_class is invalid")
    gate_files = _normalized_gate_files(profile["gate_files"], field="required_gate_profile gate_files")
    controller_gate_files = _normalized_gate_files(
        profile["controller_gate_files"], field="required_gate_profile controller_gate_files")
    expected_acceptance = " && ".join(contract["gate_commands"])
    profile_identity = _gate_identity(acceptance, {
        "gate_files": gate_files, "controller_gate_files": controller_gate_files,
    })
    expected_identity = _gate_identity(expected_acceptance, {
        "gate_files": gate_files, "controller_gate_files": controller_gate_files,
    })
    if profile_identity != expected_identity:
        raise ContractError("required_gate_profile acceptance diverges from gate_commands")
    if not _valid_gate_command(acceptance, allowed_paths=contract["allowed_paths"],
                               gate_files=gate_files,
                               controller_gate_files=controller_gate_files):
        raise ContractError("required_gate_profile acceptance is not an admitted gate")
    return {"acceptance": acceptance, "gate_class": profile["gate_class"],
            "gate_files": gate_files, "controller_gate_files": controller_gate_files}


def _require_path_subset(child: list[str], parent: list[str], *, field: str) -> list[str]:
    if any(not any(_path_subset(path, root) for root in parent) for path in child):
        raise ContractError(f"{field} widens the descriptor allowed_paths")
    return child


def _valid_git_ref(value: object) -> bool:
    if not isinstance(value, str) or not value or "*" in value \
            or any(ord(char) < 32 for char in value):
        return False
    try:
        result = subprocess.run(
            ["git", "check-ref-format", "--branch", value], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            timeout=30,
        )
    except _BOUNDARY_ERRORS:
        return False
    return result.returncode == 0


def _valid_git_pattern(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 200 \
            or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value) \
            or any(char in value for char in "?[\"<>|"):
        return False
    probe = value.replace("*", "probe")
    return _valid_git_ref(probe)


def _valid_gate_command(value: object, *, allowed_paths: list[str] | None = None,
                        gate_files: list[str] | None = None,
                        controller_gate_files: list[str] | None = None) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 \
            or "\x00" in value or any(ord(char) < 32 and char != "\t" for char in value):
        return False
    validator = Path(__file__).with_name("validate-gate.py")
    try:
        validator_args = [
            sys.executable, str(validator), "--acceptance", value, "--allowed-paths",
            ",".join(allowed_paths or []), "--gate-files", ",".join(gate_files or []),
            "--controller-gate-files",
            ",".join(controller_gate_files or []), "--strict-direct",
        ]
        if controller_gate_files:
            # Controller gate paths are rooted at this controller checkout. Bind their bytes to
            # tracked HEAD content while admitting their declared relative paths.
            validator_args += ["--controller-root", str(_MODULE_ROOT)]
        result = subprocess.run(
            # Repository descriptors bind their gate command and declared gate-file ownership;
            # controller-owned gate files are rooted and hashed by the validator below.
            validator_args,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
        )
    except _BOUNDARY_ERRORS:
        return False
    return result.returncode == 0


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "SystemRoot", "WINDIR"}
    }
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    try:
        return subprocess.run(
            ["git", "--no-replace-objects", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
            env=env, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30, check=False,
        )
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("Git command timed out", "verification-unavailable") from exc


def _git_raw(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run Git without text decoding or newline translation."""
    env = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "SystemRoot", "WINDIR"}
    }
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    try:
        return subprocess.run(
            ["git", "--no-replace-objects", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False,
        )
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("Git command timed out", "verification-unavailable") from exc


def _checked_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Keep a mocked or alternate _git implementation fail-closed at every caller boundary."""
    try:
        return _git(repo, *args)
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("Git command timed out", "verification-unavailable") from exc


def _checked_git_raw(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return _git_raw(repo, *args)
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("Git command timed out", "verification-unavailable") from exc


def _is_reparse_or_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ) or bool(getattr(info, "st_reparse_tag", 0))


def _remove_tree_without_following(path: Path) -> None:
    """Remove a private scratch tree while treating links/reparses as leaf entries."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return

    def remove(action: Any) -> None:
        try:
            action()
        except PermissionError:
            if os.name != "nt":
                raise
            # Git may mark files in this private temporary object store read-only.  Clear only
            # that DOS attribute and retry; any remaining ACL failure is deliberately surfaced.
            os.chmod(path, stat.S_IWRITE)
            action()

    if _is_reparse_or_link(info):
        if os.name == "nt" and stat.S_ISDIR(info.st_mode):
            remove(path.rmdir)
        else:
            remove(path.unlink)
        return
    if not stat.S_ISDIR(info.st_mode):
        remove(path.unlink)
        return
    for child in path.iterdir():
        _remove_tree_without_following(child)
    remove(path.rmdir)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, getattr(left, "st_nlink", 1)) == (
        right.st_dev, right.st_ino, getattr(right, "st_nlink", 1))


def _lexical_absolute(path: Path | str) -> Path:
    """Make a path absolute without resolving reparses or symlinks."""
    try:
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw:
            raise ValueError("path must be a non-empty string")
        return Path(os.path.abspath(raw))
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("path could not be made absolute", "verification-unavailable") from exc


def _verify_directory_handle(fd: int, expected: os.stat_result) -> None:
    current = os.fstat(fd)
    if _is_reparse_or_link(current) or not stat.S_ISDIR(current.st_mode) \
            or (expected.st_dev, expected.st_ino) != (current.st_dev, current.st_ino):
        raise OSError("directory identity changed while pinned")


@contextlib.contextmanager
def _pinned_real_directory(path: Path, *, allow_missing: bool = False) -> Iterator[int | None]:
    """Pin a directory and all existing ancestors without following reparses.

    The descriptor is kept open across the caller's operation.  On POSIX each component is
    opened relative to the already-pinned parent; on Windows the native OPEN_REPARSE_POINT
    handle and delete-sharing denial provide the equivalent boundary.
    """
    absolute = _lexical_absolute(path)
    if os.name == "nt":
        existing = absolute
        while True:
            try:
                os.lstat(existing)
                break
            except FileNotFoundError:
                if not allow_missing or existing == existing.parent:
                    raise
                existing = existing.parent
        with _pinned_windows_parents(existing):
            info = os.lstat(existing)
            if _is_reparse_or_link(info) or not stat.S_ISDIR(info.st_mode):
                raise OSError("directory is not a real directory")
            fd = _open_pinned_directory(existing)
            try:
                _verify_directory_handle(fd, info)
                yield None if existing != absolute else fd
                _verify_directory_handle(fd, info)
            finally:
                os.close(fd)
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    handles: list[int] = []
    identities: list[os.stat_result] = []
    current_fd: int | None = None
    try:
        parts = absolute.parts
        if not parts:
            raise OSError("directory path is empty")
        root = Path(absolute.anchor or os.sep)
        current_fd = os.open(root, flags)
        handles.append(current_fd)
        root_info = os.fstat(current_fd)
        if _is_reparse_or_link(root_info) or not stat.S_ISDIR(root_info.st_mode):
            raise OSError("directory root is not real")
        identities.append(root_info)
        for part in parts[1:] if absolute.anchor else parts:
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not allow_missing:
                    raise
                for fd, expected in zip(handles, identities):
                    _verify_directory_handle(fd, expected)
                yield None
                return
            handles.append(next_fd)
            info = os.fstat(next_fd)
            if _is_reparse_or_link(info) or not stat.S_ISDIR(info.st_mode):
                raise OSError("directory component is not real")
            identities.append(info)
            current_fd = next_fd
        for fd, expected in zip(handles, identities):
            _verify_directory_handle(fd, expected)
        yield current_fd
        for fd, expected in zip(handles, identities):
            _verify_directory_handle(fd, expected)
    finally:
        for fd in reversed(handles):
            os.close(fd)


def _open_windows_directory(path: Path, *, share_delete: bool = False) -> int:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    share_mode = 0x00000007 if share_delete else 0x00000003
    handle = kernel32.CreateFileW(str(path), 0x00120001, share_mode, None, 3,
                                  0x02000000 | 0x00200000, None)
    handle_value = _native_handle_value(handle)
    if handle_value in {None, ctypes.c_void_p(-1).value}:
        raise OSError(ctypes.get_last_error(), "ledger directory could not be pinned")
    try:
        return msvcrt.open_osfhandle(handle_value, os.O_RDONLY | os.O_BINARY)
    except OSError:
        close_handle(handle)
        raise


def _open_pinned_directory(path: Path) -> int:
    flags = os.O_RDONLY
    if os.name == "nt":
        fd = _open_windows_directory(path, share_delete=False)
    else:
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if _is_reparse_or_link(info) or not stat.S_ISDIR(info.st_mode):
            raise OSError("ledger is not a real directory")
    except OSError:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def _pinned_windows_parents(path: Path) -> Iterator[None]:
    """Hold every existing parent with delete sharing denied while a durable leaf is used."""
    if os.name != "nt":
        # The same no-follow, handle-pinned boundary is required on POSIX too.  A descriptor for
        # the parent chain prevents a rename/reparse substitution between the path checks and the
        # relative ledger open below.
        with _pinned_real_directory(path.parent):
            yield
        return
    handles: list[int] = []
    identities: list[os.stat_result] = []
    try:
        # Open from the volume root toward the leaf so a later parent cannot be renamed after
        # its identity has been checked.  The volume root itself is not a renameable component.
        for parent in reversed(tuple(path.parents)):
            if parent == parent.parent:
                continue
            info = os.lstat(parent)
            if _is_reparse_or_link(info) or not stat.S_ISDIR(info.st_mode):
                raise OSError("ledger parent is not a real directory")
            fd = _open_windows_directory(parent, share_delete=False)
            try:
                pinned = os.fstat(fd)
                if _is_reparse_or_link(pinned) or not _same_file(info, pinned):
                    raise OSError("ledger parent changed while being pinned")
            except OSError:
                os.close(fd)
                raise
            handles.append(fd)
            identities.append(pinned)
        for fd, expected in zip(handles, identities):
            _verify_directory_handle(fd, expected)
        yield
        for fd, expected in zip(handles, identities):
            _verify_directory_handle(fd, expected)
    finally:
        for fd in reversed(handles):
            os.close(fd)


@contextlib.contextmanager
def _pinned_ledger(path: Path) -> Iterator[int]:
    if os.name != "nt":
        try:
            absolute = _lexical_absolute(path)
            name = absolute.name
            with _pinned_real_directory(absolute.parent) as parent_fd:
                if parent_fd is None:
                    raise OSError("candidate ledger parent was not pinned")
                try:
                    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    try:
                        os.mkdir(name, 0o700, dir_fd=parent_fd)
                        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    except FileExistsError:
                        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if _is_reparse_or_link(info) or not stat.S_ISDIR(info.st_mode):
                    raise ContractError("candidate ledger is not a real directory", "unauthorized-result")
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(name, flags, dir_fd=parent_fd)
                try:
                    pinned = os.fstat(fd)
                    if _is_reparse_or_link(pinned) or not _same_file(info, pinned):
                        raise OSError("candidate ledger changed while being pinned")
                    yield fd
                    _verify_directory_handle(fd, info)
                finally:
                    os.close(fd)
        except ContractError:
            raise
        except _BOUNDARY_ERRORS as exc:
            raise ContractError("candidate ledger could not be pinned",
                                "verification-unavailable") from exc
        return
    try:
        with _pinned_windows_parents(path):
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                try:
                    os.mkdir(path, 0o700)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ContractError("candidate ledger could not be created",
                                        "verification-unavailable") from exc
                try:
                    info = os.lstat(path)
                except OSError as exc:
                    raise ContractError("candidate ledger could not be inspected",
                                        "verification-unavailable") from exc
            except OSError as exc:
                raise ContractError("candidate ledger could not be inspected",
                                    "verification-unavailable") from exc
            if _is_reparse_or_link(info) or not stat.S_ISDIR(info.st_mode):
                raise ContractError("candidate ledger is not a real directory", "unauthorized-result")
            fd = _open_pinned_directory(path)
            try:
                pinned = os.fstat(fd)
                if _is_reparse_or_link(pinned) or not _same_file(info, pinned):
                    raise OSError("candidate ledger changed while being pinned")
                yield fd
            finally:
                os.close(fd)
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError("candidate ledger could not be pinned",
                            "verification-unavailable") from exc


def _remove_pinned_directory(path: Path) -> None:
    absolute = _lexical_absolute(path)
    with _pinned_real_directory(absolute.parent) as parent_fd:
        if parent_fd is None:
            raise OSError("directory parent was not pinned")
        if os.name == "nt":
            absolute.rmdir()
        else:
            os.rmdir(absolute.name, dir_fd=parent_fd)


def _open_windows_relative_file(directory_fd: int, name: str, *, create: bool) -> int:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                    ("Buffer", wintypes.LPWSTR)]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE),
                    ("ObjectName", ctypes.POINTER(UnicodeString)), ("Attributes", wintypes.ULONG),
                    ("SecurityDescriptor", ctypes.c_void_p),
                    ("SecurityQualityOfService", ctypes.c_void_p)]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_void_p)]

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    nt_create = ntdll.NtCreateFile
    nt_create.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                          ctypes.POINTER(ObjectAttributes), ctypes.POINTER(IoStatusBlock),
                          ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                          wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    nt_create.restype = wintypes.LONG
    name_buffer = ctypes.create_unicode_buffer(name)
    unicode_name = UnicodeString(len(name) * 2, (len(name) + 1) * 2,
                                ctypes.cast(name_buffer, wintypes.LPWSTR))
    attributes = ObjectAttributes(ctypes.sizeof(ObjectAttributes),
                                  wintypes.HANDLE(msvcrt.get_osfhandle(directory_fd)),
                                  ctypes.pointer(unicode_name), 0, None, None)
    status_block = IoStatusBlock()
    handle = wintypes.HANDLE()
    status = nt_create(ctypes.byref(handle), 0x0012019F if create else 0x00120089,
                       ctypes.byref(attributes), ctypes.byref(status_block), None, 0, 0x7,
                       2 if create else 1, 0x00200000 | 0x00000040 | 0x00000020, None, 0)
    unsigned_status = status & 0xFFFFFFFF
    if unsigned_status == 0xC0000035:
        raise FileExistsError(name)
    if unsigned_status == 0xC0000034:
        raise FileNotFoundError(name)
    handle_value = _native_handle_value(handle)
    if status < 0 or not handle_value:
        raise OSError(unsigned_status, "candidate record could not be opened")
    try:
        return msvcrt.open_osfhandle(handle_value, (os.O_RDWR if create else os.O_RDONLY) | os.O_BINARY)
    except OSError:
        ctypes.WinDLL("Kernel32", use_last_error=True).CloseHandle(handle)
        raise


def _open_ledger_file(directory_fd: int, name: str, *, create: bool) -> int:
    if os.name == "nt":
        fd = _open_windows_relative_file(directory_fd, name, create=create)
    else:
        flags = os.O_RDONLY if not create else os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if _is_reparse_or_link(info) or not stat.S_ISREG(info.st_mode) \
                or getattr(info, "st_nlink", 1) != 1:
            raise OSError("candidate record is not a regular file")
    except OSError:
        os.close(fd)
        raise
    return fd


def _oid(repo: Path, ref: str, *, missing: str = "source-unavailable") -> str:
    result = _checked_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    value = result.stdout.strip()
    if result.returncode or not OID.fullmatch(value):
        raise ContractError(f"commit is unavailable: {ref}", missing)
    return value


def _ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = _checked_git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode not in (0, 1):
        raise ContractError("Git ancestry could not be measured", "verification-unavailable")
    return result.returncode == 0


@contextlib.contextmanager
def _pinned_git_dir(repo: Path) -> Iterator[Path]:
    """Resolve Git's directory while the repository's lexical .git entry is pinned."""
    repository = _lexical_absolute(repo)
    expected = repository / ".git"
    try:
        with _pinned_real_directory(expected) as git_fd:
            if git_fd is None:
                raise OSError("repository Git directory was not pinned")
            result = _checked_git(repo, "rev-parse", "--path-format=absolute", "--git-dir")
            if result.returncode:
                raise ContractError("repository Git directory could not be resolved",
                                    "repository-unavailable")
            raw = result.stdout.strip()
            if not raw or "\x00" in raw:
                raise ValueError("empty or NUL Git directory")
            path = _lexical_absolute(raw)
            if os.path.normcase(path) != os.path.normcase(expected):
                raise OSError("Git directory is not the pinned repository .git entry")
            yield path
    except ContractError:
        raise
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("repository Git directory is unavailable", "repository-unavailable") from exc


def _git_dir(repo: Path) -> Path:
    # Keep the historical path-returning helper for callers, while the resolver itself is bound
    # to a no-follow handle for the complete check-and-use transaction above.
    with _pinned_git_dir(repo) as path:
        return path


def _require_descriptor_repository(contract: dict[str, Any], repo: Path) -> Path:
    """Bind every operation to the descriptor's canonical checkout, not a caller-selected clone."""
    try:
        supplied = _lexical_absolute(Path(repo))
        declared = _lexical_absolute(contract["repo"])
        with _pinned_real_directory(supplied) as supplied_fd, \
                _pinned_real_directory(declared) as declared_fd:
            if supplied_fd is None or declared_fd is None:
                raise OSError("repository directory was not pinned")
            supplied_info = os.fstat(supplied_fd)
            declared_info = os.fstat(declared_fd)
            if _is_reparse_or_link(supplied_info) or _is_reparse_or_link(declared_info) \
                    or not stat.S_ISDIR(supplied_info.st_mode) \
                    or not stat.S_ISDIR(declared_info.st_mode) \
                    or not _same_file(supplied_info, declared_info):
                raise ContractError("supplied repository does not match project descriptor",
                                    "unauthorized-result")
    except ContractError:
        raise
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("project repository is unavailable", "repository-unavailable") from exc
    return supplied


def _contract_fields(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate and return the additive v2 fields from a v1-compatible descriptor."""
    if "schema_version" in contract and (
            type(contract["schema_version"]) is not int or contract["schema_version"] != 1):
        raise ContractError("schema_version must be the integer 1")
    required = {
        "id", "repo", "default_branch", "integration_branch", "task_branch_pattern",
        "external_branch_patterns", "publication_policy", "source_policy", "allowed_paths",
        "gate_commands", "eligible_hosts", "eligible_pools",
    }
    missing = sorted(key for key in required if key not in contract)
    if missing:
        raise ContractError(f"project contract is missing fields: {', '.join(missing)}")
    if not isinstance(contract["id"], str) or not IDENT.fullmatch(contract["id"]):
        raise ContractError("project id is invalid")
    if not isinstance(contract["repo"], str) or not contract["repo"].strip() \
            or any(ord(char) < 32 for char in contract["repo"]) \
            or contract["repo"] != contract["repo"].strip():
        raise ContractError("project repo must be a non-empty string")
    for key in ("default_branch", "integration_branch"):
        value = contract[key]
        if not isinstance(value, str) or not BRANCH.fullmatch(value) or value.startswith("/") \
                or not _valid_git_ref(value):
            raise ContractError(f"{key} is invalid")
    if contract["default_branch"] == contract["integration_branch"]:
        raise ContractError("default and integration branches must differ")
    pattern = contract["task_branch_pattern"]
    if not isinstance(pattern, str) or pattern.count("{task_id}") != 1:
        raise ContractError("task_branch_pattern must contain exactly one {task_id}")
    probe = pattern.replace("{task_id}", "probe")
    if not BRANCH.fullmatch(probe) or not _valid_git_ref(probe) \
            or probe in {contract["default_branch"], contract["integration_branch"]}:
        raise ContractError("task_branch_pattern is unsafe")
    patterns = contract["external_branch_patterns"]
    if not isinstance(patterns, list) or not patterns or any(
        not isinstance(item, str) or not item or item in {"*", "**"} or not _valid_git_pattern(item)
        for item in patterns
    ) or len(set(patterns)) != len(patterns):
        raise ContractError("external_branch_patterns must be a non-empty restricted list")
    task_probe = pattern.replace("{task_id}", "probe")
    task_prefix, task_suffix = pattern.split("{task_id}")
    if any(fnmatch.fnmatchcase(task_probe, item)
           or item.startswith(task_prefix)
           or task_prefix.startswith(item.rstrip("*")) for item in patterns):
        raise ContractError("external branch namespace overlaps task branch namespace")
    if contract["publication_policy"] != "verified-integration-only":
        raise ContractError("unsupported publication policy")
    if contract["source_policy"] != "exact-sha":
        raise ContractError("unsupported source policy")
    contract["allowed_paths"] = _normalized_paths(contract["allowed_paths"], field="allowed_paths")
    for key in ("gate_commands", "eligible_hosts", "eligible_pools"):
        values = contract[key]
        if not isinstance(values, list) or not values \
                or any(not isinstance(item, str) or not item or any(
                    ord(char) < 32 and (key != "gate_commands" or char != "\t") for char in item
                ) for item in values) \
                or len(set(values)) != len(values):
            raise ContractError(f"{key} must be a string array")
    required_gate_profile = _required_gate_profile(contract)
    gate_files = required_gate_profile["gate_files"] if required_gate_profile else []
    controller_gate_files = required_gate_profile["controller_gate_files"] if required_gate_profile else []
    if any(not _valid_gate_command(item, allowed_paths=contract["allowed_paths"],
                                  gate_files=gate_files,
                                  controller_gate_files=controller_gate_files)
           for item in contract["gate_commands"]):
        raise ContractError("gate_commands must contain non-vacuous commands")
    _gate_digest(contract)
    return contract


def validate_project_descriptor(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate an active descriptor without changing its v1 representation."""
    if not isinstance(contract, dict):
        raise ContractError("project descriptor must be an object")
    return _contract_fields(contract)


def task_branch(contract: dict[str, Any], task_id: str) -> str:
    validate_project_descriptor(contract)
    if not isinstance(task_id, str) or not IDENT.fullmatch(task_id):
        raise ContractError("task id is invalid")
    result = contract["task_branch_pattern"].replace("{task_id}", task_id)
    if not BRANCH.fullmatch(result) or not _valid_git_ref(result) or result in {
        contract["default_branch"], contract["integration_branch"]
    }:
        raise ContractError("resolved task branch is unsafe")
    return result


def branch_kind(contract: dict[str, Any], branch: str) -> str:
    """Return a branch namespace, refusing default and integration as execution targets."""
    validate_project_descriptor(contract)
    if not isinstance(branch, str) or not BRANCH.fullmatch(branch) or not _valid_git_ref(branch):
        raise ContractError("branch name is invalid", "unauthorized-result")
    if branch == contract["default_branch"]:
        raise ContractError("executor may not target the default branch", "unauthorized-result")
    if branch == contract["integration_branch"]:
        raise ContractError("executor may not target the integration branch", "unauthorized-result")
    # A resolved task branch is checked by constructing it from the task id.  This avoids
    # accepting arbitrary refs under a broad prefix when a project chose a custom pattern.
    prefix, suffix = contract["task_branch_pattern"].split("{task_id}")
    if branch.startswith(prefix) and branch.endswith(suffix):
        task_id = branch[len(prefix):len(branch) - len(suffix) if suffix else None]
        if task_id and IDENT.fullmatch(task_id):
            return "task"
    if any(fnmatch.fnmatchcase(branch, pattern) for pattern in contract["external_branch_patterns"]):
        return "external"
    raise ContractError("branch is outside the project execution namespaces", "unauthorized-result")


def _path_allowed(path: str, allowed: list[str]) -> bool:
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        return False
    normalized = PurePosixPath(path)
    if normalized.is_absolute() or any(part in {".", ".."} for part in normalized.parts):
        return False
    for root in allowed:
        root_path = PurePosixPath(root.rstrip("/"))
        if root == "." or normalized == root_path or root_path in normalized.parents:
            return True
    return False


def _tree_entries(repo: Path, commit: str) -> dict[str, tuple[str, str, str]]:
    """Read the candidate tree, retaining modes so symlink/submodule escapes cannot hide."""
    try:
        result = _checked_git(repo, "ls-tree", "-r", "-t", "-z", "--full-tree", commit)
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("candidate tree could not be inspected", "verification-unavailable") from exc
    if result.returncode:
        raise ContractError("candidate tree could not be inspected", "verification-unavailable")
    entries: dict[str, tuple[str, str, str]] = {}
    folded_paths: set[str] = set()
    for record in result.stdout.split("\x00"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise ContractError("candidate tree entry is malformed", "unauthorized-result")
        mode, entry_type, oid = fields
        try:
            path = _normalized_path(raw_path, field="candidate tree path")
        except ContractError as exc:
            raise ContractError("candidate tree contains an unsafe path", "unauthorized-result") from exc
        if path in entries:
            raise ContractError("candidate tree contains duplicate paths", "unauthorized-result")
        # Git permits case-distinct names, but Windows and other case-insensitive consumers do
        # not.  Refuse an ambiguous tree before any symlink lookup can overwrite one entry with
        # another (for example, `src/Escape` and `src/escape`).
        folded_path = path.casefold()
        if folded_path in folded_paths:
            raise ContractError("candidate tree contains a case-folded path collision", "unauthorized-result")
        entries[path] = (mode, entry_type, oid)
        folded_paths.add(folded_path)
    return entries


def _resolve_tree_path(path: str, symlinks: dict[str, str]) -> str:
    """Resolve a Git-tree path lexically through symlinks, refusing escapes and cycles."""
    pending = list(PurePosixPath(path).parts)
    seen: set[str] = set()
    symlink_lookup = {name.casefold(): (name, target) for name, target in symlinks.items()}
    for _ in range(64):
        current = "/".join(pending) or "."
        link_record = symlink_lookup.get(current.casefold())
        if link_record is None:
            return current
        link, target = link_record
        folded_link = link.casefold()
        if folded_link in seen:
            raise ContractError("candidate tree contains a symlink cycle", "unauthorized-result")
        seen.add(folded_link)
        if not target or any(char in target for char in ("\x00", "\n", "\r", "\\")) \
                or target.startswith("/") \
                or re.match(r"^[A-Za-z]:", target):
            raise ContractError("candidate tree contains an unsafe symlink target", "unauthorized-result")
        base = list(PurePosixPath(link).parent.parts)
        for component in target.split("/"):
            if component in ("", "."):
                continue
            if component == "..":
                if not base:
                    raise ContractError("candidate symlink escapes the repository", "unauthorized-result")
                base.pop()
            else:
                base.append(component)
        pending = base
    raise ContractError("candidate tree contains excessive symlink indirection", "unauthorized-result")


def _inspect_candidate_tree(repo: Path, commit: str, allowed: list[str]) -> None:
    """Reject tree modes and symlink resolutions that evade the declared scope."""
    entries = _tree_entries(repo, commit)
    symlinks: dict[str, str] = {}
    for path, (mode, entry_type, oid) in entries.items():
        components = PurePosixPath(path).parts
        protected = {".git", "control-plane"}
        if any(component.casefold() in protected for component in components):
            raise ContractError("candidate tree contains a protected path", "unauthorized-result")
        if mode == "160000" or entry_type == "commit":
            raise ContractError("candidate tree contains an unsafe submodule entry", "unauthorized-result")
        if mode != "120000":
            continue
        try:
            target_result = _checked_git(repo, "cat-file", "blob", oid)
        except _BOUNDARY_ERRORS as exc:
            raise ContractError("candidate symlink target could not be read", "verification-unavailable") from exc
        if target_result.returncode:
            raise ContractError("candidate symlink target could not be read", "verification-unavailable")
        symlinks[path] = target_result.stdout.rstrip("\n")
    for path in symlinks:
        resolved = _resolve_tree_path(path, symlinks)
        components = PurePosixPath(resolved).parts
        if any(component.casefold() in protected for component in components) \
                or (_path_allowed(path, allowed) and not _path_allowed(resolved, allowed)):
            raise ContractError("candidate symlink escapes allowed_paths", "unauthorized-result")


def _validate_task_workdir(repo: Path, value: str) -> str:
    try:
        normalized = _normalized_path(value, field="task workdir")
        root = _lexical_absolute(repo)
        candidate = root.joinpath(*PurePosixPath(normalized).parts)
        # Never resolve the candidate.  Pin the root and every existing component, rejecting
        # symlinks and Windows junctions before returning the repository-relative spelling.
        with _pinned_real_directory(candidate, allow_missing=True):
            pass
    except ContractError:
        raise
    except _BOUNDARY_ERRORS as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("task workdir escapes the project repository", "unauthorized-result") from exc
    return normalized


def admit_task(contract: dict[str, Any], task: dict[str, Any], repo: Path, *, new_generation: bool = True) -> dict[str, Any]:
    """Admit a task only when its source branch currently names its exact source SHA."""
    validate_project_descriptor(contract)
    repo = _require_descriptor_repository(contract, repo)
    if not isinstance(task, dict):
        raise ContractError("task must be an object")
    for key in ("workdir", "acceptance", "goal", "gate_command", "seed", "repo",
                "source_repository", "source_branch", "source_sha", "target_branch"):
        if key in task and (not isinstance(task[key], str)
                            or any(ord(char) < 32 and char != "\t" for char in task[key])):
            raise ContractError(f"task field {key} must be a string")
    if "gate_commands" in task and (
        not isinstance(task["gate_commands"], list)
        or any(not isinstance(item, str) for item in task["gate_commands"])
    ):
        raise ContractError("task field gate_commands must be a string array")
    task_gate_files: list[str] | None = None
    task_controller_gate_files: list[str] | None = None
    if "gate_files" in task:
        task_gate_files = _normalized_gate_files(task["gate_files"], field="task gate_files")
    if "controller_gate_files" in task:
        task_controller_gate_files = _normalized_gate_files(
            task["controller_gate_files"], field="task controller_gate_files")
    required_gate_profile = _required_gate_profile(contract)
    if task_gate_files is not None:
        expected_gate_files = required_gate_profile["gate_files"] if required_gate_profile else []
        if task_gate_files != expected_gate_files:
            raise ContractError("task gate_files differ from the repository gate", "unauthorized-result")
    if task_controller_gate_files is not None:
        expected_controller_gate_files = (required_gate_profile["controller_gate_files"]
                                          if required_gate_profile else [])
        if task_controller_gate_files != expected_controller_gate_files:
            raise ContractError("task controller_gate_files differ from the repository gate",
                                "unauthorized-result")
    expected_acceptance = (required_gate_profile["acceptance"] if required_gate_profile
                           else " && ".join(contract["gate_commands"]))
    if "acceptance" in task and task["acceptance"] != expected_acceptance:
        raise ContractError("task acceptance differs from the repository gate", "unauthorized-result")
    if "gate_command" in task and task["gate_command"] not in contract["gate_commands"] \
            and task["gate_command"] != expected_acceptance:
        raise ContractError("task gate_command is not permitted by the repository gate", "unauthorized-result")
    if "gate_commands" in task and task["gate_commands"] != contract["gate_commands"]:
        raise ContractError("task gate_commands differ from the repository gate", "unauthorized-result")
    normalized_workdir = None
    if "workdir" in task:
        normalized_workdir = _validate_task_workdir(repo, task["workdir"])
    task_id = task.get("id")
    task_branch(contract, task_id)
    source_branch = task.get("source_branch")
    source_sha = task.get("source_sha")
    if new_generation and (not isinstance(source_branch, str) or not source_branch):
        raise ContractError("new task requires source_branch", "source-unavailable")
    if new_generation and (not isinstance(source_sha, str) or not OID.fullmatch(source_sha)):
        raise ContractError("new task requires an exact source_sha", "source-unavailable")
    if source_branch is None:
        source_branch = contract["default_branch"]
    if not isinstance(source_branch, str) or not BRANCH.fullmatch(source_branch):
        raise ContractError("source_branch is invalid", "source-unavailable")
    if source_branch in {contract["integration_branch"]}:
        raise ContractError("source_branch may not be the integration branch", "unauthorized-result")
    if not isinstance(source_sha, str) or not OID.fullmatch(source_sha):
        if new_generation:
            raise ContractError("source_sha is absent or malformed", "source-unavailable")
        return task
    observed = _oid(repo, f"refs/heads/{source_branch}")
    if observed != source_sha:
        raise ContractError("source branch moved after exact SHA admission", "source-unavailable")
    declared_repo = task.get("source_repository", task.get("repo", contract["repo"]))
    try:
        if not isinstance(declared_repo, str):
            raise ValueError("task source repository is not a string")
        _require_descriptor_repository(contract, Path(declared_repo))
        for key in ("repo", "source_repository"):
            if key in task:
                if not isinstance(task[key], str):
                    raise ValueError(f"task {key} is not a string")
                _require_descriptor_repository(contract, Path(task[key]))
    except ContractError as exc:
        raise ContractError("task source repository does not match project", "unauthorized-result") from exc
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("task source repository does not match project", "unauthorized-result") from exc
    allowed = _normalized_paths(task.get("allowed_paths", contract["allowed_paths"]), field="task allowed_paths")
    _require_path_subset(allowed, contract["allowed_paths"], field="task allowed_paths")
    target = task.get("target_branch")
    if target is not None:
        branch_kind(contract, target)
    result = {**task, "source_branch": source_branch, "source_sha": source_sha,
              "source_repository": declared_repo, "task_branch": task_branch(contract, task_id)}
    if task_gate_files is not None:
        result["gate_files"] = task_gate_files
    if task_controller_gate_files is not None:
        result["controller_gate_files"] = task_controller_gate_files
    if normalized_workdir is not None:
        result["workdir"] = normalized_workdir
    return result


def _candidate_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise ContractError("candidate must be an object", "unauthorized-result")
    try:
        json.dumps(candidate, sort_keys=True, allow_nan=False)
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("candidate contains non-serializable input", "unauthorized-result") from exc
    required = {"project_id", "task_id", "source_branch", "source_sha", "external_branch", "candidate_commit"}
    missing = sorted(key for key in required if key not in candidate)
    if missing:
        raise ContractError(f"candidate is missing fields: {', '.join(missing)}")
    return candidate


def _validate_windows_acl_records(owner: str, controller: str,
                                  aces: list[tuple[str, int, int, int]], require_owner: bool) -> None:
    if require_owner and owner != controller:
        raise OSError("controller key owner is not the current controller")
    write_mask = 0x10000000 | 0x40000000 | 0x00000002 | 0x00000004 | 0x00000010 \
        | 0x00000100 | 0x00010000 | 0x00040000 | 0x00080000
    for ace_sid, ace_flags, mask, ace_type in aces:
        if ace_flags & 0x10 and ace_sid != owner and mask & write_mask:
            raise OSError("controller key ACL grants inherited write access")
        if ace_type in {0, 5, 9, 11} and ace_sid != owner and mask & write_mask:
            raise OSError("controller key ACL grants explicit non-owner write access")


def _check_windows_acl(path: Path, require_owner: bool) -> None:
    """Typed wrapper around the native ACL inspection implementation."""
    try:
        _check_windows_acl_impl(path, require_owner)
    except ContractError:
        raise
    except Exception as exc:
        raise ContractError("controller key ACL could not be inspected",
                            "verification-unavailable") from exc


def _check_windows_acl_impl(path: Path, require_owner: bool, *, handle_value: int | None = None) -> None:
    """Check owner and writable ACEs from the OS security descriptor, not environment text."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("Advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.HANDLE)]
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    get_token_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL

    # Read the descriptor from a no-follow handle.  Named security-info lookup alone leaves a
    # path substitution window between the lstat and the ACL decision.
    owns_handle = handle_value is None
    if owns_handle:
        handle = create_file(str(path), 0x80000000 | 0x00020000, 0x00000003, None, 3,
                             0x00000080 | 0x00200000 | 0x02000000, None)
        handle_value = _native_handle_value(handle)
        if handle_value in {None, ctypes.c_void_p(-1).value}:
            raise OSError(ctypes.get_last_error(), "controller key could not be opened without following reparses")
    security_descriptor = ctypes.c_void_p()
    owner_sid = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    get_security = advapi32.GetSecurityInfo
    get_security.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                             ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                             ctypes.POINTER(ctypes.c_void_p)]
    get_security.restype = wintypes.DWORD
    try:
        result = get_security(wintypes.HANDLE(handle_value), 1, 1 | 4,
                              ctypes.byref(owner_sid), None, ctypes.byref(dacl), None,
                              ctypes.byref(security_descriptor))
        if result:
            raise OSError(result, "controller key security descriptor could not be inspected")

        def sid_text(sid: ctypes.c_void_p) -> str:
            converted = wintypes.LPWSTR()
            convert = advapi32.ConvertSidToStringSidW
            convert.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
            convert.restype = wintypes.BOOL
            if not convert(sid, ctypes.byref(converted)):
                raise OSError(ctypes.get_last_error(), "controller key SID could not be inspected")
            try:
                return converted.value.casefold()
            finally:
                local_free(converted)

        token = wintypes.HANDLE()
        if not open_process_token(get_current_process(), 8, ctypes.byref(token)):
            raise OSError(ctypes.get_last_error(), "controller token could not be inspected")
        try:
            needed = wintypes.DWORD()
            get_token_information(token, 1, None, 0, ctypes.byref(needed))
            if not needed.value:
                raise OSError(ctypes.get_last_error(), "controller token SID could not be inspected")
            token_info = ctypes.create_string_buffer(needed.value)
            if not get_token_information(token, 1, token_info, needed, ctypes.byref(needed)):
                raise OSError(ctypes.get_last_error(), "controller token SID could not be inspected")
            class SidAndAttributes(ctypes.Structure):
                _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]
            current_sid = SidAndAttributes.from_buffer(token_info).Sid
        finally:
            close_handle(token)

        owner = sid_text(owner_sid)
        controller = sid_text(ctypes.c_void_p(current_sid))

        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        get_dacl = advapi32.GetSecurityDescriptorDacl
        get_dacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                             ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
        get_dacl.restype = wintypes.BOOL
        if not get_dacl(security_descriptor, ctypes.byref(present), ctypes.byref(dacl),
                        ctypes.byref(defaulted)) or not present.value or not dacl.value:
            raise OSError("controller key DACL could not be inspected")
        get_acl_info = advapi32.GetAclInformation
        get_acl_info.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
        get_acl_info.restype = wintypes.BOOL
        class AclSizeInformation(ctypes.Structure):
            _fields_ = [("AceCount", wintypes.DWORD), ("AclBytesInUse", wintypes.DWORD),
                        ("AclBytesFree", wintypes.DWORD)]
        size_info = AclSizeInformation()
        if not get_acl_info(dacl, ctypes.byref(size_info), ctypes.sizeof(size_info), 2):
            raise OSError("controller key DACL entries could not be inspected")
        get_ace = advapi32.GetAce
        get_ace.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        get_ace.restype = wintypes.BOOL
        aces: list[tuple[str, int, int, int]] = []
        for index in range(size_info.AceCount):
            ace = ctypes.c_void_p()
            if not get_ace(dacl, index, ctypes.byref(ace)) or not ace.value:
                raise OSError("controller key ACE could not be inspected")
            header = ctypes.string_at(ace.value, 8)
            ace_type, ace_flags = header[0], header[1]
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            if ace_type not in {0, 1, 5, 6, 9, 10, 11, 12}:
                continue
            sid_offset = 8
            if ace_type in {5, 6, 11, 12}:
                object_flags = ctypes.c_uint32.from_address(ace.value + sid_offset).value
                sid_offset += 4
                if object_flags & 0x1:
                    sid_offset += 16
                if object_flags & 0x2:
                    sid_offset += 16
            # SID bytes are inline in ACCESS_*_ACE (SidStart), not a pointer-sized field.
            ace_sid = ctypes.c_void_p(ace.value + sid_offset)
            aces.append((sid_text(ace_sid), ace_flags, mask, ace_type))
        _validate_windows_acl_records(owner, controller, aces, require_owner)
    finally:
        if security_descriptor.value:
            local_free(security_descriptor)
        if owns_handle:
            close_handle(wintypes.HANDLE(handle_value))


def _open_controller_key_fd(path: Path) -> int:
    """Open the controller key once, without following a leaf reparse or allowing replacement."""
    initial = os.lstat(path)
    if _is_reparse_or_link(initial) or not stat.S_ISREG(initial.st_mode) \
            or getattr(initial, "st_nlink", 1) != 1:
        raise OSError("controller key is not a regular non-reparse file")
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            current = os.fstat(fd)
            if _is_reparse_or_link(current) or not stat.S_ISREG(current.st_mode) \
                    or getattr(current, "st_nlink", 1) != 1 or not _same_file(initial, current):
                raise OSError("controller key changed while being opened")
            return fd
        except OSError:
            os.close(fd)
            raise
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    native = create_file(str(path), 0x80000000 | 0x00020000, 0x00000003, None, 3,
                         0x00000080 | 0x00200000 | 0x02000000, None)
    value = _native_handle_value(native)
    if value in {None, ctypes.c_void_p(-1).value}:
        raise OSError(ctypes.get_last_error(), "controller key could not be opened without following reparses")
    try:
        fd = msvcrt.open_osfhandle(value, os.O_RDONLY | os.O_BINARY)
    except OSError:
        close_handle(native)
        raise
    try:
        current = os.fstat(fd)
        if _is_reparse_or_link(current) or not stat.S_ISREG(current.st_mode) \
                or getattr(current, "st_nlink", 1) != 1 or not _same_file(initial, current):
            raise OSError("controller key changed while being opened")
        return fd
    except OSError:
        os.close(fd)
        raise


def _controller_key() -> bytes:
    # This is deliberately not configurable through caller environment.  The controller installs
    # the key at this fixed path with owner-only permissions; allowing a candidate to select a key
    # file would turn the receipt into self-attestation.
    try:
        key_path = _lexical_absolute(Path(CONTROLLER_KEY_PATH))
        with _pinned_windows_parents(key_path):
            def is_link(path: Path) -> bool:
                if path.is_symlink():
                    return True
                # Windows junctions/reparse points are not always reported by is_symlink().
                return bool(getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)

            key_info = os.lstat(key_path)
            if _is_reparse_or_link(key_info) or not stat.S_ISREG(key_info.st_mode):
                raise OSError("controller key is not a regular file")
            if hasattr(os, "getuid") and key_info.st_uid != os.getuid():
                raise OSError("controller key owner is not the current controller")
            if os.name != "nt" and key_info.st_mode & 0o077:
                raise OSError("controller key permissions are too broad")
            for parent in (key_path.parent, *key_path.parent.parents):
                if parent == parent.parent:
                    break
                if is_link(parent) or not parent.is_dir():
                    raise OSError("controller key parent is not a safe directory")
                if os.name != "nt":
                    parent_mode = parent.stat().st_mode
                    # A sticky temporary root (for hermetic controller tests) may be world-writable;
                    # every non-sticky ancestor must remain private to its owner.
                    if parent_mode & 0o022 and not (parent_mode & 0o1000 and parent_mode & 0o002):
                        raise OSError("controller key parent is writable by group or world")
            # The key is checked from the OS security descriptor.  No caller environment value and
            # no localized text rendering is part of the trust decision.  The native implementation
            # opens with OPEN_REPARSE_POINT and denies delete sharing, so the ACL decision is tied
            # to the file being read.  On POSIX, O_NOFOLLOW provides the corresponding leaf check.
            key_fd = _open_controller_key_fd(key_path)
            try:
                if os.name == "nt":
                    import msvcrt
                    _check_windows_acl_impl(key_path, True, handle_value=msvcrt.get_osfhandle(key_fd))
                key_bytes = os.read(key_fd, 4097)
                if len(key_bytes) > 4096:
                    raise OSError("controller verifier key is too large")
                raw = key_bytes.decode("utf-8").strip()
            finally:
                os.close(key_fd)
            # Ancestors are already pinned with delete sharing denied above.  Their normal
            # SYSTEM/Administrators maintenance ACEs are not key-authority ACEs; applying the
            # key's owner-only DACL rule to those directories would make the fixed production
            # path reject a legitimately installed key.
    except ContractError:
        raise
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("controller verifier key is unavailable", "verification-unavailable") from exc
    except Exception as exc:
        raise ContractError("controller verifier key is unavailable", "verification-unavailable") from exc
    if not raw or "\n" in raw or "\r" in raw:
        raise ContractError("controller verifier authentication is unavailable", "verification-unavailable")
    return raw.encode("utf-8")


def _controller_relative_gate_path(value: str, controller_root: Path) -> str | None:
    """Map deployed /opt/recursion paths and local checkout paths lexically."""
    normalized = value.replace("\\", "/")
    root = _lexical_absolute(controller_root).as_posix().replace("\\", "/").rstrip("/")
    if normalized == root or normalized.startswith(root + "/"):
        return normalized[len(root):].lstrip("/")
    logical_root = "/opt/recursion"
    if normalized == logical_root or normalized.startswith(logical_root + "/"):
        return normalized[len(logical_root):].lstrip("/")
    return None


def _gate_identity(value: str, profile: dict[str, Any]) -> tuple[tuple[str, ...], ...]:
    """Return a path-resolved identity for comparing equivalent gate spellings."""
    controller_root = _MODULE_ROOT
    controller_files = set(profile["controller_gate_files"])
    candidate_files = set(profile["gate_files"])
    identities: list[tuple[str, ...]] = []
    for raw_segment in re.split(r"\s*&&\s*", value.strip()):
        try:
            words = shlex.split(raw_segment.replace("\\", "/"), posix=True)
        except ValueError:
            return ((value.strip(),),)
        normalized: list[str] = []
        for word in words:
            relative = _controller_relative_gate_path(word, controller_root) \
                if _path_is_absolute_gate_operand(word) else None
            if relative in controller_files:
                normalized.append("controller:" + relative)
            elif word in controller_files:
                normalized.append("controller:" + word)
            elif word in candidate_files:
                normalized.append("candidate:" + word)
            elif relative is not None:
                normalized.append("absolute:" + word.casefold())
            else:
                normalized.append(word)
        if normalized and normalized[0].startswith("controller:") \
                and normalized[0].lower().endswith(".py"):
            normalized.insert(0, "python3")
        identities.append(tuple(normalized))
    return tuple(identities)


def _path_is_absolute_gate_operand(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return normalized.startswith("/") or bool(re.match(r"^[A-Za-z]:/", normalized))


def _observed_gate_contract(contract: dict[str, Any], worktree: Path,
                            *, baseline_commit: str | None = None,
                            execute: bool = False) -> dict[str, Any] | None:
    profile = _required_gate_profile(contract)
    if profile is None:
        return None
    validator = Path(__file__).with_name("validate-gate.py")
    args = [
        sys.executable, str(validator), "--acceptance", profile["acceptance"],
        "--allowed-paths", ",".join(contract["allowed_paths"]),
        "--gate-files", ",".join(profile["gate_files"]), "--repo", str(worktree),
        "--controller-root", str(_MODULE_ROOT),
        "--controller-gate-files", ",".join(profile["controller_gate_files"]), "--strict-direct",
    ]
    if baseline_commit is not None:
        args += ["--baseline-commit", baseline_commit]
    if execute:
        args.append("--execute")
    try:
        observed = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, timeout=30, check=False)
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("authoritative gate contract could not be measured",
                            "verification-unavailable") from exc
    if observed.returncode:
        raise ContractError("authoritative gate contract is not executable", "unauthorized-result")
    try:
        evidence = _strict_json_loads(observed.stdout)
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("authoritative gate contract evidence is malformed",
                            "verification-unavailable") from exc
    expected_keys = {"acceptance", "acceptance_sha256", "gate_files", "controller_gate_files"}
    allowed_keys = expected_keys | {"read_only_paths"}
    if not isinstance(evidence, dict) or not expected_keys <= set(evidence) \
            or not set(evidence) <= allowed_keys \
            or evidence["acceptance"] != profile["acceptance"] \
            or evidence["acceptance_sha256"] != hashlib.sha256(
                profile["acceptance"].encode()).hexdigest():
        raise ContractError("authoritative gate contract evidence is incomplete",
                            "verification-unavailable")
    if "read_only_paths" in evidence:
        read_only_paths = evidence["read_only_paths"]
        if not isinstance(read_only_paths, list) \
                or any(not isinstance(path, str) or not path or "\\" in path
                       or path.startswith("/") or ":" in path
                       or any(part in {"", ".", ".."} for part in path.split("/"))
                       for path in read_only_paths):
            raise ContractError("authoritative gate contract evidence is incomplete",
                                "verification-unavailable")
    for name, expected in (("gate_files", profile["gate_files"]),
                           ("controller_gate_files", profile["controller_gate_files"])):
        values = evidence.get(name)
        if not isinstance(values, dict) or set(values) != set(expected) \
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in values.values()):
            raise ContractError("authoritative gate contract evidence is incomplete",
                                "verification-unavailable")
    return evidence


def _run_independent_gates(contract: dict[str, Any], repo: Path, commit: str,
                           *, source: str | None = None) -> dict[str, Any] | None:
    """Run the descriptor's gates on an isolated detached worktree under controller control."""
    try:
        raw = tempfile.mkdtemp(prefix="recursion-gate-")
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("controller gate scratch directory could not be created",
                            "verification-unavailable") from exc
    try:
        worktree = Path(raw) / "candidate"
        created = _checked_git(repo, "worktree", "add", "--detach", "--quiet", str(worktree), commit)
        if created.returncode:
            raise ContractError("controller gate worktree could not be created", "verification-unavailable")
        try:
            if _required_gate_profile(contract) is not None:
                gate_contract = _observed_gate_contract(
                    contract, worktree, baseline_commit=source, execute=True)
                # A declared profile is authoritative.  The validator hashes and executes this
                # exact acceptance string in one transaction; never substitute contract["gate_commands"]
                # after provenance has been measured.
            else:
                gate_contract = None
                env = {key: value for key, value in os.environ.items()
                       if key in {"PATH", "HOME", "USERPROFILE", "SystemRoot", "WINDIR"}}
                env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                            "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
                for command in contract["gate_commands"]:
                    try:
                        result = subprocess.run(command, shell=True, cwd=worktree, env=env,
                                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, text=True, timeout=120, check=False)
                    except _BOUNDARY_ERRORS as exc:
                        raise ContractError("controller gate execution was unavailable",
                                            "verification-unavailable") from exc
                    if result.returncode:
                        raise ContractError("independent controller gate failed", "unauthorized-result")
        finally:
            removed = _checked_git(repo, "worktree", "remove", "--force", str(worktree))
            if removed.returncode:
                raise ContractError("controller gate worktree cleanup failed", "verification-unavailable")
        return gate_contract
    finally:
        # This is one private, uniquely-owned controller scratch directory.  Do not suppress an
        # ACL or reparse cleanup failure: a failed cleanup is itself a verification-unavailable
        # result, and links are removed as leaves without traversing their targets.
        try:
            _remove_tree_without_following(Path(raw))
        except OSError as exc:
            raise ContractError("controller gate scratch cleanup failed",
                                "verification-unavailable") from exc


def verify_candidate(contract: dict[str, Any], candidate: dict[str, Any], repo: Path,
                     *, receipt: dict[str, Any] | None = None,
                     allow_canonical: bool = False) -> dict[str, Any]:
    """Independently verify branch identity, source ancestry, scope, and verifier evidence."""
    validate_project_descriptor(contract)
    repo = _require_descriptor_repository(contract, repo)
    _candidate_payload(candidate)
    if "verification_receipt" in candidate or "verified" in candidate:
        raise ContractError("candidate may not supply verification authority", "unauthorized-result")
    if candidate["project_id"] != contract["id"]:
        raise ContractError("candidate project identity does not match", "unauthorized-result")
    task_branch(contract, candidate["task_id"])
    if candidate["source_branch"] != contract["default_branch"]:
        raise ContractError("candidate source branch is not the project source branch", "unauthorized-result")
    source = candidate["source_sha"]
    commit = candidate["candidate_commit"]
    if not isinstance(source, str) or not OID.fullmatch(source) or not isinstance(commit, str) or not OID.fullmatch(commit):
        raise ContractError("candidate commit identity is malformed", "unauthorized-result")
    external_branch = candidate["external_branch"]
    kind = branch_kind(contract, external_branch)
    if allow_canonical:
        expected_canonical = task_branch(contract, candidate["task_id"])
        if external_branch != expected_canonical or kind != "task":
            raise ContractError("canonical candidate must use its exact task branch", "unauthorized-result")
    elif kind != "external":
        raise ContractError("candidate must arrive on an external branch", "unauthorized-result")
    observed = _oid(repo, f"refs/heads/{candidate['external_branch']}", missing="unauthorized-result")
    if observed != commit:
        raise ContractError("external branch tip does not equal candidate commit", "unauthorized-result")
    resolved_source = _oid(repo, source, missing="source-unavailable")
    if resolved_source != source:
        raise ContractError("candidate source_sha is not the full canonical commit SHA", "unauthorized-result")
    _oid(repo, commit, missing="unauthorized-result")
    if not _ancestor(repo, source, commit):
        raise ContractError("candidate is not descended from its exact source SHA", "unauthorized-result")
    changed = _checked_git(repo, "diff", "--name-only", source, commit)
    if changed.returncode:
        raise ContractError("candidate diff could not be measured", "verification-unavailable")
    paths = [item for item in changed.stdout.splitlines() if item]
    allowed = _normalized_paths(candidate.get("allowed_paths", contract["allowed_paths"]), field="candidate allowed_paths")
    _require_path_subset(allowed, contract["allowed_paths"], field="candidate allowed_paths")
    if any(not _path_allowed(path, allowed) for path in paths):
        raise ContractError("candidate changed a path outside allowed_paths", "unauthorized-result")
    _inspect_candidate_tree(repo, commit, allowed)
    if not isinstance(receipt, dict):
        raise ContractError("controller verification receipt is required separately", "unauthorized-result")
    gate_contract = _run_independent_gates(contract, repo, commit, source=source)
    _verify_receipt(contract, candidate, receipt, source, commit, paths, allowed, repo,
                    gate_contract=gate_contract)
    result = dict(candidate)
    result["allowed_paths"] = allowed
    result["changed_paths"] = paths
    result["candidate_digest"] = _json_digest({key: result[key] for key in sorted(result) if key != "candidate_digest"})
    return result


def _verify_receipt(contract: dict[str, Any], candidate: dict[str, Any], receipt: dict[str, Any],
                    source: str, commit: str, paths: list[str], allowed: list[str], repo: Path,
                    *, gate_contract: dict[str, Any] | None = None) -> None:
    """Validate the controller-verifier receipt, including its tamper-evident digest binding."""
    required = {"schema", "project_id", "task_id", "source_sha", "candidate_commit", "diff_sha256",
                "scope_digest", "gate_digest", "verdict", "verifier", "provenance", "receipt_digest", "signature"}
    if not required.issubset(receipt):
        raise ContractError("controller verification receipt is incomplete", "unauthorized-result")
    if receipt.get("schema") != "recursion-controller-verifier-receipt/v1" \
            or receipt.get("verdict") != "pass" \
            or receipt.get("verifier") != "controller-verifier" \
            or receipt.get("provenance") != "recursion-controller":
        raise ContractError("controller verification receipt provenance is not trusted", "unauthorized-result")
    # Receipt provenance is the exact byte stream emitted by `git diff --binary`.  Do not route
    # this through text=True: universal-newline decoding would change the authenticated bytes on
    # Windows, and a re-encode cannot recover the original stream.
    diff = _checked_git_raw(repo, "diff", "--binary", source, commit)
    if diff.returncode:
        raise ContractError("candidate diff could not be hashed", "verification-unavailable")
    expected = {
        "schema": receipt["schema"], "project_id": contract["id"], "task_id": candidate["task_id"],
        "source_sha": source, "candidate_commit": commit,
        "diff_sha256": hashlib.sha256(diff.stdout).hexdigest(),
        "scope_digest": _json_digest(allowed),
        "gate_digest": (_json_digest(gate_contract) if gate_contract is not None
                        else _gate_digest(contract)),
        "verdict": "pass", "verifier": "controller-verifier", "provenance": "recursion-controller",
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise ContractError(f"controller receipt binding mismatch: {key}", "unauthorized-result")
    body = {key: receipt[key] for key in expected}
    if receipt.get("receipt_digest") != _json_digest(body):
        raise ContractError("controller verification receipt digest is invalid", "unauthorized-result")
    expected_signature = hmac.new(_controller_key(), receipt["receipt_digest"].encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(receipt.get("signature", "")), expected_signature):
        raise ContractError("controller verification receipt authentication is invalid", "unauthorized-result")


@contextlib.contextmanager
def _serialization_lock_path(lock: Path, timeout: float = 10.0) -> Iterator[None]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            # The lock itself is a durable ledger directory.  Pin its parent chain and use an
            # O_EXCL/no-follow owner record so both lock acquisition and owner publication are
            # bound to handles, rather than a check followed by an unpinned path write.
            with _pinned_ledger(lock) as lock_fd:
                try:
                    owner_fd = _open_ledger_file(lock_fd, "owner", create=True)
                except FileExistsError:
                    owner_fd = None
                if owner_fd is None:
                    owner_pid = 0
                    try:
                        owner_fd = _open_ledger_file(lock_fd, "owner", create=False)
                        try:
                            owner_pid = int(os.read(owner_fd, 128).decode("ascii").strip())
                        finally:
                            os.close(owner_fd)
                    except (FileNotFoundError, ValueError, TypeError, OverflowError,
                            UnicodeDecodeError, OSError):
                        owner_pid = 0
                    if owner_pid and owner_pid != os.getpid():
                        try:
                            os.kill(owner_pid, 0)
                        except (OSError, ProcessLookupError):
                            # Reclaim only the owner record while the directory handle and all
                            # ancestors remain pinned.  Creation of our replacement is atomic.
                            try:
                                if os.name == "nt":
                                    (lock / "owner").unlink()
                                else:
                                    os.unlink("owner", dir_fd=lock_fd)
                                continue
                            except _BOUNDARY_ERRORS:
                                pass
                    elif not owner_pid:
                        try:
                            if time.time() - os.fstat(lock_fd).st_mtime > timeout:
                                if os.name == "nt":
                                    (lock / "owner").unlink(missing_ok=True)
                                else:
                                    os.unlink("owner", dir_fd=lock_fd)
                                continue
                        except _BOUNDARY_ERRORS:
                            pass
                    # A live, malformed, or non-reclaimable owner keeps the lock busy.  Never
                    # remove a lock directory that we did not acquire.
                    continue
                else:
                    try:
                        payload = str(os.getpid()).encode("ascii")
                        if os.write(owner_fd, payload) != len(payload):
                            raise OSError("integration lock owner record was truncated")
                        os.fsync(owner_fd)
                    except _BOUNDARY_ERRORS as exc:
                        try:
                            os.close(owner_fd)
                        except OSError:
                            pass
                        raise ContractError("integration planning lock is unavailable",
                                            "verification-unavailable") from exc
                    os.close(owner_fd)
                    try:
                        yield
                    finally:
                        try:
                            if os.name == "nt":
                                (lock / "owner").unlink(missing_ok=True)
                            else:
                                os.unlink("owner", dir_fd=lock_fd)
                        except _BOUNDARY_ERRORS as exc:
                            raise ContractError("integration planning lock cleanup failed",
                                                "verification-unavailable") from exc
                    # Keep the pinned ledger context alive through the owner unlink; its verified
                    # handle and parent handles are closed before the lock directory is removed.
                    break
            try:
                _remove_pinned_directory(lock)
            except FileNotFoundError:
                pass
            except _BOUNDARY_ERRORS as exc:
                raise ContractError("integration planning lock cleanup failed",
                                    "verification-unavailable") from exc
            return
        except ContractError:
            raise
        except _BOUNDARY_ERRORS as exc:
            raise ContractError("integration planning lock is unavailable",
                                "verification-unavailable") from exc
        if time.monotonic() >= deadline:
            raise ContractError("integration planning lock is busy", "integration-busy")
        time.sleep(0.02)


@contextlib.contextmanager
def _serialization_lock(repo: Path, timeout: float = 10.0) -> Iterator[None]:
    """Hold the verified repository .git directory for the complete lock transaction."""
    with _pinned_git_dir(repo) as git_dir:
        with _serialization_lock_path(git_dir / "recursion-integration.lock", timeout):
            yield


def _canonical_ref(contract: dict[str, Any], task_id: str) -> str:
    return f"refs/heads/{task_branch(contract, task_id)}"


def import_candidate(contract: dict[str, Any], candidate: dict[str, Any], repo: Path,
                     *, receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Import one verified candidate exactly once; conflicting retries fail closed."""
    validate_project_descriptor(contract)
    repo = _require_descriptor_repository(contract, repo)
    with _serialization_lock(repo):
        verified = verify_candidate(contract, candidate, repo, receipt=receipt)
        digest = verified["candidate_digest"]
        with _pinned_git_dir(repo) as git_dir:
            ledger = git_dir / "recursion-candidates"
            with _pinned_ledger(ledger) as ledger_fd:
                idempotency_key = str(verified.get("idempotency_key") or digest)
                operation_key = _json_digest(["candidate-import", contract["id"], idempotency_key])
                record_name = f"{operation_key}.json"
                ref = _canonical_ref(contract, verified["task_id"])
                current_result = _checked_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
                current = current_result.stdout.strip() if current_result.returncode == 0 else ""
                if current and current != verified["candidate_commit"]:
                    raise ContractError("canonical task ref already names a different candidate", "idempotency-conflict")
                if current == verified["candidate_commit"]:
                    status = "idempotent"
                else:
                    created = _checked_git(repo, "update-ref", ref, verified["candidate_commit"], "0" * len(verified["candidate_commit"]))
                    if created.returncode:
                        # Another coordinator may have won the same import race. Re-read the ref and
                        # reconcile an identical winner; only a different winner is a real conflict.
                        raced = _checked_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
                        raced_commit = raced.stdout.strip() if raced.returncode == 0 else ""
                        if raced_commit == verified["candidate_commit"]:
                            status = "idempotent"
                        else:
                            raise ContractError("canonical task ref changed during import", "idempotency-conflict")
                    else:
                        status = "imported"
                record = {"schema": SCHEMA, "idempotency_key": verified.get("idempotency_key", digest),
                          "candidate_digest": digest, "task_id": verified["task_id"],
                          "canonical_ref": ref.removeprefix("refs/heads/"),
                          "candidate_commit": verified["candidate_commit"], "status": status}
                try:
                    try:
                        record_fd = _open_ledger_file(ledger_fd, record_name, create=False)
                    except FileNotFoundError:
                        try:
                            record_fd = _open_ledger_file(ledger_fd, record_name, create=True)
                        except FileExistsError:
                            record_fd = _open_ledger_file(ledger_fd, record_name, create=False)
                        else:
                            with os.fdopen(record_fd, "w", encoding="utf-8", newline="\n") as stream:
                                stream.write(json.dumps(record, sort_keys=True, indent=2) + "\n")
                                stream.flush()
                                os.fsync(stream.fileno())
                            if os.name != "nt":
                                os.fsync(ledger_fd)
                            record_fd = None
                    if record_fd is not None:
                        try:
                            with os.fdopen(record_fd, "r", encoding="utf-8") as stream:
                                existing = json.load(stream, parse_constant=_reject_json_constant)
                        except _BOUNDARY_ERRORS as exc:
                            raise ContractError("candidate idempotency record is unreadable or unsafe",
                                                "idempotency-conflict") from exc
                        if not isinstance(existing, dict):
                            raise ContractError("candidate idempotency record is not an object",
                                                "idempotency-conflict")
                        if existing.get("candidate_digest") != digest:
                            raise ContractError("candidate idempotency key names a different candidate", "idempotency-conflict")
                except ContractError:
                    raise
                except _BOUNDARY_ERRORS as exc:
                    raise ContractError("candidate idempotency record is unreadable or unsafe",
                                        "idempotency-conflict") from exc
                return record


def classify_stale_base(contract: dict[str, Any], candidate: dict[str, Any], repo: Path,
                        *, receipt: dict[str, Any] | None = None) -> str:
    """Classify a verified candidate against current default/integration ancestry."""
    try:
        canonical = isinstance(candidate, dict) and isinstance(candidate.get("task_id"), str) \
            and candidate.get("external_branch") == task_branch(contract, candidate["task_id"])
        candidate = verify_candidate(contract, candidate, repo, receipt=receipt, allow_canonical=canonical)
    except ContractError as exc:
        return exc.code if exc.code in OUTCOMES else "unauthorized-result"
    try:
        source = candidate["source_sha"]
        commit = candidate["candidate_commit"]
        external_branch = candidate["external_branch"]
        _oid(repo, source, missing="source-unavailable")
        _oid(repo, commit, missing="unauthorized-result")
        default = _oid(repo, f"refs/heads/{contract['default_branch']}", missing="source-unavailable")
        integration = _oid(repo, f"refs/heads/{contract['integration_branch']}", missing="source-unavailable")
    except ContractError as exc:
        return exc.code if exc.code in OUTCOMES else "invalidated"
    try:
        branch_kind(contract, external_branch)
        if _oid(repo, f"refs/heads/{external_branch}", missing="unauthorized-result") != commit:
            return "unauthorized-result"
    except ContractError:
        return "unauthorized-result"
    if not _ancestor(repo, source, commit):
        return "unauthorized-result"
    if source == default and integration == source:
        return "current-compatible"
    if _ancestor(repo, commit, default) or _ancestor(repo, commit, integration):
        return "stale-reviewable"
    if _ancestor(repo, source, default) and _ancestor(repo, source, integration):
        return "stale-rebase-required"
    return "stale-rebase-required"


def plan_integration(contract: dict[str, Any], candidate: dict[str, Any], repo: Path,
                     *, receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a serialized, read-only integration plan with an expected destination tip."""
    validate_project_descriptor(contract)
    repo = _require_descriptor_repository(contract, repo)
    with _serialization_lock(repo):
        canonical = isinstance(candidate, dict) and isinstance(candidate.get("task_id"), str) \
            and candidate.get("external_branch") == task_branch(contract, candidate["task_id"])
        verified = verify_candidate(contract, candidate, repo, receipt=receipt, allow_canonical=canonical)
        outcome = classify_stale_base(contract, verified, repo, receipt=receipt)
        integration = _oid(repo, f"refs/heads/{contract['integration_branch']}", missing="source-unavailable")
        return {"schema": SCHEMA, "task_id": verified["task_id"], "candidate_commit": verified["candidate_commit"],
                "source_sha": verified["source_sha"], "destination_branch": contract["integration_branch"],
                "expected_destination_tip": integration, "outcome": outcome,
                "can_integrate": outcome == "current-compatible", "serialized": True}


def _load(path: str) -> dict[str, Any]:
    try:
        value = _strict_json_loads(Path(path).read_text(encoding="utf-8"))
    except _BOUNDARY_ERRORS as exc:
        raise ContractError("cannot read JSON contract") from exc
    if not isinstance(value, dict):
        raise ContractError("JSON contract must be an object")
    return value


_ABSOLUTE_PATH_IN_ERROR = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\|/(?:[^/\s]+/)+)[^\s\"']*"
)


def _public_error_message(message: str) -> str:
    """Keep JSON CLI diagnostics useful without exposing host paths or OS error text."""
    return _ABSOLUTE_PATH_IN_ERROR.sub("<path>", message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("validate-project", "admit-task", "verify-candidate", "import-candidate", "classify", "plan"):
        command = subs.add_parser(name)
        command.add_argument("project")
        command.add_argument("payload", nargs="?")
        command.add_argument("--repo")
        command.add_argument("--receipt")
    args = parser.parse_args(argv)
    try:
        contract = validate_project_descriptor(_load(args.project))
        if args.command == "validate-project":
            result: Any = {"valid": True, "schema": SCHEMA, "project_id": contract["id"]}
        else:
            if not args.payload or not args.repo:
                raise ContractError(f"{args.command} requires payload and --repo")
            payload = _load(args.payload)
            repo = _lexical_absolute(args.repo)
            if args.command != "admit-task" and not args.receipt:
                raise ContractError(f"{args.command} requires a controller receipt via --receipt")
            receipt = _load(args.receipt) if args.receipt else None
            if args.command == "admit-task":
                result = admit_task(contract, payload, repo)
            elif args.command == "verify-candidate":
                result = verify_candidate(contract, payload, repo, receipt=receipt)
            elif args.command == "import-candidate":
                result = import_candidate(contract, payload, repo, receipt=receipt)
            elif args.command == "classify":
                result = {"outcome": classify_stale_base(contract, payload, repo, receipt=receipt)}
            else:
                result = plan_integration(contract, payload, repo, receipt=receipt)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ContractError as exc:
        print(json.dumps({"error": exc.code, "message": _public_error_message(str(exc))}, sort_keys=True), file=sys.stderr)
        return 2
    except _BOUNDARY_ERRORS as exc:
        error = ContractError("repository contract input or I/O was unavailable",
                              "verification-unavailable")
        print(json.dumps({"error": error.code, "message": _public_error_message(str(error))}, sort_keys=True), file=sys.stderr)
        return 2
    except Exception:
        # A CLI boundary must never expose an implementation traceback, even when a mocked or
        # platform-specific filesystem primitive raises outside the normal typed set.
        error = ContractError("repository contract validation failed", "verification-unavailable")
        print(json.dumps({"error": error.code, "message": _public_error_message(str(error))}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
