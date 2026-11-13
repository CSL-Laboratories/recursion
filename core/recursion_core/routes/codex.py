"""Codex adapter: ``codex app-server``, JSON-RPC over JSONL on stdio (CSL-797, candidate). See README.md.

One stdio worker per request: ``initialize`` / ``initialized``, ``account/rateLimits/read``,
``thread/start``, ``turn/start``, then notifications until ``turn/completed``; stdin is closed at the
end. Method names other than ``account/rateLimits/read`` and ``account/rateLimits/updated``, and every
field name below, are unverified against the vendor's source and marked so.

Typed limit signals:

* ``account/rateLimits/read`` result and ``account/rateLimits/updated`` notifications: a window whose
  ``usedPercent`` is at least 100 is a limit, with its ``resetsAt`` (fields unverified; ``resetsAt``
  is translated from Unix epoch onto the breaker clock). Codex's default bucket (``limitId``
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
                if kind != "api_key":
                    raise
                events.append(c.event("usage", "codex.account/rateLimits/read", {
                    "ignored": scalar(err.error.get("message"))}))
                limits = None
            limit = _exhausted(limits.get("rateLimits") if isinstance(limits, dict) else None,
                               "codex.account/rateLimits/read", events, self.vendor_reset)
            if limit is not None:
                return c.result(rid, c.PLAN_LIMIT, limit=limit, events=events, detail="limit before turn")
            thread = rpc.call("thread/start", {"model": model})
            thread_id = thread.get("thread", {}).get("id") if isinstance(thread, dict) else None
            if not isinstance(thread_id, str):
                raise _Protocol("thread/start returned no thread id")
            rpc.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": request["prompt"]}]})
            return rpc.until_turn_completed(rid)
        except _RpcError as err:
            return _classify_error(rid, err.error, rpc.limit, events, model)
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

    def notify(self, method: str, params: dict | None = None) -> None:
        self.process.send(json.dumps({"method": method, **({"params": params} if params else {})}))

    def call(self, method: str, params: dict) -> object:
        rid, self.next_id = self.next_id, self.next_id + 1
        self.process.send(json.dumps({"id": rid, "method": method, "params": params}))
        while True:
            msg = self._next()
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    if not isinstance(msg["error"], dict):
                        raise _Protocol("JSON-RPC error must be an object")
                    raise _RpcError(msg["error"])
                return msg.get("result")
            self._notification(msg)

    def until_turn_completed(self, rid: str) -> dict:
        while True:
            msg = self._next()
            if msg.get("method") == "turn/completed":
                turn = (msg.get("params") or {}).get("turn") or {}
                if turn.get("status") == "completed":
                    return c.result(rid, None, text="".join(self.text), events=self.events)
                error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
                return _classify_error(rid, error, self.limit, self.events, self.model)
            self._notification(msg)

    def _next(self) -> dict:
        msg = self.adapter.read_message(self.process, self.deadline_at)
        if msg is None:
            raise _RpcError({"code": "eof", "message": "app-server closed stdout", "_eof": True})
        return msg

    def _notification(self, msg: dict) -> None:
        method, params = msg.get("method"), msg.get("params") or {}
        if "id" in msg and isinstance(method, str):  # a server request (e.g. an approval): not supported here
            self.process.send(json.dumps({"id": msg["id"], "error": {"code": -32601, "message": "unsupported"}}))
            self.events.append(c.event("server_request_refused", "codex.jsonrpc", {"method": method}))
        elif method == "account/rateLimits/updated":
            self.limit = _exhausted(params.get("rateLimits"), "codex.account/rateLimits/updated",
                                    self.events, self.adapter.vendor_reset) or self.limit
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                self.text.append(item["text"])  # model payload: kept as the answer, never classified


def _exhausted(snapshot: object, source: str, events: list, translate) -> dict | None:
    if not isinstance(snapshot, dict):
        return None
    windows = {name: snapshot.get(name) for name in ("primary", "secondary") if isinstance(snapshot.get(name), dict)}
    limit_id = snapshot.get("limitId")
    events.append(c.event("usage", source, {
        "limitId": scalar(limit_id),
        **{f"{name}.usedPercent": scalar(w.get("usedPercent")) for name, w in windows.items()},
    }))
    hit = [w for w in windows.values() if isinstance(w.get("usedPercent"), (int, float)) and w["usedPercent"] >= 100]
    if not hit:
        return None
    resets = [w["resetsAt"] for w in hit if isinstance(w.get("resetsAt"), (int, float))]
    reset_at = translate(max(resets)) if resets else None
    if isinstance(limit_id, str) and limit_id and limit_id != DEFAULT_LIMIT_ID:
        return c.limit_signal("model", source, model=limit_id, reset_at=reset_at)
    return c.limit_signal("connection", source, reset_at=reset_at)


def _classify_error(rid: str, error: dict, limit: dict | None, events: list, model: str) -> dict:
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    info = error.get("codexErrorInfo", data.get("codexErrorInfo"))
    if isinstance(info, dict):  # a tagged variant: {"usageLimitExceeded": {...}} (shape unverified)
        info = next(iter(info), None)
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
