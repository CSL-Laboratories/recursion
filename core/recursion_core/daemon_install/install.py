"""Install, upgrade and uninstall the per-user daemon's systemd user unit and its stores.

``install`` creates the breaker store and the revocation store where the daemon opens them, then
writes the unit; run again, it finds both stores and the same unit bytes and changes nothing.
``upgrade`` does the same for an existing unit and rewrites it (a new interpreter, or a hand-placed
unit from before the installer). ``uninstall`` removes the unit it wrote and nothing else: not the
folders around it, which other units share, and not the daemon's state folder with the run
registry and the stores in it (lane INSTALL's question 3).

Each returns a ``Plan`` with the ``systemctl --user`` commands that apply the change. This module
runs none of them: it starts no process, opens no socket and touches no system unit.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from .errors import InstallRefused
from .layout import Owned, Roots, check_dirs, ensure_dir, owned_paths, read_regular, replace_regular, unlink_regular, write_exclusive
from .unit import MARKER, UNIT_NAME, render_unit

_UNIT_MODE = 0o644
_DIR_MODE = 0o700


@dataclass(frozen=True)
class Plan:
    action: str
    changed: bool
    unit: str
    commands: tuple[tuple[str, ...], ...]
    stores: tuple[tuple[str, str, bool], ...] = ()  # (kind, path, created now), install and upgrade


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


def ensure_stores(paths: Owned) -> tuple[tuple[str, str, bool], ...]:
    """Create the breaker store and the revocation store where the daemon opens them, or check them.

    A missing store is made by ``plugin_security.stores`` (built under a temporary name, linked into
    place exclusively, then checked with ``open_store``). A store already there must pass
    ``open_store`` and is left as it is. A refusal keeps the store's own code
    (``breaker_store_untrusted``, ``revocation_store_invalid``, ``breaker_store_exists`` for a
    leftover ``-wal`` beside a missing store, ...); a store that cannot be made at all (a full disk,
    a umask that takes the owner's own write bit) is ``store_not_created``. Returns
    ``(kind, path, created now)`` for each. The ledger is not made here: it is bound to the
    installer's anchor key, which this lane does not have yet (its open question for Lucas).
    """

    # Imported here, not with this module: ``recursiond --help`` builds its parser from this package.
    from recursion_core.plugin_security import Refused, stores

    ensure_dir(paths.home, paths.store_dir, mode=_DIR_MODE)
    done = []
    for path, magic, create in ((paths.breaker_store, stores.BREAKER_MAGIC, stores.create_breaker_store),
                                (paths.revocation_store, stores.REVOCATION_MAGIC, stores.create_revocation_store)):
        try:
            if os.path.lexists(path):
                stores.open_store(path, magic).close()
                done.append((stores.KINDS[magic], path, False))
                continue
            create(path)
        except Refused as refusal:
            raise InstallRefused(refusal.code, f"{path}: {refusal.detail}") from None
        except (OSError, sqlite3.Error) as exc:
            raise InstallRefused("store_not_created", f"{path}: {type(exc).__name__}: {exc}") from None
        done.append((stores.KINDS[magic], path, True))
    return tuple(done)


def install(roots: Roots, exec_args: tuple[str, ...]) -> Plan:
    """Create the stores, then write the unit. The same bytes already there change nothing; other
    bytes are refused before any store is made, so a refused install leaves everything as it was."""

    paths = owned_paths(roots)
    unit = render_unit(exec_args).encode()
    check_dirs(paths.home, paths.unit_dir)
    present = os.path.lexists(paths.unit)
    if present and read_regular(paths.unit) != unit:
        raise InstallRefused("unit_differs", f"{paths.unit} holds another unit; `recursiond upgrade` replaces it")
    made = ensure_stores(paths)  # first: a unit is never left naming a daemon whose stores are missing
    if not present:
        ensure_dir(paths.home, paths.unit_dir, mode=_DIR_MODE)
        write_exclusive(paths.unit, unit, _UNIT_MODE)
    return _plan("install", not present or _any_created(made), paths, made)


def upgrade(roots: Roots, exec_args: tuple[str, ...]) -> Plan:
    """Make any missing store and rewrite the unit to what ``install`` would write now. A missing
    unit is refused before anything is made."""

    paths = owned_paths(roots)
    unit = render_unit(exec_args).encode()
    check_dirs(paths.home, paths.unit_dir)
    if not os.path.lexists(paths.unit):
        raise InstallRefused("unit_missing", f"no unit at {paths.unit}; `recursiond install` writes it")
    current = read_regular(paths.unit) == unit
    made = ensure_stores(paths)
    if not current:
        replace_regular(paths.unit, unit, _UNIT_MODE)
    return _plan("upgrade", not current or _any_created(made), paths, made)


def uninstall(roots: Roots) -> Plan:
    """Remove the unit this installer wrote. Refused while it is enabled, or if another wrote it."""

    paths = owned_paths(roots)
    check_dirs(paths.home, paths.unit_dir)
    if os.path.lexists(paths.enabled):
        raise InstallRefused("unit_enabled", f"run `systemctl --user disable --now {UNIT_NAME}` first")
    if os.path.lexists(paths.unit):
        first = read_regular(paths.unit).split(b"\n", 1)[0]
        if first != MARKER.encode():
            raise InstallRefused("unit_not_ours", f"{paths.unit} was not written by `recursiond install`; it is left as it is")
    removed = unlink_regular(paths.unit)
    return _plan("uninstall", removed, paths)


def leftovers(roots: Roots) -> tuple[str, ...]:
    """The files ``uninstall`` removes that are present now: the unit. The stores stay (question 3)."""

    paths = owned_paths(roots)
    return tuple(path for path in (paths.unit,) if os.path.lexists(path))


def _any_created(made: tuple[tuple[str, str, bool], ...]) -> bool:
    return any(created for _, _, created in made)


def _plan(action: str, changed: bool, paths: Owned, stores: tuple[tuple[str, str, bool], ...] = ()) -> Plan:
    return Plan(action=action, changed=changed, unit=paths.unit, commands=plan_commands(action), stores=stores)
