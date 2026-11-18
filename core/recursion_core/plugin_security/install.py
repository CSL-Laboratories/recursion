"""Install, list and remove a plugin from a local registry folder.

Nothing here fetches, enables, disables or rolls back. A generation is one
complete copy of the profile the daemon already reads. Generations live beside
``plugins/``, never inside it: the daemon treats every directory there as a
plugin, including a dot directory.

    <config>/plugin-generations/<n>/install.json
    <config>/plugin-generations/<n>/<plugin_id>/{manifest.json,plugin.py,manifest.sig?}
    <config>/plugin-generations/current

``current`` is an ASCII integer and a newline, matching ``^[1-9][0-9]*\\n$``.
It is written only after generation ``n`` is complete. The live ``plugins/``
tree is published after that. The next list, install or remove publishes
``current`` again when that publish did not finish. Older generations stay on
disk so a later command can point ``current`` at one and publish it. This
module does not do that. A generation that ``current`` does not name (a stop
between its rename and the pointer) is left as it is; the next command takes
the number after every generation on disk.

Every check of the live ``plugins/`` path runs before a generation is written,
so a refused command leaves ``current`` and the generations as they were. The
publish moves ``install.json`` in first, by one rename over the old file, then
moves each old plugin directory out whole and each new one in whole: at every
step the live tree holds the whole old or the whole new ``install.json``. Each
written file and its directory are synced before the rename that publishes
them, and ``current`` is synced after its rename.

The registry is checked before any profile file is created. A signature is
checked on every install. When this interpreter cannot verify one, the install
is refused and nothing is written. A signature that does not verify against
the manifest's own key refuses the registry. A signature that verifies, but
whose key is not on this install's trusted list, still installs and loads as
unsigned. This command does not grant permissions and does not add a trusted key.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .errors import Refused
from .loader import CoreHealth, PluginLoader
from .manifest import PERMISSIONS, Grant, ReservedEffects, TrustStore, parse_manifest
from .registry import check_registry, platform_gap

try:
    import fcntl
except ImportError:  # Windows: every command refuses platform_unsupported before it opens a file
    fcntl = None

__all__ = ["InstallRefused", "install_plugin", "list_plugins", "remove_plugin"]

_PLUGIN_ID = re.compile(r"[a-z][a-z0-9_.-]{1,63}")
_CURRENT = re.compile(r"[1-9][0-9]*\n")
_GENERATION = re.compile(r"[1-9][0-9]{0,17}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_KEY_ID = re.compile(r"ed25519:[0-9a-f]{32}")
# Names the profile itself uses inside a generation and inside plugins/: never a plugin id.
_PROFILE_NAMES = frozenset({"install.json"})
_LABELS = frozenset({"verified", "non_verified"})
_INSTALL_KEYS = frozenset({"trusted_keys", "grants", "reserved", "authority_anchor", "registry_labels"})
_GRANT_KEYS = frozenset({
    "plugin_id", "artifact_sha256", "signer_key_id", "permissions", "unsigned_order_acknowledged",
    "order_after_revoked_key", "order_after_revocation_generation", "developer_key_id",
})
_MAX_MANIFEST = 64 * 1024
_MAX_ARTIFACT = 8 * 1024 * 1024
_MAX_SIGNATURE = 4096
_MAX_INSTALL = 1024 * 1024
_DEVELOPER_CHECKS = (
    "In-process plugin code can launch engine binaries past the launch check.",
    "In-process plugin code can stall other work.",
    "The loader reads the GIL after every load.",
    "An untrusted plugin config folder refuses every plugin.",
    "A missing revocation store reads as empty: no key counts as revoked.",
    "The registry label is a label, not a gate.",
    "A signature that fails this install's trusted-key check loads as unsigned.",
    "Signed order is asked once. This command does not grant it.",
    "Unsigned order is by hand, behind a warning. This command does not grant it.",
)


class InstallRefused(Exception):
    """The install, list or remove did not change the profile. ``code`` is stable."""

    def __init__(self, code: str, codes: list[str] | None = None, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.codes = list(codes) if codes else [code]
        self.detail = detail


@dataclass
class StoredPlugin:
    manifest: bytes
    artifact: bytes
    signature: bytes | None = None


@dataclass
class Snapshot:
    trusted_keys: dict = field(default_factory=dict)
    grants: dict = field(default_factory=dict)
    hosts: tuple = ()
    prefixes: tuple = ()
    anchor: str | None = None
    labels: dict = field(default_factory=dict)
    plugins: dict = field(default_factory=dict)
    # A grant's developer_key_id as it was read, by plugin id; written back unchanged.
    developer_key_ids: dict = field(default_factory=dict)

    def reserved(self) -> ReservedEffects:
        return ReservedEffects(frozenset(self.hosts), tuple(self.prefixes))


def install_plugin(config: os.PathLike | str, registry_root: os.PathLike | str, plugin_id: str, *,
                   version: str | None = None, state: os.PathLike | str | None = None, wait: bool = True) -> dict:
    """Check ``registry_root``, then write the release as the next generation."""
    _require_platform()
    config_path = Path(config)
    report = check_registry(registry_root)
    codes = report.codes()
    if codes:
        raise InstallRefused(codes[0], codes)
    # A signature anywhere in the registry that could not be verified refuses the
    # whole install. Unknown must not read as valid, and nothing is written.
    if any(item.code == "signature_unchecked" for item in report.notes):
        raise InstallRefused("signature_backend_unavailable")
    if not _plugin_id_ok(plugin_id) or plugin_id in _PROFILE_NAMES:
        raise InstallRefused("plugin_not_in_registry")
    if version is not None and type(version) is not str:
        raise InstallRefused("release_not_in_registry")
    chosen = _select(report, plugin_id, version)
    if chosen.artifact is None:
        raise InstallRefused("source_unavailable")
    fd = _lock(config_path, wait=wait)
    try:
        return _install_locked(config_path, chosen, plugin_id, state)
    finally:
        os.close(fd)


def list_plugins(config: os.PathLike | str, *, state: os.PathLike | str | None = None, wait: bool = True) -> dict:
    """Publish the current generation if a publish stopped early, then list what it holds."""
    _require_platform()
    del state  # list does not open the revocation store and does not create one
    config_path = Path(config)
    fd = _lock(config_path, wait=wait)
    try:
        _publish_current(config_path)
        return {"command": "list", "plugins": _rows(_load_snapshot(config_path))}
    finally:
        os.close(fd)


def remove_plugin(config: os.PathLike | str, plugin_id: str, *, state: os.PathLike | str | None = None,
                  wait: bool = True) -> dict:
    """Drop one plugin into a new generation. An unknown id writes nothing."""
    _require_platform()
    del state
    config_path = Path(config)
    if not _plugin_id_ok(plugin_id):
        raise InstallRefused("plugin_not_installed")
    fd = _lock(config_path, wait=wait)
    try:
        snapshot = _load_snapshot(config_path)
        if plugin_id not in snapshot.plugins:
            raise InstallRefused("plugin_not_installed")
        plugins = dict(snapshot.plugins)
        del plugins[plugin_id]
        grants = dict(snapshot.grants)
        grants.pop(plugin_id, None)
        kept = Snapshot(snapshot.trusted_keys, grants, snapshot.hosts, snapshot.prefixes, snapshot.anchor,
                        _labels_for(snapshot.labels, plugins), plugins, snapshot.developer_key_ids)
        generation = _commit(config_path, kept)
        return {"command": "remove", "plugin_id": plugin_id, "generation": generation, "plugins": _rows(kept)}
    finally:
        os.close(fd)


def _require_platform() -> None:
    # Same shape as daemon/plugins.py _read_owned: a typed refusal before any file is opened or made.
    # Windows has no fcntl, O_NOFOLLOW or O_DIRECTORY; a no-follow read for it is not built, so every
    # command refuses instead of dropping the guard (no default of 0, no open without it).
    # The guard names every POSIX call the commands make: the registry's reads, fcntl, geteuid, fchmod.
    missing = platform_gap()
    if missing is None and fcntl is None:
        missing = "fcntl"
    if missing is None:
        missing = next((name for name in ("geteuid", "fchmod") if not hasattr(os, name)), None)
    if missing is not None:
        raise InstallRefused("platform_unsupported", detail=f"{missing} is missing; no plugin file is opened")


def _lexists(path: Path) -> bool:
    """True if ``path`` is there, including a dangling symlink. ``Path.lexists`` is absent here."""
    return os.path.lexists(path)


def _plugin_id_ok(value: object) -> bool:
    return (type(value) is str and _PLUGIN_ID.fullmatch(value) is not None and value != "host"
            and value not in (".", "..") and "/" not in value and "\\" not in value)


def _select(report, plugin_id: str, version: str | None):
    # Only releases under registry/ can be chosen. examples/ is checked, never indexed or installed.
    matches = [item for item in report.checked if item.plugin_id == plugin_id and not item.in_examples]
    if not matches:
        raise InstallRefused("plugin_not_in_registry")
    if version is None:
        active = [item for item in matches if item.status == "active"]
        if not active:
            raise InstallRefused("release_not_active")
        return active[-1]
    for item in matches:
        if item.version == version:
            return item
    raise InstallRefused("release_not_in_registry")


def _install_locked(config: Path, chosen, plugin_id: str, state) -> dict:
    manifest = parse_manifest(chosen.manifest)
    if manifest.plugin_id != plugin_id or not _plugin_id_ok(manifest.plugin_id):
        raise InstallRefused("release_plugin_id_mismatch")
    if chosen.label not in _LABELS:
        raise InstallRefused("release_label")
    signature_text = chosen.signature_text
    snapshot = _load_snapshot(config)
    trust_name, reason, grants, findings = _admit(snapshot, manifest, signature_text, chosen.artifact,
                                                   chosen.label, state)
    refused = [item for item in findings if item.get("severity") == "refuse"]
    if refused:
        raise InstallRefused(refused[0]["code"], [item["code"] for item in refused])
    plugins = dict(snapshot.plugins)
    plugins[manifest.plugin_id] = StoredPlugin(chosen.manifest, chosen.artifact, chosen.signature)
    labels = _labels_for(snapshot.labels, plugins)
    labels[hashlib.sha256(chosen.manifest).hexdigest()] = chosen.label
    updated = Snapshot(dict(snapshot.trusted_keys), grants, snapshot.hosts, snapshot.prefixes, snapshot.anchor,
                       labels, plugins, snapshot.developer_key_ids)
    generation = _commit(config, updated)
    # What the written install.json gives this plugin, as the loader's admit step computed it from those grants.
    effective = next((item["detail"] for item in findings if item.get("code") == "effective_permissions"), "")
    return {
        "command": "install",
        "plugin_id": manifest.plugin_id,
        "version": manifest.version,
        "status": chosen.status,
        "status_reason": chosen.status_reason,
        "registry_label": chosen.label,
        "trust": trust_name,
        "signature": reason,
        "generation": generation,
        "order": "granted" if "order" in effective.split(",") else "not granted",
        "developer_checks": list(_DEVELOPER_CHECKS),
        "findings": findings,
        "requested_permissions": list(manifest.permissions),
    }


def _admit(snapshot: Snapshot, manifest, signature_text, artifact: bytes, label: str, state):
    """Classify and run doctor. A grant for another digest or signer is dropped. Nothing is granted."""
    path = _revocation_store(state)
    try:
        trust = TrustStore(dict(snapshot.trusted_keys), revocation_store=path)
    except Refused as exc:
        raise InstallRefused(exc.code, detail=exc.detail) from None
    try:
        try:
            trust_name, signer, reason = trust.classify(manifest, signature_text)
        except Refused as exc:
            raise InstallRefused(exc.code, detail=exc.detail) from None
        grants = _grants_kept(snapshot.grants, manifest, signer)
        loader = PluginLoader(trust, grants, snapshot.reserved(), CoreHealth())
        findings = loader.doctor(manifest.raw, signature_text, artifact, label)
    finally:
        trust.close()
    return trust_name, reason, grants, findings


def _grants_kept(grants: dict, manifest, signer: str | None) -> dict:
    """Keep this plugin's grant only when the artifact digest and the signer still match."""
    kept = dict(grants)
    grant = kept.get(manifest.plugin_id)
    if grant is None:
        return kept
    if grant.artifact_sha256 != manifest.artifact_sha256 or grant.signer_key_id != signer:
        del kept[manifest.plugin_id]
    return kept


def _revocation_store(state) -> str | None:
    """Open a store only when it already exists. This command does not create one."""
    if state is None:
        return None
    path = Path(state) / "plugin-security" / "revoked.sqlite3"
    if path.is_symlink():
        raise InstallRefused("revocation_store_invalid")
    if not path.is_file():
        return None
    return str(path)


def _labels_for(previous: dict, plugins: dict) -> dict:
    digests = {hashlib.sha256(plugin.manifest).hexdigest() for plugin in plugins.values()}
    return {digest: label for digest, label in previous.items() if digest in digests and label in _LABELS}


def _rows(snapshot: Snapshot) -> list[dict]:
    rows = []
    for plugin in snapshot.plugins.values():
        manifest = parse_manifest(plugin.manifest)
        digest = hashlib.sha256(plugin.manifest).hexdigest()
        rows.append({
            "plugin_id": manifest.plugin_id,
            "version": manifest.version,
            "registry_label": snapshot.labels.get(digest, "unlisted"),
        })
    rows.sort(key=lambda row: row["plugin_id"])
    return rows


def _lock(config: Path, wait: bool = True) -> int:
    """Exclusive lock on the profile. The caller closes the returned descriptor."""
    _require_config(config)
    path = config / "plugin-install.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise InstallRefused("plugin_directory_untrusted", detail=type(exc).__name__) from None
    try:
        os.fchmod(fd, 0o600)
        flags = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(fd, flags)
    except BlockingIOError:
        os.close(fd)
        raise InstallRefused("install_busy") from None
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            raise InstallRefused("install_busy") from None
        raise
    return fd


def _require_config(config: Path) -> None:
    # lstat first: a link, a non-directory, another owner or group or other write refuses, and nothing
    # is changed. Only a directory this call created gets its mode set.
    if not _lexists(config):
        config.parent.mkdir(parents=True, exist_ok=True)
        _make_dir(config)
    _require_owned_dir(config)


def _make_dir(path: Path) -> None:
    """Make ``path`` owner-only. A name that is already there is left as it is for the caller's check."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        return
    os.chmod(path, 0o700)


def _publish_current(config: Path) -> None:
    current = _read_current(config)
    if current is None:
        return
    _copy_generation_to_live(config, current)


def _read_current(config: Path) -> int | None:
    root = config / "plugin-generations"
    if not _lexists(root):
        return None
    _require_owned_dir(root)
    pointer = root / "current"
    if not _lexists(pointer):
        return None
    if pointer.is_symlink():
        raise InstallRefused("plugin_directory_untrusted")
    raw = _read_file(pointer, 64)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise InstallRefused("generation_unreadable") from None
    if _CURRENT.fullmatch(text) is None:
        raise InstallRefused("generation_unreadable")
    return int(text)


def _commit(config: Path, snapshot: Snapshot) -> int:
    # The live plugins/ path is checked before any generation is written or the pointer moves.
    _check_live(config)
    root = config / "plugin-generations"
    if not _lexists(root):
        _make_dir(root)
    _require_owned_dir(root)
    current = _read_current(config)
    number = _next_number(root, current)
    dest = root / str(number)
    if _lexists(dest):
        raise InstallRefused("generation_exists")
    partial = root / f".partial-{number}"
    if _lexists(partial):
        _remove_at(partial)
    partial.mkdir(mode=0o700)
    os.chmod(partial, 0o700)
    try:
        _write_profile(partial, snapshot)
        os.rename(partial, dest)
        _sync_dir(root)
        _write_pointer(root, number)
        # The pointer names a complete generation before the live tree is touched.
        # If publishing raises, the next command publishes this generation.
        _copy_generation_to_live(config, number)
    except Exception:
        if _lexists(partial) and not _lexists(dest):
            _remove_at(partial)
        raise
    return number


def _next_number(root: Path, current: int | None) -> int:
    """The number after ``current`` and after every generation on disk.

    A generation that ``current`` does not name is left from a stop between its rename and the
    pointer write. It is no obstacle: it stays on disk and the next number is taken.
    """
    taken = [int(name) for name in os.listdir(root) if _GENERATION.fullmatch(name)]
    return max([current or 0, *taken]) + 1


def _write_pointer(root: Path, number: int) -> None:
    _atomic_replace(root, "current", f"{number}\n".encode("ascii"))


def _copy_generation_to_live(config: Path, generation: int) -> None:
    """Replace the live ``plugins/`` tree with generation ``generation``.

    The stage directory is ``plugin-generations/.stage``, not a child of
    ``plugins/``. A dot directory under ``plugins/`` would be loaded.
    """
    config = Path(config)
    source = config / "plugin-generations" / str(generation)
    if not _lexists(source):
        raise InstallRefused("generation_missing")
    _require_owned_dir(source, "generation_missing")
    _check_live(config)
    stage = config / "plugin-generations" / ".stage"
    old = config / "plugin-generations" / ".old"
    for leftover in (stage, old):
        if _lexists(leftover):
            _remove_at(leftover)
    stage.mkdir(mode=0o700)
    os.chmod(stage, 0o700)
    try:
        snapshot = _read_profile(source, missing_install_ok=False)
        _write_profile(stage, snapshot)
        plugins = config / "plugins"
        if not _lexists(plugins):
            plugins.mkdir(mode=0o700)
            os.chmod(plugins, 0o700)
            _sync_dir(config)
        old.mkdir(mode=0o700)
        os.chmod(old, 0o700)
        _swap_into(plugins, stage, old)
    finally:
        for leftover in (stage, old):
            if _lexists(leftover):
                _remove_at(leftover)


def _check_live(config: Path) -> None:
    """The checks of the live ``plugins/`` path. They run before anything is written."""
    plugins = config / "plugins"
    if _lexists(plugins):
        if plugins.is_symlink():
            raise InstallRefused("plugin_directory_untrusted")
        _require_owned_dir(plugins)


def _swap_into(plugins: Path, stage: Path, old: Path) -> None:
    """Publish ``stage`` into ``plugins/``. Nothing is deleted from the live tree piece by piece.

    install.json goes in first, by one rename over the old file, and is never removed first. Then each
    old entry is moved out whole into ``old`` and each new plugin directory moved in whole.
    """
    staged = sorted(name for name in os.listdir(stage) if name != "install.json")
    live_install = plugins / "install.json"
    if _lexists(live_install) and stat.S_ISDIR(os.lstat(live_install).st_mode):
        os.rename(live_install, old / "install.json")
    os.rename(stage / "install.json", live_install)
    _sync_dir(plugins)
    for name in sorted(os.listdir(plugins)):
        if name == "install.json":
            continue
        path = plugins / name
        info = os.lstat(path)
        if name in staged or stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode):
            os.rename(path, old / name)
    for name in staged:
        os.rename(stage / name, plugins / name)
    _sync_dir(plugins)


def _load_snapshot(config: Path) -> Snapshot:
    current = _read_current(config)
    if current is None:
        live = config / "plugins"
        if not _lexists(live):
            return Snapshot()
        if live.is_symlink():
            raise InstallRefused("plugin_directory_untrusted")
        return _read_profile(live, missing_install_ok=True)
    path = config / "plugin-generations" / str(current)
    if not _lexists(path):
        raise InstallRefused("generation_missing")
    return _read_profile(path, missing_install_ok=False)


def _read_profile(directory: Path, *, missing_install_ok: bool) -> Snapshot:
    _require_owned_dir(directory, "generation_unreadable" if not missing_install_ok else "plugin_directory_untrusted")
    install_path = directory / "install.json"
    if not _lexists(install_path):
        if not missing_install_ok:
            raise InstallRefused("generation_unreadable")
        parsed = _empty_install()
    else:
        if install_path.is_symlink():
            raise InstallRefused("plugin_directory_untrusted")
        try:
            parsed = _parse_install(_read_file(install_path, _MAX_INSTALL))
        except InstallRefused:
            raise
        except (OSError, ValueError, UnicodeError):
            raise InstallRefused("generation_unreadable" if not missing_install_ok else "install_unreadable") from None
    plugins = {}
    for name in sorted(os.listdir(directory)):
        if name == "install.json":
            continue
        path = directory / name
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise InstallRefused("plugin_directory_untrusted")
        if not stat.S_ISDIR(info.st_mode):
            continue
        if not _plugin_id_ok(name):
            raise InstallRefused("plugin_directory_untrusted")
        plugins[name] = _read_plugin(path)
    return Snapshot(parsed["trusted_keys"], parsed["grants"], parsed["hosts"], parsed["prefixes"],
                    parsed["anchor"], parsed["labels"], plugins, parsed["developer_key_ids"])


def _empty_install() -> dict:
    return {"trusted_keys": {}, "grants": {}, "hosts": (), "prefixes": (), "anchor": None, "labels": {},
            "developer_key_ids": {}}


def _read_plugin(directory: Path) -> StoredPlugin:
    _require_owned_dir(directory)
    manifest = _read_file(directory / "manifest.json", _MAX_MANIFEST)
    artifact = _read_file(directory / "plugin.py", _MAX_ARTIFACT)
    signature_path = directory / "manifest.sig"
    signature = None
    if _lexists(signature_path):
        if signature_path.is_symlink():
            raise InstallRefused("plugin_directory_untrusted")
        signature = _read_file(signature_path, _MAX_SIGNATURE)
    try:
        parsed = parse_manifest(manifest)
    except Refused as exc:
        raise InstallRefused("generation_unreadable", detail=exc.code) from None
    if parsed.plugin_id != directory.name:
        raise InstallRefused("generation_unreadable")
    return StoredPlugin(manifest, artifact, signature)


def _parse_install(raw: bytes) -> dict:
    """Accept the install file the daemon accepts, and nothing past those fields."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise InstallRefused("install_unreadable") from None
    if not isinstance(data, dict) or set(data) - _INSTALL_KEYS:
        raise InstallRefused("install_unreadable")
    keys = data.get("trusted_keys", {})
    if not isinstance(keys, dict) or not all(isinstance(key, str) and isinstance(value, str)
                                             and _SHA256.fullmatch(value) is not None
                                             for key, value in keys.items()):
        raise InstallRefused("install_unreadable")
    listed = data.get("grants", [])
    if not isinstance(listed, list):
        raise InstallRefused("install_unreadable")
    grants = {}
    developer_key_ids = {}
    for item in listed:
        grant = _parse_grant(item)
        if grant.plugin_id in grants:
            raise InstallRefused("install_unreadable")
        grants[grant.plugin_id] = grant
        if "developer_key_id" in item:
            developer_key_ids[grant.plugin_id] = item["developer_key_id"]
    reserved = data.get("reserved", {})
    hosts = reserved.get("hosts", []) if isinstance(reserved, dict) else None
    prefixes = reserved.get("credential_prefixes", []) if isinstance(reserved, dict) else None
    if (not isinstance(reserved, dict) or set(reserved) - {"hosts", "credential_prefixes"}
            or not isinstance(hosts, list) or not isinstance(prefixes, list)
            or not all(isinstance(value, str) and value for value in [*hosts, *prefixes])):
        raise InstallRefused("install_unreadable")
    anchor = data.get("authority_anchor")
    if not (anchor is None or isinstance(anchor, str)):
        raise InstallRefused("install_unreadable")
    labels = data.get("registry_labels", {})
    if not isinstance(labels, dict):
        raise InstallRefused("install_unreadable")
    kept_labels = {}
    for digest, label in labels.items():
        if (not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
                or not isinstance(label, str) or label not in _LABELS):
            raise InstallRefused("install_unreadable")
        kept_labels[digest] = label
    return {"trusted_keys": dict(keys), "grants": grants, "hosts": tuple(hosts), "prefixes": tuple(prefixes),
            "anchor": anchor, "labels": kept_labels, "developer_key_ids": developer_key_ids}


def _parse_grant(item: object) -> Grant:
    if not isinstance(item, dict) or set(item) - _GRANT_KEYS or not {"plugin_id", "artifact_sha256",
                                                                     "permissions"} <= set(item):
        raise InstallRefused("install_unreadable")
    perms, signer = item["permissions"], item.get("signer_key_id")
    generation = item.get("order_after_revocation_generation")
    developer = item.get("developer_key_id")
    if (not isinstance(item["plugin_id"], str) or not isinstance(item["artifact_sha256"], str)
            or not isinstance(perms, list) or any(perm not in PERMISSIONS for perm in perms)
            or not (signer is None or (isinstance(signer, str) and _KEY_ID.fullmatch(signer) is not None))
            or not (developer is None or (isinstance(developer, str) and _KEY_ID.fullmatch(developer) is not None))
            or type(item.get("unsigned_order_acknowledged", False)) is not bool
            or not (item.get("order_after_revoked_key") is None or isinstance(item.get("order_after_revoked_key"), str))
            or not (generation is None or type(generation) is int)
            or not _plugin_id_ok(item["plugin_id"])):
        raise InstallRefused("install_unreadable")
    return Grant(item["plugin_id"], item["artifact_sha256"], signer, tuple(perms),
                 item.get("unsigned_order_acknowledged", False), item.get("order_after_revoked_key"), generation)


def _write_profile(directory: Path, snapshot: Snapshot) -> None:
    _write_file(directory / "install.json", _dump_install(snapshot))
    for plugin_id in sorted(snapshot.plugins):
        if not _plugin_id_ok(plugin_id):
            raise InstallRefused("plugin_directory_untrusted")
        plugin_dir = directory / plugin_id
        plugin_dir.mkdir(mode=0o700)
        os.chmod(plugin_dir, 0o700)
        plugin = snapshot.plugins[plugin_id]
        _write_file(plugin_dir / "manifest.json", plugin.manifest)
        _write_file(plugin_dir / "plugin.py", plugin.artifact)
        if plugin.signature is not None:
            _write_file(plugin_dir / "manifest.sig", plugin.signature)
        _sync_dir(plugin_dir)
    _sync_dir(directory)


def _dump_install(snapshot: Snapshot) -> bytes:
    payload = {
        "authority_anchor": snapshot.anchor,
        "grants": [_grant_json(snapshot.grants[plugin_id], snapshot.developer_key_ids)
                   for plugin_id in sorted(snapshot.grants)],
        "registry_labels": snapshot.labels,
        "reserved": {"credential_prefixes": list(snapshot.prefixes), "hosts": list(snapshot.hosts)},
        "trusted_keys": snapshot.trusted_keys,
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _grant_json(grant: Grant, developer_key_ids: dict) -> dict:
    data = {
        "plugin_id": grant.plugin_id,
        "artifact_sha256": grant.artifact_sha256,
        "signer_key_id": grant.signer_key_id,
        "permissions": list(grant.permissions),
        "unsigned_order_acknowledged": bool(grant.unsigned_order_acknowledged),
        "order_after_revoked_key": grant.order_after_revoked_key,
        "order_after_revocation_generation": grant.order_after_revocation_generation,
    }
    if grant.plugin_id in developer_key_ids:
        data["developer_key_id"] = developer_key_ids[grant.plugin_id]
    return data


def _write_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_dir(path: Path) -> None:
    """Sync a directory, so the names written or renamed in it are on disk."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_replace(directory: Path, name: str, data: bytes) -> None:
    temporary = directory / f".{name}.tmp"
    if _lexists(temporary):
        _remove_at(temporary)
    _write_file(temporary, data)
    os.rename(temporary, directory / name)
    _sync_dir(directory)


def _read_file(path: Path, limit: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise InstallRefused("plugin_file_untrusted", detail=type(exc).__name__) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise InstallRefused("plugin_file_untrusted")
        if info.st_size > limit:
            raise InstallRefused("plugin_file_too_large")
        data = _read_all(fd, limit)
    finally:
        os.close(fd)
    return data


def _read_all(fd: int, limit: int) -> bytes:
    chunks = []
    size = 0
    while size <= limit:
        chunk = os.read(fd, min(1 << 20, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    if size > limit:
        raise InstallRefused("plugin_file_too_large")
    return b"".join(chunks)


def _require_owned_dir(path: Path, code: str = "plugin_directory_untrusted") -> None:
    try:
        info = os.lstat(path)
    except OSError:
        raise InstallRefused(code) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InstallRefused(code)
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise InstallRefused(code)


def _remove_at(path: Path) -> None:
    """Remove a file, a symlink or a directory. A symlink is unlinked, not followed."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        os.unlink(path)
        return
    for name in os.listdir(path):
        _remove_at(path / name)
    os.rmdir(path)
