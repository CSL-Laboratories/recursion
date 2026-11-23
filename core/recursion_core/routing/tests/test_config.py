"""The routing config: its schema, the loader's cross-field rules, vendor identity, and the file checks.

Ported from the vault prototype's SchemaTests and VendorIdentityTests (20261107-csl729-routing-config-q45-01,
section 8.3), with the route contract's engine ids and the catalog the daemon already has (the key store's
metadata and routes.json's sign-ins) in place of the prototype's fixture catalog. The prototype's table that
kept every subscription on its own vendor's engines is not ported (it predates the owner's Nov 8 answer); only
the Claude rule is kept, and ``test_engine_vendor_compatibility`` checks exactly that.
"""

from __future__ import annotations

import copy
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.routes import contract
from recursion_core.routes import limits as route_limits
from recursion_core.routing import config as rc
from recursion_core.routing.tests.fixtures import (CONFIG, KEYS, ROUTES, config, routes_with_second_claude_login,
                                                   second_claude_route)

try:
    import jsonschema  # hosted CI installs it; the free-threaded interpreter has none
except ImportError:  # pragma: no cover - depends on the interpreter
    jsonschema = None

SCHEMA = json.loads(rc.SCHEMA_FILE.read_text(encoding="utf-8"))
POSIX_OWNER = hasattr(os, "geteuid") and sys.platform.startswith("linux")


def mutated(mutate, raw=None):
    bad = copy.deepcopy(CONFIG if raw is None else raw)
    mutate(bad)
    return bad


def _fallback(route, **changes):
    """A whole fallback route copied from ROUTES[route], with connection changes (a None value deletes)."""
    out = copy.deepcopy(ROUTES[route]["route"])
    for key, value in changes.items():
        target = out["connection"] if key.startswith("conn_") else out
        key = key[5:] if key.startswith("conn_") else key
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value
    return out


def _with_fallback(*routes):
    return lambda c: c["default"].update(limits={"on_limit": "fallback", "fallback": list(routes)})


def _drop(*path):
    def mutate(c):
        target = c
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
    return mutate


# Shape defects: the JSON Schema and the loader both refuse each one, the loader with this code. The fallback
# cases are the route contract's own rules (routes/contract.py validate_request), which the schema states too.
SHAPE_DEFECTS = [
    ("version 2", "bad_version", lambda c: c.update(version=2)),
    ("no version member", "bad_version", _drop("version")),
    ("an unknown top-level member", "unknown_field", lambda c: c.update(fallback="any_detected")),
    ("no logins member", "bad_config", _drop("logins")),
    ("no rules member", "bad_config", _drop("rules")),
    ("no default member", "bad_route", _drop("default")),
    ("a hyphenated engine id", "unknown_engine", lambda c: c["default"].update(engine="claude-code")),
    ("an unknown route member", "bad_route", lambda c: c["default"].update(provider="anthropic")),
    ("an unknown undetected_rule", "bad_undetected_rule", lambda c: c.update(undetected_rule="fallback")),
    ("a login kind that is a pool", "bad_login", lambda c: c["logins"][0].update(kind="rotating_pool")),
    ("a vendor spelled otherwise", "unknown_vendor", lambda c: c["logins"][0].update(vendor="Anthropic")),
    ("a login id ending in a line break", "bad_login", lambda c: c["logins"][0].update(id="anthropic-sub\n")),
    ("a login id in upper case", "bad_login", lambda c: c["logins"][0].update(id="Anthropic-Sub")),
    ("a login with no engines member", "bad_login", _drop("logins", 0, "engines")),
    ("a login binding one engine twice", "bad_login", lambda c: c["logins"][0].update(engines=["claude_code", "claude_code"])),
    ("a connection id of 201 characters", "bad_login", lambda c: c["logins"][0].update(connection_id="c" * 201)),
    ("a connection id ending in a line break", "bad_login", lambda c: c["logins"][0].update(connection_id="claude-main\n")),
    ("a rule id ending in a line break", "bad_rule", lambda c: c["rules"][0].update(id="review-to-codex\n")),
    ("a rule id with a space", "bad_rule", lambda c: c["rules"][0].update(id="review codex")),
    ("an empty condition", "bad_rule", lambda c: c["rules"][0].update(when={})),
    ("an empty task_kind list", "bad_rule", lambda c: c["rules"][0].update(when={"task_kind": []})),
    ("an empty tags_all list", "bad_rule", lambda c: c["rules"][2].update(when={"tags_all": []})),
    ("an empty tag", "bad_rule", lambda c: c["rules"][2].update(when={"tags_all": ["cheap", ""]})),
    ("an unknown condition beside a known one", "bad_rule", lambda c: c["rules"][0]["when"].update(repo=["x"])),
    ("an unknown condition alone", "bad_rule", lambda c: c["rules"][0].update(when={"repo": ["x"]})),
    ("a model with a space", "bad_model", lambda c: c["default"].update(model="two words")),
    ("a model ending in a line break", "bad_model", lambda c: c["default"].update(model="m\n")),
    ("a model of 129 characters", "bad_model", lambda c: c["default"].update(model="m" * 129)),
    ("on_limit fallback with nothing listed", "bad_limits", lambda c: c["default"].update(limits={"on_limit": "fallback", "fallback": []})),
    ("nine fallbacks", "bad_limits", _with_fallback(*[_fallback("codex")] * 9)),
    ("a fallback of a profile engine with no profile", "bad_limits", _with_fallback(_fallback("opencode", profile=None))),
    ("a fallback profile that is not an object", "bad_limits", _with_fallback(_fallback("opencode", profile="anthropic"))),
    ("a custody key fallback with no credential_env", "bad_limits", _with_fallback(_fallback("opencode", conn_credential_env=None))),
    ("a custody key fallback with an empty credential_env", "bad_limits", _with_fallback(_fallback("opencode", conn_credential_env=""))),
    ("a custody key fallback whose credential_env is not a string", "bad_limits", _with_fallback(_fallback(
        "opencode", conn_credential_env=3))),
    ("an explicit custody key fallback with no credential_env", "bad_limits", _with_fallback(_fallback(
        "opencode", conn_key_source="custody", conn_credential_env=None))),
    ("an api_key_helper on a subscription connection of claude_code", "bad_limits", _with_fallback(_fallback(
        "claude_code", conn_key_source="api_key_helper"))),
    ("a passthrough fallback whose method is not a string", "bad_limits", _with_fallback(_fallback(
        "claude_code", connection={"kind": "passthrough", "id": "bedrock", "method": 3}))),
    ("a passthrough fallback with an empty method", "bad_limits", _with_fallback(_fallback(
        "claude_code", connection={"kind": "passthrough", "id": "bedrock", "method": ""}))),
    ("a fallback key_source that is not one", "bad_limits", _with_fallback(_fallback("opencode", conn_key_source="keychain"))),
    ("an api_key_helper fallback outside claude_code", "bad_limits", _with_fallback(_fallback(
        "codex", connection={"kind": "api_key", "id": "chatgpt-main", "key_source": "api_key_helper"}))),
    ("a passthrough fallback with no method", "bad_limits", _with_fallback(_fallback(
        "claude_code", connection={"kind": "passthrough", "id": "bedrock"}))),
    ("a passthrough fallback outside claude_code", "bad_limits", _with_fallback(_fallback(
        "codex", connection={"kind": "passthrough", "id": "bedrock", "method": "bedrock"}))),
    ("a fallback connection with no id", "bad_limits", _with_fallback(_fallback("codex", conn_id=None))),
    ("a fallback with an empty model", "bad_limits", _with_fallback(_fallback("codex", model=""))),
    ("a fallback engine the route contract does not take", "bad_limits", _with_fallback(_fallback("codex", engine="recursion_loop"))),
]


class SchemaTests(unittest.TestCase):
    def test_every_shape_defect_is_refused_by_the_loader_with_its_code(self) -> None:
        for name, code, mutate in SHAPE_DEFECTS:
            with self.subTest(name):
                with self.assertRaises(rc.ConfigError) as cm:
                    config(mutated(mutate))
                self.assertEqual(cm.exception.code, code)

    @unittest.skipIf(jsonschema is None, "jsonschema not installed")
    def test_json_schema_accepts_example_and_rejects_bad_shapes(self) -> None:
        jsonschema.Draft202012Validator.check_schema(SCHEMA)
        v = jsonschema.Draft202012Validator(SCHEMA)
        self.assertEqual(list(v.iter_errors(CONFIG)), [])
        for good in (_with_fallback(_fallback("claude_code")), _with_fallback(_fallback("opencode"), _fallback("codex"))):
            with_limits = mutated(good)
            self.assertEqual(list(v.iter_errors(with_limits)), [])
            config(with_limits)  # and the loader takes it
        for name, code, mutate in SHAPE_DEFECTS:
            with self.subTest(name):
                self.assertNotEqual(list(v.iter_errors(mutated(mutate))), [], name)

    def test_a_helper_key_fallback_needs_no_credential_env(self) -> None:
        # claude_code's api_key connection whose key comes from its own api_key_helper names no credential_env:
        # the loader takes it on both interpreters, and the schema takes it where jsonschema is installed.
        helper = {"engine": "claude_code", "model": "m", "connection": {
            "kind": "api_key", "id": "anthropic-helper", "key_source": "api_key_helper"}}
        raw = {"version": 1,
               "logins": [{"id": "anthropic-helper", "vendor": "anthropic", "kind": "api_key",
                           "connection_id": "anthropic-helper", "engines": ["claude_code"]}],
               "default": {"engine": "claude_code", "limits": {"on_limit": "fallback", "fallback": [helper]}},
               "rules": []}
        routes = {"claude_code": {"route": helper, "deadline_s": 600, "binary": "claude"}}
        loaded = rc.load_config(raw, routes, {"anthropic-helper": {"provider": "anthropic"}})
        self.assertEqual(loaded.default.engine, "claude_code")
        if jsonschema is not None:
            self.assertEqual(list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(raw)), [])

    def test_values_a_schema_cannot_tell_apart_are_refused_by_the_loader(self) -> None:
        # JSON Schema counts true and 1.0 equal to the const 1 in some readings and 1.0 always; the loader
        # takes only the integer 1.
        for value in (True, 1.0, "1"):
            with self.assertRaises(rc.ConfigError, msg=repr(value)) as cm:
                config(mutated(lambda c, v=value: c.update(version=v)))
            self.assertEqual(cm.exception.code, "bad_version")
        for meta in ({"provider": 3}, {"provider": None}, {}, "anthropic"):
            with self.assertRaises(rc.ConfigError, msg=repr(meta)) as cm:
                config(keys={**KEYS, "anthropic-api": meta})
            self.assertEqual(cm.exception.code, "key_metadata_invalid")

    def test_schema_names_the_codes_own_engines_vendors_and_limits(self) -> None:
        defs = SCHEMA["$defs"]
        self.assertEqual(tuple(defs["engine"]["enum"]), contract.ENGINES)
        self.assertIs(rc.ENGINES, contract.ENGINES)
        self.assertEqual(tuple(defs["vendor"]["enum"]), rc.VENDORS)
        self.assertEqual(tuple(defs["login"]["properties"]["kind"]["enum"]), rc.LOGIN_KINDS)
        self.assertEqual(tuple(SCHEMA["properties"]["undetected_rule"]["enum"]), rc.UNDETECTED_RULES)
        self.assertEqual(SCHEMA["properties"]["undetected_rule"]["default"], "skip")
        self.assertEqual(tuple(defs["limits"]["properties"]["on_limit"]["enum"]), route_limits.ON_LIMIT)
        self.assertEqual(defs["limits"]["properties"]["fallback"]["maxItems"], route_limits.MAX_FALLBACK)
        self.assertEqual(set(defs["fallback_route"]["propertyNames"]["enum"]), set(route_limits._ROUTE_KEYS))
        self.assertEqual(tuple(defs["route_contract_engine"]["enum"]), contract.ENGINES)
        self.assertEqual(tuple(defs["fallback_route"]["properties"]["connection"]["properties"]["kind"]["enum"]),
                         contract.CONNECTION_KINDS)
        self.assertEqual(defs["fallback_route"]["allOf"][0]["if"]["properties"]["engine"]["enum"], list(contract.PROFILE_ENGINES))
        self.assertEqual(set(SCHEMA["properties"]), set(rc.TOP_LEVEL))
        self.assertEqual(set(defs["login"]["properties"]), set(rc.LOGIN_KEYS))
        self.assertEqual(set(defs["route"]["properties"]), set(rc.ROUTE_KEYS))
        self.assertEqual(set(defs["rule"]["properties"]["when"]["properties"]), set(rc.WHEN_KEYS))

    def test_loader_cross_field_rules(self) -> None:
        cases = {
            "second_login_same_vendor": (lambda c: c["logins"].append(
                {"id": "anthropic-sub-2", "vendor": "anthropic", "kind": "subscription", "connection_id": "claude-second",
                 "engines": ["claude_code"]}), routes_with_second_claude_login()),
            "engine_bound_twice": (lambda c: c["logins"].append(
                {"id": "google-key", "vendor": "google", "kind": "api_key", "connection_id": "gemini-api",
                 "engines": ["opencode"]}), None),
            "claude_subscription_outside_claude_code": (lambda c: c["logins"][0]["engines"].append("hermes"), None),
            "engine_without_login": (lambda c: (c["logins"].pop(4), c["rules"].append(
                {"id": "x", "when": {"task_kind": ["x"]}, "route": {"engine": "hermes"}})), None),
            "unknown_field": (lambda c: c.update(fallback="any_detected"), None),
            "duplicate_rule_id": (lambda c: c["rules"].append(copy.deepcopy(c["rules"][0])), None),
            "duplicate_login_id": (lambda c: c["logins"].append(dict(c["logins"][2], connection_id="openai-api",
                                                                     vendor="openai", engines=["pi"])), None),
            "no_logins": (lambda c: c.update(logins=[]), None),
            "login_route_mismatch": (lambda c: c["logins"][2].update(connection_id="anthropic-api-2"), None),
            "fallback_not_a_login": (lambda c: c["default"].update(limits={"on_limit": "ask", "fallback": [second_claude_route()]}), None),
        }
        for code, (mutate, routes) in cases.items():
            with self.assertRaises(rc.ConfigError, msg=code) as cm:
                config(mutated(mutate), routes)
            self.assertEqual(cm.exception.code, code)

    def test_malformed_configs_raise_only_config_error(self) -> None:
        bad_values = [None, 0, 1.0, True, "x", [], {}, [None], [[]], [{}], {"engine": None}, {"engine": ["codex"]}]
        paths = [
            lambda c, v: c.update(version=v), lambda c, v: c.update(logins=v), lambda c, v: c.update(default=v),
            lambda c, v: c.update(rules=v), lambda c, v: c.update(undetected_rule=v),
            lambda c, v: c["logins"][0].update(id=v), lambda c, v: c["logins"][0].update(engines=v),
            lambda c, v: c["logins"][0].update(connection_id=v), lambda c, v: c["logins"][0].update(vendor=v),
            lambda c, v: c["rules"][0].update(when=v), lambda c, v: c["rules"][0].update(id=v),
            lambda c, v: c["rules"][0]["when"].update(task_kind=v), lambda c, v: c["default"].update(model=v),
            lambda c, v: c["default"].update(limits=v), lambda c, v: c["rules"][0].update(route=v),
        ]
        n = 0
        for path in paths:
            for value in bad_values:
                bad = mutated(lambda c, p=path, v=value: p(c, copy.deepcopy(v)))
                try:
                    config(bad)
                except rc.ConfigError:
                    n += 1
                # A value that happens to be valid (an empty rules list, say) loads; anything else is a ConfigError.
        self.assertGreater(n, len(paths) * len(bad_values) // 2)
        for raw in (None, [], "routing", 1):
            with self.assertRaises(rc.ConfigError):
                rc.load_config(raw, ROUTES, KEYS)

    def test_limits_are_validated_by_the_route_contracts_own_function(self) -> None:
        # The routing file's limits are routes.limits' shape, checked by its own validator, not a copy.
        calls = []
        real = route_limits.validate_limits

        def spy(value):
            calls.append(value)
            return real(value)

        with mock.patch.object(route_limits, "validate_limits", spy):
            cfg = config(mutated(lambda c: c["default"].update(limits={"on_limit": "wait"})))
        self.assertEqual(calls, [{"on_limit": "wait"}])
        self.assertEqual(cfg.default.limits, {"on_limit": "wait", "fallback": []})
        for limits in ({"on_limit": "retry"}, {"on_limit": "fallback"}, {"fallback": "codex"}, {"x": 1},
                       {"on_limit": "fallback", "fallback": [{"engine": "codex"}]}, [], "ask"):
            with self.assertRaises(rc.ConfigError, msg=limits) as cm:
                config(mutated(lambda c, v=limits: c["default"].update(limits=v)))
            self.assertEqual(cm.exception.code, "bad_limits")

    def test_fallback_routes_are_only_configured_logins_of_their_engine(self) -> None:
        # A limit can only move a task to a login the user configured, bound to that engine: in the routing
        # file's own limits, and in the routes.json entry an engine would launch with (a list only shown
        # to the user, on_limit "ask", is checked too, because it is offered as a switch).
        codex_route = copy.deepcopy(ROUTES["codex"]["route"])
        ok = mutated(lambda c: c["default"].update(limits={"on_limit": "fallback", "fallback": [codex_route]}))
        self.assertEqual(config(ok).default.limits["fallback"][0]["connection"]["id"], "chatgpt-main")
        wrong_engine = dict(codex_route, engine="claude_code")
        api_key_as_sign_in = dict(codex_route, connection={"kind": "api_key", "id": "chatgpt-main", "credential_env": "X"})
        for fb in (second_claude_route(), wrong_engine, api_key_as_sign_in):
            for on_limit in ("fallback", "ask"):
                with self.assertRaises(rc.ConfigError, msg=(fb, on_limit)) as cm:
                    config(mutated(lambda c, f=fb, o=on_limit: c["rules"][0]["route"].update(limits={"on_limit": o, "fallback": [f]})))
                self.assertEqual(cm.exception.code, "fallback_not_a_login")
        with self.assertRaises(rc.ConfigError) as cm:
            config(routes=routes_with_second_claude_login())
        self.assertEqual(cm.exception.code, "fallback_not_a_login")

    def test_a_sign_in_login_needs_its_route_and_a_key_login_its_key(self) -> None:
        # The catalog is what this machine has: a sign-in login whose route routes.json does not
        # configure, or a key login whose key the key store does not hold, names an unknown connection.
        routes = copy.deepcopy(ROUTES)
        del routes["hermes"]
        with self.assertRaises(rc.ConfigError) as cm:
            config(routes=routes)
        self.assertEqual((cm.exception.code, cm.exception.detail), ("unknown_connection", "xai-sub"))
        keys = copy.deepcopy(KEYS)
        del keys["xai-api"]
        with self.assertRaises(rc.ConfigError) as cm:
            config(keys=keys)
        self.assertEqual((cm.exception.code, cm.exception.detail), ("unknown_connection", "xai-key"))
        # A key login whose engine has no route on this machine still loads: its engine is never routable.
        routes = copy.deepcopy(ROUTES)
        del routes["pi"]
        self.assertEqual(config(routes=routes).engine_login["pi"], "xai-key")

    def test_model_null_means_the_model_routes_json_configures(self) -> None:
        cfg = config()
        self.assertIsNone(cfg.default.model)
        self.assertEqual(cfg.rules[0].route.model, "codex-model-b")
        self.assertEqual(cfg.routes["claude_code"]["route"]["model"], "claude-model-a")

    def test_loaded_limits_share_nothing_with_the_callers_objects(self) -> None:
        # The limits validator copies a route and its connection one level deep; a profile or any nested
        # connection value would otherwise stay the caller's after validation.
        opencode = copy.deepcopy(ROUTES["opencode"]["route"])
        codex = copy.deepcopy(ROUTES["codex"]["route"])
        codex["connection"]["metadata"] = {"label": "original"}
        raw = copy.deepcopy(CONFIG)
        raw["default"]["limits"] = {"on_limit": "fallback", "fallback": [opencode, codex]}
        raw["rules"][0]["route"]["limits"] = {"on_limit": "ask", "fallback": [copy.deepcopy(opencode)]}
        cfg = rc.load_config(raw, copy.deepcopy(ROUTES), KEYS)
        before = (copy.deepcopy(cfg.default.limits), copy.deepcopy(cfg.rules[0].route.limits))
        opencode["profile"]["provider"] = "changed"
        codex["connection"]["metadata"]["label"] = "changed"
        raw["rules"][0]["route"]["limits"]["fallback"][0]["profile"]["provider"] = "changed"
        raw["default"]["limits"]["fallback"].append(codex)
        self.assertEqual((cfg.default.limits, cfg.rules[0].route.limits), before)
        self.assertEqual(cfg.default.limits["fallback"][0]["profile"]["provider"], "anthropic")
        self.assertEqual(cfg.default.limits["fallback"][1]["connection"]["metadata"]["label"], "original")

    def test_the_loaded_config_does_not_share_the_callers_objects(self) -> None:
        routes = copy.deepcopy(ROUTES)
        cfg = rc.load_config(copy.deepcopy(CONFIG), routes, KEYS)
        routes["claude_code"]["route"]["connection"]["id"] = "changed"
        self.assertEqual(cfg.routes["claude_code"]["route"]["connection"]["id"], "claude-main")
        with self.assertRaises(TypeError):
            cfg.routes["codex"] = {}
        with self.assertRaises(TypeError):
            cfg.engine_login["codex"] = "x"


class VendorIdentityTests(unittest.TestCase):
    """A login's vendor and kind are canonical ids matched against the catalog, not the operator's label."""

    def assertRefused(self, code, mutate, routes=None, keys=None):
        with self.assertRaises(rc.ConfigError, msg=code) as cm:
            config(mutated(mutate), routes, keys)
        self.assertEqual(cm.exception.code, code)

    def test_catalog_is_required(self) -> None:
        for routes, keys, code in ((ROUTES, None, "no_key_catalog"), (None, KEYS, "no_routes"), (ROUTES, [], "no_key_catalog")):
            with self.assertRaises(rc.ConfigError) as cm:
                rc.load_config(copy.deepcopy(CONFIG), routes, keys)
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(rc.ConfigError) as cm:
            rc.load_config(copy.deepcopy(CONFIG), ROUTES, {"anthropic-api": "anthropic"})
        self.assertEqual(cm.exception.code, "key_metadata_invalid")

    def test_claude_subscription_relabelled_as_another_vendor_is_refused(self) -> None:
        # The forgery the prototype's review described: the Claude subscription labelled openai and bound
        # to Codex. The catalog (routes.json's claude_code sign-in) says anthropic subscription.
        def forge(c):
            c["logins"][1] = {"id": "openai-sub", "vendor": "openai", "kind": "subscription", "connection_id": "claude-main",
                              "engines": ["codex"]}
            c["logins"][0]["connection_id"] = "claude-second"
        self.assertRefused("credential_mismatch", forge, routes_with_second_claude_login())
        # The same credential with its kind relabelled api_key, bound to a harness.
        self.assertRefused("credential_mismatch", lambda c: c["logins"].append(
            {"id": "relabel", "vendor": "anthropic", "kind": "api_key", "connection_id": "claude-second", "engines": ["pi"]}),
            routes_with_second_claude_login())

    def test_vendor_spellings_are_not_merged_or_accepted(self) -> None:
        for vendor in ("Anthropic", "ANTHROPIC", "anthropic-ai", " anthropic", "claude", ""):
            self.assertRefused("unknown_vendor", lambda c, v=vendor: c["logins"].append(
                {"id": "dup", "vendor": v, "kind": "subscription", "connection_id": "claude-second", "engines": ["claude_code"]}))

    def test_second_login_under_another_label_is_refused(self) -> None:
        # A second Claude subscription labelled as another canonical vendor on a free engine: the catalog
        # exposes it.
        self.assertRefused("credential_mismatch", lambda c: (c["logins"].pop(4), c["logins"].append(
            {"id": "sneaky", "vendor": "xai", "kind": "subscription", "connection_id": "claude-second", "engines": ["hermes"]})),
            routes_with_second_claude_login())
        # The same connection under a second login id.
        self.assertRefused("connection_reused", lambda c: c["logins"].append(
            {"id": "again", "vendor": "anthropic", "kind": "subscription", "connection_id": "claude-main", "engines": ["pi"]}))
        # A connection the catalog does not hold.
        self.assertRefused("unknown_connection", lambda c: c["logins"].append(
            {"id": "ghost", "vendor": "google", "kind": "api_key", "connection_id": "not-in-catalog", "engines": ["hermes"]}))

    def test_engine_vendor_compatibility(self) -> None:
        # Only the Claude rule is kept: the Claude subscription on any engine but claude_code is refused ...
        for eng in ("codex", "pi", "hermes", "opencode"):
            self.assertRefused("claude_subscription_outside_claude_code", lambda c, e=eng: c["logins"][0]["engines"].append(e))
        # ... and the prototype's table keeping every other subscription on its own vendor's engines is gone
        # (the owner, Nov 8: a subscription may run through another harness's sign-in; subscription_routes
        # decides which). An OpenAI subscription bound to Codex and to OpenCode loads.
        routes = copy.deepcopy(ROUTES)
        routes["opencode"]["route"]["connection"] = {"kind": "subscription", "id": "chatgpt-main"}
        routes["opencode"]["route"]["profile"] = {"provider": "chatgpt", "auth": "subscription", "method": "sign_in_with_chatgpt"}

        def harness(c):
            c["logins"][1]["engines"] = ["codex", "opencode"]
            del c["logins"][2]
            c["rules"] = c["rules"][:2]
        cfg = config(mutated(harness), routes)
        self.assertEqual(cfg.engine_login["opencode"], "openai-sub")
        # An API key of one vendor on another vendor's harness also loads (the key store says what it is).
        cfg = config(mutated(lambda c: c["logins"][3].update(vendor="openai", connection_id="openai-api")),
                     _with_connection(ROUTES, "pi", {"kind": "api_key", "id": "openai-api", "credential_env": "OPENAI_API_KEY"}),
                     KEYS)
        self.assertEqual((cfg.logins["xai-key"].vendor, cfg.engine_login["pi"]), ("openai", "xai-key"))

    def test_residual_a_mislabelled_catalog_is_not_detected(self) -> None:
        # The boundary of the guard, kept as a test so it is not overclaimed: if the key store's metadata
        # records an Anthropic key as an xAI one, the loader accepts it. Attesting a credential's provider is
        # a key-store job this module does not do.
        lying = {**KEYS, "anthropic-api-2": {"provider": "xai"}}
        cfg = config(mutated(lambda c: c["logins"][3].update(connection_id="anthropic-api-2")),
                     _with_connection(ROUTES, "pi", {"kind": "api_key", "id": "anthropic-api-2", "credential_env": "XAI_API_KEY"}),
                     lying)
        self.assertEqual(cfg.logins["xai-key"].connection_id, "anthropic-api-2")


def _with_connection(routes, engine, connection):
    out = copy.deepcopy(routes)
    out[engine]["route"]["connection"] = connection
    return out


class CatalogTests(unittest.TestCase):
    def test_catalog_reads_sign_ins_from_routes_and_keys_from_the_key_store(self) -> None:
        cat = rc.catalog(ROUTES, KEYS)
        self.assertEqual(cat["claude-main"], {"vendor": "anthropic", "kind": "subscription"})
        self.assertEqual(cat["chatgpt-main"], {"vendor": "openai", "kind": "subscription"})
        self.assertEqual(cat["grok-sign-in"], {"vendor": "xai", "kind": "subscription"})  # hermes, by its profile label
        self.assertEqual(cat["anthropic-api"], {"vendor": "anthropic", "kind": "api_key"})
        self.assertEqual(cat["gemini-api"], {"vendor": "google", "kind": "api_key"})
        # routes.json's api_key connections are not the catalog's: only the key store says what a key is.
        self.assertNotIn("not-a-key", rc.catalog(_with_connection(ROUTES, "pi", {"kind": "api_key", "id": "not-a-key"}), KEYS))
        # A sign-in named only in a fallback list is a routes.json sign-in too.
        self.assertEqual(rc.catalog(routes_with_second_claude_login(), KEYS)["claude-second"],
                         {"vendor": "anthropic", "kind": "subscription"})
        # A harness sign-in whose label is not one Recursion knows has no vendor, so no login can name it.
        unknown = _with_connection(ROUTES, "hermes", {"kind": "subscription", "id": "other-sign-in"})
        unknown["hermes"]["route"]["profile"] = {"provider": "someone", "auth": "subscription"}
        self.assertNotIn("other-sign-in", rc.catalog(unknown, KEYS))
        self.assertEqual(rc.catalog({}, {}), {})

    def test_one_connection_with_two_readings_is_refused(self) -> None:
        # A key id that is also a sign-in's connection id.
        with self.assertRaises(rc.ConfigError) as cm:
            rc.catalog(ROUTES, {**KEYS, "claude-main": {"provider": "anthropic"}})
        self.assertEqual(cm.exception.code, "catalog_conflict")
        # One sign-in id on two vendors' engines.
        with self.assertRaises(rc.ConfigError) as cm:
            rc.catalog(_with_connection(ROUTES, "codex", {"kind": "subscription", "id": "claude-main"}), KEYS)
        self.assertEqual(cm.exception.code, "catalog_conflict")


class _NoGeteuid:
    """``os`` as a platform without an owner check (Windows) shows it to the reader: no ``geteuid``."""

    def __getattr__(self, name):
        if name == "geteuid":
            raise AttributeError(name)
        return getattr(os, name)


class NoOwnerCheckTests(unittest.TestCase):
    """Where the platform gives no owner check (no ``os.geteuid``: Windows), a routing file that is present is
    refused as unreadable before it is opened or parsed, whatever it holds. So the reader's own rules (its owner,
    a present null, a member named twice) cannot change an answer there, and the mutants of those rules are judged
    only where the check exists (test_mutants, NEEDS_OWNER_CHECK). This runs on every platform."""

    def test_without_an_owner_check_a_present_file_is_refused_before_it_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "routing.json"
            for content in ("null", json.dumps(CONFIG), '{"version": 1, "version": 1}'):
                path.write_text(content, encoding="utf-8")
                no_uid = _NoGeteuid()
                with self.subTest(content=content[:20]), mock.patch.object(rc, "os", no_uid), \
                        mock.patch.object(no_uid, "open", side_effect=AssertionError("opened"), create=True) as opened, \
                        mock.patch.object(rc.json, "loads", side_effect=AssertionError("parsed")) as parsed:
                    with self.assertRaises(rc.RoutingUnreadable):
                        rc.read_routing_file(path)
                    opened.assert_not_called()  # refused before it is opened (astra r1's later note), and so
                    parsed.assert_not_called()  # before it is parsed


@unittest.skipUnless(POSIX_OWNER, "the routing file's owner check needs a POSIX uid")
class RoutingFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="routing-file-"))
        self.addCleanup(self._clean)

    def _clean(self) -> None:
        for path in sorted(self.dir.rglob("*"), reverse=True):
            path.unlink() if not path.is_dir() or path.is_symlink() else path.rmdir()
        self.dir.rmdir()

    def write(self, name, value, mode=0o600) -> Path:
        path = self.dir / name
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        os.chmod(path, mode)
        return path

    def test_absent_file_is_none_and_an_owner_only_file_is_read(self) -> None:
        self.assertIsNone(rc.read_routing_file(self.dir / rc.CONFIG_FILE))
        for mode in (0o600, 0o644, 0o400):
            path = self.write(rc.CONFIG_FILE, CONFIG, mode)
            self.assertEqual(rc.read_routing_file(path), CONFIG)

    def test_writable_by_others_linked_or_not_a_file_is_refused(self) -> None:
        for mode in (0o620, 0o602, 0o666):
            path = self.write(rc.CONFIG_FILE, CONFIG, mode)
            with self.assertRaises(rc.RoutingUnreadable, msg=oct(mode)):
                rc.read_routing_file(path)
        target = self.write("elsewhere.json", CONFIG)
        link = self.dir / "linked.json"
        link.symlink_to(target)
        with self.assertRaises(rc.RoutingUnreadable):
            rc.read_routing_file(link)
        fifo = self.dir / "fifo.json"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(rc.RoutingUnreadable):  # refused, and the open did not wait for a writer
            rc.read_routing_file(fifo)
        (self.dir / "folder.json").mkdir(mode=0o700)
        with self.assertRaises(rc.RoutingUnreadable):
            rc.read_routing_file(self.dir / "folder.json")

    def test_a_file_of_another_user_is_refused(self) -> None:
        path = self.write(rc.CONFIG_FILE, CONFIG, 0o600)
        self.assertEqual(rc.read_routing_file(path), CONFIG)
        with mock.patch.object(os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(rc.RoutingUnreadable):
                rc.read_routing_file(path)

    def test_a_present_null_file_is_refused_never_read_as_absent(self) -> None:
        path = self.write(rc.CONFIG_FILE, "null")
        with self.assertRaises(rc.ConfigError) as cm:
            rc.read_routing_file(path)
        self.assertEqual(cm.exception.code, "bad_config")
        self.assertIsNone(rc.read_routing_file(self.dir / "absent.json"))

    def test_a_member_named_twice_is_refused(self) -> None:
        text = json.dumps(CONFIG)[:-1] + ', "default": {"engine": "codex"}}'
        path = self.write(rc.CONFIG_FILE, text)
        with self.assertRaises(rc.RoutingUnreadable):
            rc.read_routing_file(path)
        nested = json.dumps(CONFIG).replace('"engine": "claude_code", "model": null', '"engine": "claude_code", "engine": "codex"')
        self.assertNotEqual(nested, json.dumps(CONFIG))
        with self.assertRaises(rc.RoutingUnreadable):
            rc.read_routing_file(self.write(rc.CONFIG_FILE, nested))

    def test_unparseable_file_is_refused(self) -> None:
        for text in ("{", "\xff", ""):
            path = self.dir / rc.CONFIG_FILE
            path.write_bytes(text.encode("latin-1"))
            os.chmod(path, 0o600)
            with self.assertRaises(rc.RoutingUnreadable, msg=repr(text)):
                rc.read_routing_file(path)

    def test_load_reads_routes_json_with_load_routes_and_routing_json_beside_it(self) -> None:
        try:
            from recursion_core.daemon import control_adapter
        except ImportError as exc:  # pragma: no cover - depends on the interpreter's packages
            self.skipTest(f"daemon not importable here: {exc}")
        wired = {eng: dict(entry["route"], deadline_s=600) for eng, entry in ROUTES.items() if eng in ("claude_code", "codex")}
        for item in wired.values():
            del item["engine"]
        self.write("routes.json", {"routes": wired})
        raw = {"version": 1, "logins": copy.deepcopy(CONFIG["logins"][:2]), "default": {"engine": "claude_code"},
               "rules": [copy.deepcopy(CONFIG["rules"][0])]}
        self.assertIsNone(rc.load(self.dir, {}))  # routes.json alone: no routing config
        for text in ("null", "[]", "false", "0", '"routing"'):  # present but not a config: refused, never "absent"
            self.write(rc.CONFIG_FILE, text)
            with self.assertRaises(rc.ConfigError, msg=text) as cm:
                rc.load(self.dir, {})
            self.assertEqual(cm.exception.code, "bad_config")
        self.write(rc.CONFIG_FILE, raw)
        calls = []
        real = control_adapter.load_routes
        with mock.patch.object(control_adapter, "load_routes", lambda path: calls.append(path) or real(path)):
            cfg = rc.load(self.dir, {})
        self.assertEqual(calls, [self.dir / "routes.json"])
        self.assertEqual(cfg.routes["codex"]["route"]["connection"], {"kind": "subscription", "id": "chatgpt-main"})
        self.assertEqual(cfg.routes["codex"]["route"]["engine"], "codex")
        os.chmod(self.dir / "routes.json", 0o666)
        with self.assertRaises(rc.RoutingUnreadable):
            rc.load(self.dir, {})
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / rc.CONFIG_FILE).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
