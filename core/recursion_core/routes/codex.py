"""Codex adapter: ``codex app-server``, JSON-RPC over JSONL on stdio (CSL-797, candidate). See README.md.

One stdio worker per request: ``initialize`` / ``initialized``, ``account/rateLimits/read``,
``thread/start``, ``turn/start``, then notifications until ``turn/completed``; stdin is closed at the
end. Method names other than ``account/rateLimits/read`` and ``account/rateLimits/updated``, and every
field name below, are unverified against the vendor's source and marked so.

Typed limit signals:

* ``account/rateLimits/read`` result and ``account/rateLimits/updated`` notifications: a window whose
  ``usedPercent`` is at least 100 is a limit, with its ``resetsAt`` (fields unverified; ``resetsAt``
  is translated from Unix epoch onto the breaker clock). An omitted window, or a percent
  that is not a number, is not a healthy reading and leaves the earlier window in place. Primary
  and secondary are separate windows, and a connection bucket does not erase a model bucket.
  An exhausted window with no numeric ``resetsAt`` does not erase a sibling's known reset. The
  grouped instant stays that known reset, and the signal says the reset is unknown so the breaker
  waits until the later of that instant and its own escalating cooldown. Snapshots are ordered only
  when they carry the same stamp kind (``generation``, or else ``seq``, ``sequence``, ``timestamp``,
  ``capturedAt``, ``updatedAt``, ``asOf``; names unverified). A generation is not compared with a
  timestamp. A healthy reading clears one stamp kind only when it is strictly newer by that same
  kind. A tie on that kind keeps the exhausted reading. An exhausted snapshot of a different kind
  keeps its own reset: a later healthy reading of one kind does not clear the others, and each
  stands until its reset. When neither snapshot carries a stamp, an
  ``account/rateLimits/updated`` received after ``account/rateLimits/read`` was sent wins over
  that read's result. Codex's default bucket (``limitId``
  ``codex``) is credential-wide. Any other ``limitId`` is the breaker model key itself: the requested
  route model is not used, so a notice for one bucket does not fence a different model. An
  ``api_key`` route that is told this read needs ChatGPT auth ignores that one error and continues
  the turn; a subscription route does not.
* a JSON-RPC error, or a failed turn, whose typed ``codexErrorInfo`` is ``usageLimitExceeded``
  (unverified name).

Agent message text is model payload and is never inspected. Free error text that only sounds like a
quota is a SHORT_FAULT (grill Q23).

B11 (CSL-783 F70, CSL-727 F19): the stdio worker is launched with ``-c check_for_update_on_startup=false``
(option name read from Codex source; see ``NO_UPDATE_OVERRIDE``) and this adapter starts nothing else: no updater, no ``codex update``. If the
app-server daemon mode is ever used, its config must hold ``updater.autoUpdateEnabled = false``;
``check_daemon_config`` refuses any other value, including a missing one.
"""

from __future__ import annotations

import json
import math

from recursion_core.routes import contract as c
from recursion_core.routes.base import RouteAdapter, _Protocol, scalar
from recursion_core.routes.launch import child_env

# Never inherited by a Codex child (B1 note).
CODEX_CREDENTIAL_ENV = ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY")
# The only names an api_key route may select and custody may fill: the user's own API keys. CODEX_ACCESS_TOKEN
# is a sign-in token, not an API key; Codex uses its own CLI login only and Recursion holds no ChatGPT token
# (Lucas, Nov 8; PRODUCT.md 3.3, O61), so it is scrubbed and never selectable.
CODEX_API_KEY_ENV = ("CODEX_API_KEY", "OPENAI_API_KEY")
# Top-level config key ``check_for_update_on_startup: Option<bool>`` (default true), read in openai/codex
# 8703d32bd0ea5cf26768859a3dd476df906ba053 codex-rs/config/src/config_toml.rs:533 and only consulted by the
# TUI's update check (codex-rs/tui/src/updates.rs:28, :153); no app-server source references the update
# check. A process-tree check on a real binary has not been run (hard rule 8).
NO_UPDATE_OVERRIDE = ("-c", "check_for_update_on_startup=false")
DEFAULT_LIMIT_ID = "codex"  # name of the credential-wide bucket (unverified)
_LIMIT_ERRORS = ("usageLimitExceeded",)  # codexErrorInfo values (unverified)
_AUTH_ERRORS = ("unauthorized",)  # codexErrorInfo values (unverified)


def daemon_config() -> dict:
    """The config Recursion passes if it ever runs the app-server daemon: the updater is off."""

    return {"updater": {"autoUpdateEnabled": False}}


def check_daemon_config(config: object) -> None:
    """Refuse to start a daemon unless ``updater.autoUpdateEnabled`` is explicitly ``False``."""

    updater = config.get("updater") if isinstance(config, dict) else None
    if not isinstance(updater, dict) or updater.get("autoUpdateEnabled") is not False:
        raise c.RouteError(c.LAUNCH_REFUSED, "codex daemon config must set updater.autoUpdateEnabled=false")


class CodexAdapter(RouteAdapter):
    engine = "codex"

    def __init__(self, *args, binary: str = "codex", daemon: dict | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if daemon is not None:
            check_daemon_config(daemon)
        self._binary, self._daemon = binary, daemon

    def argv(self, request: dict) -> list[str]:
        return [self._binary, *NO_UPDATE_OVERRIDE, "app-server"]

    def env(self, request: dict) -> dict[str, str]:
        cred = self.credential(request, CODEX_API_KEY_ENV)
        return child_env(self._parent_env, extra_names=("CODEX_HOME",), never=CODEX_CREDENTIAL_ENV, credential=cred)

    def drive(self, process, request, deadline_at, events):
        rid, model = request["request_id"], request["route"]["model"]
        rpc = _Rpc(self, process, deadline_at, events, model)
        try:
            rpc.call("initialize", {"clientInfo": {"name": "recursion", "version": "0"}})
            rpc.notify("initialized")
            kind = request["route"]["connection"]["kind"]
            try:
                limits = rpc.call("account/rateLimits/read", {})
            except _RpcError as err:
                # ChatGPT auth is required for this read. An API-key session is rejected with wording
                # that sounds like a quota ("rate limits"), which would short-fault every such request
                # before a turn. Ignore that one error on an api_key route and continue.
                # A typed usage limit or an auth failure is not that error: it still stops the turn.
                if kind != "api_key" or _typed_limit_or_auth(err.error):
                    raise
                events.append(c.event("usage", "codex.account/rateLimits/read", {
                    "ignored": scalar(err.error.get("message"))}))
                limits = None
            rpc._apply_snapshot(limits.get("rateLimits") if isinstance(limits, dict) else None,
                                "codex.account/rateLimits/read", local_seq=rpc._read_sent_seq)
            if rpc.limit is not None:
                return rpc.limit_result(rid, "limit before turn")
            thread = rpc.call("thread/start", {"model": model})
            inner = thread.get("thread") if isinstance(thread, dict) else None
            thread_id = inner.get("id") if isinstance(inner, dict) else None
            if not isinstance(thread_id, str):
                raise _Protocol("thread/start returned no thread id")
            # A limit learned while the thread was starting is already a typed signal. Do not start
            # the turn on a bucket the vendor has said is exhausted.
            if rpc.limit is not None:
                return rpc.limit_result(rid, "limit before turn")
            rpc.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": request["prompt"]}]})
            return rpc.until_turn_completed(rid)
        except _RpcError as err:
            return rpc.finish(_classify_error(rid, err.error, rpc.limit, events, model))
        finally:
            try:
                process.close_stdin()
            except OSError:
                pass  # the child's pipe is already gone; do not hide the drive error


class _RpcError(Exception):
    def __init__(self, error: dict) -> None:
        super().__init__(str(error.get("code")))
        self.error = error


class _Rpc:
    def __init__(self, adapter: CodexAdapter, process, deadline_at: float, events: list, model: str) -> None:
        self.adapter, self.process, self.deadline_at = adapter, process, deadline_at
        self.events, self.model = events, model
        self.next_id, self.limit, self.text = 1, None, []
        self._seq = 0
        self._read_sent_seq: int | None = None
        # (bucket, window) -> signal still exhausted.
        self._open_windows: dict[tuple[str, str], dict] = {}
        # (bucket, window) -> stamp kind -> (stamp, local seq, exhausted signal or None).
        # None means this kind's newest reading was healthy. Different kinds are not one order.
        self._kinds: dict[tuple[str, str], dict[str, tuple]] = {}

    def notify(self, method: str, params: dict | None = None) -> None:
        self.process.send(json.dumps({"method": method, **({"params": params} if params else {})}))

    def call(self, method: str, params: dict) -> object:
        rid, self.next_id = self.next_id, self.next_id + 1
        self._seq += 1
        if method == "account/rateLimits/read":
            # The result belongs to this request, not to the moment it arrives. An update received
            # after the send is newer than the result when neither side carries a stamp.
            self._read_sent_seq = self._seq
        self.process.send(json.dumps({"id": rid, "method": method, "params": params}))
        while True:
            msg = self._next()
            self._seq += 1
            if msg.get("id") == rid and "method" not in msg:
                if isinstance(msg.get("error"), dict):
                    raise _RpcError(msg["error"])
                return msg.get("result")
            self._notification(msg)

    def until_turn_completed(self, rid: str) -> dict:
        while True:
            msg = self._next()
            if msg.get("method") == "turn/completed":
                params = _object(msg.get("params"), "turn/completed params")
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                if turn.get("status") == "completed":
                    return c.result(rid, None, text="".join(self.text), events=self.events)
                error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
                return self.finish(_classify_error(rid, error, self.limit, self.events, self.model))
            self._seq += 1
            self._notification(msg)

    def _next(self) -> dict:
        msg = self.adapter.read_message(self.process, self.deadline_at)
        if msg is None:
            raise _RpcError({"code": "eof", "message": "app-server closed stdout", "_eof": True})
        return msg

    def _notification(self, msg: dict) -> None:
        method = msg.get("method")
        params = _object(msg.get("params"), "notification params")
        if "id" in msg and isinstance(method, str):  # a server request (e.g. an approval): not supported here
            self.process.send(json.dumps({"id": msg["id"], "error": {"code": -32601, "message": "unsupported"}}))
            self.events.append(c.event("server_request_refused", "codex.jsonrpc", {"method": method}))
        elif method == "account/rateLimits/updated":
            self._apply_snapshot(params.get("rateLimits"), "codex.account/rateLimits/updated")
        elif method == "item/completed":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                self.text.append(item["text"])  # model payload: kept as the answer, never classified

    def _apply_snapshot(self, snapshot: object, source: str, *, local_seq: int | None = None) -> None:
        """Fold one rate-limit snapshot into the windows still open.

        Each of primary and secondary is its own window, and each stamp kind is its own reading of
        that window. A reading replaces one kind only when it is strictly newer by that kind, or by
        local sequence when neither side of that kind is stamped. A tie keeps an exhausted reading.
        A healthy reading clears only that kind. An incomparable exhausted reading stays until its
        own reset. Healthy means a numeric usedPercent under 100. An omitted window, or a percent
        that is not a number, changes nothing.
        """

        if not isinstance(snapshot, dict):
            return
        _note_usage(snapshot, source, self.events)
        present = {name: snapshot[name] for name in ("primary", "secondary") if isinstance(snapshot.get(name), dict)}
        if not present:
            return
        bucket = _bucket(snapshot)
        stamp = _vendor_stamp(snapshot)
        local = self._seq if local_seq is None else local_seq
        kind = stamp[0] if stamp is not None else ""
        for name, raw in present.items():
            percent = _percent(raw.get("usedPercent"))
            if percent is None:
                continue
            key = (bucket, name)
            kinds = self._kinds.setdefault(key, {})
            prev = kinds.get(kind)
            if prev is not None:
                rank = _stamp_rank(stamp, local, prev[0], prev[1])
                # Strictly older changes nothing. A same-kind tie keeps an exhausted reading, so a
                # healthy snapshot does not clear it.
                if rank < 0 or (rank == 0 and percent < 100):
                    continue
            if percent >= 100:
                signal = _window_signal(snapshot, source, self.adapter.vendor_reset(raw.get("resetsAt")))
                kinds[kind] = (stamp, local, signal)
            else:
                kinds[kind] = (stamp, local, None)
            merged = _window_from_kinds(kinds)
            if merged is None:
                self._open_windows.pop(key, None)
            else:
                self._open_windows[key] = merged
        self.limit = _reported(self._open_windows)

    def limit_result(self, rid: str, detail: str) -> dict:
        return self.finish(c.result(rid, c.PLAN_LIMIT, limit=self.limit, events=self.events, detail=detail))

    def finish(self, res: dict) -> dict:
        """Attach every open window so the service books each, not only the reported signal."""

        if res.get("code") != c.PLAN_LIMIT:
            return res
        windows = _grouped(self._open_windows)
        primary = res.get("limit")
        if isinstance(primary, dict) and all(window != primary for window in windows):
            windows.append(primary)
        if windows:
            res["_windows"] = windows
        return res


def _bucket(snapshot: dict) -> str:
    limit_id = snapshot.get("limitId")
    if isinstance(limit_id, str) and limit_id and limit_id != DEFAULT_LIMIT_ID:
        return limit_id
    return "connection"


def _note_usage(snapshot: dict, source: str, events: list) -> None:
    windows = {name: snapshot[name] for name in ("primary", "secondary") if isinstance(snapshot.get(name), dict)}
    events.append(c.event("usage", source, {
        "limitId": scalar(snapshot.get("limitId")),
        **{f"{name}.usedPercent": scalar(w.get("usedPercent")) for name, w in windows.items()},
    }))


def _window_signal(snapshot: dict, source: str, reset_at: float | None) -> dict:
    limit_id = snapshot.get("limitId")
    if isinstance(limit_id, str) and limit_id and limit_id != DEFAULT_LIMIT_ID:
        return c.limit_signal("model", source, model=limit_id, reset_at=reset_at)
    return c.limit_signal("connection", source, reset_at=reset_at)


def _grouped(open_windows: dict[tuple[str, str], dict]) -> list[dict]:
    """One signal per scope and model. The reset is the latest known instant on that key.

    A window with no numeric reset does not erase a sibling's known instant. The signal is marked
    unknown so the breaker waits out the later of that instant and its own cooldown.
    """

    grouped: dict[tuple, list[dict]] = {}
    for signal in open_windows.values():
        grouped.setdefault((signal.get("scope"), signal.get("model")), []).append(signal)
    out: list[dict] = []
    for sigs in grouped.values():
        reset = None
        unknown = False
        source = sigs[-1]["source"]
        for sig in sigs:
            if sig.get("reset_unknown") is True:
                unknown = True
            value = _reset_value(sig)
            if value is None:
                unknown = True
                continue
            if reset is None or value > reset:
                reset = value
                source = sig["source"]
        scope, model = sigs[-1]["scope"], sigs[-1].get("model")
        signal = c.limit_signal(scope, source, model=model, reset_at=reset)
        if unknown:
            signal["reset_unknown"] = True
        out.append(signal)
    return out


def _reported(open_windows: dict[tuple[str, str], dict]) -> dict | None:
    chosen = None
    for signal in _grouped(open_windows):
        chosen = c.prefer_limit(chosen, signal)
    return chosen


def _window_from_kinds(kinds: dict[str, tuple]) -> dict | None:
    """The exhausted readings of one window. Incomparable kinds both stand.

    A missing reset does not drop a known one. The signal is marked unknown so one kind's missing
    instant is not stored as that known reset.
    """

    chosen = None
    unknown = False
    for _stamp, _local, signal in kinds.values():
        if signal is None:
            continue
        if _reset_value(signal) is None:
            unknown = True
        chosen = signal if chosen is None else _later_reset(chosen, signal)
    if chosen is None:
        return None
    if not unknown:
        return chosen
    marked = dict(chosen)
    marked["reset_unknown"] = True
    return marked


def _reset_value(signal: dict) -> float | None:
    value = signal.get("reset_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _later_reset(kept: dict, incoming: dict) -> dict:
    """The exhausted signal whose reset is later. A missing reset does not shorten a known one."""

    new_at, old_at = _reset_value(incoming), _reset_value(kept)
    if new_at is not None and (old_at is None or new_at > old_at):
        return incoming
    return kept


# Unverified stamp names. The first one present is the snapshot's order; a higher value is newer.
_STAMP_KEYS = ("generation", "seq", "sequence", "timestamp", "capturedAt", "updatedAt", "asOf")


def _vendor_stamp(snapshot: dict) -> tuple[str, float] | None:
    """The first numeric stamp and its field name. Different names are not one order."""

    for key in _STAMP_KEYS:
        value = snapshot.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            continue
        return key, float(value)
    return None


def _percent(value: object) -> float | None:
    """A real usedPercent. A bool or a non-numeric value such as the string 100 is unreadable."""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return float(value)


def _stamp_rank(new: tuple[str, float] | None, new_local: int,
                old: tuple[str, float] | None, old_local: int) -> int:
    """1 when ``new`` is strictly newer, -1 when it is strictly older, 0 on a tie or a mismatch.

    The same stamp kind is ordered by its value. Two snapshots with no stamp use local sequence.
    A generation is not newer than a timestamp just because its number is larger, and the reverse.
    """

    if new is not None and old is not None and new[0] == old[0]:
        if new[1] > old[1]:
            return 1
        if new[1] < old[1]:
            return -1
        return 0
    if new is None and old is None:
        if new_local > old_local:
            return 1
        if new_local < old_local:
            return -1
        return 0
    return 0


def _object(value: object, what: str) -> dict:
    """A JSON object field. ``None`` is an empty object; a list or a string is a protocol error."""

    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    raise _Protocol(f"{what} is not an object")


def _error_info(error: dict) -> object:
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    info = error.get("codexErrorInfo", data.get("codexErrorInfo"))
    if isinstance(info, dict):  # a tagged variant: {"usageLimitExceeded": {...}} (shape unverified)
        info = next(iter(info), None)
    return info


def _typed_limit_or_auth(error: dict) -> bool:
    return _error_info(error) in (*_LIMIT_ERRORS, *_AUTH_ERRORS)


def _classify_error(rid: str, error: dict, limit: dict | None, events: list, model: str) -> dict:
    info = _error_info(error)
    if info in _AUTH_ERRORS:
        return c.result(rid, c.AUTH_FAILED, events=events, detail=str(info))
    if info in _LIMIT_ERRORS:
        return c.result(rid, c.PLAN_LIMIT, limit=limit or c.limit_signal("connection", f"codex.{info}"),
                        events=events, detail="typed limit")
    # A saved exhausted-bucket signal is a typed limit even when the turn then fails with untyped text.
    # Auth (above) keeps precedence. EOF after that signal is the same limit, not an exit.
    if limit is not None:
        return c.result(rid, c.PLAN_LIMIT, limit=limit, events=events,
                        detail="exit after limit" if error.get("_eof") else "typed limit")
    if error.get("_eof"):
        return c.result(rid, c.ENGINE_EXITED, events=events, detail="stdout closed before turn/completed")
    if c.sounds_like_quota(error.get("message")):
        return c.result(rid, c.SHORT_FAULT, events=events, detail=f"untyped quota-like text on {model}")
    return c.result(rid, c.ENGINE_ERROR, events=events, detail=str(scalar(info) or scalar(error.get("code")) or "error"))
