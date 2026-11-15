"""Operator inspect reads: a GET-only surface for a separate local process (CSL-798, candidate). See README.md.

``OperatorReads.call(method, params) -> dict`` serves three O7 v0 reads and nothing else::

    operator.positions.inspect   rows under result["positions"]
    operator.decisions.inspect   rows under result["decisions"]
    operator.budgets.inspect     rows under result["budgets"]

There is no mutating method. Any other method name answers ``method_not_allowed``, ``params`` must be
``{}`` (or ``None``) or the answer is ``request_invalid``, and ``call`` never raises. This module imports
nothing from the run control API, so a caller of this surface has no path to start or stop a run.

Where the rows come from. On this branch Recursion has no paper order book, no approval-decision store
and no authority ledger; those are other build items. So this module owns no state: the host that
composes the server injects one source per read. Once composed, the reads are meant to come from

- ``positions``: the order book of the paper venue,
- ``decisions``: the approval gate's recorded decisions,
- ``budgets``: the authority ledger's budgets.

Uncomposed (no source attached for a read), the read answers ``ok`` with an empty list and
``empty_reason`` ``no_<read>_source_attached``. It never invents rows.

Source contract: a zero-argument callable returning a mapping::

    {"venue": "paper" | "stock", "rows": [flat row dicts],
     "stale"?: bool, "unreconciled"?: bool, "empty_reason"?: str, "extra"?: {field: JSON value}}

Every row is labelled with the source's venue (``venue`` is set when the row has none). The whole read
is refused, never partly served, when a source venue is not ``paper``/``stock`` or a row carries another
venue (``venue_refused``), when a row is not a flat dict of JSON scalars with snake_case keys or holds a
non-finite float (``row_invalid``), when the source's answer has the wrong shape (``source_invalid``), or
when the source raises (``source_unavailable``; only the exception type name is reported, never its
message). A source that reports ``stale`` or ``unreconciled`` answers ``stale_or_unreconciled`` and
still carries its rows.

Envelope (every answer)::

    api_version "o7.operator.v0", command, request_id, profile, authority_effect "none",
    order_path "none", code, status_code (same string as code), result, errors, stale, unreconciled,
    served_at, plus empty_reason when an ok read has no rows and detail on a refusal.

``profile`` is the summary the host supplies, or ``{"profile_id": None, "signature_state":
"no_profile"}``; this module never makes up a profile id.

``connect(path=None, *, timeout_s=5.0)`` returns the client for the owner-only Unix socket transport in
``operator_reads_socket.py``.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from collections.abc import Callable, Mapping

API_VERSION = "o7.operator.v0"
METHODS = {
    "operator.positions.inspect": "positions",
    "operator.decisions.inspect": "decisions",
    "operator.budgets.inspect": "budgets",
}
READS = frozenset(METHODS.values())
INSPECT_METHOD = re.compile(r"operator\.[a-z][a-z0-9_]*\.inspect")
VENUES = frozenset({"paper", "stock"})
NO_PROFILE = {"profile_id": None, "signature_state": "no_profile"}

OK = "ok"
METHOD_NOT_ALLOWED, REQUEST_INVALID = "method_not_allowed", "request_invalid"
SOURCE_UNAVAILABLE, SOURCE_INVALID = "source_unavailable", "source_invalid"
ROW_INVALID, VENUE_REFUSED, STALE_OR_UNRECONCILED = "row_invalid", "venue_refused", "stale_or_unreconciled"
# Answered by the socket transport, in the same envelope.
REQUEST_TOO_LARGE, REQUEST_TIMEOUT, BUSY = "request_too_large", "request_timeout", "busy"
PEER_REFUSED, REPLY_TOO_LARGE = "peer_refused", "reply_too_large"
# Answered by the client alone (no envelope: nothing was served).
CONTROL_API_UNAVAILABLE = "control_api_unavailable"

_ERRORS = {
    METHOD_NOT_ALLOWED: ("Only the three operator inspect reads are served here.",
                         "Call one of the three operator inspect reads."),
    REQUEST_INVALID: ("The request is not a read with empty params.",
                      "Send the method name with params {}."),
    SOURCE_UNAVAILABLE: ("The source for this read raised; nothing was read.",
                         "Check the host that supplies this read, then read again."),
    SOURCE_INVALID: ("The source for this read answered in the wrong shape; nothing is shown.",
                     "Fix the source to follow the source contract."),
    ROW_INVALID: ("A row is not a flat dict of JSON scalars; the whole read is withheld.",
                  "Fix the source rows, then read again."),
    VENUE_REFUSED: ("A source or row names a venue this surface does not serve; the whole read is withheld.",
                    "Serve only paper or stock rows, each from a source of the same venue."),
    STALE_OR_UNRECONCILED: ("The source marks these rows stale or unreconciled.",
                            "Refresh or reconcile the source, then read again."),
    REQUEST_TOO_LARGE: ("The request line is over the size cap.", "Send one short JSON object line."),
    REQUEST_TIMEOUT: ("The request line did not arrive before the read deadline.",
                      "Send the whole request line at once."),
    BUSY: ("Too many connections are open; this one was not served.", "Retry after a short wait."),
    PEER_REFUSED: ("The connecting process is not the socket owner's user.",
                   "Connect as the user that runs the server."),
    REPLY_TOO_LARGE: ("The answer is over the reply size cap; nothing is shown.",
                      "Have the source return fewer rows."),
}
CODES = frozenset({OK, CONTROL_API_UNAVAILABLE, *_ERRORS})

_KEY = re.compile(r"[a-z][a-z0-9_]*")
_SCALARS = (str, int, float, bool, type(None))
_SOURCE_KEYS = frozenset({"venue", "rows", "stale", "unreconciled", "empty_reason", "extra"})
_COMMAND_MAX = 128

# source() -> {"venue", "rows", "stale"?, "unreconciled"?, "empty_reason"?, "extra"?}; injected by the host.
Source = Callable[[], Mapping]


class _Refused(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


def _envelope(command, code: str, profile: Mapping, *, result=None, detail=None, stale=False,
              unreconciled=False, empty_reason=None, served_at=None) -> dict:
    out = {
        "api_version": API_VERSION,
        "command": command if type(command) is str and len(command) <= _COMMAND_MAX else None,
        "request_id": "req_" + uuid.uuid4().hex,
        "profile": json.loads(json.dumps(profile)),
        "authority_effect": "none",
        "order_path": "none",
        "code": code,
        "status_code": code,
        "result": {} if result is None else result,
        "errors": [],
        "stale": stale,
        "unreconciled": unreconciled,
        "served_at": served_at,
    }
    if code != OK:
        message, recovery = _ERRORS[code]
        out["errors"] = [{"code": code, "status_code": code, "message": message, "recovery": recovery}]
    if empty_reason is not None:
        out["empty_reason"] = empty_reason
    if detail is not None:
        out["detail"] = detail
    return out


def refusal(code: str, *, command=None, detail: str | None = None, profile: Mapping | None = None) -> dict:
    """A non-ok envelope with an empty result. The socket transport uses it for its own refusals."""

    if code == OK or code not in _ERRORS:
        raise ValueError("refusal needs a non-ok envelope code")
    return _envelope(command, code, NO_PROFILE if profile is None else profile, detail=detail)


def _row(index: int, row: object, venue: str) -> dict:
    if type(row) is not dict:
        raise _Refused(ROW_INVALID, f"row {index}: not_a_dict")
    out = {}
    for key, value in row.items():
        if type(key) is not str or _KEY.fullmatch(key) is None:
            raise _Refused(ROW_INVALID, f"row {index}: key")
        if type(value) not in _SCALARS:
            raise _Refused(ROW_INVALID, f"row {index}: value_type")
        if type(value) is float and not math.isfinite(value):
            raise _Refused(ROW_INVALID, f"row {index}: non_finite")
        out[key] = value
    if "venue" in out and out["venue"] != venue:
        raise _Refused(VENUE_REFUSED, f"row {index}: venue")
    out["venue"] = venue
    return out


def _flag(answer: Mapping, name: str) -> bool:
    value = answer.get(name, False)
    if type(value) is not bool:
        raise _Refused(SOURCE_INVALID, name)
    return value


def _json_copy(value: object, what: str):
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise _Refused(SOURCE_INVALID, what) from None


class OperatorReads:
    """The three operator inspect reads over host-injected sources. ``call`` is the only entry point."""

    def __init__(self, sources: Mapping[str, Source] | None = None, *, profile: Mapping | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        sources = dict(sources or {})
        unknown = sorted(set(sources) - READS, key=str)
        if unknown:
            raise ValueError(f"unknown read(s) {unknown}; sources are keyed by {sorted(READS)}")
        for name, source in sources.items():
            if not callable(source):
                raise TypeError(f"the {name} source must be a zero-argument callable")
        if profile is None:
            profile = NO_PROFILE
        elif not isinstance(profile, Mapping):
            raise TypeError("profile must be a mapping")
        try:
            self._profile = json.loads(json.dumps(dict(profile), allow_nan=False))
        except (TypeError, ValueError, RecursionError):
            raise ValueError("profile must be plain JSON") from None
        self._sources, self._clock = sources, clock

    @property
    def profile(self) -> dict:
        return json.loads(json.dumps(self._profile))

    def call(self, method: str, params: dict | None = None) -> dict:
        """Answer one read. Never raises; every failure is a typed code in the envelope."""

        try:
            return self._call(method, params)
        except _Refused as exc:
            return _envelope(method, exc.code, self._profile, detail=exc.detail, served_at=self._now())
        except Exception as exc:  # noqa: BLE001 - the surface answers typed, whatever went wrong
            return _envelope(method, SOURCE_UNAVAILABLE, self._profile, detail=type(exc).__name__,
                             served_at=self._now())

    def _now(self) -> float | None:
        try:
            now = self._clock()
        except Exception:  # noqa: BLE001
            return None
        return float(now) if type(now) in (int, float) and math.isfinite(now) else None

    def _call(self, method: object, params: object) -> dict:
        read = METHODS.get(method) if type(method) is str else None
        if read is None:
            raise _Refused(METHOD_NOT_ALLOWED, "method")
        if params is not None and (type(params) is not dict or params):
            raise _Refused(REQUEST_INVALID, "params")
        source = self._sources.get(read)
        if source is None:
            return _envelope(method, OK, self._profile, result={read: []},
                             empty_reason=f"no_{read}_source_attached", served_at=self._now())
        try:
            answer = source()
        except Exception as exc:  # noqa: BLE001 - the type name only; a message could carry data
            raise _Refused(SOURCE_UNAVAILABLE, type(exc).__name__) from None
        if not isinstance(answer, Mapping) or not set(answer) <= _SOURCE_KEYS:
            raise _Refused(SOURCE_INVALID, "answer")
        venue = answer.get("venue")
        if type(venue) is not str or venue not in VENUES:
            raise _Refused(VENUE_REFUSED, "source venue")
        rows = answer.get("rows")
        if type(rows) not in (list, tuple):
            raise _Refused(SOURCE_INVALID, "rows")
        result: dict = {read: [_row(i, row, venue) for i, row in enumerate(rows)]}
        extra = answer.get("extra", {})
        if not isinstance(extra, Mapping):
            raise _Refused(SOURCE_INVALID, "extra")
        for key, value in extra.items():
            if type(key) is not str or _KEY.fullmatch(key) is None or key in READS:
                raise _Refused(SOURCE_INVALID, "extra")
            result[key] = _json_copy(value, "extra")
        stale, unreconciled = _flag(answer, "stale"), _flag(answer, "unreconciled")
        empty_reason = None
        if not result[read]:
            empty_reason = answer.get("empty_reason", "source_reported_no_rows")
            if type(empty_reason) is not str or not empty_reason:
                raise _Refused(SOURCE_INVALID, "empty_reason")
        elif "empty_reason" in answer:
            raise _Refused(SOURCE_INVALID, "empty_reason")
        code = STALE_OR_UNRECONCILED if stale or unreconciled else OK
        return _envelope(method, code, self._profile, result=result, stale=stale, unreconciled=unreconciled,
                         empty_reason=empty_reason, served_at=self._now())


def connect(path: str | None = None, *, timeout_s: float = 5.0):
    """The client for the owner-only Unix socket: ``call(method, params) -> dict``, which never raises.

    ``path`` defaults to ``$XDG_RUNTIME_DIR/recursion/operator-reads.sock``, else
    ``/run/user/<uid>/recursion/operator-reads.sock``. Nothing is opened until ``call``.
    """

    from recursion_core.control.operator_reads_socket import OperatorReadsClient

    return OperatorReadsClient(path, timeout_s=timeout_s)
