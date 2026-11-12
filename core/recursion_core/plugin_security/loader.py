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
import importlib.util
import os
import sys
import threading
import types
from dataclasses import dataclass, replace

from .errors import Refused
from .manifest import Grant, Manifest, ReservedEffects, TrustStore, effective_permissions, parse_manifest
from .signing import key_id
from .runtime import GilProbe

__all__ = ["CoreHealth", "LoadedPlugin", "PluginLoader"]

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
    manifest_raw: bytes = b""
    # SHA-256 of the bytes that were compiled. Signed trust belongs to this digest.
    code_sha256: str = ""


def _finding(stage: str, code: str, severity: str, detail: str = "") -> dict:
    return {"stage": stage, "code": code, "severity": severity, "detail": detail}


def _module_name(plugin_id: str) -> str:
    """One import name per plugin id.

    Replacing both ``.`` and ``-`` with ``_`` mapped ``ab.cd``, ``ab-cd`` and
    ``ab_cd`` onto ``csl_plugin_ab_cd``. A plugin loaded earlier could plant
    ``sys.modules[__name__]`` and the one loaded later would import it.
    """
    encoded = plugin_id.replace("_", "__").replace(".", "_d").replace("-", "_h")
    return "csl_plugin_" + encoded


def _module_origins(module: object) -> tuple[str, ...]:
    if module is None:
        return ()
    origins: list[str] = []
    file_origin = getattr(module, "__file__", None)
    if type(file_origin) is str and file_origin:
        origins.append(file_origin)
    spec = getattr(module, "__spec__", None)
    spec_origin = getattr(spec, "origin", None) if spec is not None else None
    if type(spec_origin) is str and spec_origin and spec_origin not in origins:
        origins.append(spec_origin)
    return tuple(origins)


def _under_plugin_root(module: object, root: str | None) -> bool:
    if type(root) is not str or not root:
        return False
    try:
        root_real = os.path.realpath(root)
    except (OSError, ValueError):
        return False
    for origin in _module_origins(module):
        try:
            if os.path.commonpath([root_real, os.path.realpath(origin)]) == root_real:
                return True
        except (OSError, ValueError):
            continue
    return False


class _ImportRecord:
    """Modules the import system created during one load.

    ``sys.modules`` stays the interpreter dictionary. Replacing it leaves
    imports consulting the orphaned dictionary, so a name removed only from
    the replacement is still treated as loaded.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.created: dict[str, object] = {}
        self.recording = False

    def remember(self, name: str, module: object) -> None:
        with self._lock:
            if self.recording and name not in self.created:
                self.created[name] = module

    def reset(self) -> None:
        with self._lock:
            self.created.clear()
            self.recording = True

    def stop(self) -> dict[str, object]:
        with self._lock:
            self.recording = False
            found = dict(self.created)
            self.created.clear()
            return found


_imports = _ImportRecord()
_import_finding = threading.local()


def _finding_names() -> set[str]:
    names = getattr(_import_finding, "names", None)
    if names is None:
        names = set()
        _import_finding.names = names
    return names


class _RecordingLoader:
    """Run the real loader and remember the module object it was given."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def create_module(self, spec):
        create = getattr(self._inner, "create_module", None)
        if create is None:
            return None
        return create(spec)

    def exec_module(self, module) -> None:
        name = getattr(module, "__name__", None)
        if type(name) is str:
            _imports.remember(name, module)
        if getattr(module, "__loader__", None) is self:
            try:
                module.__loader__ = self._inner
            except (AttributeError, TypeError):
                pass
        spec = getattr(module, "__spec__", None)
        if spec is not None and getattr(spec, "loader", None) is self:
            spec.loader = self._inner
        exec_module = getattr(self._inner, "exec_module", None)
        if exec_module is None:
            raise ImportError("loader has no exec_module")
        exec_module(module)

    def load_module(self, fullname: str):
        module = self._inner.load_module(fullname)
        current = sys.modules.get(fullname, module)
        _imports.remember(fullname, current if current is not None else module)
        return module

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class _RecordingFinder:
    """Meta-path finder that records modules created while a load is executing."""

    def find_spec(self, fullname, path, target=None):
        finding = _finding_names()
        if not _imports.recording or fullname in finding:
            return None
        finding.add(fullname)
        try:
            for finder in [item for item in sys.meta_path if item is not self]:
                find = getattr(finder, "find_spec", None)
                if find is None:
                    continue
                spec = find(fullname, path, target)
                if spec is None:
                    continue
                loader = getattr(spec, "loader", None)
                if loader is not None and not isinstance(loader, _RecordingLoader):
                    spec.loader = _RecordingLoader(loader)
                return spec
            return None
        finally:
            finding.discard(fullname)


_recording_finder = _RecordingFinder()


def _begin_import_record() -> None:
    _imports.reset()
    if _recording_finder not in sys.meta_path:
        sys.meta_path.insert(0, _recording_finder)


def _end_import_record() -> dict[str, object]:
    try:
        sys.meta_path.remove(_recording_finder)
    except ValueError:
        pass
    return _imports.stop()


def _origin_file(origin: object) -> str | None:
    if type(origin) is not str or not origin or origin in ("built-in", "frozen"):
        return None
    try:
        real = os.path.realpath(origin)
    except (OSError, ValueError):
        return None
    if os.path.isfile(real):
        return real
    return None


def _origins_match(module_origin: object, found_origin: object) -> bool:
    if type(module_origin) is not str or type(found_origin) is not str:
        return False
    if module_origin in ("built-in", "frozen") or found_origin in ("built-in", "frozen"):
        return module_origin == found_origin
    left = _origin_file(module_origin)
    right = _origin_file(found_origin)
    return left is not None and left == right


def _find_real_spec(name: str, path_before: list, meta_before: list):
    """``find_spec`` without this load's ``sys.path`` or meta-path additions.

    The candidate is removed first. ``find_spec`` otherwise returns the
    ``__spec__`` already stored under that name.
    """
    saved_path = sys.path[:]
    saved_meta = list(sys.meta_path)
    had = name in sys.modules
    saved = sys.modules.get(name) if had else None
    if had:
        try:
            del sys.modules[name]
        except KeyError:
            had = False
    try:
        _restore_sequence(sys, "path", path_before)
        _restore_sequence(sys, "meta_path", meta_before)
        return importlib.util.find_spec(name)
    except (ImportError, ValueError, AttributeError, TypeError, KeyError):
        return None
    finally:
        _restore_sequence(sys, "path", saved_path)
        _restore_sequence(sys, "meta_path", saved_meta)
        if had and name not in sys.modules:
            sys.modules[name] = saved


def _genuine_import(name: object, module: object, root: str | None, path_before: list,
                    meta_before: list, created: dict) -> tuple[bool, object]:
    """The import-created module, when its file is the one ``find_spec`` names.

    The file is resolved with this load's ``sys.path`` additions and meta-path
    additions removed, and it must not lie under the plugin root. A name under
    ``recursion_core.*`` or ``csl_plugin_*`` stays only on that same rule.
    The second result is the object the import system created.
    """
    if type(name) is not str:
        return False, module
    recorded = created.get(name)
    if recorded is None:
        return False, module
    module = recorded
    spec = getattr(module, "__spec__", None)
    if type(spec) is not importlib.machinery.ModuleSpec:
        return False, module
    if spec.name != name or getattr(module, "__name__", None) != name:
        return False, module
    if _under_plugin_root(module, root):
        return False, module
    found = _find_real_spec(name, path_before, meta_before)
    if type(found) is not importlib.machinery.ModuleSpec:
        return False, module
    return _origins_match(spec.origin, found.origin), module


def _restore_sequence(owner: object, attr: str, snapshot: list) -> None:
    """Put ``attr`` back. Slice assignment keeps the list object other code holds."""
    current = getattr(owner, attr, None)
    try:
        current[:] = list(snapshot)
    except (AttributeError, TypeError, ValueError):
        setattr(owner, attr, list(snapshot))


def _restore_sys_modules(before: dict, root: str | None, path_before: list, meta_before: list,
                         created: dict) -> None:
    """Put back entries this load replaced, then drop names it planted.

    A name that was not in the snapshot stays only when the import system
    created it and its origin is the file ``find_spec`` finds without this
    load's path additions. An assigned module is removed even when its
    ``__file__`` points outside the plugin root. ``sys.path`` and
    ``sys.meta_path`` return to the snapshot.
    """
    for name, module in before.items():
        if sys.modules.get(name) is not module:
            sys.modules[name] = module
    _restore_sequence(sys, "path", path_before)
    _restore_sequence(sys, "meta_path", meta_before)
    for name in list(sys.modules):
        if name in before:
            continue
        module = sys.modules.get(name)
        try:
            keep, recorded = _genuine_import(name, module, root, path_before, meta_before, created)
        except Exception:
            keep, recorded = False, module
        if keep:
            if recorded is not None and sys.modules.get(name) is not recorded:
                sys.modules[name] = recorded
        else:
            sys.modules.pop(name, None)


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

    def doctor(self, manifest_bytes: bytes, signature: object, artifact: bytes,
               registry_label: str | None = None, *, root: str | None = None) -> list[dict]:
        """Run verify and admit only (no code executes) and return every finding."""
        findings: list[dict] = []
        try:
            self._verify_admit(manifest_bytes, signature, artifact, registry_label, findings, root)
        except Refused as exc:
            findings.append(_finding(findings[-1]["stage"] if findings else "verify",
                                     exc.code, "refuse", exc.detail))
        return findings

    def _verify_admit(self, manifest_bytes, signature, artifact, registry_label, findings,
                      root: str | None = None):
        if type(artifact) is not bytes:
            raise Refused("artifact_not_bytes")
        manifest = parse_manifest(manifest_bytes)
        findings.append(_finding("verify", "manifest_ok", "info", manifest.plugin_id))
        if hashlib.sha256(artifact).hexdigest() != manifest.artifact_sha256:
            raise Refused("artifact_digest_mismatch")
        trust, kid, reason = self._trust.classify(manifest, signature)
        findings.append(_finding("verify", reason, "info" if trust == "signed" else "warn", trust))
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
        perms, dropped = effective_permissions(manifest, trust, kid, self._grants.get(manifest.plugin_id),
                                               revoked, generation)
        for code, detail in dropped:
            findings.append(_finding("admit", code, "warn", detail))
        for perm in ("process", "network", "credential"):
            if perm in perms:
                findings.append(_finding("admit", perm + "_granted", "warn",
                                         "in-process code is not confined to this grant"))
        if manifest.install_scripts:
            # A declared script is only admitted once it has been read from a root.
            # Without that root the name is never checked against the filesystem.
            if root is None:
                raise Refused("plugin_root_required")
            from .paths import read_within_root
            for script in manifest.install_scripts:
                read_within_root(root, script)
        if manifest.install_scripts:
            findings.append(_finding("admit", "install_scripts_present", "warn",
                                     "re-consent on change; scripts are not run by the loader"))
        if manifest.dependency_lock_sha256 is None:
            findings.append(_finding("admit", "no_dependency_lock", "warn"))
        findings.append(_finding("admit", "effective_permissions", "info", ",".join(perms)))
        return manifest, trust, kid, label, perms

    def load(self, manifest_bytes: bytes, signature: object, artifact: bytes,
             registry_label: str | None = None, *, root: str | None = None) -> LoadedPlugin:
        with self._lock:
            self._health.require_healthy()
            findings: list[dict] = []
            active: LoadedPlugin | None = None
            try:
                if type(artifact) is bytes:
                    try:
                        preview = parse_manifest(manifest_bytes)
                    except Refused:
                        preview = None
                    if preview is not None:
                        active = self._active.get(preview.plugin_id)
                        # Trust is for the bytes that were compiled. Different bytes with
                        # the same manifest are not that digest, so the recorded signature
                        # and its permissions no longer apply.
                        if active is not None and hashlib.sha256(artifact).hexdigest() != active.code_sha256:
                            findings.append(_finding("verify", "artifact_digest_mismatch", "refuse"))
                            cleared = replace(
                                active, trust="unsigned", signer_key_id=None, permissions=(),
                                findings=tuple(findings), manifest_raw=b"")
                            self._active[preview.plugin_id] = cleared
                            self.last_findings = cleared.findings
                            raise Refused("artifact_digest_mismatch", preview.plugin_id)
                manifest, trust, kid, label, perms = self._verify_admit(
                    manifest_bytes, signature, artifact, registry_label, findings, root)
                active = self._active.get(manifest.plugin_id)
                if active is not None and active.manifest_raw == manifest.raw and (
                        active.artifact_sha256 == manifest.artifact_sha256):
                    raise Refused("plugin_already_active", manifest.plugin_id)
                if active is None:
                    code = compile(artifact, "<plugin %s>" % manifest.plugin_id, "exec", dont_inherit=True)
                    findings.append(_finding("stage", "compiled", "info"))
            except (Refused, SyntaxError, ValueError) as exc:
                # No plugin code ran, but the rule is a GIL read after every load attempt.
                if self._gil_gate("refused_load", findings):
                    raise Refused("core_poisoned_gil_enabled", "refused_load") from exc
                self.last_findings = tuple(findings)
                if isinstance(exc, Refused):
                    raise
                raise Refused("plugin_compile_failed", type(exc).__name__) from None
            if active is not None:
                # The running module is the old artifact. A changed manifest is classified
                # again and replaces the recorded trust. A changed artifact is not executed;
                # the recorded trust is dropped because it no longer describes the bytes running.
                if self._gil_gate(manifest.plugin_id, findings):
                    raise Refused("core_poisoned_gil_enabled", manifest.plugin_id)
                if manifest.artifact_sha256 != active.artifact_sha256:
                    cleared = replace(
                        active, trust="unsigned", signer_key_id=None, permissions=(),
                        findings=tuple(findings) + (_finding("verify", "plugin_artifact_changed", "refuse"),),
                        manifest_raw=manifest.raw)
                    self._active[manifest.plugin_id] = cleared
                    self.last_findings = cleared.findings
                    raise Refused("plugin_artifact_changed", manifest.plugin_id)
                updated = replace(
                    active, trust=trust, signer_key_id=kid, registry_label=label, permissions=perms,
                    findings=tuple(findings), developer_key_id=key_id(manifest.developer_key),
                    manifest_raw=manifest.raw)
                self._active[manifest.plugin_id] = updated
                self.last_findings = updated.findings
                return updated
            return self._activate(manifest, trust, kid, label, perms, code, findings,
                                  hashlib.sha256(artifact).hexdigest(), root)

    def _gil_gate(self, name: str, findings: list) -> bool:
        """Read the GIL; if it is on, poison the core and drop every active plugin."""
        if not self._gil.gil_enabled():
            return False
        self._health.poison("gil_enabled_after_load:" + name)
        self._active.clear()
        findings.append(_finding("active", "gil_enabled_after_load", "refuse"))
        self.last_findings = tuple(findings)
        return True

    def _activate(self, manifest: Manifest, trust, kid, label, perms, code, findings,
                  code_sha256: str, root: str | None = None) -> LoadedPlugin:
        before = dict(sys.modules)
        path_before = list(sys.path)
        meta_before = list(sys.meta_path)
        module = types.ModuleType(_module_name(manifest.plugin_id))
        failure: BaseException | None = None
        gil_on = False
        created: dict = {}
        _begin_import_record()
        try:
            try:
                exec(code, module.__dict__)
            except BaseException as exc:  # noqa: BLE001 - the GIL check must follow any outcome
                failure = exc
            finally:
                for name in sorted(set(sys.modules) - set(before)):
                    origin = getattr(sys.modules.get(name), "__file__", None) or ""
                    if origin.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES)):
                        findings.append(_finding("active", "extension_module_loaded", "warn", name))
                gil_on = self._gil_gate(manifest.plugin_id, findings)
                self.last_findings = tuple(findings)
        finally:
            created = _end_import_record()
        # A refused load drops assigned modules and imports that exist only because
        # this load changed the path. A genuine import of a real file stays.
        if gil_on or failure is not None:
            _restore_sys_modules(before, root, path_before, meta_before, created)
        if gil_on:
            raise Refused("core_poisoned_gil_enabled", manifest.plugin_id) from failure
        if failure is not None:
            findings.append(_finding("active", "plugin_import_failed", "refuse", type(failure).__name__))
            self.last_findings = tuple(findings)
            raise Refused("plugin_import_failed", type(failure).__name__) from None
        findings.append(_finding("active", "active", "info", "in_process_not_contained"))
        loaded = LoadedPlugin(manifest.plugin_id, manifest.artifact_sha256, trust, kid, label,
                              perms, module, tuple(findings), key_id(manifest.developer_key),
                              manifest.raw, code_sha256)
        self._active[manifest.plugin_id] = loaded
        self.last_findings = loaded.findings
        return loaded
