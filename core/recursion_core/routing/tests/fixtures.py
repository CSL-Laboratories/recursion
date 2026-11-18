"""Shared plain-data fixtures for the routing tests. No engine, no key, no file, no network.

The fixtures follow the vault prototype's (20261107-csl729-routing-config-q45-01, section 8.3) with the
route contract's engine ids: its claude-code, codex and opencode are claude_code, codex and opencode;
its grok-build (an xAI key, not installed) is pi on an xAI key; its cursor-cli (a subscription whose
sign-in cannot be verified) is hermes on its own Grok sign-in. Every key id, connection id and model
here is made up.
"""

from __future__ import annotations

import copy

from recursion_core.routing import config as rc


def _entry(engine: str, model: str, connection: dict, profile: dict | None = None) -> dict:
    route = {"engine": engine, "model": model, "connection": connection}
    if profile is not None:
        route["profile"] = profile
    return {"route": route, "deadline_s": 600.0, "binary": None}


# What load_routes gives the starter: one entry per engine.
ROUTES = {
    "claude_code": _entry("claude_code", "claude-model-a", {"kind": "subscription", "id": "claude-main"}),
    "codex": _entry("codex", "codex-model-a", {"kind": "subscription", "id": "chatgpt-main"}),
    "opencode": _entry("opencode", "anthropic/model-b",
                       {"kind": "api_key", "id": "anthropic-api", "credential_env": "ANTHROPIC_API_KEY"},
                       {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY"}),
    "pi": _entry("pi", "xai/model-c", {"kind": "api_key", "id": "xai-api", "credential_env": "XAI_API_KEY"},
                 {"provider": "xai", "auth": "api_key", "credential_env": "XAI_API_KEY"}),
    "hermes": _entry("hermes", "xai/model-d", {"kind": "subscription", "id": "grok-sign-in"},
                     {"provider": "grok", "auth": "subscription", "method": "grok_sign_in", "model": "xai/model-d"}),
}

# The key store's metadata (lane KEYS: key id -> provider, env name, ...). Never a value.
KEYS = {
    "anthropic-api": {"provider": "anthropic", "env": "ANTHROPIC_API_KEY"},
    "xai-api": {"provider": "xai", "env": "XAI_API_KEY"},
    "anthropic-api-2": {"provider": "anthropic", "env": "ANTHROPIC_API_KEY"},
    "gemini-api": {"provider": "google", "env": "GEMINI_API_KEY"},
    "openai-api": {"provider": "openai", "env": "OPENAI_API_KEY"},
}

CONFIG = {
    "version": 1,
    "logins": [
        {"id": "anthropic-sub", "vendor": "anthropic", "kind": "subscription", "connection_id": "claude-main",
         "engines": ["claude_code"]},
        {"id": "openai-sub", "vendor": "openai", "kind": "subscription", "connection_id": "chatgpt-main",
         "engines": ["codex"]},
        {"id": "anthropic-key", "vendor": "anthropic", "kind": "api_key", "connection_id": "anthropic-api",
         "engines": ["opencode"]},
        {"id": "xai-key", "vendor": "xai", "kind": "api_key", "connection_id": "xai-api", "engines": ["pi"]},
        {"id": "xai-sub", "vendor": "xai", "kind": "subscription", "connection_id": "grok-sign-in",
         "engines": ["hermes"]},
    ],
    "default": {"engine": "claude_code", "model": None},
    "rules": [
        {"id": "review-to-codex", "when": {"task_kind": ["review"]},
         "route": {"engine": "codex", "model": "codex-model-b"}},
        {"id": "search-to-pi", "when": {"task_kind": ["search"]}, "route": {"engine": "pi"}},
        {"id": "cheap-to-opencode", "when": {"tags_all": ["cheap"]}, "route": {"engine": "opencode"}},
    ],
    "undetected_rule": "skip",
}

LOGIN_OF = {"claude_code": "anthropic-sub", "codex": "openai-sub", "opencode": "anthropic-key", "pi": "xai-key",
            "hermes": "xai-sub"}
CONNECTION_OF = {"claude_code": "claude-main", "codex": "chatgpt-main", "opencode": "anthropic-api", "pi": "xai-api",
                 "hermes": "grok-sign-in"}


def second_claude_route() -> dict:
    """A second Claude subscription, as a routes.json fallback names it (the only place routes.json can)."""
    return {"engine": "claude_code", "model": "claude-model-a", "connection": {"kind": "subscription", "id": "claude-second"}}


def routes_with_second_claude_login() -> dict:
    """ROUTES with a second Claude subscription in claude_code's fallback list, so the catalog holds it."""
    routes = copy.deepcopy(ROUTES)
    routes["claude_code"]["route"]["limits"] = {"on_limit": "ask", "fallback": [second_claude_route()]}
    return routes


def detection(routable, refusals=None) -> dict:
    """A detection snapshot: every engine in ``routable`` routable, every other one refused."""
    refusals = refusals or {}
    engines = {}
    for eng in rc.ENGINES:
        if eng in routable:
            engines[eng] = {"routable": True, "reason": None}
        else:
            engines[eng] = {"routable": False, "reason": refusals.get(eng, "not_on_path")}
    return {"engines": engines}


# The prototype's machine: claude_code and codex signed in, opencode routable, hermes installed but its
# sign-in cannot be verified, pi not installed.
SENTRY = detection({"claude_code", "codex", "opencode"}, {"hermes": "signin_unverifiable"})


def config(raw=None, routes=None, keys=None):
    return rc.load_config(copy.deepcopy(CONFIG if raw is None else raw), copy.deepcopy(ROUTES if routes is None else routes),
                          copy.deepcopy(KEYS if keys is None else keys))


class Recorder:
    """Stands in for the start path: one launch per routed decision, keyed by (engine, connection id).

    The vault prototype dispatched to loopback mocks; dispatch is not ported (the daemon launches), so the
    tests count what a launcher would have been asked to launch."""

    def __init__(self, limit_on=()) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.limit_on = set(limit_on)

    def launch(self, decision: dict) -> dict:
        if not decision["ok"]:
            return {"launched": False, "code": decision["code"]}
        key = (decision["engine"], decision["connection"]["id"])
        self.calls.append((*key, decision["model"]))
        return {"launched": True, "code": "plan_limit" if key in self.limit_on else "ok"}

    def count(self, engine: str, connection_id: str) -> int:
        return sum(1 for e, c, _ in self.calls if (e, c) == (engine, connection_id))
