"""In-process plugin loader (lane G2) with developer "doctor" findings per stage.

Stages, each reporting findings the developer sees (Q16 accept item 2):

* ``verify``: manifest bytes are canonical and well formed; the artifact bytes match the
  manifest's SHA-256; the signature is classified signed or unsigned against this
  install's trusted list. A bad signature is a finding, not a refusal (4.3); a plugin
  that the finding leaves with no permission (a grant bound to the signer it lacks) is
  then refused ``plugin_not_granted`` at ``admit``.
* ``admit``: requested ∩ granted permissions, bound to digest and key; no reserved
  (effect-owner) credential or destination; process, network and install-script grants
  reported. A plugin left with no effective permission is refused here,
  ``plugin_not_granted``: its bytes are never compiled and never run (no grant, a grant
  for another digest or signer, or nothing it asked for granted).
* ``stage``: the artifact compiles.
* ``active``: the artifact runs in the daemon's process. After *every* load, including
  one that raised, the loader reads ``sys._is_gil_enabled()``; if the GIL is on, the
  core is poisoned and refuses every later load and every effect until restart. What the
  loader reads around that GIL read (the new entries of ``sys.modules``, the name of the
  exception the import raised) it reads as plain data, so none of it runs plugin code or
  raises past the GIL read (``type_name``, ``plain_modules``).

What runs before a grant is checked: only the host's own code. The manifest bytes are
parsed as canonical JSON, the artifact's SHA-256 is compared with the manifest, the
signature is checked against this install's trusted keys and the revoked list, and the
grant is read. No byte of the plugin is compiled or executed before ``admit`` has given
it at least one permission.

Locks. Loads are serial (``_load_lock``, held while plugin code runs). The loader's state
(the active plugins, and a revocation's effect on them) has its own lock, ``_lock``, which
is never held while plugin code runs, so ``revoke_key`` never waits for a plugin's import.
A key revoked while a plugin under it is importing turns that plugin's ``order`` off before
it becomes active, as for a plugin that was already active.

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
import sys
import threading
import types
from dataclasses import dataclass, replace

from .errors import Refused
from .manifest import Grant, Manifest, ReservedEffects, TrustStore, effective_permissions, parse_manifest
from .signing import key_id
from .runtime import GilProbe

__all__ = ["CoreHealth", "LoadedPlugin", "NOT_GRANTED", "PluginLoader"]

# The typed refusal of a plugin left with no effective permission (R4 finding 7, PLUGIN-HOST PR 1).
NOT_GRANTED = "plugin_not_granted"

# Reading what a plugin left behind without running its code (review 3 of #380, GPT-6.1 Sol's
# required 2). Around and after a GIL read the host names a plugin's exception, scans
# ``sys.modules`` and reads the plugin module's hooks. Done the ordinary way, each can call code the
# plugin defined: a metaclass's ``__getattribute__``, a ``str`` subclass's ``__hash__``, ``__eq__``
# or ``__format__``, an object's ``__getattr__``. These read the interpreter's own slots and walk
# dictionaries' tables instead, so they call none of it and raise nothing a plugin chose.
_CLASS_NAME = type.__dict__["__name__"].__get__               # a type's own name slot
_MODULE_DICT = types.ModuleType.__dict__["__dict__"].__get__  # a module's own namespace dict


def type_name(value) -> str:
    """The name of ``value``'s class, as a plain ``str``, read without running plugin code.

    ``type(value).__name__`` goes through the class's metaclass, and a class's name can be a
    ``str`` subclass whose formatting is plugin code: this reads the slot and copies the text.
    """
    return "".join((_CLASS_NAME(type(value)),))


def plain_items(mapping) -> list:
    """The ``(key, value)`` pairs of a dict whose key is a plain ``str``, read without running plugin code.

    Walks the dict's own table (``dict.items``), so no key is hashed or compared and a dict
    subclass's ``items`` is not called. ``TypeError`` if ``mapping`` is not a dict; ``RuntimeError``
    if another thread resizes it meanwhile.
    """
    return [(key, value) for key, value in list(dict.items(mapping)) if type(key) is str]


def plain_modules() -> dict:
    """``sys.modules`` as ``{name: module}`` for its plain-``str`` names, read without running plugin code.

    Empty if ``sys.modules`` is not a dict, or if other threads kept importing through three reads.
    """
    for _ in range(3):
        try:
            return dict(plain_items(sys.modules))
        except RuntimeError:  # another thread imported meanwhile: read again
            continue
        except TypeError:  # sys.modules was rebound to something that is not a dict
            return {}
    return {}


def module_entry(module, name: str):
    """``module.__dict__[name]`` for a plain-``str`` key, else ``None``, read without running plugin code.

    Read through the module type's own slot, so a module whose ``__class__`` was changed to a
    subclass is read the same way. ``TypeError`` if ``module`` is not a module.
    """
    for key, value in plain_items(_MODULE_DICT(module)):
        if key == name:
            return value
    return None


def _module_file(module) -> str:
    """A module's ``__file__`` if it is a plain ``str``, else ``""``; never raises for what a plugin put there."""
    if not issubclass(type(module), types.ModuleType):
        return ""
    try:
        origin = module_entry(module, "__file__")
    except RuntimeError:  # the module's namespace changed while it was read
        return ""
    return origin if type(origin) is str else ""


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


class PluginLoader:
    def __init__(self, trust: TrustStore, grants: dict[str, Grant], reserved: ReservedEffects,
                 health: CoreHealth, gil_probe: GilProbe | None = None) -> None:
        self._trust = trust
        self._grants = dict(grants)
        self._reserved = reserved
        self._health = health
        self._gil = gil_probe if gil_probe is not None else GilProbe()
        self._load_lock = threading.Lock()  # loads are serial; held while plugin code runs
        self._lock = threading.Lock()       # the active set and revocations; never held while plugin code runs
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

        This call never waits for a plugin's import: a load holds ``_lock`` only around its
        checks and its activation, never while plugin code runs. A plugin under the key that
        is importing when this is called becomes active without ``order``.
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
               registry_label: str | None = None) -> list[dict]:
        """Run verify and admit only (no code executes) and return every finding."""
        findings: list[dict] = []
        try:
            self._verify_admit(manifest_bytes, signature, artifact, registry_label, findings)
        except Refused as exc:
            findings.append(_finding(findings[-1]["stage"] if findings else "verify",
                                     exc.code, "refuse", exc.detail))
        return findings

    def _verify_admit(self, manifest_bytes, signature, artifact, registry_label, findings):
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
            findings.append(_finding("admit", "install_scripts_present", "warn",
                                     "re-consent on change; scripts are not run by the loader"))
        if manifest.dependency_lock_sha256 is None:
            findings.append(_finding("admit", "no_dependency_lock", "warn"))
        findings.append(_finding("admit", "effective_permissions", "info", ",".join(perms)))
        if not perms:
            # The pre-execution refusal: nothing of the plugin is compiled or run without a permission.
            raise Refused(NOT_GRANTED, "no effective permission; the plugin's code was not compiled or run")
        return manifest, trust, kid, label, perms

    def load(self, manifest_bytes: bytes, signature: object, artifact: bytes,
             registry_label: str | None = None) -> LoadedPlugin:
        with self._load_lock:
            # This load's findings only: a refusal before verify (a poisoned core) must not report the
            # previous plugin's findings and id (review 1 of #380, DeepSeek F4).
            self.last_findings = ()
            self._health.require_healthy()
            findings: list[dict] = []
            try:
                with self._lock:
                    manifest, trust, kid, label, perms = self._verify_admit(
                        manifest_bytes, signature, artifact, registry_label, findings)
                    if manifest.plugin_id in self._active:
                        raise Refused("plugin_already_active", manifest.plugin_id)
                    # What a revocation of this developer key looked like when the grant was read;
                    # compared again at activation (a revocation during the import turns order off).
                    developer = key_id(manifest.developer_key)
                    generation = self._trust.generation(developer)
                code = compile(artifact, "<plugin %s>" % manifest.plugin_id, "exec", dont_inherit=True)
                findings.append(_finding("stage", "compiled", "info"))
            except (Refused, SyntaxError, ValueError, RecursionError, MemoryError) as exc:
                # No plugin code ran, but the rule is a GIL read after every load attempt.
                with self._lock:
                    gil_on = self._gil_gate("refused_load", findings)
                    self.last_findings = tuple(findings)
                if gil_on:
                    raise Refused("core_poisoned_gil_enabled", "refused_load") from exc
                if isinstance(exc, Refused):
                    raise
                raise Refused("plugin_compile_failed", type(exc).__name__) from None
            return self._activate(manifest, trust, kid, label, perms, code, findings, developer, generation)

    def _gil_gate(self, name: str, findings: list) -> bool:
        """Read the GIL; if it is on, poison the core and drop every active plugin. Call under ``_lock``."""
        if not self._gil.gil_enabled():
            return False
        self._health.poison("gil_enabled_after_load:" + name)
        self._active.clear()
        findings.append(_finding("active", "gil_enabled_after_load", "refuse"))
        self.last_findings = tuple(findings)
        return True

    def _activate(self, manifest: Manifest, trust, kid, label, perms, code, findings,
                  developer: str, generation: int | None) -> LoadedPlugin:
        before = set(plain_modules())
        module = types.ModuleType("csl_plugin_" + manifest.plugin_id.replace(".", "_").replace("-", "_"))
        failure: BaseException | None = None
        try:
            exec(code, module.__dict__)  # plugin code: _load_lock is held, _lock is not
        except BaseException as exc:  # noqa: BLE001 - the GIL check must follow any outcome
            failure = exc
        finally:
            try:
                # Plain data only: nothing a plugin put in sys.modules runs here, and nothing raised
                # here skips the GIL read below (review 3 of #380: an object in sys.modules whose
                # __getattr__ raised ended the load here, with the GIL never read).
                fresh = plain_modules()
                for name in sorted(set(fresh) - before):
                    if _module_file(fresh[name]).endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES)):
                        findings.append(_finding("active", "extension_module_loaded", "warn", name))
            finally:
                with self._lock:
                    gil_on = self._gil_gate(manifest.plugin_id, findings)
                    self.last_findings = tuple(findings)
        if gil_on:
            raise Refused("core_poisoned_gil_enabled", manifest.plugin_id) from failure
        if failure is not None:
            findings.append(_finding("active", "plugin_import_failed", "refuse", type_name(failure)))
            self.last_findings = tuple(findings)
            raise Refused("plugin_import_failed", type_name(failure)) from None
        with self._lock:
            if "order" in perms and self._trust.generation(developer) != generation:
                # revoke_key ran while this plugin was importing: the same rule as for an active plugin.
                perms = tuple(p for p in perms if p != "order")
                findings.append(_finding("active", "signer_key_revoked_order_off", "warn", developer))
            findings.append(_finding("active", "active", "info", "in_process_not_contained"))
            loaded = LoadedPlugin(manifest.plugin_id, manifest.artifact_sha256, trust, kid, label,
                                  perms, module, tuple(findings), developer)
            self._active[manifest.plugin_id] = loaded
            self.last_findings = loaded.findings
        return loaded
