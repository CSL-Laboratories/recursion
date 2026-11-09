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
    if bool(nets) != ("network" in perms):
        raise Refused("manifest_network_mismatch")
    if bool(creds) != ("credential" in perms):
        raise Refused("manifest_credential_mismatch")
    scripts = _str_list(data["install_scripts"], re.compile(r"[ -~]{1,200}"), "manifest_install_scripts",
                        ordered=False)
    return Manifest(data["plugin_id"], data["version"], data["artifact_sha256"], lock, scripts,
                    data["developer_key"], tuple(perms), nets, creds, raw)


_REVOCATION_MAGIC = "csl-revocation-store/v0"


def create_revocation_store(path: str) -> None:
    """Installer step: create the durable revoked-key store. Refuses if it exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        db.execute("CREATE TABLE revoked (key_id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO meta VALUES ('magic', ?)", (_REVOCATION_MAGIC,))
    finally:
        db.close()


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
    back as revoked when the trust store is built. The daemon never creates the store;
    a named store that is missing or invalid refuses. Without a store, ``revoke`` still
    revokes in memory but refuses ``revocation_not_durable`` so the host knows. The
    same-uid limit in ``authority.py`` applies: in-process plugin code can rewrite this
    file (B17).
    """

    def __init__(self, trusted_keys: dict[str, str], revoked_key_ids: frozenset = frozenset(),
                 *, revocation_store: str | None = None):
        keys = {}
        for label, hex_key in trusted_keys.items():
            keys[key_id(hex_key)] = (label, hex_key)
        self._keys = MappingProxyType(keys)
        self._lock = threading.Lock()
        self._db = None if revocation_store is None else self._open_store(revocation_store)
        stored = () if self._db is None else (r[0] for r in self._db.execute("SELECT key_id FROM revoked"))
        self._revoked = frozenset(revoked_key_ids) | frozenset(stored)

    @staticmethod
    def _open_store(path: str) -> sqlite3.Connection:
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
        return db

    @property
    def revoked(self) -> frozenset:
        return self._revoked

    def revoke(self, kid: str) -> None:
        """Revoke a key id; later loads, including after a restart, classify it unsigned (4.4).

        The durable write comes first. The in-memory list is updated even if that write
        fails or there is no store, then ``revocation_not_durable`` is raised.
        """
        with self._lock:
            durable = False
            try:
                if self._db is not None:
                    self._db.execute("INSERT OR IGNORE INTO revoked VALUES (?)", (kid,))
                    durable = True
            finally:
                self._revoked = self._revoked | {kid}
            if not durable:
                raise Refused("revocation_not_durable", kid)

    def close(self) -> None:
        if self._db is not None:
            self._db.close()

    def classify(self, manifest: Manifest, signature: object) -> tuple[str, str | None, str]:
        """Return ``(trust, signer_key_id, reason)``; trust is ``signed`` or ``unsigned``."""
        kid = key_id(manifest.developer_key)
        if kid not in self._keys:
            return "unsigned", None, "signer_not_on_trusted_list"
        if kid in self._revoked:
            return "unsigned", None, "signer_key_revoked"
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


@dataclass(frozen=True)
class ReservedEffects:
    """Credentials and destinations only an effect owner may hold (injected; consumer-neutral)."""

    hosts: frozenset = frozenset()
    credential_prefixes: tuple = ()

    def host_reserved(self, destination: str) -> bool:
        host = destination.split(":", 1)[0]
        return any(host == r or host.endswith("." + r) for r in self.hosts)

    def credential_reserved(self, ref: str) -> bool:
        return any(ref.startswith(p) for p in self.credential_prefixes)


def effective_permissions(manifest: Manifest, trust: str, signer_key_id: str | None,
                          grant: Grant | None) -> tuple[tuple, list[tuple[str, str]]]:
    """Requested ∩ granted, in canonical order, plus the reasons anything was dropped."""
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
        out.append(perm)
    return tuple(out), dropped
