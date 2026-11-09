"""Install, upgrade and uninstall the Linux user unit (CSL-758).

``install`` writes the unit and a grant pointer. ``upgrade`` replaces the unit
and does not open the registry. ``uninstall`` removes the unit, the socket
path and the grant pointer, and does not remove the registry.

The returned commands are for the operator to run. This module does not call
``systemctl``, does not open a socket, and does not issue an engine-user
privilege. The grant file is a pointer uninstall must clear. F21's grant is
not created here.

Spec cite: CSLR-D-011, PRODUCT.md 055cc7a5c793b7135e5d0565dc4198cf96230531
(SHA-256 e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .errors import InstallRefused
from .layout import Owned, Roots, ensure_dir, owned_paths, read_regular, replace_regular, unlink_owned, write_exclusive
from .unit import UNIT_NAME, render_unit

GRANT_SCHEMA = "recursion-daemon-install/grant-pointer/v0"
_GRANT_MODE = 0o600
_UNIT_MODE = 0o644
_DIR_MODE = 0o700


@dataclass(frozen=True)
class Plan:
    action: str
    commands: tuple[tuple[str, ...], ...]
    unit: str
    socket: str
    grant: str
    registry: str


def plan_commands(action: str) -> tuple[tuple[str, ...], ...]:
    """User-manager commands only. No ``sudo`` and no system unit."""

    if action == "install":
        commands = (
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", UNIT_NAME),
        )
    elif action == "upgrade":
        commands = (("systemctl", "--user", "daemon-reload"),)
    elif action == "uninstall":
        commands = (
            ("systemctl", "--user", "disable", "--now", UNIT_NAME),
            ("systemctl", "--user", "daemon-reload"),
        )
    else:
        raise InstallRefused("action")
    for command in commands:
        if command[0] != "systemctl" or command[1] != "--user" or "sudo" in command:
            raise InstallRefused("command")
    return commands


def install(roots: Roots, exec_args: tuple[str, ...]) -> Plan:
    """Create the unit and the grant pointer. Same bytes may be installed again.

    A unit or grant pointer that already exists with different bytes is refused
    (``unit_differs`` / ``grant_differs``). The registry directory is created
    if it is missing and is otherwise left untouched.
    """

    paths = _prepare_dirs(roots)
    unit = render_unit(exec_args).encode()
    _place(paths.unit, unit, _UNIT_MODE, differs="unit_differs")
    _place(paths.grant, _grant_bytes(), _GRANT_MODE, differs="grant_differs")
    _ensure_registry(paths)
    return _plan("install", paths)


def upgrade(roots: Roots, exec_args: tuple[str, ...]) -> Plan:
    """Replace the unit file. Registry files are not opened."""

    paths = _prepare_dirs(roots)
    if os.path.islink(paths.unit) or not os.path.isfile(paths.unit):
        raise InstallRefused("unit_missing")
    replace_regular(paths.unit, render_unit(exec_args).encode(), _UNIT_MODE)
    if not os.path.lexists(paths.grant):
        write_exclusive(paths.grant, _grant_bytes(), _GRANT_MODE)
    _ensure_registry(paths)
    return _plan("upgrade", paths)


def uninstall(roots: Roots) -> Plan:
    """Remove the unit, the socket path and the grant pointer.

    The registry directory and everything inside it stay. A non-empty grants
    directory stays after the pointer itself is removed.
    """

    paths = owned_paths(roots)
    unlink_owned(roots.home, paths.unit)
    unlink_owned(roots.runtime_dir, paths.socket)
    unlink_owned(roots.state_dir, paths.grant)
    grant_dir = os.path.dirname(paths.grant)
    if os.path.isdir(grant_dir) and not os.path.islink(grant_dir) and not os.listdir(grant_dir):
        os.rmdir(grant_dir)
    return _plan("uninstall", paths)


def leftovers(roots: Roots) -> tuple[str, ...]:
    """Owned paths that are still present: unit, socket, grant pointer."""

    paths = owned_paths(roots)
    found = []
    for path in (paths.unit, paths.socket, paths.grant):
        if os.path.lexists(path):
            found.append(path)
    return tuple(found)


def _prepare_dirs(roots: Roots) -> Owned:
    paths = owned_paths(roots)
    ensure_dir(roots.home, os.path.dirname(paths.unit), mode=_DIR_MODE)
    ensure_dir(roots.state_dir, os.path.dirname(paths.grant), mode=_DIR_MODE)
    return paths


def _ensure_registry(paths: Owned) -> None:
    ensure_dir(os.path.dirname(paths.registry), paths.registry, mode=_DIR_MODE)


def _place(path: str, data: bytes, mode: int, *, differs: str) -> None:
    if not os.path.lexists(path):
        write_exclusive(path, data, mode)
        return
    if read_regular(path) != data:
        raise InstallRefused(differs)


def _grant_bytes() -> bytes:
    payload = {
        "engine_user_grant": "absent",
        "schema": GRANT_SCHEMA,
        "status": "not_a_privilege",
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _plan(action: str, paths: Owned) -> Plan:
    return Plan(
        action=action,
        commands=plan_commands(action),
        unit=paths.unit,
        socket=paths.socket,
        grant=paths.grant,
        registry=paths.registry,
    )
