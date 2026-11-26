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
PACKAGE_FILE_DIGEST_MISMATCH_TEXT = ("%s is listed in the signed manifest, but its SHA-256 is not the one the manifest "
                                     "names, so the plugin was not loaded and none of its code ran: install the package "
                                     "again from its source, or ask its developer for a package that matches its manifest.")
PACKAGE_FILE_UNTRUSTED_TEXT = ("%s is not owned by the user the daemon runs as, or a group or other users can write to "
                               "it, so the plugin was not loaded and none of its code ran: make it yours and writable "
                               "by you alone (chmod go-w), then load the plugin again.")
PACKAGE_FOLDER_UNREADABLE_TEXT = ("%s could not be read (%s), so the loader could not see that every file there is "
                                  "listed and checked; the plugin was not loaded and none of its code ran: make the "
                                  "folder readable by the user the daemon runs as, then load the plugin again.")
PACKAGE_FILE_UNLISTED_TEXT = ("%s is in the plugin's folder but the signed manifest does not list it, and Python could "
                              "import it in place of a listed file, so the plugin was not loaded and none of its code "
                              "ran: remove it (a __pycache__ folder of compiled files too), or install the package "
                              "again from its source.")
# The package's own names that a manifest cannot list: the signature's envelope, at the top of the folder only.
# The entry (plugin.py) is listed in every version 1 manifest, so it needs no exemption. Nothing else is exempt:
# not a dot-file (a listed path never starts with a dot) and not __pycache__, whose compiled files Python would
# import in place of the checked source.
PACKAGE_ENVELOPE = ("manifest.json", "manifest.sig")
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
    # True when, before any plugin code ran, every file the signed version 1 manifest lists was read and matched
    # its SHA-256 and nothing unlisted stood in the package folder (``_verify_package``).
    package_verified: bool = False


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


# Listing a held folder by its descriptor as well: then the content and the unlisted checks below read the
# same folders the presence check walked, and a folder swapped for a link between two steps is not followed.
_LIST_BY_FD = _WALK_BY_FD and os.listdir in os.supports_fd
_DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
              | getattr(os, "O_NONBLOCK", 0))
# O_NONBLOCK: a FIFO put in the place of a listed file opens at once and is then refused as not a regular file.
_FILE_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
               | getattr(os, "O_BINARY", 0))


def _owned_by_operator(info: os.stat_result) -> bool:
    """Owned by the user the loader runs as and not writable by a group or other users.

    Where the system has no POSIX owner (no ``os.geteuid``: Windows) there is nothing to compare, and this is
    True; the daemon itself refuses that system before it reads a plugin file.
    """
    if not hasattr(os, "geteuid"):
        return True
    return info.st_uid == os.geteuid() and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def _named(rel: str) -> str:
    """A folder as a refusal names it: its path with a slash, or the package folder itself."""
    return rel + "/" if rel else "the plugin's folder"


class _FolderTree:
    """The package folder read one held folder at a time, following no link (see ``_package_file_present``)."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._fds: dict[str, int] = {}

    def folder(self, rel: str) -> int:
        fd = self._fds.get(rel)
        if fd is None:
            parent, _, name = rel.rpartition("/")
            if rel:
                fd = os.open(name, _DIR_FLAGS, dir_fd=self.folder(parent))
            else:
                fd = os.open(self._root, _DIR_FLAGS)
            self._fds[rel] = fd
        return fd

    def folder_info(self, rel: str) -> os.stat_result:
        return os.fstat(self.folder(rel))

    def names(self, rel: str) -> list:
        return sorted(os.listdir(self.folder(rel)))

    def is_folder(self, rel: str) -> bool:
        parent, _, name = rel.rpartition("/")
        return stat.S_ISDIR(os.stat(name, dir_fd=self.folder(parent), follow_symlinks=False).st_mode)

    def open_file(self, rel: str) -> int:
        parent, _, name = rel.rpartition("/")
        return os.open(name, _FILE_FLAGS, dir_fd=self.folder(parent))

    def close(self) -> None:
        for fd in self._fds.values():
            os.close(fd)
        self._fds.clear()


class _PathTree:
    """The same reads by path where a folder cannot be held by its descriptor: each step is an ``lstat``, a
    link is never followed, and a file is read only when the opened file is the one ``lstat`` saw."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, rel: str) -> Path:
        return self._root / rel if rel else self._root

    def folder_info(self, rel: str) -> os.stat_result:
        info = os.lstat(self._path(rel))
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(rel)
        return info

    def names(self, rel: str) -> list:
        self.folder_info(rel)
        return sorted(os.listdir(self._path(rel)))

    def is_folder(self, rel: str) -> bool:
        return stat.S_ISDIR(os.lstat(self._path(rel)).st_mode)

    def open_file(self, rel: str) -> int:
        seen = os.lstat(self._path(rel))
        if not stat.S_ISREG(seen.st_mode):
            raise FileNotFoundError(rel)
        fd = os.open(self._path(rel), _FILE_FLAGS)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (seen.st_dev, seen.st_ino):
            os.close(fd)
            raise FileNotFoundError(rel)
        return fd

    def close(self) -> None:
        pass


def _verify_package(directory: Path, files: tuple) -> None:
    """Refuse a signed version 1 package unless its folder holds exactly what the manifest lists, unchanged.

    Runs after the presence check and before any plugin code. Every listed file is read through a walk that
    follows no link; it and every folder on its path must be owned by the user the loader runs as and not
    writable by a group or other users (``package_file_untrusted``), and its SHA-256 must be the manifest's
    (``package_file_digest_mismatch``). Then every name in the package folder and in the folders on a listed
    path must be a listed file, a folder on a listed path, or at the top one of ``PACKAGE_ENVELOPE``
    (``package_file_unlisted``, which names the first such name in sorted order). Each refusal names the path.

    What this does not hold: the files are read again by Python when the plugin imports them itself, after
    this check. Only a writer with the daemon user's own rights can change them in between (the files and
    their folders are that user's alone), and that writer is not contained in any case.
    """
    listed = dict(files)
    folders = {"/".join(path.split("/")[:depth]) for path in listed for depth in range(1, path.count("/") + 1)}
    tree = _FolderTree(directory) if _LIST_BY_FD else _PathTree(directory)
    try:
        for path, digest in files:
            parts = path.split("/")
            for depth in range(len(parts)):
                rel = "/".join(parts[:depth])
                try:
                    info = tree.folder_info(rel)
                except OSError:
                    raise Refused("package_file_missing", PACKAGE_FILE_MISSING_TEXT % path) from None
                if not _owned_by_operator(info):
                    raise Refused("package_file_untrusted", PACKAGE_FILE_UNTRUSTED_TEXT % _named(rel))
            try:
                fd = tree.open_file(path)
            except OSError:
                raise Refused("package_file_missing", PACKAGE_FILE_MISSING_TEXT % path) from None
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise Refused("package_file_missing", PACKAGE_FILE_MISSING_TEXT % path)
                if not _owned_by_operator(info):
                    raise Refused("package_file_untrusted", PACKAGE_FILE_UNTRUSTED_TEXT % path)
                with open(fd, "rb", closefd=False) as handle:
                    actual = hashlib.file_digest(handle, "sha256").hexdigest()
            finally:
                os.close(fd)
            if actual != digest:
                raise Refused("package_file_digest_mismatch", PACKAGE_FILE_DIGEST_MISMATCH_TEXT % path)
        pending = [""]
        while pending:
            rel = pending.pop(0)
            try:
                names = tree.names(rel)
            except OSError as exc:
                raise Refused("package_file_untrusted",
                              PACKAGE_FOLDER_UNREADABLE_TEXT % (_named(rel), type(exc).__name__)) from None
            for name in names:
                child = rel + "/" + name if rel else name
                if child in listed or (not rel and name in PACKAGE_ENVELOPE):
                    continue
                try:
                    is_folder = tree.is_folder(child)
                except OSError:
                    is_folder = False
                if child in folders and is_folder:
                    pending.append(child)
                    continue
                shown = child + "/" if is_folder else child
                raise Refused("package_file_unlisted", PACKAGE_FILE_UNLISTED_TEXT % shown)
    finally:
        tree.close()


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
        package_verified = False
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
            # Presence first, as before, so a missing file is still named first; then the content of every
            # listed file and nothing unlisted, all before any plugin code runs.
            if package_dir is not None and manifest.names_every_file:
                _verify_package(package_dir, manifest.files)
                package_verified = True
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
        return manifest, trust, kid, label, perms, package_verified

    def load(self, manifest_bytes: bytes, signature: object, artifact: bytes | None,
             registry_label: str | None = None, *, package_dir: Path | None = None) -> LoadedPlugin:
        """Load an entry artifact and check signed manifests' listed files in package_dir.

        Entry bytes supply the entry file when no directory is given. package_dir itself must be
        a folder, not a symbolic link, and no link inside it is followed. For a signed version 1
        manifest every listed file must be there, unchanged and the operator's alone, and nothing
        unlisted may stand in the folder (``_verify_package``). With a package folder the
        interpreter writes no compiled file from then on (``sys.dont_write_bytecode``), so the
        plugin's own imports leave no ``__pycache__`` in its package.
        """
        with self._lock:
            self._health.require_healthy()
            findings: list[dict] = []
            try:
                manifest, trust, kid, label, perms, package_verified = self._verify_admit(
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
            if package_dir is not None:
                # A compiled file written into the package would be unlisted at the next load, and Python
                # would import it in place of the checked source. The setting is the interpreter's own and
                # stays on for the process: the plugin's later imports are its own, outside this call.
                sys.dont_write_bytecode = True
            return self._activate(manifest, trust, kid, label, perms, code, findings, package_verified)

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
                  package_verified: bool = False) -> LoadedPlugin:
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
                              perms, module, tuple(findings), key_id(manifest.developer_key), package_verified)
        self._active[manifest.plugin_id] = loaded
        self.last_findings = loaded.findings
        return loaded
