"""Install, upgrade and uninstall the per-user daemon's systemd user unit and its stores.

``install`` creates the breaker store and the revocation store where the daemon opens them, then
writes the unit; run again, it finds both stores and the same unit bytes and changes nothing.
The ledger step takes ``anchor_public_key``. This command cannot fill that parameter, so the step
reports ``needs the anchor key (Lucas)`` and does not create the ledger.
``upgrade`` does the same for an existing unit and rewrites it (a new interpreter, or a hand-placed
unit from before the installer). ``uninstall`` removes the unit it wrote and nothing else: not the
folders around it, which other units share, and not the daemon's state folder with the run
registry and the stores in it (lane INSTALL's question 3).

Each returns a ``Plan`` with the ``systemctl --user`` commands that apply the change. This module
runs none of them: it starts no process, opens no socket and touches no system unit.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from dataclasses import dataclass

from .errors import InstallRefused
from .layout import (BREAKER_FILE, LEDGER_FILE, REVOCATION_FILE, STATE_FOLDER, STORE_FOLDER, Owned, Roots,
                     check_dirs, check_private_dir, check_state_root, default_state_home, ensure_dir,
                     ensure_state_root, file_untrusted, held_regular, owned_paths, replace_regular, still_holds,
                     still_names, unlink_regular, write_exclusive)
from .unit import MARKER, UNIT_NAME, render_unit, state_elsewhere

_UNIT_MODE = 0o644
_DIR_MODE = 0o700
# The whole detail of a ledger step that has no anchor to bind. The installer does not choose one.
NEEDS_ANCHOR = "needs the anchor key (Lucas)"


@dataclass(frozen=True)
class Plan:
    action: str
    changed: bool
    unit: str
    commands: tuple[tuple[str, ...], ...]
    stores: tuple[tuple[str, str, bool], ...] = ()  # (kind, path, created now), install and upgrade
    # (path, created now, detail). Detail is ``needs the anchor key (Lucas)`` when the step had no key
    # and the ledger was not there. None on uninstall, which does not touch the ledger.
    ledger: tuple[str, bool, str] | None = None
    # install and upgrade with a chosen place: the daemon's state folder in use, and one plain sentence when
    # the default's plugin-security folder holds a store, which is left where it is and not used. "" otherwise.
    state: str = ""
    left: str = ""


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
    a umask that takes the owner's own write bit, an I/O error at the final link: the helper's
    ``<store>_invalid`` for a new store, its code kept in the detail) is ``store_not_created``. Returns
    ``(kind, path, created now)`` for each. The ledger is not one of these: ``ensure_ledger`` is the
    step that would create it, and it has no anchor key unless the caller passes one.
    """

    # Imported here, not with this module: ``recursiond --help`` builds its parser from this package.
    from recursion_core.plugin_security import Refused, stores

    if type(paths) is not Owned:
        raise InstallRefused("paths", "ensure_stores takes owned_paths(roots)")
    ensure_state_root(paths)
    ensure_dir(paths.state_root, paths.store_dir, mode=_DIR_MODE)
    done = []
    for path, magic, create in ((paths.breaker_store, stores.BREAKER_MAGIC, stores.create_breaker_store),
                                (paths.revocation_store, stores.REVOCATION_MAGIC, stores.create_revocation_store)):
        exists = os.path.lexists(path)
        try:
            if exists:
                stores.open_store(path, magic).close()
            else:
                create(path)
        except Refused as refusal:
            if not exists and refusal.code.endswith("_invalid"):
                # The helper's refusal of a new store it could not build or put in place (an I/O error at
                # its final link included): not a store found wrong, a store not made.
                raise InstallRefused("store_not_created", f"{path}: {refusal.code}: {refusal.detail}") from None
            raise InstallRefused(refusal.code, f"{path}: {refusal.detail}") from None
        except (OSError, sqlite3.Error) as exc:
            raise InstallRefused("store_not_created", f"{path}: {type(exc).__name__}: {exc}") from None
        done.append((stores.KINDS[magic], path, not exists))
    return tuple(done)


def ensure_ledger(paths: Owned, anchor_public_key: str | None) -> tuple[str, bool, str]:
    """The ledger step. Returns ``(path, created now, detail)``.

    ``anchor_public_key`` is the key ``authority.create_ledger`` binds the new ledger to.
    ``install`` and ``upgrade`` pass None: nothing in this command has that key, and
    ``install.json``'s ``authority_anchor`` is not read. None, or an empty string, reports
    ``needs the anchor key (Lucas)`` when the ledger is not there and creates nothing. A
    caller that has the key creates a missing ledger through ``plugin_security.stores.create_ledger``
    (that calls ``authority.create_ledger``). A ledger already there is checked with
    ``open_store`` and left as it is, whether or not a key was passed.
    """

    from recursion_core.plugin_security import Refused, stores

    if type(paths) is not Owned:
        raise InstallRefused("paths", "ensure_ledger takes owned_paths(roots)")
    if anchor_public_key is not None and type(anchor_public_key) is not str:
        raise InstallRefused("anchor_public_key", "the anchor is the installer's public key, or absent")
    ensure_state_root(paths)
    ensure_dir(paths.state_root, paths.store_dir, mode=_DIR_MODE)
    path = paths.ledger
    exists = os.path.lexists(path)
    if not anchor_public_key:
        if not exists:
            return (path, False, NEEDS_ANCHOR)
        _open_ledger(path)
        return (path, False, "")
    if exists:
        _open_ledger(path)
        return (path, False, "")
    try:
        stores.create_ledger(path, anchor_public_key)
    except Refused as refusal:
        if not exists and refusal.code.endswith("_invalid"):
            raise InstallRefused("store_not_created", f"{path}: {refusal.code}: {refusal.detail}") from None
        raise InstallRefused(refusal.code, f"{path}: {refusal.detail}") from None
    except (OSError, sqlite3.Error) as exc:
        raise InstallRefused("store_not_created", f"{path}: {type(exc).__name__}: {exc}") from None
    return (path, True, "")


def _open_ledger(path: str) -> None:
    from recursion_core.plugin_security import Refused, stores

    try:
        stores.open_store(path, stores.LEDGER_MAGIC).close()
    except Refused as refusal:
        raise InstallRefused(refusal.code, f"{path}: {refusal.detail}") from None
    except (OSError, sqlite3.Error) as exc:
        raise InstallRefused("store_not_created", f"{path}: {type(exc).__name__}: {exc}") from None


def install(roots: Roots, exec_args: tuple[str, ...], *, anchor_public_key: str | None = None) -> Plan:
    """Create the stores and the route homes' custody root, then write the unit. The same bytes
    already there change nothing; other bytes, a unit another user could change (another user's, or
    writable by group or world) and a custody root open to anyone else or unusable by its owner are
    refused before anything is made; the root as made (or as another caller made it meanwhile) is held
    to the same rule before the unit is written. A unit found is held open until the answer and checked against
    its bytes and then its path last (``still_holds``, then ``still_names``): one removed, replaced or rewritten
    in place meanwhile is ``unit_missing`` or ``unit_changed``. A refusal
    after the stores are begun (that, or a store refused after the other was made) leaves what was
    made; each store made is a whole, valid store, and the command run again finds it. ``anchor_public_key``
    is passed to ``ensure_ledger``; the default None is the command, which has no key. A platform
    with no ``os.geteuid`` is refused ``platform_unsupported`` (one line naming the platform) before
    any path is read or written."""

    paths = owned_paths(roots)
    unit = render_unit(exec_args, paths.state_home if paths.chosen else None).encode()
    check_dirs(paths.home, paths.unit_dir)
    with contextlib.ExitStack() as held:  # a unit found stays open until the answer: its inode is not reused
        present = os.path.lexists(paths.unit)
        if present:
            read = held.enter_context(held_regular(paths.unit))  # checked against the path while open
            current, info = read
            if current != unit:
                raise InstallRefused("unit_differs", f"{paths.unit} holds another unit; `recursiond upgrade` replaces it")
            why = file_untrusted(paths.unit, info)
            if why:
                raise InstallRefused("unit_untrusted", f"{why}; `recursiond upgrade` writes it again, ours and 0644")
        check_state_root(paths)  # a chosen place: before anything is made
        check_private_dir(paths.state_root, paths.homes)  # before anything is made
        made = ensure_stores(paths)  # first: a unit is never left naming a daemon whose stores are missing
        ledger = ensure_ledger(paths, anchor_public_key)
        homes = ensure_dir(paths.state_root, paths.homes, mode=_DIR_MODE)
        check_private_dir(paths.state_root, paths.homes, required=True)  # as made, or as another caller made it
        if present:
            still_holds(paths.unit, read)  # the bytes read, first: an in-place rewrite shows here
            still_names(paths.unit, info)  # last: the path still names the file that was read
        else:
            ensure_dir(paths.home, paths.unit_dir, mode=_DIR_MODE)
            write_exclusive(paths.unit, unit, _UNIT_MODE)
        return _plan("install", not present or _any_created(made) or homes or ledger[1], paths, made, ledger)


def upgrade(roots: Roots, exec_args: tuple[str, ...], *, anchor_public_key: str | None = None) -> Plan:
    """Make any missing store and the route homes' custody root, and rewrite the unit to what
    ``install`` would write now. A missing unit, and a custody root open to anyone else or unusable by
    its owner, are refused before anything is made; the root as made is held to the same rule before
    the unit is rewritten. A unit whose daemon may keep its state in another folder (``state_elsewhere``:
    its own ``XDG_STATE_HOME``, an ``EnvironmentFile``, the manager's value passed through) is refused
    ``unit_state_elsewhere`` before anything is made: replacing it would point the daemon at another
    state folder, without the revocations and breaker trips kept in its own. The same bytes are not
    written again unless
    another user could change the file; a unit kept is held open until the answer and checked
    against its bytes and then its path last, as in ``install``, whose words on a refusal after the stores
    apply. A platform with no ``os.geteuid`` is refused
    ``platform_unsupported`` (one line naming the platform) before any path is read or written."""

    paths = owned_paths(roots)
    unit = render_unit(exec_args, paths.state_home if paths.chosen else None).encode()
    check_dirs(paths.home, paths.unit_dir)
    if not os.path.lexists(paths.unit):
        raise InstallRefused("unit_missing", f"no unit at {paths.unit}; `recursiond install` writes it")
    with held_regular(paths.unit) as read:  # held open until the answer: its inode is not reused
        data, info = read
        current = data == unit and not file_untrusted(paths.unit, info)
        why = None if data == unit else state_elsewhere(data, paths.home, paths.state_home)
        if why:  # the S6 sweep's F1 on #410: before anything is made, the unit left as it is
            raise InstallRefused(
                "unit_state_elsewhere",
                f"{paths.unit} {why}: the daemon it starts may keep its state (the revocation and breaker "
                f"stores, the run registry) there, and the unit `recursiond upgrade` writes puts it in "
                f"{paths.state}. Stop the daemon, move that state folder to {paths.state}, take the line out "
                "of the unit, then run `recursiond upgrade` again")
        check_state_root(paths)  # a chosen place: before anything is made
        check_private_dir(paths.state_root, paths.homes)  # before anything is made
        made = ensure_stores(paths)
        ledger = ensure_ledger(paths, anchor_public_key)
        homes = ensure_dir(paths.state_root, paths.homes, mode=_DIR_MODE)
        check_private_dir(paths.state_root, paths.homes, required=True)  # as made, or as another caller made it
        if current:
            still_holds(paths.unit, read)  # the bytes read, first: an in-place rewrite shows here
            still_names(paths.unit, info)  # last: the path still names the file that was read
        else:
            replace_regular(paths.unit, unit, _UNIT_MODE)
        return _plan("upgrade", not current or _any_created(made) or homes or ledger[1], paths, made, ledger)


def uninstall(roots: Roots) -> Plan:
    """Remove the unit this installer wrote. Refused while it is enabled, or if another wrote it. The unit
    is removed while the file read is still held open, after the path was checked to name it."""

    paths = owned_paths(roots)
    check_dirs(paths.home, paths.unit_dir)
    if os.path.lexists(paths.enabled):
        raise InstallRefused("unit_enabled", f"run `systemctl --user disable --now {UNIT_NAME}` first")
    if not os.path.lexists(paths.unit):
        return _plan("uninstall", False, paths)
    try:
        with held_regular(paths.unit) as (data, _):  # checked against the path while held; removed while held
            if data.split(b"\n", 1)[0] != MARKER.encode():
                raise InstallRefused("unit_not_ours", f"{paths.unit} was not written by `recursiond install`; it is left as it is")
            return _plan("uninstall", unlink_regular(paths.unit), paths)
    except InstallRefused as refusal:
        if refusal.kind != "unit_missing":
            raise
        return _plan("uninstall", False, paths)  # removed since the lexists above: nothing is left to remove


def leftovers(roots: Roots) -> tuple[str, ...]:
    """The files ``uninstall`` removes that are present now: the unit. The stores stay (question 3)."""

    paths = owned_paths(roots)
    return tuple(path for path in (paths.unit,) if os.path.lexists(path))


def _any_created(made: tuple[tuple[str, str, bool], ...]) -> bool:
    return any(created for _, _, created in made)


def _plan(action: str, changed: bool, paths: Owned, stores: tuple[tuple[str, str, bool], ...] = (),
          ledger: tuple[str, bool, str] | None = None) -> Plan:
    chosen = paths.chosen and action != "uninstall"
    return Plan(action=action, changed=changed, unit=paths.unit, commands=plan_commands(action),
                stores=stores, ledger=ledger, state=paths.state if chosen else "",
                left=_default_stores_left(paths) if chosen else "")


def _default_stores_left(paths: Owned) -> str:
    """One plain sentence when the default's plugin-security folder holds a store while the person chose
    another place; "" otherwise. Nothing there is read, moved or removed: only whether a name is present."""

    folder = os.path.join(default_state_home(paths.home), STATE_FOLDER, STORE_FOLDER)
    found = [name for name in (BREAKER_FILE, REVOCATION_FILE, LEDGER_FILE) if os.path.lexists(os.path.join(folder, name))]
    if not found:
        return ""
    return (f"{folder} holds {', '.join(found)} from the default state folder; they are left where they are "
            f"and not used, since XDG_STATE_HOME chose {paths.state_home}")
