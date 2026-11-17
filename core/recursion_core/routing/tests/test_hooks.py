"""The start registrant (seam S6), through SPINE's own ``Hooks``: make it, call ``register``, freeze, then start
(lane ROUTING, slice 3). The files are made-up ``routes.json`` and ``routing.json`` in a scratch config folder."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from recursion_core.routes import limits as route_limits
from recursion_core.routing.tests.fixtures import CONFIG, ROUTES, detection

LINUX = sys.platform.startswith("linux")  # load_routes' owner check needs a uid
if LINUX:
    from recursion_core.control import hooks as H
    from recursion_core.daemon.control_adapter import load_routes
    from recursion_core.routing import config as rc
    from recursion_core.routing import detect as rd
    from recursion_core.routing import hooks as rh
    from recursion_core.routing import router

WAIT = {"on_limit": "wait"}


def routes_file(*engines: str) -> dict:
    return {"routes": {e: {"model": ROUTES[e]["route"]["model"], "connection": ROUTES[e]["route"]["connection"],
                           "deadline_s": 600} for e in engines}}


def routing_file() -> dict:
    return {"version": 1, "logins": copy.deepcopy(CONFIG["logins"][:2]),
            "default": {"engine": "claude_code", "model": None, "limits": WAIT},
            "rules": [copy.deepcopy(CONFIG["rules"][0])], "undetected_rule": "skip"}


START = {"tenant": "t-1", "prompt": "hello"}


class _Unreadable(Mapping):
    """A hand pick that says it has two members and raises when any is read."""

    def __getitem__(self, key):
        raise RuntimeError("unreadable")

    def get(self, key, default=None):
        raise RuntimeError("unreadable")

    def __iter__(self):
        raise RuntimeError("unreadable")

    def __len__(self):
        return 2


@unittest.skipUnless(LINUX, "load_routes reads the operator's file with a POSIX owner check")
class RegistrantTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "recursion"
        self.folder.mkdir()
        self.write("routes.json", routes_file("claude_code", "codex"))
        self.write("routing.json", routing_file())
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hooks = H.Hooks()
        rh.register(self.hooks)
        self.hooks.freeze()

    def write(self, name: str, data, mode: int = 0o600) -> None:
        path = self.folder / name
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        os.chmod(path, mode)

    def start(self, **params):
        return self.hooks.launch_snapshot(H.OWNER, {**START, **params})

    def refused(self, detail: str, **params) -> None:
        self.assertEqual(self.start(**params), (None, {"code": "request_invalid", "detail": detail}))

    def loaded(self, engine: str) -> dict:
        return load_routes(self.folder / "routes.json", ("claude_code", "codex"))[engine]

    def test_registered_as_routing_first_and_choosing_the_engine(self) -> None:
        self.assertEqual(self.hooks.start_order(), ("routing",))
        self.assertTrue(self.hooks.engine_may_be_chosen)
        self.assertEqual(self.hooks.declared_fields(), frozenset({"hand_pick"}))

    def test_control_hooks_own_compose_finds_routings_registrant(self) -> None:
        # The daemon's composition: control/hooks.py's REGISTRANTS names recursion_core.routing.hooks, and compose()
        # imports it and calls register(). (SPINE's daemon harness gives installed registrants stand-ins, so this
        # is where routing's real registration is shown.)
        self.assertIn("recursion_core.routing.hooks", H.REGISTRANTS)
        made = H.compose()
        self.assertIn("routing", made.start_order())
        self.assertTrue(made.engine_may_be_chosen)
        launch, refusal = made.launch_snapshot(H.OWNER, dict(START))
        self.assertIsNone(refusal)
        self.assertEqual(launch["route_decision"]["reason"], "default")

    def test_no_engine_named_runs_the_default(self) -> None:
        launch, refusal = self.start()
        self.assertIsNone(refusal)
        expected = self.loaded("claude_code")
        expected["route"]["limits"] = route_limits.validate_limits(WAIT)  # the routing file's limits for the default
        self.assertEqual(launch["route_entry"], expected)
        self.assertEqual(launch["route_decision"], {
            "chosen": {"engine": "claude_code", "model": "claude-model-a",
                       "connection": {"kind": "subscription", "id": "claude-main"}},
            "reason": "default", "candidates": [{"engine": "claude_code", "model": "claude-model-a", "skipped": None}],
            "hand_pick": None})

    def test_a_hand_pick_wins_over_the_default(self) -> None:
        launch, refusal = self.start(hand_pick={"engine": "codex", "model": None})
        self.assertIsNone(refusal)
        self.assertEqual(launch["route_entry"], self.loaded("codex"))  # routes.json's model, its own limits (none)
        self.assertEqual((launch["route_decision"]["reason"], launch["route_decision"]["hand_pick"]),
                         ("hand_pick", {"engine": "codex", "model": None}))
        launch, _ = self.start(hand_pick={"engine": "codex", "model": "codex-model-x"})
        self.assertEqual(launch["route_entry"]["route"]["model"], "codex-model-x")
        self.assertEqual(launch["route_decision"]["chosen"]["model"], "codex-model-x")

    def test_a_hand_pick_of_an_engine_that_is_not_routable_is_refused_and_nothing_is_chosen(self) -> None:
        self.write("routes.json", routes_file("claude_code"))
        raw = routing_file()
        raw["logins"] = raw["logins"][:1]
        raw["rules"] = []
        self.write("routing.json", raw)
        self.refused("hand_pick_engine_without_login:no_login", hand_pick={"engine": "codex", "model": None})
        self.write("routes.json", routes_file("claude_code", "codex"))
        self.write("routing.json", routing_file())
        self.write("routes.json", routes_file("claude_code"))  # codex's login stays, its route goes
        self.refused("routing_config_invalid:unknown_connection", hand_pick={"engine": "codex", "model": None})

    def test_a_named_engine_is_not_routed_and_nothing_is_read(self) -> None:
        self.assertEqual(self.start(engine="codex")[0]["route_entry"], None)
        self.write("routing.json", "not json")
        launch, refusal = self.start(engine="codex")
        self.assertIsNone(refusal)
        self.assertEqual((launch["route_entry"], launch["route_decision"]), (None, None))

    def test_an_engine_and_a_hand_pick_of_another_engine_is_refused(self) -> None:
        self.refused("engine_and_hand_pick_differ", engine="codex", hand_pick={"engine": "claude_code", "model": None})
        launch, refusal = self.start(engine="codex", hand_pick={"engine": "codex", "model": None})
        self.assertIsNone(refusal)
        self.assertEqual(launch["route_entry"]["route"]["engine"], "codex")
        self.assertEqual(launch["route_decision"]["reason"], "hand_pick")

    def test_no_routing_file_is_todays_start(self) -> None:
        (self.folder / "routing.json").unlink()
        self.write("routes.json", "not json")  # with no routing file, routes.json is not read either
        for params in ({}, {"engine": "codex"}):  # requests that carry no hand pick (the Liaison's reading)
            launch, refusal = self.start(**params)
            self.assertIsNone(refusal)
            self.assertEqual((launch["route_entry"], launch["route_decision"]), (None, None))

    def test_a_routing_file_that_cannot_be_used_refuses(self) -> None:
        self.write("routing.json", "null")
        self.refused("routing_config_invalid:bad_config")
        raw = routing_file()
        raw["logins"].append(copy.deepcopy(CONFIG["logins"][2]))  # an API-key login: the registrant has no key metadata
        self.write("routing.json", raw)
        self.refused("routing_config_invalid:unknown_connection")
        self.write("routing.json", routing_file(), mode=0o622)
        self.refused("routing_config_invalid:unreadable")
        self.write("routing.json", routing_file())
        self.write("routes.json", "not json")
        self.refused("routing_config_invalid:unreadable")

    def test_the_registrant_writes_nothing(self) -> None:
        def listing():
            return sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in self.folder.iterdir())
        before = listing()
        for params in ({}, {"hand_pick": {"engine": "codex", "model": None}}, {"engine": "codex"},
                       {"engine": "codex", "hand_pick": {"engine": "claude_code", "model": None}}):
            self.start(**params)
        self.assertEqual(listing(), before)

    def test_the_chosen_entry_names_routes_json_binary_and_connection(self) -> None:
        # The start path launches a chosen entry only when its binary is the composed adapter's (``launchable``),
        # and routing's loader already holds the login to the connection routes.json launches with.
        files = routes_file("claude_code", "codex")
        files["routes"]["codex"]["binary"] = "/opt/made-up/codex"
        self.write("routes.json", files)
        launch, _ = self.start(hand_pick={"engine": "codex", "model": None})
        self.assertEqual(launch["route_entry"]["binary"], "/opt/made-up/codex")
        self.assertEqual(launch["route_entry"]["route"]["connection"], {"kind": "subscription", "id": "chatgpt-main"})
        self.assertEqual(launch["route_entry"]["deadline_s"], 600.0)

    # The first DeepSeek read of #419 (f30bc965): R1 and R2 (MEDIUM), R3, R4, R5 and R7 (LOW).
    def test_with_no_routing_file_a_hand_pick_is_never_dropped(self) -> None:  # ds R1
        (self.folder / "routing.json").unlink()
        self.write("routes.json", "not json")  # with no routing file, routes.json is not read either
        codex, claude = {"engine": "codex", "model": None}, {"engine": "claude_code", "model": None}
        model = {"engine": "codex", "model": "codex-model-x"}
        self.refused("engine_and_hand_pick_differ", engine="codex", hand_pick=claude)
        for hand in ({}, [], "codex"):  # not a hand pick: refused, never dropped (astra R1)
            self.refused("bad_hand_pick:hand_pick", engine="codex", hand_pick=hand)
        self.refused("hand_pick_without_routing", engine="codex", hand_pick=model)
        self.refused("hand_pick_without_routing", hand_pick=model)
        self.refused("hand_pick_without_routing", hand_pick=codex)  # ds r2 F3: no engine named, it asks for one
        launch, refusal = self.start(engine="codex", hand_pick=codex)  # a null model of the named engine: today's start
        self.assertIsNone(refusal)
        self.assertEqual((launch["route_entry"], launch["route_decision"]), (None, None))

    def test_a_file_nested_deeper_than_the_parser_goes_is_unreadable(self) -> None:  # ds R3
        deep = "[" * 100000 + "]" * 100000
        self.write("routing.json", deep)
        self.refused("routing_config_invalid:unreadable")
        self.write("routing.json", routing_file())
        self.write("routes.json", deep)
        self.refused("routing_config_invalid:unreadable")

    def test_a_hand_pick_that_is_not_one_is_refused_alike_with_or_without_an_engine(self) -> None:  # ds R4
        for hand in (_Unreadable(), {}, {"engine": ["codex"], "model": None}, "codex"):
            for params in ({"engine": "codex"}, {}):
                with self.subTest(hand=type(hand).__name__, **params):
                    self.refused("bad_hand_pick:hand_pick", hand_pick=hand, **params)
        self.refused("bad_hand_pick:hand_pick_model", engine="codex", hand_pick={"engine": "codex", "model": 5})

    def test_load_refuses_one_string_for_its_engines(self) -> None:  # ds R5
        for engines in ("claude_code", b"claude_code"):
            with self.assertRaises(TypeError):
                rc.load(self.folder, {}, engines=engines)
        self.assertIsNotNone(rc.load(self.folder, {}, engines=["claude_code", "codex"]))

    def test_route_entry_answers_none_for_anything_not_a_routed_decision(self) -> None:  # ds R7
        config = rc.load(self.folder, {}, engines=("claude_code", "codex"))
        detection = rd.detect(config, route_refusal=rd.daemon_route_refusal(None), active=("claude_code", "codex"))
        routed = router.decide(config, detection, {})
        self.assertEqual(rh.route_entry(config, routed)["route"]["engine"], "claude_code")
        for decision in ({}, {"engine": "codex"}, None, "x", {**routed, "engine": "nope"}, {**routed, "model": 5},
                         {**routed, "ok": False}, router.decide(config, detection, None)):
            with self.subTest(decision=repr(decision)[:60]):
                self.assertIsNone(rh.route_entry(config, decision))


    def third_engine(self):
        """A composition where routing registers first and a lane registers a third engine after it (astra R3),
        with a profile route whose own model selector names its model."""
        files = routes_file("claude_code", "codex")
        files["routes"]["opencode"] = {
            "model": "chatgpt/configured", "connection": {"kind": "subscription", "id": "chatgpt-main"},
            "profile": {"provider": "chatgpt", "auth": "subscription", "method": "sign_in_with_chatgpt",
                        "model": "chatgpt/configured"}, "deadline_s": 60}
        raw = routing_file()
        raw["logins"][1]["engines"].append("opencode")
        raw["default"], raw["rules"] = {"engine": "opencode"}, []
        self.write("routes.json", files)
        self.write("routing.json", raw)
        hooks = H.Hooks()
        rh.register(hooks)  # routing registers first; the engine comes later, still at composition
        hooks.engines.register("opencode", lambda *args, **kwargs: None)
        hooks.freeze()
        return hooks

    def test_engine_registered_after_routing_is_used(self) -> None:  # ds R2, astra R3
        hooks = self.third_engine()
        launch, refusal = hooks.launch_snapshot(H.OWNER, dict(START))
        self.assertIsNone(refusal)
        self.assertEqual(launch["route_entry"]["route"]["engine"], "opencode")
        self.assertEqual((launch["route_decision"]["chosen"]["engine"], launch["route_decision"]["chosen"]["model"],
                          launch["route_decision"]["reason"]), ("opencode", "chatgpt/configured", "default"))
        # The same files under a composition without that engine: its routes.json names an engine it does not have.
        self.refused("routing_config_invalid:unreadable")

    def test_selected_model_reaches_profile_as_well_as_outer_route(self) -> None:  # astra R2
        hooks = self.third_engine()
        launch, refusal = hooks.launch_snapshot(H.OWNER, {**START, "hand_pick": {"engine": "opencode",
                                                                                   "model": "chatgpt/picked"}})
        self.assertIsNone(refusal)
        route = launch["route_entry"]["route"]
        self.assertEqual((route["model"], route["profile"]["model"]), ("chatgpt/picked", "chatgpt/picked"))
        self.assertEqual(launch["route_decision"]["chosen"]["model"], "chatgpt/picked")
        launch, _ = hooks.launch_snapshot(H.OWNER, dict(START))  # the config's own entry is unchanged
        self.assertEqual(launch["route_entry"]["route"]["profile"]["model"], "chatgpt/configured")

    def test_daemon_prelaunch_refusal_remains_a_routing_refusal(self) -> None:  # astra R3
        files = routes_file("claude_code", "codex")
        # A made-up, inconsistent route that load_routes catalogues and the daemon's refusal callback rejects.
        claude = files["routes"]["claude_code"]  # a copy of the connection: the shared fixture is not touched
        claude["connection"] = {**claude["connection"], "key_source": "api_key_helper"}
        self.write("routes.json", files)
        self.refused("default_engine_unavailable:launch_refused")

    def test_entry_is_detached_from_configuration_and_other_entries(self) -> None:  # astra R3
        config = rc.load(self.folder, {}, engines=self.hooks.engines.names())
        before = copy.deepcopy(dict(config.routes))
        decision = router.decide(config, rd.detect(config), {"hand_pick": {"engine": "codex", "model": "picked-model"}})
        first = rh.route_entry(config, decision)
        self.assertEqual(dict(config.routes), before)
        first["route"]["connection"]["id"] = "only-this-copy"
        second = rh.route_entry(config, decision)
        self.assertEqual(second["route"]["connection"]["id"], "chatgpt-main")
        self.assertEqual(dict(config.routes), before)


    # The second astra read of #419 (c2566d39): a routing.json limits value nested deeper than load's copy goes.
    def install(self, depth: int):
        """The third-engine composition, with a fallback profile carrying a value nested ``depth`` deep."""
        hooks = self.third_engine()
        raw = json.loads((self.folder / "routing.json").read_text(encoding="utf-8"))
        fallback = {"engine": "opencode", "model": "chatgpt/configured",
                    "connection": {"kind": "subscription", "id": "chatgpt-main"},
                    "profile": {"provider": "chatgpt", "auth": "subscription", "method": "sign_in_with_chatgpt",
                                "model": "chatgpt/configured", "extra": "__nested__"}}
        raw["default"]["limits"] = {"on_limit": "ask", "fallback": [fallback]}
        self.write("routing.json", json.dumps(raw).replace('"__nested__"', "[" * depth + "0" + "]" * depth))
        return hooks

    def test_shallow_fallback_profile_still_routes(self) -> None:  # astra r2 1, the control
        launch, refusal = self.install(3).launch_snapshot(H.OWNER, dict(START))
        self.assertIsNone(refusal)
        self.assertEqual(launch["route_entry"]["route"]["engine"], "opencode")

    def test_nested_fallback_profile_load_is_typed(self) -> None:  # astra r2 1, sol r2 1
        hooks = self.install(600)  # a 1,201-byte value that parses, and that load_config's copy cannot take
        with self.assertRaises(rc.RoutingUnreadable):
            rc.load(self.folder, {}, engines=hooks.engines.names())

    def test_nested_fallback_profile_start_is_typed(self) -> None:  # astra r2 1, sol r2 1
        self.assertEqual(self.install(600).launch_snapshot(H.OWNER, dict(START)),
                         (None, {"code": "request_invalid", "detail": "routing_config_invalid:unreadable"}))


    def test_a_profile_without_a_model_selector_is_given_none(self) -> None:  # ds r2 F4
        files = routes_file("claude_code")
        files["routes"]["opencode"] = {"model": "xai/model-d", "connection": {"kind": "subscription", "id": "grok-sign-in"},
                                       "profile": {"provider": "grok"}, "deadline_s": 600}
        self.write("routes.json", files)
        raw = routing_file()
        raw["logins"] = [raw["logins"][0], {"id": "xai-sub", "vendor": "xai", "kind": "subscription",
                                            "connection_id": "grok-sign-in", "engines": ["opencode"]}]
        raw["default"], raw["rules"] = {"engine": "opencode", "model": None}, []
        self.write("routing.json", raw)
        hooks = H.Hooks()
        rh.register(hooks)
        hooks.engines.register("opencode", lambda *args, **kwargs: None)
        hooks.freeze()
        launch, refusal = hooks.launch_snapshot(H.OWNER, {**START, "hand_pick": {"engine": "opencode",
                                                                                   "model": "xai/model-e"}})
        self.assertIsNone(refusal)
        self.assertEqual(launch["route_entry"]["route"]["model"], "xai/model-e")
        self.assertEqual(launch["route_entry"]["route"]["profile"], {"provider": "grok"})  # no selector is added

    def test_a_failure_in_routings_own_limits_lookup_is_not_laid_on_the_file(self) -> None:  # ds r2 F2
        with mock.patch.object(rh.router, "_route_limits", side_effect=RuntimeError("routing's own bug")):
            self.assertEqual(self.start(), (None, {"code": "unavailable", "detail": "start_registrant_failed"}))

    def test_a_route_that_cannot_be_described_is_never_launched(self) -> None:  # ds R7, sol R3
        with mock.patch.object(rh, "route_entry", return_value=None):  # an entry that cannot be copied: unusable config
            self.refused("routing_config_invalid:unreadable")
        with mock.patch.object(rh.router, "route_decision", return_value=None):  # never for decide's own answers
            self.assertEqual(self.start(), (None, {"code": "unavailable", "detail": "start_registrant_failed"}))


@unittest.skipUnless(LINUX, "load_routes reads the operator's file with a POSIX owner check")
class HookRegressions(unittest.TestCase):
    """The first sol read of #419 (f30bc965, gpt-6.1-sol): its R1, R2 (A7-A12) and R3 tests, by its names, through
    the registrant itself (``make_normalize`` with the composition's names and a config folder of its own)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        self.folder = Path(tmp.name) / "recursion"
        self.folder.mkdir()
        self.raw = routing_file()
        self.write("routing.json", self.raw)
        self.write("routes.json", routes_file("claude_code", "codex"))
        self.normalize = rh.make_normalize(H.Hooks(), environ={"XDG_CONFIG_HOME": tmp.name})

    def write(self, name: str, data) -> None:
        path = self.folder / name
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        os.chmod(path, 0o600)

    def run_start(self, **more):
        return self.normalize(H.OWNER, {**START, **more})

    def assertRefused(self, part, detail: str) -> None:
        self.assertIsInstance(part, H.Refusal)
        self.assertEqual(part.answer(), {"code": "request_invalid", "detail": detail})

    def test_absent_routing_file_preserves_named_engine_legacy_behavior(self) -> None:  # sol's probe, no hand pick
        # The Liaison's reading (4:53 PM, Nov 15): a request that carries no hand pick behaves exactly as today's start.
        (self.folder / "routing.json").unlink()
        self.write("routes.json", "not json")  # and with no routing file, routes.json is not read
        self.assertEqual(self.run_start(engine="codex"), {})
        self.assertEqual(self.run_start(), {})

    def test_absent_routing_file_cannot_silently_drop_a_picked_model(self) -> None:  # sol R1
        (self.folder / "routing.json").unlink()
        self.assertRefused(self.run_start(engine="codex", hand_pick={"engine": "codex", "model": "review-picked-model"}),
                           "hand_pick_without_routing")

    def test_load_accepts_composition_engine_outside_seed_engines(self) -> None:  # sol R2, A7
        route = copy.deepcopy(ROUTES["hermes"]["route"])
        del route["engine"]
        route["deadline_s"] = 600
        self.write("routes.json", {"routes": {"hermes": route}})
        self.write("routing.json", {"version": 1, "logins": [copy.deepcopy(CONFIG["logins"][4])],
                                    "default": {"engine": "hermes"}, "rules": []})
        loaded = rc.load(self.folder, {}, engines=iter(("hermes",)))  # a one-shot iterable, read once
        self.assertEqual(set(loaded.routes), {"hermes"})

    def test_normalizer_forwards_complete_composed_engine_names(self) -> None:  # sol R2, A8
        hooks = SimpleNamespace(engines=SimpleNamespace(names=lambda: ("codex", "hermes")))
        normalize = rh.make_normalize(hooks, environ={"XDG_CONFIG_HOME": self.home})
        with mock.patch.object(rc, "load", return_value=None) as load:
            self.assertEqual(normalize(H.OWNER, {}), {})
        load.assert_called_once_with(self.folder, {}, engines=("codex", "hermes"))

    def test_normalizer_enforces_active_composition_engines(self) -> None:  # sol R2, A9
        loaded = rc.load(self.folder, {}, engines=("claude_code", "codex"))
        hooks = SimpleNamespace(engines=SimpleNamespace(names=lambda: ("codex",)))
        normalize = rh.make_normalize(hooks, environ={"XDG_CONFIG_HOME": self.home})
        with mock.patch.object(rc, "load", return_value=loaded):  # a config loaded beside the table, not from it
            self.assertRefused(normalize(H.OWNER, {}), "default_engine_unavailable:engine_not_active")

    def test_normalizer_enforces_daemon_prelaunch_refusals(self) -> None:  # sol R2, A10
        with mock.patch.object(rd, "daemon_route_refusal", return_value=lambda entry: "launch_refused"):
            self.assertRefused(self.run_start(), "default_engine_unavailable:launch_refused")

    def test_route_entry_carries_rule_limits(self) -> None:  # sol R2, A12
        self.raw["rules"][0]["route"]["limits"] = {"on_limit": "wait"}
        self.write("routing.json", self.raw)
        loaded = rc.load(self.folder, {})
        decision = router.decide(loaded, detection({"claude_code", "codex"}), {"task_kind": "review"})
        self.assertEqual(decision["reason"], "config")
        self.assertEqual(rh.route_entry(loaded, decision)["route"]["limits"]["on_limit"], "wait")

    def test_route_entry_owns_its_copy(self) -> None:  # sol R2, A11
        loaded = rc.load(self.folder, {})
        before = copy.deepcopy(dict(loaded.routes))
        decision = router.decide(loaded, detection({"claude_code", "codex"}),
                                 {"hand_pick": {"engine": "codex", "model": "other-model"}})
        entry = rh.route_entry(loaded, decision)
        entry["binary"] = "/made-up/reviewer-engine"
        self.assertEqual(dict(loaded.routes), before)

    def test_nested_routing_json_has_a_typed_refusal(self) -> None:  # sol R3
        self.write("routing.json", "[" * 16000 + "0" + "]" * 16000)
        part = self.run_start()
        self.assertIsInstance(part, H.Refusal)
        self.assertEqual(part.answer()["code"], "request_invalid")
        self.assertTrue(part.answer()["detail"].startswith("routing_config_invalid:"), part.answer())

    def test_nested_routes_json_has_a_typed_refusal(self) -> None:  # sol R3
        files = routes_file("claude_code", "codex")
        files["routes"]["claude_code"]["review_extra"] = "__review_nested__"
        self.write("routes.json", json.dumps(files).replace('"__review_nested__"', "[" * 16000 + "0" + "]" * 16000))
        part = self.run_start()
        self.assertIsInstance(part, H.Refusal)
        self.assertEqual(part.answer()["code"], "request_invalid")
        self.assertTrue(part.answer()["detail"].startswith("routing_config_invalid:"), part.answer())


    # The second sol read of #419 (c2566d39): its 1 and 2, by its names.
    def test_R7_incomplete_routed_decision_must_not_get_an_entry(self) -> None:  # sol r2 2
        loaded = rc.load(self.folder, {})
        decision = router.decide(loaded, detection({"claude_code", "codex"}), {})
        missing = {key: value for key, value in decision.items() if key != "connection"}
        for label, bad in (("null-model", {**decision, "model": None}), ("missing-connection", missing),
                           ("unknown-reason", {**decision, "reason": "invented"})):
            with self.subTest(case=label):
                self.assertIsNone(router.route_decision(bad))
                self.assertIsNone(rh.route_entry(loaded, bad))
        self.assertEqual(rh.route_entry(loaded, decision)["route"]["engine"], "claude_code")  # the control

    def test_R3_routing_limits_copy_depth_has_a_typed_refusal(self) -> None:  # sol r2 1
        fallback = {"engine": "claude_code", "model": "claude-model-a",
                    "connection": {"kind": "subscription", "id": "claude-main"}, "profile": {"extra": "DEPTH_MARK"}}
        self.raw["default"]["limits"] = {"on_limit": "fallback", "fallback": [fallback]}
        self.write("routing.json", json.dumps(self.raw).replace('"DEPTH_MARK"', "[" * 600 + "0" + "]" * 600))
        self.assertIsInstance(rc.read_routing_file(self.folder / "routing.json"), dict)  # it parses; the copy fails
        self.assertRefused(self.run_start(), "routing_config_invalid:unreadable")


    def test_hostile_engine_is_typed_even_with_a_hand_pick(self) -> None:  # ds r2 F1
        class Hostile(str):
            def __eq__(self, other):
                raise RuntimeError("made-up hostile engine")

            def __ne__(self, other):
                raise RuntimeError("made-up hostile engine")

            __hash__ = str.__hash__
        self.assertRefused(self.run_start(engine=Hostile("codex"), hand_pick={"engine": "claude_code", "model": None}),
                           "engine_and_hand_pick_differ")
        part = self.run_start(engine=Hostile("codex"), hand_pick={"engine": "codex", "model": None})
        self.assertEqual((part["route_decision"]["chosen"]["engine"], part["route_decision"]["reason"]),
                         ("codex", "hand_pick"))


    def test_a_non_str_engine_with_a_hand_pick_is_typed(self) -> None:  # ds r3 F1
        class HostileEq:
            def __eq__(self, other):
                raise RuntimeError("made-up hostile engine")

            def __ne__(self, other):
                raise RuntimeError("made-up hostile engine")

            __hash__ = object.__hash__
        hand = {"engine": "codex", "model": None}
        self.assertRefused(self.run_start(engine=HostileEq(), hand_pick=hand), "engine_and_hand_pick_differ")
        (self.folder / "routing.json").unlink()  # and with routing off
        self.assertRefused(self.run_start(engine=HostileEq(), hand_pick=hand), "engine_and_hand_pick_differ")

    def test_a_rule_decision_without_its_rule_gets_no_entry(self) -> None:  # ds r3 F2
        loaded = rc.load(self.folder, {})
        decision = router.decide(loaded, detection({"claude_code", "codex"}), {"task_kind": "review"})
        self.assertEqual(decision["reason"], "config")
        missing = {key: value for key, value in decision.items() if key != "rule"}
        for label, bad in (("no rule member", missing), ("a rule that is not a word", {**decision, "rule": 5})):
            with self.subTest(case=label):
                self.assertIsNone(rh.route_entry(loaded, bad))
        self.assertIsNotNone(router.route_decision(missing))  # route_decision does not need the rule; route_entry does
        self.assertEqual(rh.route_entry(loaded, decision)["route"]["engine"], "codex")  # the control


    def test_the_limits_lookup_reads_route_entrys_own_copy_of_the_decision(self) -> None:  # grok r4 R1, ds r4 F1
        class ReadOnce(dict):
            """A rule decision whose rule is read once (``get``) and raises if it is read again (``[]``)."""

            def __getitem__(self, key):
                if key == "rule":
                    raise RuntimeError("the decision's rule read a second time")
                return super().__getitem__(key)
        self.raw["rules"][0]["route"]["limits"] = {"on_limit": "wait"}
        self.write("routing.json", self.raw)
        loaded = rc.load(self.folder, {})
        decision = router.decide(loaded, detection({"claude_code", "codex"}), {"task_kind": "review"})
        self.assertEqual(decision["reason"], "config")
        entry = rh.route_entry(loaded, ReadOnce(decision))  # the lookup gets route_entry's copy, never this object
        self.assertEqual((entry["route"]["engine"], entry["route"]["limits"]["on_limit"]), ("codex", "wait"))

    def test_a_rule_that_is_a_str_subclass_gets_no_entry(self) -> None:  # grok r4, its advisable note beside R1
        class ComparingRaises(str):
            def __eq__(self, other):
                raise RuntimeError("made-up rule compare")

            __hash__ = str.__hash__
        loaded = rc.load(self.folder, {})
        decision = router.decide(loaded, detection({"claude_code", "codex"}), {"task_kind": "review"})
        self.assertIsNone(rh.route_entry(loaded, {**decision, "rule": ComparingRaises(decision["rule"])}))

    def test_hostile_hand_pick_is_typed_even_with_named_engine(self) -> None:  # sol's hardening note
        class BadRead(dict):
            def get(self, *args):
                raise RuntimeError("made-up unreadable mapping")
        self.assertRefused(self.run_start(engine="codex", hand_pick=BadRead(engine="codex", model=None)),
                           "bad_hand_pick:hand_pick")


if __name__ == "__main__":
    unittest.main()
