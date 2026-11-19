"""Install, upgrade and uninstall the per-user daemon's systemd user unit and its stores.

``install`` creates the breaker store and the revocation store where the daemon opens them, then
writes the unit; run again, it finds both stores and the same unit bytes and changes nothing.
The ledger step binds the authority ledger to an anchor key. ``recursiond install`` (``make_anchor``)
makes the install's own Ed25519 key, owner-only under ``~/.config/recursion/authority/``, and binds the
new ledger to its public half; run again with key and ledger agreeing, it writes nothing, and a key and a
ledger that disagree are refused, typed, before anything is written. A caller that passes neither a key
nor ``make_anchor`` gets ``needs the anchor key, which recursiond install makes`` and no ledger.
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
import stat
from dataclasses import dataclass

from .errors import InstallRefused
from .layout import (BREAKER_FILE, LEDGER_FILE, REVOCATION_FILE, STATE_FOLDER, STORE_FOLDER, Owned, Roots,
                     check_dirs, check_private_dir, check_state_root, default_state_home, ensure_dir,
                     ensure_state_root, file_untrusted, held_regular, owned_paths, read_regular_and_stat,
                     replace_regular, still_holds, still_names, unlink_regular, write_exclusive)
from .unit import MARKER, UNIT_NAME, render_unit, state_elsewhere

_UNIT_MODE = 0o644
_DIR_MODE = 0o700
_KEY_MODE = 0o600
# The whole detail of a ledger step that has no anchor to bind. The installer does not choose one.
NEEDS_ANCHOR = "needs the anchor key, which recursiond install makes"
# The whole detail of the ledger step of ``make_anchor`` where this interpreter cannot import
# ``cryptography`` (the optional ``plugins`` extra): no key can be made, so no ledger is made either.
NEEDS_BACKEND = "needs the cryptography package to make this install's authority key (csl-recursion[plugins])"
# Key ids (``signing.key_id``) on CSL's published list: such a key is refused as an anchor
# (``authority.py``: a profile is signed on the user's machine, never by CSL). None is published yet.
CSL_KEY_IDS: frozenset = frozenset()
# The one line ``recursiond install`` prints when it made the key.
ANCHOR_MADE = ("Made this install's own authority key (owner-only, {path}); it signs your order permissions on "
               "this machine and never leaves it. Losing it means making new permissions; it is not recoverable.")


@dataclass(frozen=True)
class Plan:
    action: str
    changed: bool
    unit: str
    commands: tuple[tuple[str, ...], ...]
    stores: tuple[tuple[str, str, bool], ...] = ()  # (kind, path, created now), install and upgrade
    # (path, created now, detail). Detail is ``needs the anchor key, which recursiond install makes`` when the step had no
    # key and the ledger was not there. None on uninstall, which does not touch the ledger.
    ledger: tuple[str, bool, str] | None = None
    # install and upgrade with a chosen place: the daemon's state folder in use, and one plain sentence when
    # the default's plugin-security folder holds a store, which is left where it is and not used. "" otherwise.
    state: str = ""
    left: str = ""
    # make_anchor: (the key's path, made now). None when no key was asked for, or none can be made here.
    anchor: tuple[str, bool] | None = None


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

    ``anchor_public_key`` is the key ``authority.create_ledger`` binds the new ledger to:
    ``install`` and ``upgrade`` pass the install's own key with ``make_anchor`` (``find_anchor``),
    or the key their caller passed. None, or an empty string, reports
    ``needs the anchor key, which recursiond install makes`` when the ledger is not there and creates nothing. A
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


@dataclass(frozen=True)
class _Found:
    """What ``find_anchor`` read: the backend's classes, and the key's public half (None: no key yet)."""

    backend: object
    public: str | None


def find_anchor(paths: Owned) -> _Found | None:
    """Read the install's own key, the ledger's binding and ``install.json``'s ``authority_anchor``, and
    refuse unless they agree. Reads only; called before anything is made. None where this interpreter
    cannot import ``cryptography``: then nothing is read here and no key can be made.

    Agreeing: no key, no ledger and no ``authority_anchor`` (a fresh profile: ``make_anchor_key`` makes
    the key), or a key whose public half the ledger is bound to, with ``authority_anchor`` null or that
    same key. Any other state is ``authority_anchor_disagrees``, its detail naming which part: a key
    without a ledger, a ledger without a key, a ledger bound to another key, an ``authority_anchor``
    without a key or naming another key. A new key is never made over a ledger: it would orphan the
    ledger's spend history. A key or an ``authority_anchor`` on CSL's published list is
    ``anchor_is_csl_key``. The key file must be a regular file (a link is refused, ``not_regular``, never
    followed), the person's and open to nobody else (``anchor_key_untrusted``), in a folder open to
    nobody else (``folder_untrusted``), holding an unencrypted Ed25519 PKCS#8 PEM key
    (``anchor_key_invalid``). No detail names the key's bytes."""

    from recursion_core.plugin_security.signing import key_id

    if type(paths) is not Owned:
        raise InstallRefused("paths", "find_anchor takes owned_paths(roots)")
    backend = _ed25519()
    if backend is None:
        return None
    configured = _configured_anchor(paths)
    if configured is not None and _key_id(configured, "install.json's authority_anchor") in CSL_KEY_IDS:
        raise InstallRefused("anchor_is_csl_key", f"install.json's authority_anchor ({configured}) is on CSL's "
                             "published list; an install's anchor is its own key")
    ledger_there = os.path.lexists(paths.ledger)
    check_dirs(paths.home, paths.authority_dir)  # a link, or a folder another user could change, on the way
    if os.path.lexists(paths.authority_dir):
        _check_key_dir(paths.authority_dir)
    if not os.path.lexists(paths.anchor_key):
        if ledger_there:
            raise InstallRefused("authority_anchor_disagrees", f"{paths.ledger} is there and no key is at "
                                 f"{paths.anchor_key}: a new key would orphan the ledger's spend history, so none "
                                 "is made; nothing was written")
        if configured is not None:
            raise InstallRefused("authority_anchor_disagrees", f"install.json's authority_anchor is {configured} "
                                 f"and no key is at {paths.anchor_key}; nothing was written")
        return _Found(backend, None)
    public = _read_anchor_key(paths.anchor_key, backend)
    if key_id(public) in CSL_KEY_IDS:
        raise InstallRefused("anchor_is_csl_key", f"the key at {paths.anchor_key} is on CSL's published list; "
                             "an install's anchor is its own key")
    if not ledger_there:
        raise InstallRefused("authority_anchor_disagrees", f"the key at {paths.anchor_key} is there and no ledger "
                             f"is at {paths.ledger}; nothing was written")
    bound = _ledger_anchor(paths.ledger)
    if bound != key_id(public):
        raise InstallRefused("authority_anchor_disagrees", f"{paths.ledger} is bound to another key ({bound}) "
                             f"than the one at {paths.anchor_key} ({key_id(public)}); nothing was written")
    if configured is not None and configured != public:
        raise InstallRefused("authority_anchor_disagrees", f"install.json's authority_anchor is {configured}, not "
                             f"the public half of the key at {paths.anchor_key} ({public}); nothing was written")
    return _Found(backend, public)


def make_anchor_key(paths: Owned, found: _Found) -> str:
    """Make the install's own Ed25519 key pair and write its private half, owner-only, to
    ``paths.anchor_key`` (folder 0700 under the home's checks, file 0600, a fresh temporary name made with
    ``O_EXCL`` and ``O_NOFOLLOW``, then linked into place exclusively: anything there meanwhile is
    ``already_present``). PKCS#8 PEM, unencrypted (the same-uid limit is stated in ``authority.py``, not
    solved). Returns the public half, 64 lowercase hex. Never printed, logged or answered; no copy is made."""

    if found.public is not None:
        raise InstallRefused("anchor_key", "the key is there already")
    private_class, serialization = found.backend
    ensure_dir(paths.home, paths.authority_dir, mode=_DIR_MODE)
    _check_key_dir(paths.authority_dir)
    key = private_class.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    write_exclusive(paths.anchor_key, pem, _KEY_MODE)
    return public


def _ed25519():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        return None
    return (Ed25519PrivateKey, serialization)


def _key_id(value: object, where: str) -> str:
    from recursion_core.plugin_security import Refused
    from recursion_core.plugin_security.signing import key_id

    try:
        return key_id(value)
    except Refused:
        raise InstallRefused("authority_anchor_disagrees", f"{where} is not a public key (64 lowercase hex)") from None


def _configured_anchor(paths: Owned) -> str | None:
    """``install.json``'s ``authority_anchor`` through the daemon's own reader (one parser); None when the file
    or the value is absent. Read only: this step never writes ``install.json``."""

    from recursion_core.daemon.plugins import PLUGINS_DIR, PluginRefused, read_install

    try:
        return read_install(os.path.join(paths.config, PLUGINS_DIR))["authority_anchor"]
    except PluginRefused as refusal:
        raise InstallRefused(refusal.code, f"install.json: {refusal.detail}") from None


def _check_key_dir(path: str) -> None:
    info = os.lstat(path)
    if info.st_mode & 0o077 or (info.st_mode & stat.S_IRWXU) != stat.S_IRWXU:
        raise InstallRefused("folder_untrusted", f"{path} holds the install's authority key and must be open to "
                             "nobody else and usable by its owner: `chmod 700` it")


def _read_anchor_key(path: str, backend) -> str:
    private_class, serialization = backend
    data, info = read_regular_and_stat(path)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise InstallRefused("anchor_key_untrusted", f"{path} must be the person's and open to nobody else: "
                             "`chmod 600` it")
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except Exception:  # a malformed, encrypted or unsupported key; its bytes are never named
        key = None
    if not isinstance(key, private_class):
        raise InstallRefused("anchor_key_invalid", f"{path} does not hold an unencrypted Ed25519 PKCS#8 PEM key")
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def _ledger_anchor(path: str) -> str | None:
    from recursion_core.plugin_security import Refused, stores

    try:
        db = stores.open_store(path, stores.LEDGER_MAGIC)
    except Refused as refusal:
        raise InstallRefused(refusal.code, f"{path}: {refusal.detail}") from None
    except (OSError, sqlite3.Error) as exc:
        raise InstallRefused("store_not_created", f"{path}: {type(exc).__name__}: {exc}") from None
    try:
        row = db.execute("SELECT v FROM meta WHERE k='anchor'").fetchone()
    except sqlite3.Error:
        raise InstallRefused("ledger_invalid", f"{path}: not a readable ledger") from None
    finally:
        db.close()
    return None if row is None else row[0]


def _found(paths: Owned, anchor_public_key: str | None, make_anchor: bool) -> _Found | None:
    if not make_anchor:
        return None
    if anchor_public_key is not None:
        raise InstallRefused("anchor_public_key", "pass the anchor key or make_anchor, not both")
    return find_anchor(paths)


def _anchor_and_ledger(paths: Owned, anchor_public_key: str | None, found: _Found | None,
                       make_anchor: bool) -> tuple[tuple[str, bool, str], tuple[str, bool] | None]:
    """The ledger step, after the stores. With ``make_anchor``: a fresh profile gets its key, then the
    ledger bound to it; a key made here is removed again if the ledger cannot be made, so a refusal leaves
    no key without a ledger."""

    if not make_anchor:
        return ensure_ledger(paths, anchor_public_key), None
    if found is None:  # no cryptography here: no key, so no ledger
        ledger = ensure_ledger(paths, None)
        return (ledger[0], ledger[1], NEEDS_BACKEND if ledger[2] == NEEDS_ANCHOR else ledger[2]), None
    if found.public is not None:
        return ensure_ledger(paths, found.public), (paths.anchor_key, False)
    public = make_anchor_key(paths, found)
    try:
        ledger = ensure_ledger(paths, public)
    except BaseException:
        unlink_regular(paths.anchor_key)
        raise
    return ledger, (paths.anchor_key, True)


def install(roots: Roots, exec_args: tuple[str, ...], *, anchor_public_key: str | None = None,
            make_anchor: bool = False) -> Plan:
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
    any path is read or written. With ``make_anchor`` (``recursiond install``) the ledger is bound to the
    install's own key, made on a fresh profile (``find_anchor``, ``make_anchor_key``); key, ledger and
    ``install.json``'s ``authority_anchor`` that disagree are refused before anything is made."""

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
        found = _found(paths, anchor_public_key, make_anchor)  # before anything is made
        made = ensure_stores(paths)  # first: a unit is never left naming a daemon whose stores are missing
        ledger, anchor = _anchor_and_ledger(paths, anchor_public_key, found, make_anchor)
        homes = ensure_dir(paths.state_root, paths.homes, mode=_DIR_MODE)
        check_private_dir(paths.state_root, paths.homes, required=True)  # as made, or as another caller made it
        if present:
            still_holds(paths.unit, read)  # the bytes read, first: an in-place rewrite shows here
            still_names(paths.unit, info)  # last: the path still names the file that was read
        else:
            ensure_dir(paths.home, paths.unit_dir, mode=_DIR_MODE)
            write_exclusive(paths.unit, unit, _UNIT_MODE)
        return _plan("install", not present or _any_created(made) or homes or ledger[1], paths, made, ledger, anchor)


def upgrade(roots: Roots, exec_args: tuple[str, ...], *, anchor_public_key: str | None = None,
            make_anchor: bool = False) -> Plan:
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
    ``platform_unsupported`` (one line naming the platform) before any path is read or written.
    ``make_anchor`` as in ``install``: a profile installed before the key existed gets it here."""

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
            raise InstallRefused("unit_state_elsewhere", _elsewhere_detail(paths, data, why))
        check_state_root(paths)  # a chosen place: before anything is made
        check_private_dir(paths.state_root, paths.homes)  # before anything is made
        found = _found(paths, anchor_public_key, make_anchor)  # before anything is made
        made = ensure_stores(paths)
        ledger, anchor = _anchor_and_ledger(paths, anchor_public_key, found, make_anchor)
        homes = ensure_dir(paths.state_root, paths.homes, mode=_DIR_MODE)
        check_private_dir(paths.state_root, paths.homes, required=True)  # as made, or as another caller made it
        if current:
            still_holds(paths.unit, read)  # the bytes read, first: an in-place rewrite shows here
            still_names(paths.unit, info)  # last: the path still names the file that was read
        else:
            replace_regular(paths.unit, unit, _UNIT_MODE)
        return _plan("upgrade", not current or _any_created(made) or homes or ledger[1], paths, made, ledger, anchor)


def _elsewhere_detail(paths: Owned, data: bytes, why: str) -> str:
    """The text of ``unit_state_elsewhere``: what the unit does, and only ways out that work.

    With a place the person chose, or a unit this installer wrote (which names a chosen place when it is
    refused), the two ways that work: run ``recursiond upgrade`` again with ``XDG_STATE_HOME`` naming the
    folder the unit names (unset where the unit names the default) to keep the state where it is, or
    ``recursiond uninstall`` then ``recursiond install`` to start in the folder this command would use, the
    stores already made left where they are and not used (``stores_left`` names the default's). A unit
    placed by hand, while the default applies, is still told to move the state folder and take its line
    out: that unit has no chosen place of this installer's in it."""

    said = (f"{paths.unit} {why}: the daemon it starts may keep its state (the revocation and breaker stores, the "
            f"run registry) there, and the unit `recursiond upgrade` writes puts it in {paths.state}. ")
    ours = data.split(b"\n", 1)[0] == MARKER.encode()
    if not ours and not paths.chosen:
        return said + (f"Stop the daemon, move that state folder to {paths.state}, take the line out of the unit, "
                       "then run `recursiond upgrade` again")
    keep = "unset" if state_elsewhere(data, paths.home) is None else "set to the folder that unit names"
    now = f"set to {paths.state_home}" if paths.chosen else "unset"
    start = ("run `recursiond uninstall`, then" if ours else
             f"stop the daemon, remove {paths.unit}, which `recursiond install` did not write, then run")
    return said + (f"To keep the state where it is, run `recursiond upgrade` again with XDG_STATE_HOME {keep}. "
                   f"To start in {paths.state} instead, {start} `recursiond install` with XDG_STATE_HOME {now}; "
                   "the stores already made are left where they are and not used")


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
          ledger: tuple[str, bool, str] | None = None, anchor: tuple[str, bool] | None = None) -> Plan:
    chosen = paths.chosen and action != "uninstall"
    return Plan(action=action, changed=changed or bool(anchor and anchor[1]), unit=paths.unit,
                commands=plan_commands(action), stores=stores, ledger=ledger, state=paths.state if chosen else "",
                left=_default_stores_left(paths) if chosen else "", anchor=anchor)


def _default_stores_left(paths: Owned) -> str:
    """One plain sentence when the default's plugin-security folder holds a store while the person chose
    another place; "" otherwise. Nothing there is read, moved or removed: only whether a name is present."""

    folder = os.path.join(default_state_home(paths.home), STATE_FOLDER, STORE_FOLDER)
    found = [name for name in (BREAKER_FILE, REVOCATION_FILE, LEDGER_FILE) if os.path.lexists(os.path.join(folder, name))]
    if not found:
        return ""
    return (f"{folder} holds {', '.join(found)} from the default state folder; they are left where they are "
            f"and not used, since XDG_STATE_HOME chose {paths.state_home}")
