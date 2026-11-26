"""In-process plugin loader (lane G2) with developer "doctor" findings per stage.

Stages, each reporting findings the developer sees (Q16 accept item 2):

* ``verify``: manifest bytes are canonical and well formed; the artifact bytes match the
  manifest's SHA-256; the signature is classified signed or unsigned against this
  install's trusted list. A bad signature is a finding, not a refusal (4.3).
* ``admit``: requested ∩ granted permissions, bound to digest and key; no reserved
  (effect-owner) credential or destination; process, network and install-script grants
  reported.
* ``stage``: the artifact compiles.
* ``active``: the artifact runs in the daemon's process. After *every* load, including
  one that raised, the loader reads ``sys._is_gil_enabled()``; if the GIL is on, the
  core is poisoned and refuses every later load and every effect until restart.

What this does **not** do: contain the plugin. Plugin code runs as ordinary Python in
the daemon's interpreter, with the daemon's uid. It can import ``gc``, walk to any core
object and rewrite it, replace functions, read memory, open files and sockets, and spawn
processes. Every check here binds honest plugins and records what happened; none of it
holds against hostile in-process code (fleet F35, P1, measures that; B17 tracks the
same-uid authority anchor). Nothing here spawns a process, so there is no cleanup path;
any later spawn must use cgroup plus pidfd, never ``preexec_fn`` or ``PR_SET_PDEATHSIG``.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import os
import stat
import sys
import threading
import types
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import Refused
from .manifest import (
    ENTRY_FILE, RUNTIME_INPROCESS_RETIRING, Grant, Manifest, ReservedEffects, TrustStore,
    _bound_developer_key, effective_permissions, parse_manifest,
)
from .signing import key_id
from .runtime import GilProbe

__all__ = ["CoreHealth", "LoadedPlugin", "PluginLoader"]

_RUNTIME_SANDBOXED = "python-sandboxed/1"

PACKAGE_FILE_MISSING_TEXT = ("%s is listed in the signed manifest but is not a regular file in the plugin's folder: "
                             "install the package again from its source, or ask its developer for a whole package.")
PACKAGE_FOLDER_NOT_A_FOLDER_TEXT = ("the plugin's folder is missing, is a symbolic link or is not a folder, and the loader "
                                    "follows no link in a package, so the plugin was not loaded and none of its code ran: "
                                    "give the folder itself, or install the package again from its source.")
GRANT_UNBOUND_TEXT = ("The grant for this plugin in install.json names no developer key ID (developer_key_id), so the "
                      "plugin was not loaded and none of its code ran. Make the grant again with developer_key_id set "
                      "to this plugin's developer key ID, then load the plugin again.")


class CoreHealth:
    """Shared poison flag. Once poisoned, only a daemon restart clears it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reason: str | None = None

    def poison(self, reason: str) -> None:
        with self._lock:
            if self._reason is None:
                self._reason = reason

    @property
    def poisoned(self) -> str | None:
        return self._reason

    def require_healthy(self) -> None:
        if self._reason is not None:
            raise Refused("core_poisoned_restart_required", self._reason)


@dataclass(frozen=True)
class LoadedPlugin:
    plugin_id: str
    artifact_sha256: str
    trust: str
    signer_key_id: str | None
    registry_label: str
    permissions: tuple
    module: types.ModuleType
    findings: tuple
    developer_key_id: str | None = None


def _finding(stage: str, code: str, severity: str, detail: str = "") -> dict:
    return {"stage": stage, "code": code, "severity": severity, "detail": detail}


# Where the system opens a folder relative to another without following a link (POSIX), the walk holds
# each folder open, so a folder swapped for a link between two steps is not followed either.
_WALK_BY_FD = (os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
               and os.stat in os.supports_follow_symlinks
               and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"))


def _package_folder_present(directory: Path) -> bool:
    """True when the package folder itself is a folder, not a symbolic link."""
    try:
        return stat.S_ISDIR(directory.lstat().st_mode)
    except OSError:
        return False


def _package_file_present(directory: Path, path: str) -> bool:
    """Check a listed path for a regular file, following no symbolic link: not the package folder
    itself and none within it.

    With ``_WALK_BY_FD`` each folder is opened relative to the one before it with ``O_NOFOLLOW``
    and held open, so a folder replaced by a link between two steps is not followed. Elsewhere
    each step is an ``lstat`` of the path, the package folder included; a folder replaced between
    two of them is then the caller's to rule out (the daemon loads only an operator-owned folder
    that is not group or other writable).
    """
    *parents, name = path.split("/")
    if _WALK_BY_FD:
        return _package_file_present_by_fd(directory, parents, name)
    if not _package_folder_present(directory):
        return False
    try:
        for part in parents:
            directory = directory / part
            if not stat.S_ISDIR(directory.lstat().st_mode):
                return False
        return stat.S_ISREG((directory / name).lstat().st_mode)
    except OSError:
        return False


def _package_file_present_by_fd(directory: Path, parents: list, name: str) -> bool:
    flags = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NONBLOCK", 0))
    opened: list[int] = []
    try:
        opened.append(os.open(directory, flags))
        for part in parents:
            opened.append(os.open(part, flags, dir_fd=opened[-1]))
        return stat.S_ISREG(os.stat(name, dir_fd=opened[-1], follow_symlinks=False).st_mode)
    except OSError:
        return False
    finally:
        for fd in opened:
            os.close(fd)


class PluginLoader:
    def __init__(self, trust: TrustStore, grants: dict[str, Grant], reserved: ReservedEffects,
                 health: CoreHealth, gil_probe: GilProbe | None = None) -> None:
        self._trust = trust
        self._grants = dict(grants)
        self._reserved = reserved
        self._health = health
        self._gil = gil_probe if gil_probe is not None else GilProbe()
        self._lock = threading.Lock()
        self._active: dict[str, LoadedPlugin] = {}
        self.last_findings: tuple = ()

    def permissions(self, plugin_id: str) -> tuple:
        plugin = self._active.get(plugin_id)
        return () if plugin is None or self._health.poisoned else plugin.permissions

    def revoke_key(self, kid: str) -> list[str]:
        """Revoke a developer key now (PRODUCT.md 4.4) and return the plugin ids it turned off.

        Later loads treat the key as unsigned. Every active plugin whose manifest names that
        developer key loses ``order`` at once, signed or manually granted, so a stolen key
        cannot keep trading until the next restart. Its other grants stay (O12 open).
        Turning order back on is the user's step: a fresh grant that names the revoked key in
        ``order_after_revoked_key``, then a load; never this call. The first revocation is
        generation 1, and naming the key is enough for that one. Each later call advances
        the generation, and the new grant has to set ``order_after_revocation_generation``
        to the value ``revoke`` stored. The revocation is stored
        durably (``TrustStore``), so a restart keeps the key revoked, and every later load
        keeps order off for a grant that does not cite the current generation, signed or
        manual, on the trusted list or not. Order is turned off before the durable write,
        so if that write fails the running plugins are already off and
        ``revocation_not_durable`` is raised.
        """
        if type(kid) is not str or not kid.startswith("ed25519:"):
            raise Refused("invalid_key_id")
        with self._lock:
            turned_off = self._order_off(kid)
            self._trust.revoke(kid)
            return turned_off

    def revocation_generation(self, kid: str) -> int | None:
        return self._trust.generation(kid)

    def _order_off(self, kid: str) -> list[str]:
        turned_off = []
        for plugin_id, plugin in sorted(self._active.items()):
            if plugin.developer_key_id == kid and "order" in plugin.permissions:
                perms = tuple(p for p in plugin.permissions if p != "order")
                finding = _finding("active", "signer_key_revoked_order_off", "warn", kid)
                self._active[plugin_id] = replace(plugin, permissions=perms,
                                                  findings=plugin.findings + (finding,))
                turned_off.append(plugin_id)
        return turned_off

    def artifact(self, plugin_id: str) -> str | None:
        plugin = self._active.get(plugin_id)
        return None if plugin is None else plugin.artifact_sha256

    def doctor(self, manifest_bytes: bytes, signature: object, artifact: bytes | None,
               registry_label: str | None = None, *, package_dir: Path | None = None) -> list[dict]:
        """Run verify and admit only (no code executes) and return every finding."""
        findings: list[dict] = []
        try:
            self._verify_admit(manifest_bytes, signature, artifact, registry_label, findings, package_dir)
        except Refused as exc:
            findings.append(_finding(findings[-1]["stage"] if findings else "verify",
                                     exc.code, "refuse", exc.detail))
        return findings

    def _verify_admit(self, manifest_bytes, signature, artifact, registry_label, findings, package_dir):
        if isinstance(package_dir, (str, os.PathLike)):
            package_dir = Path(package_dir)
        # The daemon can supply None for a missing entry so a signed manifest can name it.
        if type(artifact) is not bytes and (artifact is not None or package_dir is None):
            raise Refused("artifact_not_bytes")
        manifest = parse_manifest(manifest_bytes)
        findings.append(_finding("verify", "manifest_ok", "info", manifest.plugin_id))
        if manifest.runtime == RUNTIME_INPROCESS_RETIRING:
            findings.append(_finding("verify", "runtime_inprocess_retiring", "warn",
                                     "python-inprocess/1 retires and is refused from 1.1.0; until then it runs "
                                     "the same way as python-sandboxed/1, in the daemon's own process"))
        if manifest.runtime == _RUNTIME_SANDBOXED:
            # The host that contains a python-sandboxed/1 plugin is not in this build. The warning only says
            # so: no manifest field changes it, and it changes no verdict.
            findings.append(_finding("verify", "runtime_not_yet_contained", "warn",
                                     "python-sandboxed/1 is not contained in this build: the plugin runs in "
                                     "the daemon's own process, with the daemon's rights, until the host that "
                                     "contains it is built"))
        if artifact is not None and hashlib.sha256(artifact).hexdigest() != manifest.artifact_sha256:
            raise Refused("artifact_digest_mismatch")
        trust, kid, reason = self._trust.classify(manifest, signature)
        findings.append(_finding("verify", reason, "info" if trust == "signed" else "warn", trust))
        if trust == "signed":
            if package_dir is not None and not _package_folder_present(package_dir):
                raise Refused("package_file_missing", PACKAGE_FOLDER_NOT_A_FOLDER_TEXT)
            for path, _ in manifest.files:
                # Bytes alone can supply the entry. With a directory, require the listed file there too.
                if package_dir is None:
                    present = path == ENTRY_FILE and artifact is not None
                else:
                    present = (path != ENTRY_FILE or artifact is not None) and _package_file_present(package_dir, path)
                if not present:
                    raise Refused("package_file_missing", PACKAGE_FILE_MISSING_TEXT % path)
        if artifact is None:
            raise Refused("plugin_file_missing", ENTRY_FILE)
        label = registry_label if registry_label in ("verified", "non_verified") else "unlisted"
        findings.append(_finding("verify", "registry_label", "info",
                                 label + " (a label, not a gate)"))
        for dest in manifest.network_destinations:
            if self._reserved.host_reserved(dest):
                raise Refused("reserved_destination", dest)
        for ref in manifest.credential_refs:
            if self._reserved.credential_reserved(ref):
                raise Refused("reserved_credential", ref)
        dev_kid = key_id(manifest.developer_key)
        revoked = dev_kid if dev_kid in self._trust.revoked else None
        generation = None if revoked is None else self._trust.generation(dev_kid)
        grant = self._grants.get(manifest.plugin_id)
        perms, dropped = effective_permissions(manifest, trust, kid, grant, revoked, generation)
        for code, detail in dropped:
            findings.append(_finding("admit", code, "warn", detail))
        # A manual grant bound to no developer key, signed manifest or not: refused before any code runs.
        if (grant is not None and grant.signer_key_id is None
                and _bound_developer_key(grant) is None and manifest.permissions and not perms):
            raise Refused("grant_unbound", GRANT_UNBOUND_TEXT)
        for perm in ("process", "network", "credential"):
            if perm in perms:
                findings.append(_finding("admit", perm + "_granted", "warn",
                                         "in-process code is not confined to this grant"))
        if manifest.install_scripts:
            findings.append(_finding("admit", "install_scripts_present", "warn",
                                     "re-consent on change; scripts are not run by the loader"))
        if manifest.dependency_lock_sha256 is None:
            findings.append(_finding("admit", "no_dependency_lock", "warn"))
        findings.append(_finding("admit", "effective_permissions", "info", ",".join(perms)))
        return manifest, trust, kid, label, perms

    def load(self, manifest_bytes: bytes, signature: object, artifact: bytes | None,
             registry_label: str | None = None, *, package_dir: Path | None = None) -> LoadedPlugin:
        """Load an entry artifact and check signed manifests' listed files in package_dir.

        Entry bytes supply the entry file when no directory is given. package_dir itself must be
        a folder, not a symbolic link, and no link inside it is followed.
        """
        with self._lock:
            self._health.require_healthy()
            findings: list[dict] = []
            try:
                manifest, trust, kid, label, perms = self._verify_admit(
                    manifest_bytes, signature, artifact, registry_label, findings, package_dir)
                if manifest.plugin_id in self._active:
                    raise Refused("plugin_already_active", manifest.plugin_id)
                code = compile(artifact, "<plugin %s>" % manifest.plugin_id, "exec", dont_inherit=True)
                findings.append(_finding("stage", "compiled", "info"))
            except (Refused, SyntaxError, ValueError, RecursionError, MemoryError) as exc:
                # No plugin code ran, but the rule is a GIL read after every load attempt.
                if self._gil_gate("refused_load", findings):
                    raise Refused("core_poisoned_gil_enabled", "refused_load") from exc
                self.last_findings = tuple(findings)
                if isinstance(exc, Refused):
                    raise
                raise Refused("plugin_compile_failed", type(exc).__name__) from None
            return self._activate(manifest, trust, kid, label, perms, code, findings)

    def _gil_gate(self, name: str, findings: list) -> bool:
        """Read the GIL; if it is on, poison the core and drop every active plugin."""
        if not self._gil.gil_enabled():
            return False
        self._health.poison("gil_enabled_after_load:" + name)
        self._active.clear()
        findings.append(_finding("active", "gil_enabled_after_load", "refuse"))
        self.last_findings = tuple(findings)
        return True

    def _activate(self, manifest: Manifest, trust, kid, label, perms, code, findings) -> LoadedPlugin:
        before = set(sys.modules)
        module = types.ModuleType("csl_plugin_" + manifest.plugin_id.replace(".", "_").replace("-", "_"))
        failure: BaseException | None = None
        try:
            exec(code, module.__dict__)
        except BaseException as exc:  # noqa: BLE001 - the GIL check must follow any outcome
            failure = exc
        finally:
            for name in sorted(set(sys.modules) - before):
                origin = getattr(sys.modules.get(name), "__file__", None) or ""
                if origin.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES)):
                    findings.append(_finding("active", "extension_module_loaded", "warn", name))
            gil_on = self._gil_gate(manifest.plugin_id, findings)
            self.last_findings = tuple(findings)
        if gil_on:
            raise Refused("core_poisoned_gil_enabled", manifest.plugin_id) from failure
        if failure is not None:
            findings.append(_finding("active", "plugin_import_failed", "refuse", type(failure).__name__))
            self.last_findings = tuple(findings)
            raise Refused("plugin_import_failed", type(failure).__name__) from None
        findings.append(_finding("active", "active", "info", "in_process_not_contained"))
        loaded = LoadedPlugin(manifest.plugin_id, manifest.artifact_sha256, trust, kid, label,
                              perms, module, tuple(findings), key_id(manifest.developer_key))
        self._active[manifest.plugin_id] = loaded
        self.last_findings = loaded.findings
        return loaded
