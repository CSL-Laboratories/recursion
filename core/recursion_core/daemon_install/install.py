"""Install, upgrade and uninstall the per-user daemon's systemd user unit.

``install`` writes the unit; written again with the same bytes, nothing changes. ``upgrade``
rewrites it (a new interpreter, or a hand-placed unit from before the installer). ``uninstall``
removes the unit it wrote and nothing else: not the folders around it, which other units share,
and not the daemon's state folder (the run registry and, from slice 2, the stores).

Each returns a ``Plan`` with the ``systemctl --user`` commands that apply the change. This module
runs none of them: it starts no process, opens no socket and touches no system unit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import InstallRefused
from .layout import (Owned, Roots, check_dirs, ensure_dir, file_untrusted, owned_paths, read_regular,
                     read_regular_and_stat, replace_regular, unlink_regular, write_exclusive)
from .unit import MARKER, UNIT_NAME, render_unit

_UNIT_MODE = 0o644
_DIR_MODE = 0o700


@dataclass(frozen=True)
class Plan:
    action: str
    changed: bool
    unit: str
    commands: tuple[tuple[str, ...], ...]


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


def install(roots: Roots, exec_args: tuple[str, ...]) -> Plan:
    """Write the unit. The same bytes already there change nothing; other bytes are refused, and so
    is a unit another user could change (another user's, or writable by group or world)."""

    paths = owned_paths(roots)
    unit = render_unit(exec_args).encode()
    check_dirs(paths.home, paths.unit_dir)
    if os.path.lexists(paths.unit):
        current, info = read_regular_and_stat(paths.unit)  # owner and mode of the file read, not looked up again
        _still_the_file_read(paths.unit, info)
        if current != unit:
            raise InstallRefused("unit_differs", f"{paths.unit} holds another unit; `recursiond upgrade` replaces it")
        why = file_untrusted(paths.unit, info)
        if why:
            raise InstallRefused("unit_untrusted", f"{why}; `recursiond upgrade` writes it again, ours and 0644")
        return _plan("install", False, paths)
    ensure_dir(paths.home, paths.unit_dir, mode=_DIR_MODE)
    write_exclusive(paths.unit, unit, _UNIT_MODE)
    return _plan("install", True, paths)


def upgrade(roots: Roots, exec_args: tuple[str, ...]) -> Plan:
    """Rewrite the unit to what ``install`` would write now. A missing unit is refused. The same
    bytes change nothing unless another user could change the file; then it is written again."""

    paths = owned_paths(roots)
    unit = render_unit(exec_args).encode()
    check_dirs(paths.home, paths.unit_dir)
    if not os.path.lexists(paths.unit):
        raise InstallRefused("unit_missing", f"no unit at {paths.unit}; `recursiond install` writes it")
    current, info = read_regular_and_stat(paths.unit)
    _still_the_file_read(paths.unit, info)
    if current == unit and not file_untrusted(paths.unit, info):
        return _plan("upgrade", False, paths)
    replace_regular(paths.unit, unit, _UNIT_MODE)
    return _plan("upgrade", True, paths)


def uninstall(roots: Roots) -> Plan:
    """Remove the unit this installer wrote. Refused while it is enabled, or if another wrote it."""

    paths = owned_paths(roots)
    check_dirs(paths.home, paths.unit_dir)
    if os.path.lexists(paths.enabled):
        raise InstallRefused("unit_enabled", f"run `systemctl --user disable --now {UNIT_NAME}` first")
    if os.path.lexists(paths.unit):
        try:
            first = read_regular(paths.unit).split(b"\n", 1)[0]
        except InstallRefused as refusal:
            if refusal.kind != "unit_missing":
                raise
            return _plan("uninstall", False, paths)  # removed since the lexists above: nothing is left to remove
        if first != MARKER.encode():
            raise InstallRefused("unit_not_ours", f"{paths.unit} was not written by `recursiond install`; it is left as it is")
    removed = unlink_regular(paths.unit)
    return _plan("uninstall", removed, paths)


def _still_the_file_read(path: str, info: os.stat_result) -> None:
    """Refuse unless ``path`` still names the file that was read (the same device and inode), so the
    answer is never about a file that is no longer there: removed meanwhile (a concurrent
    ``uninstall``) is ``unit_missing``, replaced is ``unit_changed``. Nothing is written either way, and
    the command run again reads what is there then."""

    try:
        now = os.lstat(path)
    except FileNotFoundError:
        raise InstallRefused("unit_missing", f"{path} was removed while it was read; run the command again") from None
    if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
        raise InstallRefused("unit_changed", f"{path} was replaced while it was read; run the command again")


def leftovers(roots: Roots) -> tuple[str, ...]:
    """The files this installer owns that are present now: the unit."""

    paths = owned_paths(roots)
    return tuple(path for path in (paths.unit,) if os.path.lexists(path))


def _plan(action: str, changed: bool, paths: Owned) -> Plan:
    return Plan(action=action, changed=changed, unit=paths.unit, commands=plan_commands(action))
