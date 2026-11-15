"""The start registrant (seam S6), through SPINE's own ``Hooks``: make it, call ``register``, freeze, then start
(lane ROUTING, slice 3). The files are made-up ``routes.json`` and ``routing.json`` in a scratch config folder."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.routes import limits as route_limits
from recursion_core.routing.tests.fixtures import CONFIG, ROUTES

LINUX = sys.platform.startswith("linux")  # load_routes' owner check needs a uid
if LINUX:
    from recursion_core.control import hooks as H
    from recursion_core.daemon.control_adapter import load_routes
    from recursion_core.routing import hooks as rh

WAIT = {"on_limit": "wait"}


def routes_file(*engines: str) -> dict:
    return {"routes": {e: {"model": ROUTES[e]["route"]["model"], "connection": ROUTES[e]["route"]["connection"],
                           "deadline_s": 600} for e in engines}}


def routing_file() -> dict:
    return {"version": 1, "logins": copy.deepcopy(CONFIG["logins"][:2]),
            "default": {"engine": "claude_code", "model": None, "limits": WAIT},
            "rules": [copy.deepcopy(CONFIG["rules"][0])], "undetected_rule": "skip"}


START = {"tenant": "t-1", "prompt": "hello"}


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
        for params in ({}, {"hand_pick": {"engine": "codex", "model": None}}):
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


if __name__ == "__main__":
    unittest.main()
