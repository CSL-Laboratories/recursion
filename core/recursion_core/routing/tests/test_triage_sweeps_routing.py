"""Triage of the Nov 15 sweeps (events-routing-plugins): red tests for routing/config.py.

Each test asserts what the module's own docstrings promise; each is RED on master ff3f8071 when
the finding it names reproduces. Connection ids and models are made up; no engine is launched.
RT1 replaces a concurrent writer with an edit made at the same point of the call
(after the real validator returned), which is the interleaving the sweeps reported.
"""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
import unittest
from unittest import mock

from recursion_core.routing import config as rc
from recursion_core.routing import router


def inputs():
    raw = {"version": 1,
           "logins": [{"id": "openai-sub", "vendor": "openai", "kind": "subscription",
                       "connection_id": "chatgpt-main", "engines": ["codex"]}],
           "default": {"engine": "codex"}, "rules": []}
    routes = {"codex": {"route": {"engine": "codex", "model": "model-a",
                                  "connection": {"kind": "subscription", "id": "chatgpt-main"}},
                        "deadline_s": 600.0, "binary": None}}
    return raw, routes, {}


DETECTED = {"engines": {"codex": {"routable": True, "reason": None}}}


class ConfigTests(unittest.TestCase):
    def test_RT1_the_returned_routes_are_the_routes_that_were_checked(self):
        # s2b-routing-sol F1, s2b-routing-astra F2: routes change after the login check, before the copy.
        raw, routes, keys = inputs()
        real = rc._fallbacks_are_logins

        def then_edit(*args, **kwargs):
            out = real(*args, **kwargs)
            routes["codex"]["route"]["connection"]["id"] = "chatgpt-other"
            return out

        with mock.patch.object(rc, "_fallbacks_are_logins", then_edit):
            try:
                cfg = rc.load_config(raw, routes, keys)
            except rc.ConfigError:
                return  # refusing an input that changed is a fix too
        decision = router.decide(cfg, DETECTED, {})
        self.assertTrue(decision["ok"])
        self.assertEqual(cfg.routes["codex"]["route"]["connection"]["id"], decision["connection"]["id"])

    def test_RT3_deep_fallback_profile_is_a_typed_refusal(self):
        # s2b-routing-sol F3, s2b-routing-astra F3 (witness A).
        raw, routes, keys = inputs()
        profile = {"provider": "chatgpt"}
        for _ in range(600):
            profile = {"nested": profile}
        fallback = copy.deepcopy(routes["codex"]["route"])
        fallback["profile"] = profile
        raw["default"]["limits"] = {"on_limit": "ask", "fallback": [fallback]}
        try:
            rc.load_config(raw, routes, keys)
        except rc.ConfigError:
            pass  # a typed refusal is the contract; accepting the value is fine too

    def test_RT7_a_sign_in_spelling_the_route_guard_accepts_is_catalogued(self):
        # s2-routes-astra F7 (config.py:199): subscription_routes folds "ChatGPT" to "chatgpt".
        routes = {"opencode": {"route": {"engine": "opencode", "model": "chatgpt/sample-model",
                                         "profile": {"provider": "ChatGPT", "auth": "subscription",
                                                     "method": "Sign in with ChatGPT",
                                                     "model": "chatgpt/sample-model"},
                                         "connection": {"kind": "subscription", "id": "sample-chatgpt"}}}}
        self.assertEqual(rc.catalog(routes, {}).get("sample-chatgpt"),
                         {"vendor": "openai", "kind": "subscription"})


class FileTests(unittest.TestCase):
    def test_RT4_a_path_with_nul_is_routing_unreadable(self):
        # s2b-routing-astra F3 (witness B), s6-exc F04.
        folder = tempfile.mkdtemp(prefix="triage-routing-")
        self.addCleanup(shutil.rmtree, folder, True)
        with self.assertRaises(rc.RoutingUnreadable):
            rc.read_routing_file(os.path.join(folder, "routing\0.json"))


if __name__ == "__main__":
    unittest.main()
