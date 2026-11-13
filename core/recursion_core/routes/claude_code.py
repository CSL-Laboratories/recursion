"""Claude Code adapter: stream-json over stdio (CSL-797, candidate). See README.md.

Launch: ``claude -p --input-format stream-json --output-format stream-json --verbose --model <m>``.
One user message goes in as a JSONL line, stdin is closed, and stdout is read until the ``result``
envelope. The Claude subscription is used only here (Q12); other harnesses go through
``profile_route`` and ``subscription_routes``.

Typed limit signals read (field names from the PRODUCT.md 055cc7a5 11.2 records: ``rate_limit_event``
and ``api_error_params.rate_limit_info``; inner names marked unverified were not read from the
vendor's source):

* a ``rate_limit_event`` envelope whose ``rate_limit_info.status`` is ``"rejected"``;
* ``api_error_params.rate_limit_info`` on an error ``result`` envelope (a missing status there counts
  as rejected);
* an envelope-level typed ``error == "rate_limit"`` on an error result.

``system``/``api_retry`` is a typed ``retry`` event only. A 429 Claude is still retrying does not
open the breaker, including when that envelope carries ``rate_limit_info``. ``resetsAt`` is Unix
epoch seconds and is translated onto the breaker clock (``vendor_reset``) before it is stored.
``rateLimitType`` ``seven_day_opus`` and ``seven_day_sonnet`` are that family's window (model scope);
``five_hour``, ``seven_day``, ``seven_day_overage_included``, ``overage`` and any unlisted type stay
connection scope. Assistant and user message content is model payload and is
never inspected, so JSON or limit wording inside it cannot trip the breaker. Free text on an error
``result`` that only sounds like a quota is a SHORT_FAULT (grill Q23). Stderr is not read.
"""

from __future__ import annotations

import json

from recursion_core.routes import bare_default as bd
from recursion_core.routes import contract as c
from recursion_core.routes.base import RouteAdapter, _Protocol, scalar
from recursion_core.routes.launch import child_env

# Lucas, Nov 7 ~6:34pm PST (correction to the Nov 8 briefing; replaces "refuse --bare, clear apiKeyHelper,
# CLAUDE_CODE_OAUTH_TOKEN and CLAUDE_CODE_SIMPLE"): launch the unmodified ``claude`` binary; each run uses
# the route the user picked and only that run's environment changes; nothing removes a sign-in method
# from every run. Recursion never edits the user's Claude Code settings or credential files, never sets
# DISABLE_LOGIN_COMMAND, and never sets, stores or reads CLAUDE_CODE_OAUTH_TOKEN itself.
#
# Subscription route: dropped for this run only, because they ignore or outrank the sign-in. The
# federation variable names are not in repository evidence; any name not on the allowlist is dropped
# anyway, so an unlisted federation variable never reaches a subscription run.
SUBSCRIPTION_DROP_ENV = (
    "CLAUDE_CODE_SIMPLE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
)
# The user's own sign-in and config location, passed through untouched on every route (name copied, value
# never inspected, logged or stored). No route drops CLAUDE_CODE_OAUTH_TOKEN.
USER_KEEP_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")
SUBSCRIPTION_KEEP_ENV = USER_KEEP_ENV
# API-key route: the user's key outranks the sign-in by the engine's own precedence, so only the bearer
# token and the cloud switches stay out of this run. The key comes from custody (ANTHROPIC_API_KEY) or the
# user's own apiKeyHelper.
API_KEY_DROP_ENV = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                    "CLAUDE_CODE_USE_FOUNDRY")
# Kept for callers that import the old name; it now means "names an API-key run does not inherit from the
# parent" (its own key comes from custody instead).
CLAUDE_CREDENTIAL_ENV = API_KEY_DROP_ENV + ("ANTHROPIC_API_KEY",)
API_KEY_HELPER_STATES = ("absent", "present")
# B20 (CSL-726): whether this claude build makes --bare the -p default, as reported by the surface from
# bare_default.probe_auth_mode(). Unknown refuses; a documented opt-out is passed explicitly.
BARE_DEFAULT_STATES = ("absent", "present", "unknown")
# Passthrough route: the method the user picked gets its own variables for this run, and nothing else from
# the daemon's environment (no other engine's key, no stray ANTHROPIC_API_KEY that would move the run to the
# API meter; PRODUCT.md 3.4 S8, 11.5). Names are from Claude Code's public docs (code.claude.com/docs/en/
# amazon-bedrock, google-vertex-ai, microsoft-foundry, llm-gateway; retrieved 2026-11-09), not from engine
# source. A method not listed here is refused, never widened to the whole environment.
PASSTHROUGH_METHOD_ENV = {
    "bedrock": ("CLAUDE_CODE_USE_BEDROCK", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK", "AWS_CONFIG_FILE",
                "AWS_SHARED_CREDENTIALS_FILE", "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION",
                # AWS SDK web-identity role (EKS IRSA and similar); names from the AWS SDKs and Tools
                # reference guide, "Assume role credential provider" (retrieved 2026-11-09).
                "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_SESSION_NAME"),
    "vertex": ("CLAUDE_CODE_USE_VERTEX", "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_VERTEX_BASE_URL",
               "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"),
    "foundry": ("CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
                "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_FOUNDRY_BASE_URL"),
    "gateway": ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS"),
}
# Per-model Vertex region overrides (VERTEX_REGION_CLAUDE_<model>) are a documented name family.
PASSTHROUGH_METHOD_PREFIX = {"vertex": ("VERTEX_REGION_CLAUDE_",)}
# The variables that make the run actually use the picked method. Without them claude falls back to the
# user's sign-in, so a route labelled Bedrock or gateway would bill the subscription (and a gateway would
# be handed the sign-in): refused before launch instead.
PASSTHROUGH_METHOD_REQUIRED = {
    "bedrock": ("CLAUDE_CODE_USE_BEDROCK",),
    "vertex": ("CLAUDE_CODE_USE_VERTEX",),
    "foundry": ("CLAUDE_CODE_USE_FOUNDRY",),
    "gateway": ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"),
}
# A CLAUDE_CODE_USE_* switch counts as on only for a clearly true value; anything else ("off", "no", a
# typo) is refused, since claude would read it as off and bill the sign-in under a cloud label (fail closed).
# The child receives the stripped lowercase spelling. Claude Code's bool parser accepts 1 / true / yes / on
# (decoded 2.1.169 env schema, https://github.com/wtfwhs/tengu-decoded blob versions/2.1.169/environment-variables.md,
# retrieved 2026-11-09). Padding or capitals are not passed through unchanged. The parent env is not edited.
_SWITCH_ON = ("1", "true", "yes", "on")
# rateLimitType from the Claude Code 2.1.205 schema quoted in
# https://github.com/meawoppl/rust-code-agent-sdks/pull/180 (extracted from the CLI binary) and from
# anthropics/claude-code#50518. Retrieved 2026-11-09. The two family windows are per model family;
# every other listed type, and any type not listed, is the login's window.
_MODEL_FAMILY = {"seven_day_opus": "opus", "seven_day_sonnet": "sonnet"}


def _method_var_missing(name: str, value: str | None) -> bool:
    value = (value or "").strip()
    if name.startswith("CLAUDE_CODE_USE_"):
        return value.lower() not in _SWITCH_ON
    return value.lower() in ("", "0", "false")  # a gateway URL or token: set, and not a placeholder off value


class ClaudeCodeAdapter(RouteAdapter):
    engine = "claude_code"

    def __init__(self, *args, binary: str = "claude", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Anthropic's own binary, unmodified: no wrapper name here. Its exact path, hash and version are
        # the R2a launch check's job (CSL-732), not this adapter's.
        if not isinstance(binary, str) or binary.rsplit("/", 1)[-1] != "claude":
            raise ValueError("Claude Code routes launch the claude binary itself")
        self._binary = binary

    def argv(self, request: dict) -> list[str]:
        self._run_label = None
        route = request["route"]
        kind = route["connection"]["kind"]
        bare = route.get("bare", False)
        if bare is not True and bare is not False:
            raise c.RouteError(c.REQUEST_INVALID, "route.bare")
        if bare and kind == "subscription":
            # --bare ignores the sign-in. API-key and other user-picked built-in methods may use it.
            raise c.RouteError(c.LAUNCH_REFUSED, "--bare ignores the sign-in: not on the subscription route")
        auth_args = []
        if kind == "subscription":
            # Every subscription launch goes through the measured-hash gate. Missing
            # build or help refuses. A beta admission launches with no --bare and
            # carries the beta label. Unknown is not an allow.
            auth_args = self._subscription_auth_args(route)
            self._check_api_key_helper(route)
            self._run_label = "beta"
        # --verbose: stream-json output in print mode is documented to need it (unverified on this box).
        return [self._binary, *auth_args, *(["--bare"] if bare else []), "-p", "--input-format", "stream-json",
                "--output-format", "stream-json", "--verbose", "--model", route["model"]]

    def env(self, request: dict) -> dict[str, str]:
        conn = request["route"]["connection"]
        kind = conn["kind"]
        if kind == "subscription":
            return child_env(self._parent_env, extra_names=SUBSCRIPTION_KEEP_ENV, never=SUBSCRIPTION_DROP_ENV)
        if kind == "api_key":
            never = API_KEY_DROP_ENV + ("ANTHROPIC_API_KEY",)
            if conn.get("key_source") == "api_key_helper":
                # The user's own apiKeyHelper in their settings supplies the key; Recursion adds none.
                return child_env(self._parent_env, extra_names=USER_KEEP_ENV, never=never)
            cred = self.credential(request, ("ANTHROPIC_API_KEY",))
            return child_env(self._parent_env, extra_names=USER_KEEP_ENV, never=never, credential=cred)
        if kind == "passthrough":
            # Cloud providers (Bedrock, Vertex, Foundry) or a gateway the user picked: that method's own
            # variables pass through for this run, with the user's own sign-in and config location.
            # CLAUDE_CODE_USE_* is copied as its stripped lowercase spelling so the child sees 1/true/yes/on.
            method = conn.get("method")
            names = PASSTHROUGH_METHOD_ENV.get(method) if isinstance(method, str) else None
            if names is None:
                raise c.RouteError(c.LAUNCH_REFUSED, f"passthrough method {scalar(method)} has no listed variables")
            missing = [n for n in PASSTHROUGH_METHOD_REQUIRED[method]
                       if _method_var_missing(n, self._parent_env.get(n))]
            if missing:
                raise c.RouteError(c.LAUNCH_REFUSED, f"passthrough method {method} needs {', '.join(missing)} "
                                   "set; without it the run would use the sign-in instead")
            prefixes = PASSTHROUGH_METHOD_PREFIX.get(method, ())
            names += tuple(sorted(n for n in self._parent_env if prefixes and n.startswith(prefixes)))
            env = child_env(self._parent_env, extra_names=USER_KEEP_ENV + names)
            for name in list(env):
                if name.startswith("CLAUDE_CODE_USE_"):
                    env[name] = env[name].strip().lower()
            return env
        raise c.RouteError(c.LAUNCH_REFUSED, f"connection kind {kind} not supported for claude_code")

    @staticmethod
    def _subscription_auth_args(route: dict) -> list[str]:
        """Admit a subscription launch only through the measured-help gate.

        Missing ``claude_build`` or ``help_text`` refuses. ``bare_default: absent``
        does not launch and does not skip the beta label. A match returns no auth
        flag: the measured fixture has neither ``--bare`` nor ``--no-bare``. Help
        that differs names the build and says it needs re-measuring. A build below
        the 2.1.314 floor refuses. Unknown is not beta.
        """
        state = route.get("bare_default")
        flag = route.get("bare_opt_out")
        if state is not None and state not in BARE_DEFAULT_STATES:
            raise c.RouteError(c.REQUEST_INVALID, "route.bare_default")
        if flag not in (None, "--no-bare"):
            raise c.RouteError(c.REQUEST_INVALID, "route.bare_opt_out")
        decision = bd.subscription_beta(route.get("claude_build"), route.get("help_text"))
        if decision["disposition"] != "beta" or decision["label"] != "beta":
            raise c.RouteError(c.LAUNCH_REFUSED, decision["detail"])
        return []

    @staticmethod
    def _check_api_key_helper(route: dict) -> None:
        """An apiKeyHelper in the user's settings outranks the sign-in: show it and let the user pick.

        The surface reports whether the user's settings carry one (``route.api_key_helper``); this adapter
        never opens the settings. Present, or not reported, is a typed question, not a silent override.
        The user answers with ``route.billing_ack = "api_key_helper"`` (run anyway, billing their API key)
        or by picking the API-key route.
        """
        state = route.get("api_key_helper")
        if state is not None and state not in API_KEY_HELPER_STATES:
            raise c.RouteError(c.REQUEST_INVALID, "route.api_key_helper")
        if state == "absent":
            return
        if state == "present" and route.get("billing_ack") == "api_key_helper":
            return
        if state == "present":
            why = ("your Claude Code settings carry an apiKeyHelper, which outranks the sign-in: "
                   "this run would bill your API key")
        else:
            why = ("whether your Claude Code settings carry an apiKeyHelper was not reported; if they do, "
                   "this run would bill your API key")
        raise c.RouteError(c.ROUTE_CHOICE_REQUIRED, why + "; choose: run anyway "
                           "(billing_ack=api_key_helper), the API-key route, or cancel")

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
                # Not booked. A retry Claude is still attempting is not a rejected plan limit, even when
                # the envelope carries rate_limit_info (11.2: a throttle is not a connection-wide limit).
            elif kind == "rate_limit_event":
                info = msg.get("rate_limit_info")
                events.append(c.event("usage", "claude.rate_limit_event", _info_fields(info)))
                limit = _rejected(info, "claude.rate_limit_event", self.vendor_reset) or limit
            elif kind == "result":
                return _classify_result(rid, msg, limit, events, model, self.vendor_reset)
            # assistant / user / other system envelopes: model payload or progress, never classified


def _info_fields(info: object) -> dict:
    if not isinstance(info, dict):
        return {}
    return {k: scalar(info.get(k)) for k in ("status", "resetsAt", "rateLimitType")}


def _rejected(info: object, source: str, translate) -> dict | None:
    if not isinstance(info, dict) or info.get("status") != "rejected":
        return None
    reset = translate(info.get("resetsAt"))
    family = _MODEL_FAMILY.get(info.get("rateLimitType"))
    if isinstance(family, str):
        # The family token, not the requested model id: a later Opus id shares the Opus window.
        return c.limit_signal("model", source, model=family, reset_at=reset)
    return c.limit_signal("connection", source, reset_at=reset)


def _rate_limit_info(params: object, source: str, translate) -> dict | None:
    if not isinstance(params, dict) or not isinstance(params.get("rate_limit_info"), dict):
        return None
    info = dict(params["rate_limit_info"])
    info.setdefault("status", "rejected")  # present on a rejected result even without a status (D2-17)
    return _rejected(info, source, translate)


def _classify_result(rid: str, msg: dict, limit: dict | None, events: list, model: str, translate) -> dict:
    if msg.get("is_error") is not True and msg.get("subtype") == "success":
        text = msg.get("result")
        return c.result(rid, None, text=text if isinstance(text, str) else "", events=events)
    limit = _rate_limit_info(msg.get("api_error_params"), "claude.result.rate_limit_info", translate) or limit
    if limit is None and msg.get("error") == "rate_limit":
        limit = c.limit_signal("connection", "claude.result.error")
    if limit is not None:
        return c.result(rid, c.PLAN_LIMIT, limit=limit, events=events, detail="typed limit")
    # Auth error names and api_error_status are unverified field names.
    if msg.get("error") in ("authentication_failed", "oauth_token_expired") or msg.get("api_error_status") == 401:
        return c.result(rid, c.AUTH_FAILED, events=events, detail=str(msg.get("error") or "401"))
    text = msg.get("result")
    errors = msg.get("errors")
    if errors is None:
        errors = []
    elif not isinstance(errors, list):
        raise _Protocol("result errors is not a list")
    if c.sounds_like_quota(text) or any(c.sounds_like_quota(e) for e in errors if isinstance(e, str)):
        return c.result(rid, c.SHORT_FAULT, events=events, detail=f"untyped quota-like text on {model}")
    return c.result(rid, c.ENGINE_ERROR, events=events, detail=str(scalar(msg.get("subtype")) or "error"))
