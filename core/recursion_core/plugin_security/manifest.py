"""Redesigned plugin manifest (``csl-plugin-manifest/vNext``), trust store and grants.

PRODUCT.md 4.1 and 4.3 (at 055cc7a5, CSLR-D-011), as implemented here:

* The developer signs the canonical manifest bytes with their own Ed25519 key. The
  manifest carries the artifact's SHA-256, so the signature covers the code.
* "Signed" means signed by a key on *this install's* trusted list. A signature by any
  other key, a revoked key, or a malformed signature loads as unsigned; it does not
  refuse the plugin. Verified (the registry's mark) is a label, never a gate.
* Authority is the intersection of what the manifest requests and what the installer
  granted, bound to the artifact digest and the signer's key (11.10, F-34). A grant
  for another digest or key grants nothing. Order permission needs the signed
  "ask once" grant, or for an unsigned plugin an explicit manual grant made behind the
  warning (``unsigned_order_acknowledged``).
* No side door (4.2): no grant may name a credential or network destination that an
  effect owner reserves. The core is consumer-neutral, so the reserved set is injected
  by the composition root (``ReservedEffects``); the core names no venue.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType

from .canonical import parse
from .errors import Refused
from .signing import key_id, parse_public_key, verify

__all__ = [
    "MANIFEST_SCHEMA",
    "PERMISSIONS",
    "Grant",
    "Manifest",
    "ReservedEffects",
    "TrustStore",
    "create_revocation_store",
    "effective_permissions",
    "parse_manifest",
]

MANIFEST_SCHEMA = "csl-plugin-manifest/vNext"
# Canonical permission order. Requests must list permissions in this order, so the
# signed bytes for one permission set are unique.
PERMISSIONS = ("read", "propose", "panel", "network", "credential", "process", "order")
_FIELDS = frozenset({
    "schema", "plugin_id", "version", "artifact_sha256", "dependency_lock_sha256",
    "install_scripts", "developer_key", "permissions", "network_destinations",
    "credential_refs",
})
_ID = re.compile(r"[a-z][a-z0-9_.-]{1,63}")
# Caller names the core binds for itself. A plugin manifest may never claim one, so no
# plugin client can carry a host-only identity (B17, CSL-787).
RESERVED_PLUGIN_IDS = frozenset({"host"})
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HOST = re.compile(r"[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)+(:[0-9]{1,5})?")
_CRED = re.compile(r"[a-z][a-z0-9_.:/-]{1,127}")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class Manifest:
    plugin_id: str
    version: str
    artifact_sha256: str
    dependency_lock_sha256: str | None
    install_scripts: tuple
    developer_key: str
    permissions: tuple
    network_destinations: tuple
    credential_refs: tuple
    raw: bytes = field(repr=False)


def _folded_names(value: str) -> tuple[str, ...]:
    """Posix and Windows folds of ``value``, including the tail after a drive prefix."""
    slash = value.replace("\\", "/")
    windows = ntpath.normpath(value).replace("\\", "/")
    names = [value, slash, posixpath.normpath(slash), windows, posixpath.normpath(windows)]
    for candidate in (slash, windows):
        if _DRIVE_PREFIX.match(candidate):
            tail = candidate[2:]
            names.append(tail)
            names.append(posixpath.normpath(tail))
            names.append(ntpath.normpath(tail).replace("\\", "/"))
    return tuple(dict.fromkeys(names))


def _fold_escapes(value: str) -> bool:
    """True when a posix or Windows fold walks out with a ``../`` prefix."""
    for name in _folded_names(value):
        folded = name.replace("\\", "/")
        if folded == ".." or folded.startswith("../"):
            return True
    return False


def _untrusted_path(value: str) -> bool:
    """True when a manifest path is absolute, drive-prefixed, or not its canonical relative form.

    ``a/../venue:live-key`` normalizes to the reserved name ``venue:live-key``.
    ``../outside.sh`` and ``/tmp/install.sh`` leave the plugin tree. ``C:/Windows/...``
    and ``c:../outside.sh`` are absolute or drive-relative on Windows; both are
    refused on every host. A NUL byte is refused here. The declared string has
    to be that canonical relative path.
    """
    if "\0" in value or "\\" in value or posixpath.isabs(value) or ntpath.isabs(value):
        return True
    if _DRIVE_PREFIX.match(value):
        return True
    norm = posixpath.normpath(value)
    if norm != value or norm == ".." or norm.startswith("../"):
        return True
    return _fold_escapes(value)


def _str_list(value: object, pattern: re.Pattern, code: str, ordered: bool = True) -> tuple:
    if type(value) is not list or any(type(v) is not str or not pattern.fullmatch(v) for v in value):
        raise Refused(code)
    if len(set(value)) != len(value) or (ordered and value != sorted(value)):
        raise Refused(code, "must be unique and sorted")
    return tuple(value)


def parse_manifest(raw: bytes) -> Manifest:
    """Parse and check manifest bytes. Refuses anything malformed; never guesses."""
    data = parse(raw)
    if type(data) is not dict or set(data) != _FIELDS:
        raise Refused("manifest_fields")
    if data["schema"] != MANIFEST_SCHEMA:
        raise Refused("manifest_schema")
    if type(data["plugin_id"]) is not str or not _ID.fullmatch(data["plugin_id"]):
        raise Refused("manifest_plugin_id")
    if data["plugin_id"] in RESERVED_PLUGIN_IDS:
        raise Refused("manifest_plugin_id_reserved")
    if type(data["version"]) is not str or not 0 < len(data["version"]) <= 64:
        raise Refused("manifest_version")
    if type(data["artifact_sha256"]) is not str or not _HEX64.fullmatch(data["artifact_sha256"]):
        raise Refused("manifest_artifact_digest")
    lock = data["dependency_lock_sha256"]
    if lock is not None and (type(lock) is not str or not _HEX64.fullmatch(lock)):
        raise Refused("manifest_dependency_lock")
    parse_public_key(data["developer_key"])
    perms = data["permissions"]
    if type(perms) is not list or any(p not in PERMISSIONS for p in perms):
        raise Refused("manifest_unknown_permission")
    if len(set(perms)) != len(perms) or perms != sorted(perms, key=PERMISSIONS.index):
        raise Refused("manifest_permission_order")
    nets = _str_list(data["network_destinations"], _HOST, "manifest_network_destinations")
    creds = _str_list(data["credential_refs"], _CRED, "manifest_credential_refs")
    if any(_untrusted_path(ref) for ref in creds):
        raise Refused("manifest_credential_refs", "path escapes")
    if bool(nets) != ("network" in perms):
        raise Refused("manifest_network_mismatch")
    if bool(creds) != ("credential" in perms):
        raise Refused("manifest_credential_mismatch")
    scripts = _str_list(data["install_scripts"], re.compile(r"[ -~]{1,200}"), "manifest_install_scripts",
                        ordered=False)
    if any(_untrusted_path(script) or _script_escapes(script) for script in scripts):
        raise Refused("manifest_install_scripts", "path escapes")
    return Manifest(data["plugin_id"], data["version"], data["artifact_sha256"], lock, scripts,
                    data["developer_key"], tuple(perms), nets, creds, raw)


_REVOCATION_MAGIC = "csl-revocation-store/v0"


def _schema_locked(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "database table is locked" in text or "database schema is locked" in text or text == "database is locked"


def _migrate_revoked_generation(db: sqlite3.Connection) -> None:
    """Add ``generation`` to a revocation store created before that column existed.

    The schema is read inside ``BEGIN IMMEDIATE``, then migrated there. A second
    opener waits and reads again, so it does not repeat the ``ALTER TABLE``.
    ``duplicate column name: generation`` means the other opener already added it.
    """
    deadline = time.monotonic() + 30.0
    while True:
        try:
            _alter_revoked_generation(db)
            return
        except sqlite3.OperationalError as exc:
            if _schema_locked(exc) and time.monotonic() < deadline:
                time.sleep(0.01)
                continue
            db.close()
            raise Refused("revocation_store_invalid") from None
        except sqlite3.DatabaseError:
            db.close()
            raise Refused("revocation_store_invalid") from None


def _alter_revoked_generation(db: sqlite3.Connection) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info(revoked)")]
        if "generation" not in columns:
            try:
                db.execute(
                    "ALTER TABLE revoked ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
            except sqlite3.DatabaseError as exc:
                if "duplicate column name: generation" not in str(exc).lower():
                    raise
        db.execute("COMMIT")
    except BaseException:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def _stripped_script_component(part: str) -> bool:
    """True when ``part`` is empty, ``.``, ``..``, a stream, or Win32 would make it one of those.

    Windows strips trailing spaces and dots from every component, so ``.. `` and
    ``...`` walk out of the plugin tree on that host. A colon names an alternate
    data stream (``install.sh:stream``, ``::$DATA``, ``::$INDEX_ALLOCATION``).
    Both are refused on every host.
    """
    if part in ("", ".", ".."):
        return True
    if ":" in part:
        return True
    if part.endswith(" ") or part.endswith("."):
        return True
    return part.rstrip(" .") == ".."


# Win32 device basenames. The host cuts the component at the first "." or ":",
# strips trailing spaces from that stem, then compares case-insensitively.
# ``NUL.txt`` and ``nul .txt`` are both ``nul``. COM0 and LPT0 are not devices.
# The check is a string test so every host refuses them.
_DEVICE_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
    "conin$", "conout$",
})


def _device_stem(part: str) -> str:
    cut = len(part)
    for sep in (".", ":"):
        index = part.find(sep)
        if index != -1 and index < cut:
            cut = index
    return part[:cut].rstrip(" ").casefold()


def _reserved_device_component(part: str) -> bool:
    return _device_stem(part) in _DEVICE_NAMES


def _script_escapes(value: str) -> bool:
    """An install-script name that is absolute, a stream, a device, or walks out with ``..``."""
    if os.path.isabs(value) or "\\" in value:
        return True
    parts = value.split("/")
    return not parts or any(
        _stripped_script_component(part) or _reserved_device_component(part) for part in parts)


def create_revocation_store(path: str) -> None:
    """Installer step: create the durable revoked-key store. Refuses if it exists."""
    from .paths import abandon_store, init_created_store, open_new_store
    path = open_new_store(path)
    try:
        db = sqlite3.connect(path, isolation_level=None)
    except (sqlite3.Error, OSError, UnicodeEncodeError):
        abandon_store(path)
        raise Refused("store_path_too_long") from None

    def build() -> None:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        db.execute(
            "CREATE TABLE revoked (key_id TEXT PRIMARY KEY, generation INTEGER NOT NULL)")
        db.execute("INSERT INTO meta VALUES ('magic', ?)", (_REVOCATION_MAGIC,))

    init_created_store(path, db, build)


class TrustStore:
    """This install's own trusted developer keys and its revoked-key list.

    Built by the composition root from installer-owned configuration, never from a
    plugin. CSL's key, if present, is one entry like any other. A key revoked while
    plugins are running goes through ``PluginLoader.revoke_key``, which also turns off
    order permission for active plugins under that key.

    A runtime revocation must outlive a restart (4.4: order stays off "until the user
    turns it back on"), and the installer configuration never sees it. So it is written
    to an installer-created SQLite store (``create_revocation_store``; WAL,
    ``synchronous=FULL``) before it takes effect, and every key in that store is read
    back as revoked when the trust store is built. Each ``revoke`` advances that key's
    generation, including a repeat, so a grant that turned order back on after an
    earlier revocation does not apply to the later one. The daemon never creates the
    store; a named store that is missing or invalid refuses. Without a store,
    ``revoke`` still revokes in memory and refuses ``revocation_not_durable``.
    When the SQLite write fails, memory is left unchanged or refreshed from the
    row, and the same refusal is raised (the SQLite error does not escape). The same-uid limit
    in ``authority.py`` applies: in-process plugin code can rewrite this file (B17).
    """

    def __init__(self, trusted_keys: dict[str, str], revoked_key_ids: frozenset = frozenset(),
                 *, revocation_store: str | None = None):
        keys = {}
        for label, hex_key in trusted_keys.items():
            keys[key_id(hex_key)] = (label, hex_key)
        self._keys = MappingProxyType(keys)
        self._lock = threading.RLock()
        # Keep the absolute path resolved now. A later chdir must not make a
        # failed write reopen a different relative ``rev.db``.
        if revocation_store is not None:
            from .paths import check_store_path
            revocation_store = check_store_path(revocation_store)
        self._store_path = revocation_store
        self._db = None if revocation_store is None else self._open_store(revocation_store)
        generations = {}
        if self._db is not None:
            try:
                rows = self._db.execute("SELECT key_id, generation FROM revoked").fetchall()
            except sqlite3.DatabaseError:
                self._db.close()
                raise Refused("revocation_store_invalid") from None
            for kid, generation in rows:
                if type(generation) is not int or generation < 1:
                    self._db.close()
                    raise Refused("revocation_store_invalid")
                generations[kid] = generation
        for kid in revoked_key_ids:
            generations.setdefault(kid, 1)
        self._generation = MappingProxyType(generations)
        self._revoked = frozenset(generations)

    @staticmethod
    def _open_store(path: str) -> sqlite3.Connection:
        from .paths import check_store_path
        path = check_store_path(path)
        if not os.path.isfile(path):
            raise Refused("revocation_store_missing")
        db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=30000")
            meta = dict(db.execute("SELECT k, v FROM meta").fetchall())
            db.execute("SELECT key_id FROM revoked LIMIT 1").fetchall()
        except sqlite3.DatabaseError:
            db.close()
            raise Refused("revocation_store_invalid") from None
        if meta.get("magic") != _REVOCATION_MAGIC:
            db.close()
            raise Refused("revocation_store_invalid")
        _migrate_revoked_generation(db)
        return db

    @property
    def revoked(self) -> frozenset:
        with self._lock:
            return self._revoked

    def generation(self, kid: str) -> int | None:
        """Revocation generation for ``kid``, or ``None`` when that key is not revoked."""
        with self._lock:
            return self._generation.get(kid)

    def revoke(self, kid: str) -> int:
        """Revoke a key id and return its new generation (4.4).

        Later loads, including after a restart, classify it unsigned. A repeat revoke
        of the same key advances the generation again. An opener that still has an
        older generation in memory cannot write that older number back: the stored
        generation only moves forward. The in-memory generation is published from
        the stored row after that write commits. Publishing updates the revoked
        set first and then the generation, and that update still runs when
        ``_publish_generation`` raises, so a committed row is not invisible.
        ``generation`` and ``revoked`` take the same lock, so a reader blocks
        until that update is in place and does not observe the pre-publish
        value after commit. A failed write leaves memory unchanged, or refreshes
        it from the row, and then ``revocation_not_durable`` is raised. With no
        store the revocation is kept in memory and the same refusal is raised.
        A ``sqlite3`` error is that refusal; it does not escape the method.
        """
        with self._lock:
            if self._db is None:
                generation = self._generation.get(kid, 0) + 1
                try:
                    self._publish_generation(kid, generation)
                finally:
                    self._install_revocation(kid, generation)
                raise Refused("revocation_not_durable", kid)
            generation = self._generation.get(kid, 0) + 1
            row = None
            try:
                # Memory stays as it was until COMMIT. The value published is the
                # row read back in this transaction. The row only moves forward:
                # one past what is stored, or this opener's generation, whichever
                # is higher.
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    self._db.execute(
                        "INSERT INTO revoked (key_id, generation) VALUES (?, ?) "
                        "ON CONFLICT(key_id) DO UPDATE SET "
                        "generation = MAX(revoked.generation + 1, excluded.generation)",
                        (kid, generation),
                    )
                    row = self._db.execute(
                        "SELECT generation FROM revoked WHERE key_id = ?", (kid,)).fetchone()
                    self._db.execute("COMMIT")
                except BaseException:
                    try:
                        self._db.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            except sqlite3.Error:
                self._reopen_store()
                self._refresh_generation(kid)
                raise Refused("revocation_not_durable", kid) from None
            if row is None or type(row[0]) is not int or row[0] < generation:
                self._refresh_generation(kid)
                raise Refused("revocation_not_durable", kid)
            try:
                self._publish_generation(kid, row[0])
            finally:
                # Commit already succeeded. Record the row even if publish raised,
                # revoked set first so the key is revoked if the generation write fails.
                self._install_revocation(kid, row[0])
            return row[0]

    def _publish_generation(self, kid: str, generation: int) -> None:
        self._install_revocation(kid, generation)

    def _install_revocation(self, kid: str, generation: int) -> None:
        """Record one committed revocation. The revoked set is updated first."""
        self._revoked = frozenset(set(self._revoked) | {kid})
        updated = dict(self._generation)
        updated[kid] = generation
        self._generation = MappingProxyType(updated)

    def _refresh_generation(self, kid: str) -> None:
        """Copy this key's stored generation into memory. A failed read leaves memory as it was."""
        db = self._db
        if db is None:
            return
        try:
            row = db.execute("SELECT generation FROM revoked WHERE key_id = ?", (kid,)).fetchone()
        except sqlite3.Error:
            return
        if row is not None and type(row[0]) is int and row[0] >= 1:
            self._publish_generation(kid, row[0])

    def _reopen_store(self) -> None:
        """Drop a connection a failed write left unusable and open a fresh one."""
        db = self._db
        self._db = None
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass
        if self._store_path is not None:
            try:
                self._db = self._open_store(self._store_path)
            except (Refused, sqlite3.Error, OSError):
                self._db = None

    def close(self) -> None:
        db = self._db
        self._db = None
        if db is not None:
            db.close()

    def classify(self, manifest: Manifest, signature: object) -> tuple[str, str | None, str]:
        """Return ``(trust, signer_key_id, reason)``; trust is ``signed`` or ``unsigned``."""
        kid = key_id(manifest.developer_key)
        with self._lock:
            revoked = kid in self._revoked
        if revoked:  # checked first: revocation also holds for an untrusted key
            return "unsigned", None, "signer_key_revoked"
        if kid not in self._keys:
            return "unsigned", None, "signer_not_on_trusted_list"
        if not verify(manifest.developer_key, signature, manifest.raw):
            return "unsigned", None, "signature_invalid"
        return "signed", kid, "signature_valid_trusted_key"


@dataclass(frozen=True)
class Grant:
    """What the installer granted one artifact. Bound to digest and signer key."""

    plugin_id: str
    artifact_sha256: str
    signer_key_id: str | None
    permissions: tuple
    unsigned_order_acknowledged: bool = False
    # The user's step to turn order back on under a revoked developer key (4.4): the
    # grant names that key id. A grant that only names the key covers generation 1,
    # the first revocation. A later revocation needs ``order_after_revocation_generation``
    # equal to ``TrustStore.generation`` for that key (the ``key.revoke`` result).
    order_after_revoked_key: str | None = None
    order_after_revocation_generation: int | None = None


@dataclass(frozen=True)
class ReservedEffects:
    """Credentials and destinations only an effect owner may hold (injected; consumer-neutral)."""

    hosts: frozenset = frozenset()
    credential_prefixes: tuple = ()

    def host_reserved(self, destination: str) -> bool:
        host = destination.split(":", 1)[0]
        return any(host == r or host.endswith("." + r) for r in self.hosts)

    def credential_reserved(self, ref: str) -> bool:
        # Fold both posix and Windows forms. ``a/../venue:key`` is the reserved
        # name once ``..`` is folded, and a ``../`` prefix left after either fold
        # is refused. A drive prefix is folded away first (``c:../venue:live-key``).
        if type(ref) is not str or "\0" in ref or _fold_escapes(ref):
            return True
        return any(form.startswith(prefix) for form in _folded_names(ref) for prefix in self.credential_prefixes)


def _order_cites_this_revocation(grant: Grant, revoked_key_id: str, generation: int) -> bool:
    """True when the grant is the user's step for this revocation, not an older one."""
    if grant.order_after_revoked_key != revoked_key_id:
        return False
    cited = grant.order_after_revocation_generation
    if cited is None:
        return generation == 1
    return type(cited) is int and cited == generation


def effective_permissions(manifest: Manifest, trust: str, signer_key_id: str | None,
                          grant: Grant | None, revoked_key_id: str | None = None,
                          revocation_generation: int | None = None
                          ) -> tuple[tuple, list[tuple[str, str]]]:
    """Requested ∩ granted, in canonical order, plus the reasons anything was dropped.

    ``revoked_key_id`` is the manifest's developer key id when that key is revoked. Then
    order stays off, for a signed or a manual grant, until the grant names that key.
    Naming the key alone covers the first revocation (generation 1). A later revocation
    stays off until the grant's ``order_after_revocation_generation`` equals
    ``revocation_generation`` (PRODUCT.md 4.4). ``None`` means generation 1.
    """
    dropped: list[tuple[str, str]] = []
    if grant is None:
        return (), [("no_grant", "no installer grant for this plugin")]
    if grant.plugin_id != manifest.plugin_id or grant.artifact_sha256 != manifest.artifact_sha256:
        return (), [("grant_artifact_mismatch", "grant is for another artifact; grant again")]
    if grant.signer_key_id != signer_key_id:
        return (), [("grant_signer_mismatch", "grant is for another signer; grant again")]
    out = []
    for perm in PERMISSIONS:
        if perm not in manifest.permissions:
            continue
        if perm not in grant.permissions:
            dropped.append(("not_granted", perm))
            continue
        if perm == "order" and trust != "signed" and not grant.unsigned_order_acknowledged:
            dropped.append(("unsigned_order_requires_manual_grant", perm))
            continue
        if perm == "order" and revoked_key_id is not None:
            generation = 1 if revocation_generation is None else revocation_generation
            if type(generation) is not int or not _order_cites_this_revocation(grant, revoked_key_id, generation):
                dropped.append(("signer_key_revoked_order_off", revoked_key_id))
                continue
        out.append(perm)
    return tuple(out), dropped
