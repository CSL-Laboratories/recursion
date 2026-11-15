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
     not an absent one, so it never falls through to a plugin's identity grants.
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

Every refusal is ``("scope_refused", detail)``; ``scope_refused`` is the method table's code for it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

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
    the control registry's ``get``. It never raises: a store that cannot be opened (or no state directory)
    is logged and leaves token principals holding nothing, and the daemon still starts. It is ready once
    it has attached. ``stop`` detaches, so a call after it holds only what owner, internal and the page hold.
    """

    name = SERVICE_NAME

    def __init__(self, authorizer: Authorizer) -> None:
        self._authorizer = authorizer
        self.ready = False

    def start(self, context) -> None:
        store = None
        state_dir = getattr(context, "state_dir", None)
        if state_dir is None:
            log.error("control.scopes: no state directory; no credential is honoured")
        else:
            try:
                store = CredentialStore(os.path.join(os.fspath(state_dir), *CREDENTIALS_PATH))
            except CredentialStoreError as exc:
                log.error("control.scopes: the credential store cannot be used (%s); no credential is honoured", exc)
            except (TypeError, ValueError) as exc:   # a state_dir that is not a path (the read of #385 by DeepSeek, F3)
                log.error("control.scopes: the state directory is not a path (%s); no credential is honoured",
                          type(exc).__name__)
        registry = getattr(context, "control_registry", None)
        reader = getattr(registry, "get", None)
        self._authorizer.attach(credentials=store, run_record=reader if callable(reader) else None)
        self.ready = True

    def stop(self, context) -> None:
        self.ready = False
        self._authorizer.attach(credentials=None, run_record=None)


def _same_session(run_session: str, principal_session: str) -> bool:
    """The default subtree test: the run's own session only. The session lineage is the sessions lane's;
    until it is passed in, a session principal cannot stop a run of a session it started.

    A lineage passed as ``in_subtree`` must return exactly ``True`` for a session inside the principal's
    subtree. Anything else (``1``, ``"yes"``, a raise) counts as outside it, so a lineage that answers a
    truthy value stops nothing (the read of #377 by DeepSeek V4.1 Flash, F12)."""

    return run_session == principal_session


def _attr(principal, name: str, default):
    """A principal's attribute; ``default`` when it is missing or reading it raises (it then counts as unknown)."""

    try:
        return getattr(principal, name, default)
    except Exception:  # noqa: BLE001 - an attribute that cannot be read makes the principal hold nothing
        return default


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

    def attach(self, *, credentials: CredentialStore | None, run_record: Callable[[str], dict | None] | None) -> None:
        """What the service hands over at start (and takes back at stop). A call in flight reads each of the two
        once, so it sees the old one or the new one, never a store that vanished between two reads (the read of
        #385 by DeepSeek V4.1 Flash, F4: on 3.14t a stop landing between them raised AttributeError)."""

        self._credentials, self._run_record = credentials, run_record

    def held(self, principal) -> frozenset[str]:
        """The scopes ``principal`` holds now, within its kind's ceiling."""

        kind, pid = _attr(principal, "kind", None), _attr(principal, "id", None)
        try:
            grant = getattr(principal, "grant", None)   # absent (SPINE's Principal today): None
        except Exception:  # noqa: BLE001 - unreadable is not absent: such a principal holds nothing at all
            return frozenset()   # (the round-2 read of #377 by Sol, 2: it fell through to the plugin's grants)
        credentials = self._credentials   # read once: attach may replace it while this call runs
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
        try:
            held = self.held(principal)
        except Exception:  # noqa: BLE001 - a credential store that cannot be read refuses; it never lets through
            log.exception("the credential store could not be read; the call is refused")
            return (SCOPE_REFUSED, "credentials_unavailable")
        # The method table is the composition's own; if reading it raises, S5 refuses the call (authorizer_failed)
        # and logs it, so the failure is left to S5.
        scope = self._scope_of(method) if isinstance(method, str) else None
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
        run_id = params.get("run_id") if isinstance(params, dict) else None
        record, run_record = None, self._run_record   # read once, like the store in held()
        if isinstance(run_id, str) and run_record is not None:
            try:
                record = run_record(run_id)
            except Exception:  # noqa: BLE001 - a target that cannot be read is not shown to be owned
                record = None
        launch = record.get("launch") if isinstance(record, dict) else None
        if not isinstance(launch, dict) or not isinstance(pid, str) or not pid:
            return (SCOPE_REFUSED, "run_not_owned")
        if kind == "plugin":
            owner = launch.get("owner")
            if isinstance(owner, str) and owner and owner != HOST_OWNER and owner == pid:
                return None
            return (SCOPE_REFUSED, "run_not_owned")
        session = launch.get("session_id")
        if isinstance(session, str) and session:
            try:
                if self._in_subtree(session, pid) is True:
                    return None
            except Exception:  # noqa: BLE001 - a lineage that cannot be read does not widen the subtree
                pass
        return (SCOPE_REFUSED, "run_outside_session")
