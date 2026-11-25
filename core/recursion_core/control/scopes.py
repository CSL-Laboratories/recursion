"""The control API's authorizer: one decision per client call, from the method table's scope column.

Lane CONTROL, frontier plan v3.3, step 3. This module is a registrant that ``control/hooks.py`` imports
by name (its ``REGISTRANTS`` list names ``recursion_core.control.scopes``). ``register(hooks)`` registers
the authorizer into S5, reading every method's scope from the method table (``control.methods.METHODS``).
It holds no credentials and reads no run record, so only owner, internal and the page's device principal
hold scopes: the stricter state, and today's callers (the owner-only socket, in-process calls made for
the owner, the page) keep every answer they have.

``ScopesService`` (seam S13, ``control.scopes``) is built and tested here but NOT registered: once
started it hands the authorizer the credential store (``<state_dir>/control/credentials.json``) and the
control registry's run records, which any principal backed by a grant needs: a plugin, a session, a
paired device other than the page, an agent. No entry point makes such a principal yet, and registering
a service turns the composition's admission gate on for every daemon, so it is registered by the change
that brings the first such principal (``questions.md``). A store that cannot be used (malformed
included) is logged and leaves token principals holding nothing; it never stops the daemon.

S5's contract (``control/hooks.py``, SPINE): ``authorize(principal, method, params)`` is called for every
client call, before the method runs, and answers None (go on), a refusal code, or ``(code, detail)``. A
registered authorizer takes over the whole decision, today's run-unit refusal included. The daemon's own
recovery Stop never passes here.

``Authorizer(scope_of=..., credentials=..., run_record=..., in_subtree=...)`` decides, in this order:

1. A principal whose ``in_run_unit`` is not exactly False is refused ``scope_refused`` (today's predicate:
   true inside a run's unit, and when the peer's cgroup cannot be read).
2. The scopes the principal holds NOW:
   - ``owner`` and ``internal`` hold all four;
   - the page's own device principal (``device`` with id ``page``, which no grant may name) holds
     ``PAGE_SCOPES``;
   - a principal made from a verified token carries that grant's id as ``grant`` (set by the server from
     the token, never from params) and holds exactly that one grant's scopes while it is live, so a
     read-only token is not widened by another grant of the same identity;
   - a ``plugin`` principal with no ``grant`` is host-attributed (the plugin host names it) and holds the
     union of its identity's live grants;
   - any other device, session or agent principal with no ``grant`` holds nothing;
   - a principal whose ``grant`` cannot be read holds nothing, whatever its kind: an unreadable grant is
     not an absent one, so it never falls through to a plugin's identity grants. A grant is absent when
     nothing named ``grant`` is declared on the principal, its class, its bases or its metaclass (each
     class's own dictionary is read, so no metaclass can hide a declaration), or when the declared one
     reads ``None`` (SPINE's ``Principal`` default, the same answer for every kind). A declared one
     is read by the reader Python itself selects, the ``__getattribute__`` found in the dictionaries of the
     principal's class and its bases (no metaclass can answer in its place), never a ``__getattr__``
     fallback; if that read raises, even ``AttributeError``, it is unreadable. An unset ``__slots__`` grant
     is unreadable too. A grant nothing declares is read the ordinary way, so a ``__getattr__`` may supply
     one: it then counts as a named grant and holds only that grant's scopes;
   - a principal that names a grant and claims ``owner`` or ``internal`` holds nothing: those kinds come
     from how a request arrives, never from a token.
   Every set is cut to the kind's ceiling (``credentials.CEILING``), so a plugin, session or agent never
   holds ``approve`` and no device ever holds ``manage``, whatever a grant says. SPINE's ``Principal`` has
   no ``grant`` field yet (``questions.md``); until it has, token principals hold nothing.
3. ``scope_of(method)`` reads the method's scope from the method table on every call. A name the table
   does not have runs nothing (the API answers ``method_unknown``); it passes only for a principal holding
   all four scopes, so their answer is today's, and is refused to everyone else. A method that is not text
   at all (bytes, an object whose equality says ``run.stop``) has no row and never takes that shortcut: it
   is refused to every principal, ``method_not_text`` to one holding all four scopes.
4. A principal without that scope is refused ``scope_refused``, detail ``needs_<scope>``. ``route.list``,
   ``plugin.list`` and ``operator.read`` are ``read`` rows like any other.
5. ``run.stop``, exactly as the method table's row (4) "Who may Stop" freezes it (PROVISIONAL, the owner's
   decision): it needs a currently held ``run`` scope; an owner or a device with ``run`` may stop any run,
   one whose record names no principal included; a session principal only runs of its own session and the
   sessions it started; a plugin principal only a run whose record's ``launch.owner`` is that plugin, never
   a host-owned run, another plugin's, or one with no owner recorded. The row names no other kind, so an
   internal or agent principal is refused (``questions.md``). A target that cannot be read is refused to a
   session or plugin principal, which may stop only runs it can be shown to own.

Every refusal the authorizer makes is ``("scope_refused", detail)``; ``scope_refused`` is the method table's
code for it, and S5 (``control/hooks.py`` ``Hooks.authorize``) turns the pair into ``{"code", "detail"}``. A
method table that raises is not caught here: S5 refuses that call ``authorizer_failed`` and logs it.
"""

from __future__ import annotations

import inspect
import logging
import os
import threading
import types
from collections.abc import Callable
from typing import NamedTuple

from recursion_core.control.credentials import (ALL_SCOPES, PAGE_DEVICE_ID, SCOPE_REFUSED, CredentialStore,
                                                CredentialStoreError, ceiling)
from recursion_core.control.methods import METHODS

log = logging.getLogger(__name__)

PAGE_PRINCIPAL_ID = PAGE_DEVICE_ID   # control/hooks.py DEVICE: the page's door; reserved against every grant
# The page's device principal: read, run, approve (the brief's recommended option (a); the owner decides).
PAGE_SCOPES = frozenset({"read", "run", "approve"})
STOP_METHOD = "run.stop"
HOST_OWNER = "host"          # launch.owner of a run the host started (control/hooks.py launch_snapshot)
SERVICE_NAME = "control.scopes"
CREDENTIALS_PATH = ("control", "credentials.json")   # under the daemon's state directory

Verdict = tuple[str, str] | None


def table_scope(method: str) -> str | None:
    """The method's scope in the method table this daemon runs (``control.methods.METHODS``), or None."""

    row = METHODS.get(method) if isinstance(method, str) else None   # a list asked directly is no method
    return row.scope if row is not None else None


def register(hooks) -> None:
    """The registrant entry ``control/hooks.py`` calls once at composition: the authorizer into S5.

    ``ScopesService`` is not registered yet (see the module text): ``hooks.service(ScopesService(a))``
    is the one line that does it, for the change that brings the first plugin or session principal."""

    hooks.set("authorize", Authorizer(scope_of=table_scope))


class ScopesService:
    """Seam S13 service ``control.scopes``: attaches the credential store and the run records to the authorizer.

    ``start`` opens ``CredentialStore(<state_dir>/control/credentials.json)`` and reads run records with
    the control registry's ``get``. It never raises: a store that cannot be opened, no state directory, or
    a state directory or registry that cannot be read is logged and leaves token principals holding
    nothing (or no run record read), and the daemon still starts. It is ready once it has attached. ``stop`` detaches, so a call after it holds only what owner, internal and the page hold.

    Start and stop are one ordered sequence. ``start`` takes its place in that order when it begins, opens
    the store and reads the registry outside the order (so ``stop`` never waits for those reads), and then
    attaches and sets ``ready`` only if no ``stop`` came after its beginning; otherwise it attaches nothing,
    leaves ``ready`` as it is and logs that the start was set aside. ``stop`` detaches and clears ``ready``
    in one step of that order. So once ``stop`` has returned, nothing of a start that began before it can
    attach, open or use the credential store through the authorizer.
    """

    name = SERVICE_NAME

    def __init__(self, authorizer: Authorizer) -> None:
        self._authorizer = authorizer
        self.ready = False
        self._order = threading.Lock()   # start's final step and stop, one at a time
        self._stops = 0                  # how many stops the order has seen; a start compares it at its end

    def start(self, context) -> None:
        with self._order:
            began_after = self._stops   # this start's place in the order
        store = None
        try:
            state_dir = getattr(context, "state_dir", None)
            if state_dir is None:
                log.error("control.scopes: no state directory; no credential is honoured")
            else:
                store = CredentialStore(os.path.join(os.fspath(state_dir), *CREDENTIALS_PATH))
        except CredentialStoreError as exc:
            log.error("control.scopes: the credential store cannot be used (%s); no credential is honoured", exc)
        except Exception as exc:  # noqa: BLE001 - a state_dir that is not a path, or cannot be read, honours nothing
            # (the reads of #385 by DeepSeek: round 1 F3, a state_dir of 5; round 4 F2, a raising __fspath__ or
            # state_dir property)
            log.error("control.scopes: the state directory is not a usable path (%s); no credential is honoured",
                      type(exc).__name__)
        try:
            reader = getattr(getattr(context, "control_registry", None), "get", None)
        except Exception as exc:  # noqa: BLE001 - a registry that cannot be read reads no record (round 4, DeepSeek F2)
            log.error("control.scopes: the control registry cannot be read (%s); no run record is read",
                      type(exc).__name__)
            reader = None
        with self._order:
            if self._stops != began_after:
                # A stop came after this start began: what it read belongs to a lifetime that has ended.
                if self.ready:
                    log.warning("control.scopes: a start that began before the last stop was not applied; "
                                "a later start stands")
                else:
                    log.warning("control.scopes: a start that began before the last stop was not applied; "
                                "the service stays stopped")
                return
            self._authorizer.attach(credentials=store, run_record=reader if callable(reader) else None)
            self.ready = True

    def stop(self, context) -> None:
        with self._order:
            self._stops += 1
            self.ready = False
            self._authorizer.attach(credentials=None, run_record=None)


def _same_session(run_session: str, principal_session: str) -> bool:
    """The default subtree test: the run's own session only. The session lineage is the sessions lane's;
    until it is passed in, a session principal cannot stop a run of a session it started.

    A lineage passed as ``in_subtree`` must return exactly ``True`` for a session inside the principal's
    subtree. Anything else (``1``, ``"yes"``, a raise) counts as outside it, so a lineage that answers a
    truthy value stops nothing (the read of #377 by DeepSeek V4.1 Flash, F12)."""

    return run_session == principal_session


_ABSENT = object()   # nothing named grant declared, told apart from a declared grant that cannot be read


class _Binding(NamedTuple):
    """What one call decides from, read once at its top: the credential store, the run reader and the
    generation of the attach that published them. A call never mixes two attaches."""

    credentials: CredentialStore | None
    run_record: Callable[[str], dict | None] | None
    generation: int


def _get(mapping, key):
    """``mapping.get(key)`` for a mapping that is exactly a ``dict``; None for anything else, a ``dict`` subclass
    included (an ``OrderedDict`` too), so a run reader must answer plain dicts, as the control registry's
    ``get`` does. A target that cannot be read is not shown to be owned."""

    if type(mapping) is not dict:   # exactly a dict: no get of a subclass picks the target (round 6, DeepSeek F2)
        return None
    try:
        return mapping.get(key)
    except Exception:  # noqa: BLE001 - an unreadable record opens nothing
        return None


def _exact(value):
    """``value`` if it is exactly a ``str``, else None. A principal's kind and id decide what it holds, so a
    ``str`` subclass, whose own comparison or hash could answer anything, counts as no kind and no id (the
    round-5 read of #385 by DeepSeek: F1, a kind comparing equal to ``owner`` held all four scopes; F2, an id
    comparing equal to ``page`` held the page's)."""

    return value if type(value) is str else None


def _identity(principal) -> tuple[str | None, str | None]:
    """The principal's kind and id, each read ONCE, as exact text. One call decides everything from these two
    values: read twice, a principal answering ``plugin`` to the scope check and ``device`` or ``owner`` to the
    Stop fence stopped runs no plugin may stop (the round-6 read of #385 by DeepSeek, F1)."""

    return _exact(_attr(principal, "kind", None)), _exact(_attr(principal, "id", None))


def _attr(principal, name: str, default):
    """A principal's attribute; ``default`` when it is missing or reading it raises (it then counts as unknown)."""

    try:
        return getattr(principal, name, default)
    except Exception:  # noqa: BLE001 - an attribute that cannot be read makes the principal hold nothing
        return default


# A class's own MRO and dictionary, read through type's descriptors: no metaclass is asked for either.
_TYPE_MRO = type.__dict__["__mro__"].__get__
_TYPE_DICT = type.__dict__["__dict__"].__get__


def _special(cls: type, name: str):
    """``name`` as Python finds a special method for an instance of ``cls``: in the dictionaries of ``cls`` and
    its bases, in MRO order, asking no metaclass. ``_ABSENT`` when none has it."""

    for base in _TYPE_MRO(cls):
        found = _TYPE_DICT(base).get(name, _ABSENT)
        if found is not _ABSENT:
            return found
    return _ABSENT


def _instance_dictionary_shadowed(cls: type) -> bool:
    """Whether something other than Python's own ``__dict__`` descriptor stands at ``__dict__`` for instances of
    ``cls`` (the first class in its MRO that has one, read past any metaclass). Then nothing can show that an
    instance declares no ``grant``, so its absence is not taken as proved (round 8 of #377, Codex GPT-6 Astra 1
    and GPT-6.1 Sol 1: a shadowed instance dictionary hid an unreadable declaration from ``getattr_static``).
    The two native descriptor types are recognised by identity: ``in`` on a tuple of types asks the member
    type's metaclass for equality, and a metaclass ``__eq__`` answering true passed a shadow as Python's own
    (round 9: DeepSeek F1, Codex GPT-6 Astra 1 and GPT-6.1 Sol 1)."""

    for base in _TYPE_MRO(cls):
        member = _TYPE_DICT(base).get("__dict__", _ABSENT)
        if member is not _ABSENT:
            kind = type(member)
            return not ((kind is types.GetSetDescriptorType or kind is types.MemberDescriptorType)
                        and member.__name__ == "__dict__" and member.__objclass__ is base)
    return False


def _read_declared_grant(principal):
    """``principal.grant`` as Python reads it with no ``__getattr__`` fallback: the ``__getattribute__`` Python
    selects for the type, bound as Python binds it. A metaclass cannot put another reader in its place (the
    round-6 reads of #377, Astra 1 and Sol 1: ``type(principal).__getattribute__`` asked the metaclass, which
    could hand back ``object.__getattribute__`` and so read a declared default instead). A failed read raises."""

    cls = type(principal)
    reader = _special(cls, "__getattribute__")
    bind = _special(type(reader), "__get__")
    if bind is not _ABSENT:
        reader = bind(reader, principal, cls)
    return reader("grant")


class Authorizer:
    """``authorizer(principal, method, params) -> None | ("scope_refused", detail)``. See the module text.

    Not meant to be subclassed. A call reads the principal's kind and id once and decides through ``_held`` and
    ``_stop`` from those two values (round 6 of #385, DeepSeek F1), so overriding ``held`` changes only what
    ``held`` answers to a caller that asks it, never what a call decides (round 7, DeepSeek F2)."""

    def __init__(self, *, scope_of: Callable[[str], str | None], credentials: CredentialStore | None = None,
                 run_record: Callable[[str], dict | None] | None = None,
                 in_subtree: Callable[[str, str], bool] = _same_session,
                 page_scopes: frozenset[str] = PAGE_SCOPES) -> None:
        if not callable(scope_of) or not callable(in_subtree):
            raise TypeError("scope_of and in_subtree must be callable")
        self._lock = threading.Lock()   # attach and the one read of a call: never a half-published binding
        self._scope_of, self._credentials, self._run_record = scope_of, credentials, run_record
        self._generation = 0
        self._in_subtree = in_subtree
        self._page_scopes = frozenset(page_scopes)   # cut to the device ceiling by held(), like every set

    def attach(self, *, credentials: CredentialStore | None, run_record: Callable[[str], dict | None] | None) -> None:
        """What the service hands over at start (and takes back at stop), published as ONE binding: the store,
        the run reader and the next generation together, all or nothing. A call reads the binding once at its
        top and decides from that one value, so it sees the old pair or the new pair, never the old store with
        the new run reader, and never a store that vanished between two reads (the read of #385 by DeepSeek
        V4.1 Flash, F4: on 3.14t a stop landing between them raised AttributeError). Which attach wins is the
        service's order: start and stop are one sequence there (``ScopesService``)."""

        with self._lock:
            self._credentials, self._run_record = credentials, run_record
            self._generation += 1

    def _binding(self) -> _Binding:
        """The binding as the last attach published it, read in one step: one call reads it once."""

        with self._lock:
            return _Binding(self._credentials, self._run_record, self._generation)

    def held(self, principal) -> frozenset[str]:
        """The scopes ``principal`` holds now, within its kind's ceiling.

        A credential store that cannot be read raises here (``CredentialStoreError``, or whatever the store
        raised); a call of the authorizer itself turns that into the refusal ``credentials_unavailable``."""

        return self._held(principal, *_identity(principal), self._binding())

    def _held(self, principal, kind: str | None, pid: str | None, binding: _Binding) -> frozenset[str]:
        """``held`` for a kind and id already read once by the caller, from the caller's one binding."""

        try:
            # Each class's own dictionary first, read past any metaclass: a metaclass answering __dict__ hid a
            # declaration from getattr_static (round 7, Sol 1); then the instance and the metaclass as before.
            # A shadowed instance dictionary counts as a declaration: absence cannot be shown (round 8, Astra 1,
            # Sol 1), so the grant is read with the type's reader and, if that fails, the principal holds nothing.
            declared = (_instance_dictionary_shadowed(type(principal))
                        or _special(type(principal), "grant") is not _ABSENT
                        or _special(type(type(principal)), "grant") is not _ABSENT
                        or inspect.getattr_static(principal, "grant", _ABSENT) is not _ABSENT
                        or inspect.getattr_static(type(principal), "grant", _ABSENT) is not _ABSENT)
            if declared:
                # Declared on the principal, its class or its metaclass: read with the type's own reader, a
                # custom __getattribute__ included, but never a __getattr__ fallback, so no default stands in
                # for a read that failed; such a failure, AttributeError included, is unreadable (the round-3
                # reads of #377, Astra 1 and Sol 1; round 4, Astra 1; round 5, Astra 1, Sol 1 and DeepSeek F6:
                # a metaclass declaration no instance read reaches is unreadable too). The reader is the one
                # Python selects, so a metaclass cannot answer for it (round 6, Astra 1 and Sol 1).
                grant = _read_declared_grant(principal)
            else:
                grant = getattr(principal, "grant", None)   # not declared: absent (SPINE's Principal today)
        except Exception:  # noqa: BLE001 - unreadable is not absent: such a principal holds nothing at all
            return frozenset()   # (the round-2 read of #377 by Sol, 2: it fell through to the plugin's grants)
        if grant is not None and kind in ("owner", "internal"):
            # Owner and internal come from how a request arrives, never from a token: a principal naming a grant
            # is a token principal, and one that also claims those kinds holds nothing (round 5, DeepSeek F5).
            return frozenset()
        credentials = binding.credentials   # the caller's one read: attach may publish another meanwhile
        if kind in ("owner", "internal"):
            held = ALL_SCOPES
        elif grant is not None:
            # A token principal: its one verified grant, while it is live, and only if it is this identity's.
            held = (credentials.grant_scopes(grant, kind, pid)
                    if credentials is not None and isinstance(grant, str) else frozenset())
        elif kind == "device" and pid == PAGE_PRINCIPAL_ID:
            held = self._page_scopes
        elif kind == "plugin" and credentials is not None and isinstance(pid, str):
            held = credentials.held(kind, pid)   # host-attributed: the identity's live grants
        else:
            held = frozenset()   # a device, session or agent principal holds only what its own grant gives
        return held & ceiling(kind)

    def __call__(self, principal, method, params) -> Verdict:
        if _attr(principal, "in_run_unit", True) is not False:
            return (SCOPE_REFUSED, "peer is inside a run unit")
        kind, pid = _identity(principal)   # read once; the whole decision below uses only these (round 6, F1)
        binding = self._binding()          # read once; held and the Stop fence both decide from it
        try:
            held = self._held(principal, kind, pid, binding)
        except Exception:  # noqa: BLE001 - a credential store that cannot be read refuses; it never lets through
            log.exception("the credential store could not be read; the call is refused")
            return (SCOPE_REFUSED, "credentials_unavailable")
        # The method table is the composition's own; if reading it raises, S5 refuses the call (authorizer_failed)
        # and logs it, so the failure is left to S5.
        if issubclass(type(method), str):
            # Its text, as a plain str: no __eq__ or __hash__ of a subclass decides which row applies, so a method
            # name that compares unequal cannot step past the Stop row below (round 6, DeepSeek 5).
            method = str.__str__(method)
        if type(method) is not str:
            # Not text at all (bytes, an object whose equality says run.stop): no row, and never the shortcut
            # below, so no principal steps past the Stop row's refusal for its kind. Those holding fewer than
            # all four scopes keep today's answer.
            return (SCOPE_REFUSED, "method_not_text" if held >= ALL_SCOPES else "method_not_in_table")
        scope = self._scope_of(method)
        if scope is None:
            return None if held >= ALL_SCOPES else (SCOPE_REFUSED, "method_not_in_table")
        if type(scope) is not str or scope not in ALL_SCOPES:   # exactly a str (round 5 of #385, DeepSeek F3)
            return (SCOPE_REFUSED, "scope_unknown")   # a table row naming no known scope opens nothing
        if scope not in held:
            return (SCOPE_REFUSED, f"needs_{scope}")
        if method == STOP_METHOD:
            if "run" not in held:
                # The Stop row's own rule, whatever the table's scope column says for run.stop.
                return (SCOPE_REFUSED, "needs_run")
            return self._stop(kind, pid, params, binding)
        return None

    def _stop(self, kind: str | None, pid: str | None, params, binding: _Binding) -> Verdict:
        if kind in ("owner", "device"):
            return None    # with run: any run, including one whose record names no principal
        if kind not in ("session", "plugin"):
            return (SCOPE_REFUSED, "stop_not_granted_to_kind")
        # Each read of params, record and launch is guarded (round 5, DeepSeek F1), and the run named is exactly
        # the text sent (round 6 of #385, DeepSeek F2: a str subclass or a dict subclass chose another record).
        run_id = _exact(_get(params, "run_id"))
        record, run_record = None, binding.run_record   # the call's one binding, like the store in held()
        if isinstance(run_id, str) and run_record is not None:
            try:
                record = run_record(run_id)
            except Exception:  # noqa: BLE001 - a target that cannot be read is not shown to be owned
                record = None
        launch = _get(record, "launch")
        # pid is exact text or None (_identity), and every record field the rule decides from is read once as
        # exact text: a str subclass in the record cannot answer host, a plugin's id or another session as it
        # pleases (round 7 of #385, DeepSeek F1).
        if type(launch) is not dict or not pid:
            return (SCOPE_REFUSED, "run_not_owned")
        if kind == "plugin":
            owner = _exact(_get(launch, "owner"))
            if owner and owner != HOST_OWNER and owner == pid:
                return None
            return (SCOPE_REFUSED, "run_not_owned")
        session = _exact(_get(launch, "session_id"))
        if session:
            try:
                if self._in_subtree(session, pid) is True:
                    return None
            except Exception:  # noqa: BLE001 - a lineage that cannot be read does not widen the subtree
                pass
        return (SCOPE_REFUSED, "run_outside_session")
