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
2. The scopes the principal holds NOW: ``owner`` and ``internal`` hold all four; the page's own device
   principal (``device`` with id ``page``) holds ``PAGE_SCOPES``; any other device, plugin, session or
   agent principal holds what its live grants in the ``CredentialStore`` give it. Every set is cut to the
   kind's ceiling (``credentials.CEILING``), so a plugin, session or agent never holds ``approve`` and no
   device ever holds ``manage``, whatever a grant says.
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

from collections.abc import Callable

from recursion_core.control.credentials import ALL_SCOPES, SCOPE_REFUSED, CredentialStore, ceiling

PAGE_PRINCIPAL_ID = "page"   # control/hooks.py DEVICE: the page's door, before per-device principals
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
    until it is passed in, a session principal cannot stop a run of a session it started."""

    return run_session == principal_session


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
        """The scopes ``principal`` holds now, within its kind's ceiling."""

        kind, pid = getattr(principal, "kind", None), getattr(principal, "id", None)
        if kind in ("owner", "internal"):
            held = ALL_SCOPES
        elif kind == "device" and pid == PAGE_PRINCIPAL_ID:
            held = self._page_scopes
        elif self._credentials is not None and isinstance(pid, str):
            held = self._credentials.held(kind, pid)
        else:
            held = frozenset()
        return held & ceiling(kind)

    def __call__(self, principal, method, params) -> Verdict:
        if getattr(principal, "in_run_unit", True) is not False:
            return (SCOPE_REFUSED, "peer is inside a run unit")
        held = self.held(principal)
        scope = self._scope_of(method) if isinstance(method, str) else None
        if scope is None:
            return None if held >= ALL_SCOPES else (SCOPE_REFUSED, "method_not_in_table")
        if scope not in ALL_SCOPES:
            return (SCOPE_REFUSED, "scope_unknown")   # a table row naming no known scope opens nothing
        if scope not in held:
            return (SCOPE_REFUSED, f"needs_{scope}")
        if method == STOP_METHOD:
            return self._stop(principal, params)
        return None

    def _stop(self, principal, params) -> Verdict:
        kind, pid = principal.kind, principal.id
        if kind in ("owner", "device"):
            return None    # with run: any run, including one whose record names no principal
        if kind not in ("session", "plugin"):
            return (SCOPE_REFUSED, "stop_not_granted_to_kind")
        run_id = params.get("run_id") if isinstance(params, dict) else None
        record = None
        if isinstance(run_id, str) and self._run_record is not None:
            try:
                record = self._run_record(run_id)
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
