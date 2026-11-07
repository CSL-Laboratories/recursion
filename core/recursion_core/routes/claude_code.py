"""Claude Code adapter: stream-json over stdio (CSL-797, candidate). See README.md.

Launch: ``claude -p --input-format stream-json --output-format stream-json --verbose --model <m>``.
One user message goes in as a JSONL line, stdin is closed, and stdout is read until the ``result``
envelope. The Claude subscription is used only here (Q12); other harnesses go through
``profile_route`` and ``subscription_routes``.

Typed limit signals read (field names from the PRODUCT.md 398cd812 11.2 records: ``rate_limit_event``
and ``api_error_params.rate_limit_info``; inner names marked unverified were not read from the
vendor's source):

* a ``rate_limit_event`` envelope whose ``rate_limit_info.status`` is ``"rejected"`` (unverified);
* ``api_error_params.rate_limit_info`` on a ``system``/``api_retry`` or an error ``result`` envelope;
* an envelope-level typed ``error == "rate_limit"`` on an error result (unverified).

``resetsAt`` (unverified, epoch seconds) becomes the signal's ``reset_at``. ``system``/``api_retry``
is surfaced as a typed ``retry`` event. Assistant and user message content is model payload and is
never inspected, so JSON or limit wording inside it cannot trip the breaker. Free text on an error
``result`` that only sounds like a quota is a SHORT_FAULT (grill Q23). Stderr is not read.
"""

from __future__ import annotations

import json

from recursion_core.routes import contract as c
from recursion_core.routes.base import RouteAdapter, scalar
from recursion_core.routes.launch import child_env

# Anthropic credential names a Claude Code child never inherits; an API-key route adds its one key back.
CLAUDE_CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


class ClaudeCodeAdapter(RouteAdapter):
    engine = "claude_code"

    def __init__(self, *args, binary: str = "claude", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._binary = binary

    def argv(self, request: dict) -> list[str]:
        model = request["route"]["model"]
        # --verbose: stream-json output in print mode is documented to need it (unverified on this box).
        return [self._binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--model", model]

    def env(self, request: dict) -> dict[str, str]:
        cred = self.credential(request, ("ANTHROPIC_API_KEY",))
        return child_env(self._parent_env, never=CLAUDE_CREDENTIAL_ENV, credential=cred)

    def drive(self, process, request, deadline_at, events):
        rid, model = request["request_id"], request["route"]["model"]
        process.send(json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": request["prompt"]}]}}))
        process.close_stdin()
        limit = None
        while True:
            msg = self.read_message(process, deadline_at)
            if msg is None:
                if limit is not None:
                    return c.result(rid, c.PLAN_LIMIT, limit=limit, events=events, detail="exit after limit")
                return c.result(rid, c.ENGINE_EXITED, events=events, detail="stdout closed before result")
            kind, sub = msg.get("type"), msg.get("subtype")
            if kind == "system" and sub == "api_retry":
                events.append(c.event("retry", "claude.system.api_retry", {
                    k: scalar(msg.get(k)) for k in ("attempt", "max_retries", "retry_delay_ms", "error_status", "error")}))
                limit = _rate_limit_info(msg.get("api_error_params"), "claude.api_retry.rate_limit_info") or limit
            elif kind == "rate_limit_event":
                info = msg.get("rate_limit_info")
                events.append(c.event("usage", "claude.rate_limit_event", _info_fields(info)))
                limit = _rejected(info, "claude.rate_limit_event", model) or limit
            elif kind == "result":
                return _classify_result(rid, msg, limit, events, model)
            # assistant / user / other system envelopes: model payload or progress, never classified


def _info_fields(info: object) -> dict:
    if not isinstance(info, dict):
        return {}
    return {k: scalar(info.get(k)) for k in ("status", "resetsAt", "rateLimitType")}


def _rejected(info: object, source: str, model: str) -> dict | None:
    if not isinstance(info, dict) or info.get("status") != "rejected":
        return None
    reset = info.get("resetsAt")
    # Claude session and weekly limits apply to the whole login across models (B-05): connection scope.
    return c.limit_signal("connection", source, reset_at=reset if isinstance(reset, (int, float)) else None)


def _rate_limit_info(params: object, source: str) -> dict | None:
    if not isinstance(params, dict) or not isinstance(params.get("rate_limit_info"), dict):
        return None
    info = dict(params["rate_limit_info"])
    info.setdefault("status", "rejected")  # present on a rejected turn even without a status (D2-17)
    return _rejected(info, source, "")


def _classify_result(rid: str, msg: dict, limit: dict | None, events: list, model: str) -> dict:
    if msg.get("is_error") is not True and msg.get("subtype") == "success":
        text = msg.get("result")
        return c.result(rid, None, text=text if isinstance(text, str) else "", events=events)
    limit = _rate_limit_info(msg.get("api_error_params"), "claude.result.rate_limit_info") or limit
    if limit is None and msg.get("error") == "rate_limit":
        limit = c.limit_signal("connection", "claude.result.error")
    if limit is not None:
        return c.result(rid, c.PLAN_LIMIT, limit=limit, events=events, detail="typed limit")
    # Auth error names and api_error_status are unverified field names.
    if msg.get("error") in ("authentication_failed", "oauth_token_expired") or msg.get("api_error_status") == 401:
        return c.result(rid, c.AUTH_FAILED, events=events, detail=str(msg.get("error") or "401"))
    text = msg.get("result")
    if c.sounds_like_quota(text) or any(c.sounds_like_quota(e) for e in msg.get("errors") or [] if isinstance(e, str)):
        return c.result(rid, c.SHORT_FAULT, events=events, detail=f"untyped quota-like text on {model}")
    return c.result(rid, c.ENGINE_ERROR, events=events, detail=str(scalar(msg.get("subtype")) or "error"))
