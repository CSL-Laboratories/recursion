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

What runs is what was verified. Given a ``VerifiedPackage`` (``package=``), the loader
compiles the entry from its bytes and serves every listed module the plugin imports from its
own package (``from . import x``, at load or later) from those bytes too, through an import
finder bound to that package: nothing is read from the plugin's folder again, no bytecode
cache is read or written, and an import of a module of the plugin's package that the manifest
does not list is refused. ``importlib.resources.files(__package__)`` reads the listed files from
the same bytes. A file the plugin opens by its path, and a module from outside its package, is
not covered: the daemon says so in its record.

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
import io
import itertools
import linecache
import os
import stat
import sys
import threading
import types
from dataclasses import dataclass, replace
from importlib.resources.abc import Traversable
from pathlib import Path

from .errors import Refused
from .manifest import (
    ENTRY_FILE, RUNTIME_INPROCESS_RETIRING, Grant, Manifest, ReservedEffects, TrustStore,
    _bound_developer_key, effective_permissions, parse_manifest,
)
from .package import VerifiedPackage, files_digest
from .signing import key_id
from .runtime import GilProbe

__all__ = ["CoreHealth", "LoadedPlugin", "PluginLoader"]

_RUNTIME_SANDBOXED = "python-sandboxed/1"

PACKAGE_FILE_MISSING_TEXT = ("%s is listed in the signed manifest but is not a regular file in the plugin's folder: "
                             "install the package again from its source, or ask its developer for a whole package.")
PACKAGE_FOLDER_NOT_A_FOLDER_TEXT = ("the plugin's folder is missing, is a symbolic link or is not a folder, and the loader "
                                    "follows no link in a package, so the plugin was not loaded and none of its code ran: "
                                    "give the folder itself, or install the package again from its source.")
UNLISTED_IMPORT_TEXT = ("%s is not a file of this plugin's signed package, so it was not imported: a plugin imports "
                        "from its own package only the files its manifest lists, and they come from the bytes that "
                        "were verified, never from the folder. Add the file to the package and sign it again.")
PACKAGE_CHANGED_WHILE_CHECKED_TEXT = ("the plugin's files changed while they were being checked, so nothing of it was "
                                      "loaded and none of its code ran: install the plugin again from its source.")
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
    files_sha256: str | None = None   # files_digest of the bytes the loader serves this plugin's code from


class _VerifiedFiles(Traversable):
    """A listed path of one loaded package, read from the verified bytes (importlib.resources)."""

    def __init__(self, files: dict, path: str) -> None:
        self._files, self._path = files, path

    @property
    def name(self) -> str:
        return self._path.rsplit("/", 1)[-1]

    def is_file(self) -> bool:
        return self._path in self._files

    def is_dir(self) -> bool:
        prefix = self._path + "/" if self._path else ""
        return not self.is_file() and any(path.startswith(prefix) for path in self._files)

    def iterdir(self):
        prefix = self._path + "/" if self._path else ""
        names = sorted({path[len(prefix):].split("/", 1)[0] for path in self._files if path.startswith(prefix)})
        return iter([_VerifiedFiles(self._files, prefix + name) for name in names])

    def joinpath(self, *descendants) -> "_VerifiedFiles":
        parts = [] if not self._path else self._path.split("/")
        for item in descendants:
            for part in str(item).split("/"):
                if part in ("", "."):
                    continue
                if part == "..":
                    parts = parts[:-1]  # stays inside the package: there is nothing above it
                else:
                    parts.append(part)
        return _VerifiedFiles(self._files, "/".join(parts))

    def __truediv__(self, child) -> "_VerifiedFiles":
        return self.joinpath(child)

    def open(self, mode: str = "r", *args, **kwargs):
        if mode not in ("r", "rb"):
            raise ValueError("a verified package file is opened only for reading")
        if not self.is_file():
            raise FileNotFoundError(self._path)
        raw = io.BytesIO(self._files[self._path])
        return raw if mode == "rb" else io.TextIOWrapper(raw, *args, **kwargs)

    def read_bytes(self) -> bytes:
        if not self.is_file():
            raise FileNotFoundError(self._path)
        return self._files[self._path]

    def read_text(self, encoding=None, errors=None) -> str:
        return self.read_bytes().decode(encoding or "utf-8", errors or "strict")


class _VerifiedReader:
    def __init__(self, files: dict, path: str) -> None:
        self._root = _VerifiedFiles(files, path)

    def files(self) -> _VerifiedFiles:
        return self._root


def _code_name(package_name: str, plugin_id: str, path: str | None = None) -> str:
    """The name a plugin's code is compiled under, the name an error report shows for its lines:
    ``<plugin ID #N>`` for the entry, ``<plugin ID #N: PATH>`` for a listed module, N the load's
    own number. It begins with '<' and ends with '>', a name the line cache (tracebacks, warnings,
    the debugger, inspect) never opens as a file, so no file planted in the working folder or a
    sys.path folder is ever shown as the plugin's source."""
    return "<plugin %s #%s%s>" % (plugin_id, package_name.rpartition("_")[2], "" if path is None else ": " + path)


def _remember_lines(code_name: str, source: bytes) -> None:
    """Put the verified lines in the line cache under ``code_name``, so that an error report shows
    the lines that were checked. The entry has no time, so the cache never compares it with a file."""
    try:
        text = importlib.util.decode_source(source)
    except (SyntaxError, UnicodeError, LookupError):
        return  # compile() refuses these bytes too
    lines = io.StringIO(text).readlines()  # split as the tokenizer counts lines: at a newline only
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    linecache.cache[code_name] = (len(source), None, lines, code_name)


class _PackageImports:
    """One loaded plugin's own package: its modules are compiled from the verified bytes only."""

    def __init__(self, name: str, plugin_id: str, files) -> None:
        self.name, self.plugin_id = name, plugin_id
        self.files = dict(files)  # path -> bytes, a private copy of what was verified
        self.served: dict[str, str] = {}  # listed path -> SHA-256 of the bytes compiled from it
        self.code_names: set[str] = set()  # the names its code was compiled under, in the line cache
        self._lock = threading.Lock()

    def code_name(self, path: str | None = None) -> str:
        """The code name of the entry (``path`` None) or of a listed module, with its verified
        lines put in the line cache under it until the package is taken out (``forget_lines``)."""
        name = _code_name(self.name, self.plugin_id, path)
        with self._lock:
            self.code_names.add(name)
        _remember_lines(name, self.files[ENTRY_FILE if path is None else path])
        return name

    def forget_lines(self) -> None:
        with self._lock:
            names, self.code_names = self.code_names, set()
        for name in names:
            linecache.cache.pop(name, None)

    def locate(self, fullname: str):
        """(listed path or None, is a package) for a module of this package, or None if unlisted."""
        relative = fullname[len(self.name) + 1:].replace(".", "/")
        if relative + ".py" in self.files:
            return relative + ".py", False
        if relative + "/__init__.py" in self.files:
            return relative + "/__init__.py", True
        if any(path.startswith(relative + "/") for path in self.files):
            return None, True  # a folder of listed files without __init__.py: a namespace part
        return None

    # The loader protocol (importlib.abc.Loader), for this package's own modules.
    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        if module.__name__ == self.name:
            return  # the entry: run by the plugin loader itself, from the same bytes
        where = self.locate(module.__name__)
        if where is None:
            raise ImportError(UNLISTED_IMPORT_TEXT % module.__name__[len(self.name) + 1:], name=module.__name__)
        path, _ = where
        if path is None:
            return
        source = self.files[path]
        with self._lock:
            self.served[path] = hashlib.sha256(source).hexdigest()
        exec(compile(source, self.code_name(path), "exec", dont_inherit=True), module.__dict__)

    def is_package(self, fullname: str) -> bool:
        where = None if fullname == self.name else self.locate(fullname)
        return fullname == self.name or (where is not None and where[1])

    def get_data(self, path: str) -> bytes:
        relative = str(path).lstrip("/")
        if relative not in self.files:
            raise FileNotFoundError(relative)
        return self.files[relative]

    def get_resource_reader(self, fullname: str) -> _VerifiedReader:
        if fullname == self.name:
            return _VerifiedReader(self.files, "")
        where = self.locate(fullname)
        folder = fullname[len(self.name) + 1:].replace(".", "/")
        return _VerifiedReader(self.files, folder if where is not None and where[1] else folder.rpartition("/")[0])


class _VerifiedFinder:
    """The one meta-path finder of this process for plugin packages. It answers only for a
    module inside a loaded plugin's own package and never looks at a folder."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._packages: dict[str, _PackageImports] = {}
        self._names = itertools.count(1)

    def new_name(self, plugin_id: str) -> str:
        with self._lock:
            number = next(self._names)
        return "csl_plugin_%s_%d" % (plugin_id.replace(".", "_").replace("-", "_"), number)

    def add(self, package: _PackageImports) -> None:
        with self._lock:
            self._packages[package.name] = package
            if self not in sys.meta_path:
                sys.meta_path.insert(0, self)

    def drop(self, name: str) -> None:
        with self._lock:
            package = self._packages.pop(name, None)
        if package is not None:
            package.forget_lines()
        for module_name in [key for key in list(sys.modules) if key == name or key.startswith(name + ".")]:
            sys.modules.pop(module_name, None)

    def find_spec(self, fullname, path=None, target=None):
        head = fullname.partition(".")[0]
        with self._lock:
            package = self._packages.get(head)
        if package is None or fullname == head:
            return None
        where = package.locate(fullname)
        if where is None:
            # ImportError, not ModuleNotFoundError: ``from . import x`` would turn the latter into
            # a bare "cannot import name", and the words below are what a person needs.
            raise ImportError(UNLISTED_IMPORT_TEXT % fullname[len(head) + 1:], name=fullname)
        return importlib.machinery.ModuleSpec(fullname, package, is_package=where[1])

    def invalidate_caches(self) -> None:
        return None


_FINDER = _VerifiedFinder()


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
        self._package_names: list[str] = []
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
               registry_label: str | None = None, *, package_dir: Path | None = None,
               package: VerifiedPackage | None = None) -> list[dict]:
        """Run verify and admit only (no code executes) and return every finding."""
        findings: list[dict] = []
        try:
            self._verify_admit(manifest_bytes, signature, artifact, registry_label, findings, package_dir, package)
        except Refused as exc:
            findings.append(_finding(findings[-1]["stage"] if findings else "verify",
                                     exc.code, "refuse", exc.detail))
        return findings

    def _verify_admit(self, manifest_bytes, signature, artifact, registry_label, findings, package_dir,
                      package=None):
        if isinstance(package_dir, (str, os.PathLike)):
            package_dir = Path(package_dir)
        if package is not None:
            # The verified bytes are the only source: no folder beside them, the same manifest,
            # the entry as it was read, and nothing that changed while it was checked.
            if type(package) is not VerifiedPackage or package_dir is not None:
                raise Refused("package_not_verified", "give the VerifiedPackage alone, without a folder")
            if package.changed_while_checked:
                raise Refused("package_changed_while_checked", PACKAGE_CHANGED_WHILE_CHECKED_TEXT)
            if manifest_bytes != package.manifest.raw:
                raise Refused("package_not_verified", "the manifest is not the one that was verified")
            if artifact is not None and artifact != package.files.get(ENTRY_FILE, package.entry):
                raise Refused("package_not_verified", "plugin.py is not the one that was read")
        # The daemon can supply None for a missing entry so a signed manifest can name it.
        if type(artifact) is not bytes and (artifact is not None or (package_dir is None and package is None)):
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
                # Bytes alone can supply the entry. With a directory, require the listed file there too;
                # with a verified package, among its verified bytes.
                if package is not None:
                    present = (path != ENTRY_FILE or artifact is not None) and path in package.files
                elif package_dir is None:
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
             registry_label: str | None = None, *, package_dir: Path | None = None,
             package: VerifiedPackage | None = None) -> LoadedPlugin:
        """Load an entry artifact and check signed manifests' listed files in package_dir.

        Entry bytes supply the entry file when no directory is given. package_dir itself must be
        a folder, not a symbolic link, and no link inside it is followed. With ``package`` (a
        ``verify_package`` result, and no ``package_dir``) the listed files are its verified
        bytes, and every module of the plugin's own package is compiled from them.
        """
        with self._lock:
            self._health.require_healthy()
            findings: list[dict] = []
            try:
                manifest, trust, kid, label, perms = self._verify_admit(
                    manifest_bytes, signature, artifact, registry_label, findings, package_dir, package)
                if manifest.plugin_id in self._active:
                    raise Refused("plugin_already_active", manifest.plugin_id)
                name = _FINDER.new_name(manifest.plugin_id)
                code = compile(artifact, _code_name(name, manifest.plugin_id), "exec", dont_inherit=True)
                findings.append(_finding("stage", "compiled", "info"))
            except (Refused, SyntaxError, ValueError, RecursionError, MemoryError) as exc:
                # No plugin code ran, but the rule is a GIL read after every load attempt.
                if self._gil_gate("refused_load", findings):
                    raise Refused("core_poisoned_gil_enabled", "refused_load") from exc
                self.last_findings = tuple(findings)
                if isinstance(exc, Refused):
                    raise
                raise Refused("plugin_compile_failed", type(exc).__name__) from None
            files = dict(package.files) if package is not None else {}
            files[ENTRY_FILE] = artifact  # digest-checked above: the bytes compiled into ``code``
            return self._activate(manifest, trust, kid, label, perms, code, findings, files, name)

    def close(self) -> None:
        """Take this loader's plugin packages out of the import system (the modules stay alive
        where plugin code still holds them; no new import is served for them)."""
        with self._lock:
            for name in self._package_names:
                _FINDER.drop(name)
            self._package_names.clear()

    def served(self, plugin_id: str) -> dict:
        """Listed path -> SHA-256 of the bytes compiled for this plugin's own modules so far."""
        plugin = self._active.get(plugin_id)
        loader = getattr(getattr(plugin, "module", None), "__loader__", None)
        if not isinstance(loader, _PackageImports):
            return {}
        with loader._lock:
            return dict(loader.served)

    def _gil_gate(self, name: str, findings: list) -> bool:
        """Read the GIL; if it is on, poison the core and drop every active plugin."""
        if not self._gil.gil_enabled():
            return False
        self._health.poison("gil_enabled_after_load:" + name)
        self._active.clear()
        findings.append(_finding("active", "gil_enabled_after_load", "refuse"))
        self.last_findings = tuple(findings)
        return True

    def _activate(self, manifest: Manifest, trust, kid, label, perms, code, findings, files,
                  name: str) -> LoadedPlugin:
        before = set(sys.modules)
        # The entry is a package whose modules come only from ``files``: the finder serves
        # ``from . import x`` from these bytes, never from the folder.
        imports = _PackageImports(name, manifest.plugin_id, files)
        imports.code_name()  # the entry's verified lines, for its error reports
        imports.served[ENTRY_FILE] = hashlib.sha256(files[ENTRY_FILE]).hexdigest()
        module = importlib.util.module_from_spec(importlib.machinery.ModuleSpec(imports.name, imports, is_package=True))
        _FINDER.add(imports)
        sys.modules[imports.name] = module
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
        if gil_on or failure is not None:
            _FINDER.drop(imports.name)
        else:
            self._package_names.append(imports.name)
        if gil_on:
            raise Refused("core_poisoned_gil_enabled", manifest.plugin_id) from failure
        if failure is not None:
            findings.append(_finding("active", "plugin_import_failed", "refuse", type(failure).__name__))
            self.last_findings = tuple(findings)
            raise Refused("plugin_import_failed", type(failure).__name__) from None
        findings.append(_finding("active", "active", "info", "in_process_not_contained"))
        loaded = LoadedPlugin(manifest.plugin_id, manifest.artifact_sha256, trust, kid, label,
                              perms, module, tuple(findings), key_id(manifest.developer_key),
                              files_digest(imports.files))
        self._active[manifest.plugin_id] = loaded
        self.last_findings = loaded.findings
        return loaded
