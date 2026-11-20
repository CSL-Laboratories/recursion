"""Plugin manifests (``csl-plugin-manifest/1`` and ``/vNext``), trust store and grants.

PRODUCT.md 4.1 and 4.3 (at 055cc7a5, CSLR-D-011), as implemented here:

* The developer signs the canonical manifest bytes with their own Ed25519 key. The
  manifest carries the artifact's SHA-256, so the signature covers the code.
* Two forms are read. ``csl-plugin-manifest/1`` (SPINE's draft schema
  ``schemas/plugin/v1/manifest.v1.json``) adds ``runtime``, ``interface`` and ``files``:
  every file of the package with its SHA-256, so the signed bytes name the whole package.
  ``csl-plugin-manifest/vNext`` names ``plugin.py`` only; Lucas's rule (Nov 11, 2026) is
  that such a signature is not enough for non-paper authority. Parsing ``files`` does not
  verify the package: that is ``verify_package`` (lane PLUGIN-SIGN, next slice), and the
  loader imports only ``plugin.py`` until it uses it.
* Grants are bound to the developer key they were given for (review R4, finding 6, under
  Lucas's D0, Nov 12, 2026): a grant naming another developer key grants nothing, and so
  does a manual grant that names none, in either form (grant again). Such a grant does not
  stop the plugin: it is still loaded and its code still runs, with an empty permission set.
  This build does not refuse a plugin whose grant is not bound.
* Installer configuration is refused unless it is in normal form: a reserved host is a
  lower-case host name with no port, no trailing dot and no IP address; a reserved
  credential prefix is lower case. A declared destination's port is 1 to 65535, and a
  manifest may not name a raw IP address (Lucas's decision B44, Nov 14, 2026).
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

import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from types import MappingProxyType

from .canonical import parse
from .errors import Refused
from .signing import key_id, parse_public_key, verify

__all__ = [
    "ENTRY_FILE",
    "MANIFEST_SCHEMA",
    "MANIFEST_SCHEMA_V1",
    "PERMISSIONS",
    "PERMISSIONS_V1",
    "Grant",
    "Manifest",
    "ReservedEffects",
    "TrustStore",
    "create_revocation_store",
    "destination_in_normal_form",
    "effective_permissions",
    "host_in_normal_form",
    "parse_manifest",
]

# The form whose signature covers plugin.py only. Still read; not enough for non-paper
# authority (Lucas, Nov 11, 2026: sign the whole package first).
MANIFEST_SCHEMA = "csl-plugin-manifest/vNext"
# The form that names every file of the package (SPINE's manifest.v1.json, a draft).
MANIFEST_SCHEMA_V1 = "csl-plugin-manifest/1"
# Canonical permission order. Requests must list permissions in this order, so the
# signed bytes for one permission set are unique. A vNext manifest requests only these
# seven. install.json may name PERMISSIONS_V1 (daemon/plugins.py, read_install).
PERMISSIONS = ("read", "propose", "panel", "network", "credential", "process", "order")
# Version 1 only appends: an existing name never changes its place, so the signed bytes
# for an existing set never change. The five new names and their order are PROVISIONAL
# until Lucas answers DEC F12 (schemas/plugin/v1/README.md); a vNext manifest refuses them.
PERMISSIONS_V1 = PERMISSIONS + ("surface", "command", "subscribe", "tool", "run")
# Runtimes, only ever appended to: a later runtime is a new name, under unchanged plugins.
# python-sandboxed/1 names Python run by a host of its own outside the daemon; that host is not in this
# build, so a plugin of either runtime runs in the daemon's own process (loader.py warns
# runtime_not_yet_contained). python-inprocess/1 retires: accepted in every 1.0.x release and run the same
# way, with the finding runtime_inprocess_retiring (loader.py); from 1.1.0 on it is refused.
_RUNTIMES_V1 = frozenset({"python-sandboxed/1", "python-inprocess/1"})
RUNTIME_INPROCESS_RETIRING = "python-inprocess/1"
# Runtime names reserved for a later release, each with its own text. Refused runtime_reserved and never
# run. Reserved is not frozen: a name may be corrected until the first build that answers it.
_RESERVED_RUNTIMES = MappingProxyType({
    "wasm-component/1": "wasm-component/1 is reserved for a later release; this release runs python-sandboxed/1 only.",
    "data/1": "data/1 is reserved; not run in this release.",
})
_UNKNOWN_RUNTIME = "unknown runtime; this release runs python-sandboxed/1 only"
# Permission names reserved for a later release, refused permission_reserved in either form of manifest.
# They are not in PERMISSIONS or PERMISSIONS_V1, and nothing grants them.
_RESERVED_PERMISSIONS = frozenset({"schedule", "memory", "computer"})
_INTERFACES_V1 = frozenset({"csl-plugin-api/1"})
# The file the loader runs; its digest is artifact_sha256 (daemon/plugins.py, ARTIFACT).
ENTRY_FILE = "plugin.py"
# The signature's own envelope. The manifest cannot name its own digest, so neither file
# may be listed in ``files``, under any case.
_ENVELOPE_FILES = frozenset({"manifest.json", "manifest.sig"})
_MAX_FILES, _MAX_PATH, _MAX_SEGMENT = 4096, 512, 255
_FIELDS = frozenset({
    "schema", "plugin_id", "version", "artifact_sha256", "dependency_lock_sha256",
    "install_scripts", "developer_key", "permissions", "network_destinations",
    "credential_refs",
})
_FIELDS_V1 = _FIELDS | {"runtime", "interface", "files"}
_ID = re.compile(r"[a-z][a-z0-9_.-]{1,63}")
# Caller names the core binds for itself. A plugin manifest may never claim one, so no
# plugin client can carry a host-only identity (B17, CSL-787).
RESERVED_PLUGIN_IDS = frozenset({"host"})
_HEX64 = re.compile(r"[0-9a-f]{64}")
# A host name and a declared destination are one expression each (``_HOSTNAME``, ``_HOST``), so
# the marketplace checker and the plugin schema can copy them verbatim. Both forms of manifest,
# and the reserved hosts of install.json, use them:
# - a label is 1 to 63 characters (the vNext pattern, and SPINE's draft copy, allowed 64);
# - the last label begins with a letter. That refuses a raw IP address in every spelling a
#   resolver reads as IPv4 ("10.0.0.1", "127.1", "0x7f.1", "0177.0.0.1", "2130706433"): each ends
#   in a part that begins with a digit, and no top-level domain does (RFC 3696, section 2). Lucas's
#   decision B44, Nov 14, 2026: a plugin's manifest may not name a raw IP address. IPv6 never
#   matches (no ':' inside a host, no '[');
# - a host name is at most 253 characters (the lookahead);
# - a port is 1 to 65535, with no sign and no leading zero.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_LAST_LABEL = r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_NAME = r"(?=[^:]{1,253}(?::|$))(?:" + _LABEL + r"\.)+" + _LAST_LABEL
_PORT_NUMBER = r"(?:[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])"
_HOSTNAME = re.compile(_HOST_NAME)
_HOST = re.compile(_HOST_NAME + r"(?::" + _PORT_NUMBER + r")?")
_CRED = re.compile(r"[a-z][a-z0-9_.:/-]{1,127}")
# A reserved credential prefix is a prefix of a credential name, in the same lower case.
_CRED_PREFIX = re.compile(r"[a-z][a-z0-9_.:/-]{0,127}")
_SCRIPT = re.compile(r"[ -~]{1,200}")
_VERSION_V1 = re.compile(r"[ -~]{1,64}")
_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*")
# Refused so that one package names one set of files on every system: a segment ending in '.'
# (Windows drops it, so "lib." is "lib") and a name Windows reserves for a device, in any case and
# with any extension ("CON.txt"): CON, PRN, AUX, NUL, COM1-COM9, LPT1-LPT9 (Microsoft, "Naming
# Files, Paths, and Namespaces"; its superscript-digit forms cannot occur in this ASCII grammar).
_WINDOWS_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?")
# The install schema's key_id (schemas/plugin/v1/install.v1.json): ed25519: and 32 lower-case hex.
_KEY_ID = re.compile(r"ed25519:[0-9a-f]{32}")
# The daemon reads a manifest only up to this many bytes. The same bound sits beside
# package verification: verify_package is the next slice, and a manifest the daemon
# will not read is not parsed.
MAX_MANIFEST_BYTES = 64 * 1024


def developer_key_id_ok(value: object) -> bool:
    """True for null or a key id. Any other shape is not a developer key id."""
    return value is None or (type(value) is str and _KEY_ID.fullmatch(value) is not None)


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
    schema: str = MANIFEST_SCHEMA
    # Version 1 only (None for vNext). ``files`` is ``((path, sha256), ...)`` in path order;
    # a vNext manifest names no file but plugin.py, through ``artifact_sha256``.
    runtime: str | None = None
    interface: str | None = None
    files: tuple = ()

    @property
    def names_every_file(self) -> bool:
        """True when the manifest is version 1 and lists at least one file.

        A statement about the manifest, not about the package on disk: only
        ``verify_package`` shows that the files match it. Only a manifest from
        ``parse_manifest`` is known to hold what its signed bytes say; one built by hand
        is not, and one with no files is never taken as naming every file.
        """
        return self.schema == MANIFEST_SCHEMA_V1 and bool(self.files)


def host_in_normal_form(host: object) -> bool:
    """True for a lower-case host name of two or more labels whose last label begins with a
    letter: no port, no trailing dot, no IP address (Lucas's decision B44)."""
    return type(host) is str and _HOSTNAME.fullmatch(host) is not None


def destination_in_normal_form(destination: object) -> bool:
    """A host in normal form, optionally ``:port`` with a port from 1 to 65535 and no
    leading zero: exactly ``_HOST``."""
    return type(destination) is str and _HOST.fullmatch(destination) is not None


def _matches(pattern: re.Pattern):
    return lambda value: type(value) is str and pattern.fullmatch(value) is not None


def _str_list(value: object, ok, code: str, ordered: bool = True) -> tuple:
    if type(value) is not list:
        raise Refused(code)
    items = tuple(value)  # the manifest keeps an immutable tuple, and every check below reads it
    if not all(ok(v) for v in items):
        raise Refused(code)
    if len(set(items)) != len(items) or (ordered and list(items) != sorted(items)):
        raise Refused(code, "must be unique and sorted")
    return items


def _parse_files(value: object, artifact: str) -> tuple:
    """``files`` of a version 1 manifest: safe relative paths to SHA-256, the entry file
    with the artifact's digest, and no two paths that one file system could confuse."""
    if type(value) is not dict or not 1 <= len(value) <= _MAX_FILES:
        raise Refused("manifest_files", "1 to %d entries" % _MAX_FILES)
    entries = tuple(value.items())
    folded: dict[str, str] = {}
    for path, digest in entries:
        if (type(path) is not str or len(path) > _MAX_PATH or not _PATH.fullmatch(path)
                or any(len(segment) > _MAX_SEGMENT for segment in path.split("/"))):
            raise Refused("manifest_files_path",
                          "a relative path of letters, digits, '_', '.' and '-', no segment "
                          "starting with a dot, at most %d characters" % _MAX_PATH)
        lowered = path.lower()
        if any(segment.endswith(".") or _WINDOWS_DEVICE.fullmatch(segment) for segment in lowered.split("/")):
            raise Refused("manifest_files_path",
                          "%s: not portable (a segment ending in '.', or a name Windows keeps for a device)" % path)
        if lowered.split("/", 1)[0] in _ENVELOPE_FILES:
            raise Refused("manifest_files_envelope", path)
        if type(digest) is not str or not _HEX64.fullmatch(digest):
            raise Refused("manifest_files_digest", path)
        if lowered in folded:
            raise Refused("manifest_files_conflict", "%s and %s differ only in case" % (folded[lowered], path))
        folded[lowered] = path
    for lowered, path in folded.items():
        parts = lowered.split("/")
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            if parent in folded:
                raise Refused("manifest_files_conflict", "%s is a file and a folder of %s" % (folded[parent], path))
    if value.get(ENTRY_FILE) != artifact:
        raise Refused("manifest_files_entry", "%s must be listed with artifact_sha256" % ENTRY_FILE)
    return tuple(sorted(entries))


def parse_manifest(raw: bytes) -> Manifest:
    """Parse and check manifest bytes, either form. Refuses anything malformed; never guesses.

    ``csl-plugin-manifest/1`` is told apart by its ``schema``; anything else is read as
    vNext. A vNext manifest is checked as before except for its declared destinations, which
    now follow ``_HOST`` (D0 and Lucas's B44): a raw IP address, a port outside 1 to 65535 or
    with a leading zero, a label of 64 characters or more and a host of more than 253 are
    refused, as ``manifest_network_destinations``, where the earlier parser accepted them.

    Bytes longer than ``MAX_MANIFEST_BYTES`` (64 KiB, the daemon's manifest bound) are
    refused ``plugin_file_too_large`` before they are parsed. That bound sits beside
    ``verify_package``: parsing ``files`` does not verify the package.
    """
    if type(raw) is bytes and len(raw) > MAX_MANIFEST_BYTES:
        raise Refused("plugin_file_too_large", "manifest.json")
    data = parse(raw)
    v1 = type(data) is dict and data.get("schema") == MANIFEST_SCHEMA_V1
    if type(data) is not dict or set(data) != (_FIELDS_V1 if v1 else _FIELDS):
        raise Refused("manifest_fields")
    if data["schema"] != (MANIFEST_SCHEMA_V1 if v1 else MANIFEST_SCHEMA):
        raise Refused("manifest_schema")
    order = PERMISSIONS_V1 if v1 else PERMISSIONS
    if type(data["plugin_id"]) is not str or not _ID.fullmatch(data["plugin_id"]):
        raise Refused("manifest_plugin_id")
    if data["plugin_id"] in RESERVED_PLUGIN_IDS:
        raise Refused("manifest_plugin_id_reserved")
    version = data["version"]
    if type(version) is not str or not 0 < len(version) <= 64 or (v1 and not _VERSION_V1.fullmatch(version)):
        raise Refused("manifest_version")
    if type(data["artifact_sha256"]) is not str or not _HEX64.fullmatch(data["artifact_sha256"]):
        raise Refused("manifest_artifact_digest")
    lock = data["dependency_lock_sha256"]
    if lock is not None and (type(lock) is not str or not _HEX64.fullmatch(lock)):
        raise Refused("manifest_dependency_lock")
    parse_public_key(data["developer_key"])
    perms = data["permissions"]
    if type(perms) is list:
        for p in perms:
            if type(p) is str and p in _RESERVED_PERMISSIONS:
                raise Refused("permission_reserved", "%s is reserved; not available in this release" % p)
    if type(perms) is not list or any(p not in order for p in perms):
        raise Refused("manifest_unknown_permission")
    if len(set(perms)) != len(perms) or perms != sorted(perms, key=order.index):
        raise Refused("manifest_permission_order")
    nets = _str_list(data["network_destinations"], destination_in_normal_form, "manifest_network_destinations")
    creds = _str_list(data["credential_refs"], _matches(_CRED), "manifest_credential_refs")
    if bool(nets) != ("network" in perms):
        raise Refused("manifest_network_mismatch")
    if bool(creds) != ("credential" in perms):
        raise Refused("manifest_credential_mismatch")
    scripts = _str_list(data["install_scripts"], _matches(_SCRIPT), "manifest_install_scripts", ordered=False)
    if not v1:
        return Manifest(data["plugin_id"], data["version"], data["artifact_sha256"], lock, scripts,
                        data["developer_key"], tuple(perms), nets, creds, raw)
    runtime = data["runtime"]
    if type(runtime) is str and runtime in _RESERVED_RUNTIMES:
        raise Refused("runtime_reserved", _RESERVED_RUNTIMES[runtime])
    if type(runtime) is not str or runtime not in _RUNTIMES_V1:
        raise Refused("manifest_runtime", _UNKNOWN_RUNTIME)
    if type(data["interface"]) is not str or data["interface"] not in _INTERFACES_V1:
        raise Refused("manifest_interface", "only csl-plugin-api/1 is defined")
    files = _parse_files(data["files"], data["artifact_sha256"])
    return Manifest(data["plugin_id"], version, data["artifact_sha256"], lock, scripts,
                    data["developer_key"], tuple(perms), nets, creds, raw, MANIFEST_SCHEMA_V1,
                    data["runtime"], data["interface"], files)


_REVOCATION_MAGIC = "csl-revocation-store/v0"


def create_revocation_store(path: str) -> None:
    """Installer step: create the durable revoked-key store. Refuses if it exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        db.execute(
            "CREATE TABLE revoked (key_id TEXT PRIMARY KEY, generation INTEGER NOT NULL)")
        db.execute("INSERT INTO meta VALUES ('magic', ?)", (_REVOCATION_MAGIC,))
    finally:
        db.close()


# A key id in the form ``signing.key_id`` gives it. ``revoke`` stores nothing else, so
# a refusal never repeats caller text and the store holds only ids a key can have.
_KEY_ID = re.compile(r"ed25519:[0-9a-f]{32}")
# The largest generation SQLite keeps as an INTEGER; a revoke that would pass it is
# refused before anything is written (SQLite would turn it into a float).
_GENERATION_MAX = 2 ** 63 - 1


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
    earlier revocation does not apply to the later one. The next generation is taken
    from the stored row inside one write transaction, so two handles on one store never
    write a generation that is not above the row (no roll-back to an older number whose
    grant would apply again). A row another writer left below 1 is not a floor: the
    revoke refuses and writes nothing. The daemon never creates the
    store; a named store that is missing or invalid refuses. Without a store, or when
    the SQLite write fails, ``revoke`` still revokes in memory but refuses
    ``revocation_not_durable`` (the SQLite error does not escape). The same-uid limit
    in ``authority.py`` applies: in-process plugin code can rewrite this file (B17).
    """

    def __init__(self, trusted_keys: dict[str, str], revoked_key_ids: frozenset = frozenset(),
                 *, revocation_store: str | None = None):
        keys = {}
        for label, hex_key in trusted_keys.items():
            keys[key_id(hex_key)] = (label, hex_key)
        self._keys = MappingProxyType(keys)
        self._lock = threading.Lock()
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
        if not os.path.isfile(path):
            raise Refused("revocation_store_missing")
        db = None
        try:
            db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=30000")
            meta = dict(db.execute("SELECT k, v FROM meta").fetchall())
            db.execute("SELECT key_id FROM revoked LIMIT 1").fetchall()
            columns = [row[1] for row in db.execute("PRAGMA table_info(revoked)")]
        except sqlite3.Error:
            if db is not None:
                db.close()
            raise Refused("revocation_store_invalid") from None
        if meta.get("magic") != _REVOCATION_MAGIC:
            db.close()
            raise Refused("revocation_store_invalid")
        if "generation" not in columns:
            try:
                db.execute(
                    "ALTER TABLE revoked ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
            except sqlite3.Error:
                db.close()
                raise Refused("revocation_store_invalid") from None
        return db

    @property
    def revoked(self) -> frozenset:
        return self._revoked

    def generation(self, kid: str) -> int | None:
        """Revocation generation for ``kid``, or ``None`` when that key is not revoked."""
        return self._generation.get(kid)

    def revoke(self, kid: str) -> int:
        """Revoke a key id and return its new generation (4.4).

        Later loads, including after a restart, classify it unsigned. A repeat revoke
        of the same key advances the generation again. The in-memory generation is
        updated even when the durable write fails or there is no store, and then
        ``revocation_not_durable`` is raised. A ``sqlite3`` error is that refusal; it
        does not escape the method.

        The generation returned is one past the stored row, and never below this
        handle's own next number (a failed write leaves memory ahead of the row), so
        another handle opened earlier on the same store cannot write an older number.
        A key id not in ``signing.key_id``'s form is refused ``invalid_key_id`` before
        anything changes, and the refusal does not repeat it. A stored generation that
        is not an integer or is below 1, or a next generation past SQLite's INTEGER
        range, writes nothing and refuses ``revocation_not_durable``.
        """
        if type(kid) is not str or not _KEY_ID.fullmatch(kid):
            raise Refused("invalid_key_id")
        with self._lock:
            generation = self._generation.get(kid, 0) + 1
            self._remember(kid, generation)
            if self._db is None:
                raise Refused("revocation_not_durable", kid)
            try:
                stored = self._advance(kid, generation)
            except sqlite3.Error:
                raise Refused("revocation_not_durable", kid) from None
            if stored is None:
                raise Refused("revocation_not_durable", kid)
            self._remember(kid, stored)
            return stored

    def _remember(self, kid: str, generation: int) -> None:
        updated = dict(self._generation)
        updated[kid] = generation
        self._generation = MappingProxyType(updated)
        self._revoked = frozenset(updated)

    def _advance(self, kid: str, floor: int) -> int | None:
        """Write ``kid``'s next generation, at least ``floor``, and return it.

        The row is read and written inside ``BEGIN IMMEDIATE``: no other connection
        writes the store between the read and the write. ``None`` when nothing was
        written (see ``revoke``). A ``sqlite3`` error rolls back and propagates.
        When that roll-back itself fails, the connection is dropped and the store is
        opened again, then the original error propagates.
        """
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT generation FROM revoked WHERE key_id = ?", (kid,)).fetchone()
            stored = 0 if row is None else row[0]
            # An absent row is a first revoke (stored stands in as 0). A row that exists
            # and is below 1 is a damaged store, same as a value that is not an integer:
            # writing generation 1 again would let a grant that cites generation 1 apply.
            if row is not None and (type(stored) is not int or stored < 1):
                db.execute("ROLLBACK")
                return None
            generation = max(stored + 1, floor)
            if generation > _GENERATION_MAX:
                db.execute("ROLLBACK")
                return None
            db.execute(
                "INSERT INTO revoked (key_id, generation) VALUES (?, ?) "
                "ON CONFLICT(key_id) DO UPDATE SET generation = excluded.generation",
                (kid, generation),
            )
            row = db.execute("SELECT generation FROM revoked WHERE key_id = ?", (kid,)).fetchone()
            if row is None or row[0] != generation:
                db.execute("ROLLBACK")
                return None
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    self._replace_store_connection(db)
            raise
        return generation

    def _replace_store_connection(self, db) -> None:
        """A roll-back that itself fails leaves the connection inside a transaction.

        Drop that connection and open the store again so a later revoke can write.
        """
        try:
            db.close()
        except sqlite3.Error:
            pass
        if self._db is db:
            self._db = None
        path = self._store_path
        if not path:
            return
        try:
            self._db = self._open_store(path)
        except Refused:
            self._db = None

    def close(self) -> None:
        with self._lock:
            db = self._db
            self._db = None
            if db is not None:
                db.close()

    def classify(self, manifest: Manifest, signature: object) -> tuple[str, str | None, str]:
        """Return ``(trust, signer_key_id, reason)``; trust is ``signed`` or ``unsigned``."""
        kid = key_id(manifest.developer_key)
        if kid in self._revoked:  # checked first: revocation also holds for an untrusted key
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
    # The developer key id the manifest named when the user made this grant (R4 finding 6,
    # under D0: a manual grant is bound to the developer key it was given for). A grant
    # naming another key grants nothing. A manual grant (signer_key_id None) that names no
    # key here may still be bound by ``order_after_revoked_key``, which names the key the
    # user turned order back on for; with neither it is unbound and grants nothing, in
    # either manifest form: the user grants again, for the key the manifest names now. An unbound
    # grant only empties the permission set; the plugin is still loaded and its code still runs.
    # Null, or a key id (``ed25519:`` and 32 lower-case hex). Anything else is refused.
    developer_key_id: str | None = None

    def __post_init__(self) -> None:
        if not developer_key_id_ok(self.developer_key_id):
            raise Refused("grant_invalid", "developer_key_id")


def _bound_developer_key(grant: Grant) -> str | None:
    """The developer key id a grant names for itself: ``developer_key_id`` when set; for a
    manual grant (no signer) otherwise ``order_after_revoked_key``; else None. A signed grant
    is bound through its signer, so its ``order_after_revoked_key`` is not read here."""
    if grant.developer_key_id is not None:
        return grant.developer_key_id
    if grant.signer_key_id is None:
        return grant.order_after_revoked_key
    return None


_RESERVED_SHAPE = ("lower-case host name of two or more labels, the last beginning with a letter; "
                   "no port, no trailing dot, no IP address")


@dataclass(frozen=True)
class ReservedEffects:
    """Credentials and destinations only an effect owner may hold (injected; consumer-neutral).

    Every entry must be in normal form (D0, Nov 12, 2026): an entry a destination could
    never equal would reserve nothing, so it is refused as bad installer configuration
    (``plugin_install_config_invalid``), never normalised and never kept.
    """

    hosts: frozenset = frozenset()
    credential_prefixes: tuple = ()

    def __post_init__(self) -> None:
        # A frozenset, tuple or list is read in one copy; a set is refused, because reading a set
        # another thread is still changing raises RuntimeError instead of this refusal.
        if (type(self.hosts) not in (frozenset, tuple, list)
                or type(self.credential_prefixes) not in (frozenset, tuple, list)):
            raise Refused("plugin_install_config_invalid",
                          "reserved: hosts and credential_prefixes are lists (a frozenset, tuple or list)")
        hosts, prefixes = tuple(self.hosts), tuple(self.credential_prefixes)  # one snapshot each
        for host in hosts:
            if not host_in_normal_form(host):
                raise Refused("plugin_install_config_invalid",
                              "reserved host %s is not in normal form (%s)" % (ascii(host)[:100], _RESERVED_SHAPE))
        for prefix in prefixes:
            if type(prefix) is not str or not _CRED_PREFIX.fullmatch(prefix):
                raise Refused("plugin_install_config_invalid",
                              "reserved credential prefix %s is not in normal form (lower case, as a "
                              "credential name begins)" % ascii(prefix)[:100])
        object.__setattr__(self, "hosts", frozenset(hosts))
        object.__setattr__(self, "credential_prefixes", prefixes)

    def host_reserved(self, destination: str) -> bool:
        host = destination.split(":", 1)[0]
        return any(host == r or host.endswith("." + r) for r in self.hosts)

    def credential_reserved(self, ref: str) -> bool:
        return any(ref.startswith(p) for p in self.credential_prefixes)


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

    The grant is also bound to the developer key it was given for (R4 finding 6, D0): a
    grant whose bound key (``_bound_developer_key``) is not the manifest's developer key
    grants nothing, so rewriting an unsigned manifest under another key does not carry a
    manual grant (or escape that key's revocation). A manual grant bound to no key cannot
    show which key it was for, so it grants nothing in either form (``grant_developer_key_missing``);
    its identity is never inferred from the unsigned manifest on disk. This result is a permission
    set and findings: it does not refuse the plugin, which is still loaded and whose code still runs
    with the empty set; that the loader does not refuse it before it starts is today's behaviour.
    """
    dropped: list[tuple[str, str]] = []
    if type(manifest) is not Manifest:
        raise Refused("manifest_invalid", "effective_permissions takes a parsed Manifest")
    if grant is None:
        return (), [("no_grant", "no installer grant for this plugin")]
    if type(grant) is not Grant:
        raise Refused("grant_invalid", "effective_permissions takes a Grant (not an install.json entry)")
    if grant.plugin_id != manifest.plugin_id or grant.artifact_sha256 != manifest.artifact_sha256:
        return (), [("grant_artifact_mismatch", "grant is for another artifact; grant again")]
    if grant.signer_key_id != signer_key_id:
        return (), [("grant_signer_mismatch", "grant is for another signer; grant again")]
    bound = _bound_developer_key(grant)
    if bound is not None and bound != key_id(manifest.developer_key):
        return (), [("grant_developer_key_mismatch", "grant is for another developer key; grant again")]
    if grant.signer_key_id is None and bound is None:
        return (), [("grant_developer_key_missing", "manual grant names no developer key; grant again")]
    out = []
    for perm in PERMISSIONS_V1:
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
