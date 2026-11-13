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

A failed load puts the import tables back. Every ``sys.modules`` name added
during the attempt is dropped, and a name that was already loaded is put back
to the module the snapshot recorded. For each module that was already loaded,
package or not, the bindings of those dotted names are put back too: set to
the pre-load value, or deleted if the attribute was absent. The snapshot
decides, not whether the attribute object is the module being dropped and not
whether the parent has ``__path__``. A module-typed binding recorded in the
snapshot is put back by identity when the attribute no longer holds that
object, including when the ``sys.modules`` entry for that dotted name was left
unchanged. A stdlib import made by the failed attempt is dropped with the
rest; importing it again yields one module.

The snapshot records each pre-loaded module's class and the bindings of
module objects reachable as module-typed attributes, two levels deep,
deduplicated by identity, up to ``_MODULE_WALK_BOUND``. Restore puts the
class back first, then writes the real module dict, so a swapped class whose
``__setattr__`` raises cannot keep the old binding. The check reads that
dict and, for every snapshotted module-valued name, ``getattr``. If the
class cannot be restored, a write fails, or ``getattr`` is not the
snapshotted object, the load is refused with
``plugin_import_state_unrestored``.

A finalizer on a dropped module can insert another name or binding before
the load returns. That includes a finalizer a cycle holds until garbage
collection, and one that arms a further cycle. After each settle,
``gc.collect()`` runs until a collection finds nothing or
``_GC_COLLECT_BOUND`` collections have run, and the tables are settled
again after each one. The failure traceback is dropped first so a cycle the
frame still held can be collected. Names or bindings that are still
different after the restore bound are refused with
``plugin_import_state_unrestored``. They are not reported as
``plugin_import_failed``: that code means the import tables were proved
equal to the snapshot. A finalizer that keeps re-arming past the bound can
still leave a name or a binding. A chain of deferred finalizers longer than
the collection bound can still plant one after the load returns.

A second ``load`` on the same loader is refused, including a nested call on
this thread and a call from another thread. The loader lock is not waited on.
A load that starts while another loader's activation is inside exec is refused
the same way. That import lock is not re-entered and it is not waited on.

Residuals this process does not close: a non-module attribute changed on a
module whose ``sys.modules`` entry stayed the same object; a thread the plugin
started that puts a module back after the restore returns; a patch of
importlib classes (``PathFinder``, ``FileFinder``, ``SourceFileLoader``) so a
later import in this process runs attacker code; a deferred finalizer chain
longer than ``_GC_COLLECT_BOUND``. Closing those means running the plugin in
a subprocess. Whether to do that is an owner decision. This loader does not
spawn one.
"""

from __future__ import annotations

import gc
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


class _ImportRecord:
    """Modules the import system created during one load.

    ``sys.modules`` stays the interpreter dictionary. Replacing it leaves
    imports consulting the orphaned dictionary, so a name removed only from
    the replacement is still treated as loaded.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.created: dict[str, tuple] = {}
        self._specs: dict[str, tuple] = {}
        self.recording = False

    def note_spec(self, name: str, origin: str | None, finder: object) -> None:
        """Remember the origin the finder set, before the module body can rewrite it."""
        with self._lock:
            if self.recording and name not in self._specs:
                self._specs[name] = (origin, finder)

    def remember(self, name: str, module: object) -> None:
        with self._lock:
            if self.recording and name not in self.created:
                origin, finder = self._specs.get(name, (None, None))
                self.created[name] = (module, origin, finder)

    def reset(self) -> None:
        with self._lock:
            self.created.clear()
            self._specs.clear()
            self.recording = True

    def stop(self) -> dict[str, tuple]:
        with self._lock:
            self.recording = False
            found = dict(self.created)
            self.created.clear()
            self._specs.clear()
            return found


# One activation at a time. A second load while this is held is refused: waiting
# deadlocks a nested call on this thread and a cross-thread cycle.
_import_isolation = threading.Lock()
_import_depth = threading.local()
_import_finding = threading.local()


def _finding_names() -> set[str]:
    names = getattr(_import_finding, "names", None)
    if names is None:
        names = set()
        _import_finding.names = names
    return names


class _RecordingLoader:
    """Run the real loader and remember the module object it was given."""

    def __init__(self, inner: object, record: _ImportRecord) -> None:
        self._inner = inner
        self._record = record

    def create_module(self, spec):
        create = getattr(self._inner, "create_module", None)
        if create is None:
            return None
        return create(spec)

    def exec_module(self, module) -> None:
        name = getattr(module, "__name__", None)
        if type(name) is str:
            self._record.remember(name, module)
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
        self._record.remember(fullname, current if current is not None else module)
        return module

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class _RecordingFinder:
    """Meta-path finder for one load. It does not decide what a failure keeps."""

    def __init__(self, record: _ImportRecord) -> None:
        self._record = record

    def find_spec(self, fullname, path, target=None):
        finding = _finding_names()
        if not self._record.recording or fullname in finding:
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
                origin = getattr(spec, "origin", None)
                if type(origin) is not str:
                    origin = None
                self._record.note_spec(fullname, origin, finder)
                loader = getattr(spec, "loader", None)
                if loader is not None and not isinstance(loader, _RecordingLoader):
                    spec.loader = _RecordingLoader(loader, self._record)
                return spec
            return None
        finally:
            finding.discard(fullname)


def _begin_import_record(record: _ImportRecord) -> None:
    record.reset()
    record.finder = _RecordingFinder(record)
    sys.meta_path.insert(0, record.finder)


def _end_import_record(record: _ImportRecord) -> dict:
    finder = getattr(record, "finder", None)
    if finder is not None:
        try:
            sys.meta_path.remove(finder)
        except ValueError:
            pass
    return record.stop()


class _ModuleView:
    """One module object as it stood before the plugin ran."""

    __slots__ = ("module", "cls", "bindings", "visible", "names")

    def __init__(self, module: object, cls: type, bindings: dict, visible: dict) -> None:
        self.module = module
        self.cls = cls
        self.bindings = bindings
        self.visible = visible
        # Import names this object was entered under. A new module whose
        # ``__name__`` is ``parent.attr`` is an import binding; a module stored
        # on a pre-existing non-module attribute under some other name is not.
        self.names: set[str] = set()


def _is_module(value: object) -> bool:
    try:
        return isinstance(value, types.ModuleType)
    except Exception:
        return False


def _module_mapping(module: object):
    """The module's real dict, past a swapped class's ``__dict__`` or ``__getattribute__``."""
    try:
        return types.ModuleType.__dict__["__dict__"].__get__(module)
    except Exception:
        return object.__getattribute__(module, "__dict__")


# How far to walk module-typed attributes of modules that are not themselves
# in ``sys.modules``. Level 0 is the pre-loaded table. Two more levels cover
# ``nest_only.child.grand`` without following an arbitrary object graph.
_MODULE_WALK_DEPTH = 2
# Cap on captured module objects. Every ``sys.modules`` entry is captured
# first; the cap refuses the walk of further attributes once it is hit.
_MODULE_WALK_BOUND = 16384


class _ImportSnapshot:
    """Import tables as they stood before one plugin attempt."""

    def __init__(self) -> None:
        modules = sys.modules
        self.modules_dict = modules
        self.modules = dict(modules)
        self.path = sys.path
        self.path_items = list(sys.path)
        self.meta = sys.meta_path
        self.meta_items = list(sys.meta_path)
        self.hooks = sys.path_hooks
        self.hooks_items = list(sys.path_hooks)
        cache = sys.path_importer_cache
        self.cache = cache
        self.cache_items = dict(cache)
        self.parent_bindings: dict[str, dict[str, object]] = {}
        self.module_views: list[_ModuleView] = []
        self._views_by_id: dict[int, _ModuleView] = {}
        self._seen: set[int] = set()
        self.restore_blocked = False
        self.truncated = False
        paths: dict[str, tuple] = {}
        for name, module in self.modules.items():
            # Every pre-loaded module, not only packages. ``os`` has no
            # ``__path__`` and ``json.encoder`` has none either; a dotted name
            # under either one still has a parent attribute to put back.
            if _is_module(module):
                view = self._capture(module)
                if view is not None:
                    view.names.add(name)
                    self.parent_bindings[name] = view.bindings
            try:
                pkg_path = getattr(module, "__path__", None)
            except Exception:
                continue
            if pkg_path is None:
                continue
            if isinstance(pkg_path, list):
                try:
                    items = list(pkg_path)
                except Exception:
                    continue
                paths[name] = (pkg_path, items, None)
                continue
            inner = getattr(pkg_path, "_path", None)
            if not isinstance(inner, list):
                continue
            try:
                items = list(inner)
            except Exception:
                continue
            paths[name] = (pkg_path, items, inner)
        self.package_paths = paths
        self._capture_reachable()

    def _capture(self, module: object) -> _ModuleView | None:
        """Record one module's class, real dict, and visible module bindings."""
        ident = id(module)
        if ident in self._seen:
            return self._views_by_id.get(ident)
        self._seen.add(ident)
        try:
            cls = type(module)
            bindings = dict(_module_mapping(module))
        except Exception:
            return None
        visible: dict[str, object] = {}
        for attr, value in bindings.items():
            if not _is_module(value):
                continue
            try:
                visible[attr] = getattr(module, attr)
            except Exception:
                visible[attr] = _UNREADABLE
        view = _ModuleView(module, cls, bindings, visible)
        self._views_by_id[ident] = view
        self.module_views.append(view)
        return view

    def _capture_reachable(self) -> None:
        """Snapshot module objects hung off snapshotted modules, two levels deep."""
        queue: list[tuple[object, int]] = [(view.module, 0) for view in self.module_views]
        head = 0
        while head < len(queue):
            module, depth = queue[head]
            head += 1
            if depth >= _MODULE_WALK_DEPTH:
                continue
            view = self._views_by_id.get(id(module))
            if view is None:
                continue
            for attr, value in view.bindings.items():
                if not _is_module(value) or id(value) in self._seen:
                    continue
                if len(self._seen) >= _MODULE_WALK_BOUND:
                    self.truncated = True
                    return
                child = self._capture(value)
                if child is None:
                    continue
                for parent_name in view.names:
                    child.names.add(parent_name + "." + attr)
                queue.append((value, depth + 1))


def _restore_list(owner: object, attr: str, original: list, items: list) -> None:
    """Put the snapshotted list object back and refill it."""
    current = getattr(owner, attr, None)
    if current is not original:
        try:
            setattr(owner, attr, original)
            current = original
        except (AttributeError, TypeError):
            pass
    try:
        current[:] = list(items)
    except (AttributeError, TypeError, ValueError):
        setattr(owner, attr, list(items))


_MISSING = object()
_UNREADABLE = object()


def _restore_module_class(module: object, cls: type) -> bool:
    """Put ``module``'s class back. A subclass ``__setattr__`` must not block this."""
    if type(module) is cls:
        return True
    try:
        object.__setattr__(module, "__class__", cls)
    except Exception:
        return False
    return type(module) is cls


def _named_like_import(view: _ModuleView, attr: str, live: object) -> bool:
    """True when ``live`` is the module an import of ``parent.attr`` would bind."""
    try:
        mod_name = getattr(live, "__name__", None)
    except Exception:
        return False
    if not isinstance(mod_name, str):
        return False
    for parent_name in view.names:
        if mod_name == parent_name + "." + attr:
            return True
    return False


def _binding_must_match(view: _ModuleView, attr: str, previous: object, live: object) -> bool:
    """True when this attribute is an import binding the snapshot has to prove.

    A pre-existing module is compared by identity. A module attribute that was
    absent is a planted import. A module whose ``__name__`` is the dotted
    import name is one too. A pre-existing non-module attribute that now holds
    some other module is left alone: changing it is the non-module residual.
    """
    if _is_module(previous):
        return True
    if not _is_module(live):
        return False
    if attr not in view.bindings:
        return True
    return _named_like_import(view, attr, live)


def _restore_module_view(view: _ModuleView) -> bool:
    """Restore one module's class and its import bindings through the real dict.

    Returns False when the class or a binding cannot be written. The caller
    refuses the load: a swallowed ``setattr`` would leave the attacker reachable
    and still look like an ordinary import failure.
    """
    module = view.module
    if not _restore_module_class(module, view.cls):
        return False
    try:
        current = _module_mapping(module)
        for attr, previous in view.bindings.items():
            live = current.get(attr, _MISSING)
            if _binding_must_match(view, attr, previous, live) and live is not previous:
                current[attr] = previous
        extras = [attr for attr, live in current.items()
                  if attr not in view.bindings and _is_module(live)]
        for attr in extras:
            del current[attr]
    except Exception:
        return False
    return True


def _view_dirty(view: _ModuleView) -> bool:
    """True when this module's class or import bindings are not the snapshot."""
    module = view.module
    if type(module) is not view.cls:
        return True
    try:
        current = _module_mapping(module)
        for attr, previous in view.bindings.items():
            live = current.get(attr, _MISSING)
            if _binding_must_match(view, attr, previous, live) and live is not previous:
                return True
        for attr, live in current.items():
            if attr not in view.bindings and _is_module(live):
                return True
        for attr, expected in view.visible.items():
            try:
                got = getattr(module, attr)
            except Exception:
                got = _UNREADABLE
            if got is not expected:
                return True
    except Exception:
        return True
    return False


def _restore_parent_binding(snap: _ImportSnapshot, modules: dict, name: str) -> bool:
    """Put one parent attribute back to the pre-load binding.

    The snapshot decides. An attribute that was there is set to that value,
    including when that value is the module just dropped from ``sys.modules``.
    An attribute that was not there is deleted. The write goes through the
    module's real dict, after the class is put back, so a subclass whose
    ``__setattr__`` or ``__delattr__`` raises cannot hide the binding.
    Returns False when that write cannot be completed.
    """
    parent_name, dot, attr = name.rpartition(".")
    if not dot:
        return True
    parent = modules.get(parent_name)
    if not _is_module(parent):
        return True
    bindings = snap.parent_bindings.get(parent_name)
    if bindings is None:
        return True
    view = snap._views_by_id.get(id(parent))
    if view is not None and not _restore_module_class(parent, view.cls):
        return False
    try:
        current = _module_mapping(parent)
    except Exception:
        return False
    try:
        if attr in bindings:
            if current.get(attr, _MISSING) is not bindings[attr]:
                current[attr] = bindings[attr]
        elif attr in current:
            del current[attr]
    except Exception:
        return False
    return True


def _module_bindings_to_restore(snap: _ImportSnapshot, modules: dict,
                                added: list[str]) -> list[str]:
    """Dotted names whose parent attribute must return to the pre-load value.

    That is every dotted name added to ``sys.modules``, every pre-loaded dotted
    name whose entry was replaced or removed, and a module binding the snapshot
    recorded when the attribute no longer holds that object. That recorded
    binding is compared by identity. ``__name__`` does not decide it.
    """
    names: set[str] = set()
    for name in added:
        if "." in name:
            names.add(name)
    for name, module in snap.modules.items():
        if "." in name and modules.get(name) is not module:
            names.add(name)
    for parent_name, bindings in snap.parent_bindings.items():
        candidates: list[types.ModuleType] = []
        live = modules.get(parent_name)
        if isinstance(live, types.ModuleType):
            candidates.append(live)
        original = snap.modules.get(parent_name)
        if isinstance(original, types.ModuleType) and original is not live:
            candidates.append(original)
        for parent in candidates:
            try:
                current = _module_mapping(parent)
            except Exception:
                continue
            for attr, value in current.items():
                if not isinstance(value, types.ModuleType):
                    continue
                previous = bindings.get(attr, _MISSING)
                if previous is value or isinstance(previous, types.ModuleType):
                    # Recorded module bindings are compared by identity below.
                    continue
                dotted = parent_name + "." + attr
                if previous is _MISSING or getattr(value, "__name__", None) == dotted:
                    names.add(dotted)
            for attr, value in bindings.items():
                if isinstance(value, types.ModuleType) and current.get(attr, _MISSING) is not value:
                    names.add(parent_name + "." + attr)
    return list(names)


def _import_modules_dirty(snap: _ImportSnapshot, modules: dict) -> bool:
    """True when ``sys.modules`` is not the snapshotted dictionary of modules."""
    if sys.modules is not modules:
        return True
    for name in modules:
        if name not in snap.modules:
            return True
    for name, module in snap.modules.items():
        if modules.get(name) is not module:
            return True
    return False


def _import_bindings_dirty(snap: _ImportSnapshot, _modules: dict) -> bool:
    """True when a recorded module binding, class, or visible module is still wrong.

    The snapshot holds the module objects themselves, including ones that were
    never entered in ``sys.modules``.
    """
    if snap.restore_blocked or snap.truncated:
        return True
    for view in snap.module_views:
        if _view_dirty(view):
            return True
    return False


# Rounds of pop-and-restore between collections. A finalizer that inserts a
# fresh name each time it runs can outlast this.
_RESTORE_BOUND = 8
# Collections after the refcount settle. Each one can run a finalizer that
# arms another cycle; that cycle waits for the next collection. A longer
# chain is the deferred-finalizer residual.
_GC_COLLECT_BOUND = 4


def _apply_import_restore(snap: _ImportSnapshot) -> None:
    """Put every snapshotted import table back once.

    Every name added during the attempt is dropped. A name that was already
    loaded is put back to the module the snapshot recorded. Parent attributes
    of those dotted names are put back from the module snapshot. A finalizer
    that runs during this pass can insert a name or change an attribute again;
    the caller repeats the pass.
    """
    modules = snap.modules_dict
    snap.restore_blocked = False
    if sys.modules is not modules:
        sys.modules = modules
    added = [name for name in list(modules) if name not in snap.modules]
    dotted = _module_bindings_to_restore(snap, modules, added)
    for name in added:
        modules.pop(name, None)
    for name, module in snap.modules.items():
        if modules.get(name) is not module:
            modules[name] = module
    for view in snap.module_views:
        if not _restore_module_view(view):
            snap.restore_blocked = True
    for name in dotted:
        if not _restore_parent_binding(snap, modules, name):
            snap.restore_blocked = True
    _restore_list(sys, "path", snap.path, snap.path_items)
    _restore_list(sys, "meta_path", snap.meta, snap.meta_items)
    _restore_list(sys, "path_hooks", snap.hooks, snap.hooks_items)
    cache = snap.cache
    if sys.path_importer_cache is not cache:
        sys.path_importer_cache = cache
    cache.clear()
    cache.update(snap.cache_items)
    for name, (path_obj, items, inner) in snap.package_paths.items():
        module = modules.get(name)
        if module is None:
            continue
        try:
            current = getattr(module, "__path__", None)
            if current is not path_obj:
                try:
                    setattr(module, "__path__", path_obj)
                    current = path_obj
                except Exception:
                    pass
            if inner is None:
                if isinstance(current, list):
                    current[:] = list(items)
                continue
            live_inner = getattr(current, "_path", None)
            if live_inner is not inner:
                try:
                    setattr(current, "_path", inner)
                    live_inner = inner
                except Exception:
                    pass
            if isinstance(live_inner, list):
                live_inner[:] = list(items)
        except Exception:
            continue


def _restore_import_state(snap: _ImportSnapshot) -> None:
    """Put the import tables back, then drop names a finalizer inserted.

    ``gc.collect()`` repeats until it finds nothing or ``_GC_COLLECT_BOUND``
    collections have run. A cycle's finalizer does not run until a collection,
    and a finalizer can arm another cycle that waits for the next one. The
    tables are settled again after every collection. Bindings that are still
    wrong after the restore bound refuse the load with
    ``plugin_import_state_unrestored``, even when ``sys.modules`` itself
    matches. A chain of deferred finalizers longer than the collection bound
    can still plant a name after this returns.
    """
    modules = snap.modules_dict

    def settle(bound: int) -> bool:
        for _ in range(bound):
            _apply_import_restore(snap)
            # Bindings second: a ``getattr`` check must not hide a name it just added.
            if not _import_bindings_dirty(snap, modules) and not _import_modules_dirty(snap, modules):
                return True
        return False

    def proved_after_collect() -> bool:
        collected = gc.collect()
        if settle(_RESTORE_BOUND):
            return collected == 0
        if collected == 0:
            raise Refused("plugin_import_state_unrestored")
        return False

    if not settle(_RESTORE_BOUND):
        # A cycle may be holding the finalizer that keeps a name or binding
        # in place. Collect before treating the bound as exhausted.
        if gc.collect() == 0 or not settle(_RESTORE_BOUND):
            if not settle(1):
                raise Refused("plugin_import_state_unrestored")
    for _ in range(_GC_COLLECT_BOUND):
        if proved_after_collect():
            return
    if not settle(_RESTORE_BOUND):
        raise Refused("plugin_import_state_unrestored")


class PluginLoader:
    def __init__(self, trust: TrustStore, grants: dict[str, Grant], reserved: ReservedEffects,
                 health: CoreHealth, gil_probe: GilProbe | None = None) -> None:
        self._trust = trust
        self._grants = dict(grants)
        self._reserved = reserved
        self._health = health
        self._gil = gil_probe if gil_probe is not None else GilProbe()
        self._lock = threading.Lock()
        # Set for the thread inside ``load``. A nested call on that thread is
        # refused before it tries to take ``_lock`` again.
        self._load_depth = threading.local()
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
        # Non-blocking, and per thread. Waiting here deadlocks a nested call on
        # this thread and queues another thread behind the plugin still running.
        if getattr(self._load_depth, "depth", 0):
            raise Refused("plugin_load_busy")
        if not self._lock.acquire(blocking=False):
            raise Refused("plugin_load_busy")
        self._load_depth.depth = 1
        try:
            return self._load_holding_lock(manifest_bytes, signature, artifact, registry_label, root=root)
        finally:
            self._load_depth.depth = 0
            self._lock.release()

    def _load_holding_lock(self, manifest_bytes: bytes, signature: object, artifact: bytes,
                           registry_label: str | None, *, root: str | None) -> LoadedPlugin:
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
                              hashlib.sha256(artifact).hexdigest())

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
                  code_sha256: str) -> LoadedPlugin:
        if getattr(_import_depth, "depth", 0):
            raise Refused("plugin_load_busy")
        if not _import_isolation.acquire(blocking=False):
            raise Refused("plugin_load_busy")
        _import_depth.depth = 1
        try:
            snap = _ImportSnapshot()
            record = _ImportRecord()
            module = types.ModuleType(_module_name(manifest.plugin_id))
            failure: BaseException | None = None
            gil_on = False
            _begin_import_record(record)
            try:
                try:
                    exec(code, module.__dict__)
                except BaseException as exc:  # noqa: BLE001 - the GIL check must follow any outcome
                    failure = exc
                finally:
                    for name in sorted(set(sys.modules) - set(snap.modules)):
                        origin = getattr(sys.modules.get(name), "__file__", None) or ""
                        if origin.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES)):
                            findings.append(_finding("active", "extension_module_loaded", "warn", name))
                    gil_on = self._gil_gate(manifest.plugin_id, findings)
                    self.last_findings = tuple(findings)
            finally:
                _end_import_record(record)
                if gil_on or failure is not None:
                    # The traceback and this frame hold the plugin module. Drop
                    # them before collection so a cycle the activation still
                    # referenced can be collected inside the restore, while the
                    # module dict is still intact for finalizers that close
                    # over it.
                    if failure is not None:
                        failure.__traceback__ = None
                        failure.__context__ = None
                        failure.__cause__ = None
                    module = None
                    _restore_import_state(snap)
        finally:
            _import_depth.depth = 0
            _import_isolation.release()
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
