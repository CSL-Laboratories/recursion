"""The control API's authorizer: one decision per client call, from the method table's scope column.

Lane CONTROL, frontier plan v3.3, step 3, built in phase c0 against a stand-in for seam S5. This module
is a registrant that ``control/hooks.py`` imports by name once lane SPINE's seams land (its ``REGISTRANTS``
list names ``recursion_core.control.scopes``). In THIS pull request ``register(hooks)`` registers
nothing, so no daemon behaviour changes; the authorizer is registered into S5 in the next one, together
with the method table it reads and the tests through every entry point.

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
     not an absent one, so it never falls through to a plugin's identity grants. A grant is absent only
     when nothing named ``grant`` is declared on the principal, its class or its metaclass. A declared one
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
   all four scopes, so their answer is today's, and is refused to everyone else.
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
from collections.abc import Callable

from recursion_core.control.credentials import ALL_SCOPES, PAGE_DEVICE_ID, SCOPE_REFUSED, CredentialStore, ceiling

log = logging.getLogger(__name__)

PAGE_PRINCIPAL_ID = PAGE_DEVICE_ID   # control/hooks.py DEVICE: the page's door; reserved against every grant
# The page's device principal: read, run, approve (the brief's recommended option (a); the owner decides).
PAGE_SCOPES = frozenset({"read", "run", "approve"})
STOP_METHOD = "run.stop"
HOST_OWNER = "host"          # launch.owner of a run the host started (control/hooks.py launch_snapshot)

Verdict = tuple[str, str] | None


def register(hooks) -> None:
    """The registrant entry ``control/hooks.py`` calls once at composition. Registers nothing yet.

    The authorizer goes into S5 with ``hooks.set("authorize", ...)`` in the next pull request, which is
    stacked on the seams. Until then this module changes no answer of any daemon.
    """

    return None


def _same_session(run_session: str, principal_session: str) -> bool:
    """The default subtree test: the run's own session only. The session lineage is the sessions lane's;
    until it is passed in, a session principal cannot stop a run of a session it started.

    A lineage passed as ``in_subtree`` must return exactly ``True`` for a session inside the principal's
    subtree. Anything else (``1``, ``"yes"``, a raise) counts as outside it, so a lineage that answers a
    truthy value stops nothing (the read of #377 by DeepSeek V4.1 Flash, F12)."""

    return run_session == principal_session


_ABSENT = object()   # nothing named grant declared, told apart from a declared grant that cannot be read


def _get(mapping, key):
    """``mapping.get(key)`` for a dict; None for anything else, and None when the read raises (a dict subclass
    can): a target that cannot be read is not shown to be owned."""

    if not isinstance(mapping, dict):
        return None
    try:
        return mapping.get(key)
    except Exception:  # noqa: BLE001 - an unreadable record opens nothing
        return None


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
    """``authorizer(principal, method, params) -> None | ("scope_refused", detail)``. See the module text."""

    def __init__(self, *, scope_of: Callable[[str], str | None], credentials: CredentialStore | None = None,
                 run_record: Callable[[str], dict | None] | None = None,
                 in_subtree: Callable[[str, str], bool] = _same_session,
                 page_scopes: frozenset[str] = PAGE_SCOPES) -> None:
        if not callable(scope_of) or not callable(in_subtree):
            raise TypeError("scope_of and in_subtree must be callable")
        self._scope_of, self._credentials, self._run_record = scope_of, credentials, run_record
        self._in_subtree = in_subtree
        self._page_scopes = frozenset(page_scopes)   # cut to the device ceiling by held(), like every set

    def held(self, principal) -> frozenset[str]:
        """The scopes ``principal`` holds now, within its kind's ceiling.

        A credential store that cannot be read raises here (``CredentialStoreError``, or whatever the store
        raised); a call of the authorizer itself turns that into the refusal ``credentials_unavailable``."""

        kind, pid = _attr(principal, "kind", None), _attr(principal, "id", None)
        try:
            declared = (inspect.getattr_static(principal, "grant", _ABSENT) is not _ABSENT
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
        if kind in ("owner", "internal"):
            held = ALL_SCOPES
        elif grant is not None:
            # A token principal: its one verified grant, while it is live, and only if it is this identity's.
            held = (self._credentials.grant_scopes(grant, kind, pid)
                    if self._credentials is not None and isinstance(grant, str) else frozenset())
        elif kind == "device" and pid == PAGE_PRINCIPAL_ID:
            held = self._page_scopes
        elif kind == "plugin" and self._credentials is not None and isinstance(pid, str):
            held = self._credentials.held(kind, pid)   # host-attributed: the identity's live grants
        else:
            held = frozenset()   # a device, session or agent principal holds only what its own grant gives
        return held & ceiling(kind)

    def __call__(self, principal, method, params) -> Verdict:
        if _attr(principal, "in_run_unit", True) is not False:
            return (SCOPE_REFUSED, "peer is inside a run unit")
        try:
            held = self.held(principal)
        except Exception:  # noqa: BLE001 - a credential store that cannot be read refuses; it never lets through
            log.exception("the credential store could not be read; the call is refused")
            return (SCOPE_REFUSED, "credentials_unavailable")
        # The method table is the composition's own; if reading it raises, S5 refuses the call (authorizer_failed)
        # and logs it, so the failure is left to S5.
        if issubclass(type(method), str):
            # Its text, as a plain str: no __eq__ or __hash__ of a subclass decides which row applies, so a method
            # name that compares unequal cannot step past the Stop row below (round 6, DeepSeek 5).
            method = str.__str__(method)
        scope = self._scope_of(method) if type(method) is str else None
        if scope is None:
            return None if held >= ALL_SCOPES else (SCOPE_REFUSED, "method_not_in_table")
        if not isinstance(scope, str) or scope not in ALL_SCOPES:
            return (SCOPE_REFUSED, "scope_unknown")   # a table row naming no known scope opens nothing
        if scope not in held:
            return (SCOPE_REFUSED, f"needs_{scope}")
        if method == STOP_METHOD:
            if "run" not in held:
                # The Stop row's own rule, whatever the table's scope column says for run.stop.
                return (SCOPE_REFUSED, "needs_run")
            return self._stop(principal, params)
        return None

    def _stop(self, principal, params) -> Verdict:
        kind, pid = _attr(principal, "kind", None), _attr(principal, "id", None)
        if kind in ("owner", "device"):
            return None    # with run: any run, including one whose record names no principal
        if kind not in ("session", "plugin"):
            return (SCOPE_REFUSED, "stop_not_granted_to_kind")
        run_id = _get(params, "run_id")   # each read of params, record and launch is guarded (round 5, DeepSeek F1)
        record = None
        if isinstance(run_id, str) and self._run_record is not None:
            try:
                record = self._run_record(run_id)
            except Exception:  # noqa: BLE001 - a target that cannot be read is not shown to be owned
                record = None
        launch = _get(record, "launch")
        if not isinstance(launch, dict) or not isinstance(pid, str) or not pid:
            return (SCOPE_REFUSED, "run_not_owned")
        if kind == "plugin":
            owner = _get(launch, "owner")
            if isinstance(owner, str) and owner and owner != HOST_OWNER and owner == pid:
                return None
            return (SCOPE_REFUSED, "run_not_owned")
        session = _get(launch, "session_id")
        if isinstance(session, str) and session:
            try:
                if self._in_subtree(session, pid) is True:
                    return None
            except Exception:  # noqa: BLE001 - a lineage that cannot be read does not widen the subtree
                pass
        return (SCOPE_REFUSED, "run_outside_session")
