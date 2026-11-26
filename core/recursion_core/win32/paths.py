"""State directory under ``%LOCALAPPDATA%\\Recursion``.

The directory is created with a protected DACL that grants the current user SID
only. An existing directory is kept only when its owner SID is that user; a
different owner is ``owner_mismatch`` and is not rewritten. Any reparse point
on the chain (a junction or any other link) is refused before a create or a
security write, so the call does not follow the link. Decisions live here. The
Windows calls live in ``_api`` and are loaded only for a real Windows backend.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

STATE_NAME = "Recursion"
_SID = re.compile(r"^S-\d+(-\d+)+$", re.IGNORECASE)
_ACE = re.compile(r"\(([^)]*)\)")
_BANNED_ACCOUNTS = frozenset({"WD", "AU", "S-1-1-0", "S-1-5-11"})


class Win32Refused(Exception):
    """A typed refusal. ``reason`` is a stable code, not a path and not a SID value beyond the code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class SharingViolation(OSError):
    """An exclusive, share-mode-0 open found the file already held."""


@dataclass(frozen=True)
class Probe:
    """What a path is, without following a reparse point.

    ``owner`` and ``dacl`` are ``None`` when the caller did not read security
    (a reparse point, or a stat that only needed the attributes).
    """

    is_dir: bool
    is_reparse: bool
    owner: str | None
    dacl: str | None


def protected_sddl(sid: str) -> str:
    """Owner and primary group are the user. The DACL is protected and has one allow ACE.

    ``P`` stops inheritance. The ACE carries no inherit flags and no inherited
    flag, so a parent ACE cannot remain and this ACE is not itself inherited.
    The account is the SID text only, which blocks an SDDL breakout.
    """

    if _SID.fullmatch(sid) is None:
        raise Win32Refused("sid_refused")
    return f"O:{sid}G:{sid}D:P(A;;FA;;;{sid})"


def normalize_windows_path(path: str) -> str:
    """Lexical absolute Windows path. Does not touch the filesystem and does not follow a link.

    ``..``, a relative path, a NUL, a trailing dot or space, and an alternate
    data stream are refused. ``.`` components are dropped. The drive letter is
    uppercased. A drive root stays ``C:\\``.
    """

    if not isinstance(path, str) or path == "" or "\0" in path:
        raise Win32Refused("path_refused")
    extended = False
    body = path.replace("/", "\\")
    if body.startswith("\\\\?\\"):
        extended = True
        body = body[4:]
        if body.upper().startswith("UNC\\"):
            raise Win32Refused("path_refused")
    if body.startswith("\\\\"):
        raise Win32Refused("path_refused")
    if len(body) < 2 or body[1] != ":":
        raise Win32Refused("path_refused")
    drive = body[0]
    if drive not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz":
        raise Win32Refused("path_refused")
    rest = body[2:]
    if rest.startswith("\\"):
        rest = rest[1:]
    elif rest != "":
        raise Win32Refused("path_refused")
    parts: list[str] = []
    for part in rest.split("\\"):
        if part == "":
            continue
        if part == ".":
            continue
        if part == "..":
            raise Win32Refused("path_refused")
        if part.endswith(" ") or part.endswith("."):
            raise Win32Refused("path_refused")
        if any(ch in part for ch in '<>:"|?*'):
            raise Win32Refused("path_refused")
        parts.append(part)
    built = drive.upper() + ":\\" + "\\".join(parts) if parts else drive.upper() + ":\\"
    if extended:
        return "\\\\?\\" + built
    return built


def chain(path: str) -> list[str]:
    """Prefixes from the drive root through ``path``. ``path`` is already normalized."""

    extended = path.startswith("\\\\?\\")
    body = path[4:] if extended else path
    drive_root = body[:3]
    prefix = "\\\\?\\" + drive_root if extended else drive_root
    out = [prefix]
    rest = body[3:]
    if not rest:
        return out
    acc = prefix
    for part in rest.split("\\"):
        acc = acc + part if acc.endswith("\\") else acc + "\\" + part
        out.append(acc)
    return out


def _join(parent: str, name: str) -> str:
    if parent.endswith("\\"):
        return parent + name
    return parent + "\\" + name


def _sid_key(value: str | None) -> str:
    return (value or "").upper()


def _dacl_section(sddl: str) -> tuple[str, str] | None:
    start = sddl.find("D:")
    if start < 0:
        return None
    rest = sddl[start + 2:]
    flags: list[str] = []
    index = 0
    while index < len(rest) and rest[index] != "(":
        flags.append(rest[index])
        index += 1
    body = rest[index:]
    sacl = body.find("S:")
    if sacl >= 0:
        body = body[:sacl]
    return "".join(flags), body


def _protected(flags: str) -> bool:
    leftover = flags.replace("AI", "").replace("AR", "")
    return leftover == "P"


def _flag_codes(flags: str) -> set[str]:
    if len(flags) % 2:
        return {"BAD"}
    return {flags[index:index + 2] for index in range(0, len(flags), 2)}


def user_only_dacl(sddl: str | None, sid: str) -> bool:
    """True when the DACL is protected and its only ACE is an allow for ``sid`` that is not inherited."""

    if not sddl or _SID.fullmatch(sid) is None:
        return False
    section = _dacl_section(sddl)
    if section is None or not _protected(section[0]):
        return False
    aces = _ACE.findall(section[1])
    if len(aces) != 1:
        return False
    parts = aces[0].split(";")
    if len(parts) != 6:
        return False
    ace_type, ace_flags, _rights, _object, _inherit, account = parts
    if ace_type != "A":
        return False
    codes = _flag_codes(ace_flags)
    if "ID" in codes or "BAD" in codes:
        return False
    if _sid_key(account) in _BANNED_ACCOUNTS:
        return False
    return _sid_key(account) == _sid_key(sid)


def backend(api: object | None = None) -> object:
    """The caller-supplied fake, or the real Windows backend.

    On POSIX this refuses before importing ``_api``, so no Windows DLL is loaded.
    """

    if api is not None:
        return api
    if os.name != "nt":
        raise Win32Refused("platform_unsupported")
    from recursion_core.win32 import _api

    return _api.Api()


def ensure_state_dir(api: object | None = None) -> str:
    """Return the user-only state directory, creating it when it is missing.

    Refuses ``localappdata_missing`` when an ancestor of the state directory is
    absent, ``reparse_point`` when any existing component is a reparse point,
    ``owner_mismatch`` when the owner SID is not the current user (and does not
    rewrite that directory), and ``dacl_refused`` when a directory we own still
    does not read back as a protected user-only DACL after the write.
    """

    real = backend(api)
    sid = real.current_user_sid()
    sddl = protected_sddl(sid)
    raw_root = real.local_appdata()
    if not isinstance(raw_root, str) or raw_root == "":
        raise Win32Refused("localappdata_missing")
    root = normalize_windows_path(raw_root)
    state = _join(root, STATE_NAME)
    for prefix in chain(state):
        info = real.stat(prefix)
        if info is None:
            if prefix != state:
                raise Win32Refused("localappdata_missing")
            break
        if info.is_reparse:
            raise Win32Refused("reparse_point")
        if prefix != state and not info.is_dir:
            raise Win32Refused("path_refused")
    info = real.stat(state)
    if info is None:
        try:
            real.create_directory(state, sddl)
        except FileExistsError:
            pass
        info = real.stat(state)
        if info is None:
            raise Win32Refused("path_refused")
    if info.is_reparse:
        raise Win32Refused("reparse_point")
    if not info.is_dir:
        raise Win32Refused("path_refused")
    sec = real.security(state)
    if sec.is_reparse:
        raise Win32Refused("reparse_point")
    if _sid_key(sec.owner) != _sid_key(sid):
        raise Win32Refused("owner_mismatch")
    if user_only_dacl(sec.dacl, sid):
        return state
    real.apply_security(state, sddl)
    sec = real.security(state)
    if sec.is_reparse:
        raise Win32Refused("reparse_point")
    if _sid_key(sec.owner) != _sid_key(sid):
        raise Win32Refused("owner_mismatch")
    if not user_only_dacl(sec.dacl, sid):
        raise Win32Refused("dacl_refused")
    return state
