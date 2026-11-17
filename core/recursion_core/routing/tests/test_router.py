"""The routing decision: hand pick wins, detected engines only, no alternation, a pure function.

Ported from the vault prototype's test_router.py (20261107-csl729-routing-config-q45-01, section 8.3),
with the route contract's engine ids. Tests keep the prototype's names where they port one of its 22
tests. The prototype's two loopback-gate tests guarded ``dispatch()``, which is not ported (the daemon
launches); their place is taken by the two purity tests in ``PurityTests``, which show that a decision
reaches nothing outside its inputs.
"""

from __future__ import annotations

import contextlib
import copy
import itertools
import os
import socket
import subprocess
import unittest
from unittest import mock

from recursion_core.routing import config as rc
from recursion_core.routing import router
from recursion_core.routing.tests.fixtures import (CONFIG, CONNECTION_OF, LOGIN_OF, ROUTES, SENTRY, Recorder, config,
                                                   detection)


class HandPickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = config()

    def test_hand_pick_overrides_matching_rule_and_default(self) -> None:
        # The task matches review-to-codex and cheap-to-opencode; the default is claude_code. The hand
        # pick names opencode with its own model and wins.
        launcher = Recorder()
        task = {"task_kind": "review", "tags": ["cheap"], "hand_pick": {"engine": "opencode", "model": "hand-model"}}
        d = router.decide(self.cfg, SENTRY, task)
        self.assertTrue(d["ok"])
        self.assertEqual((d["reason"], d["engine"], d["login"], d["model"]),
                         ("hand_pick", "opencode", "anthropic-key", "hand-model"))
        self.assertEqual(d["connection"], {"kind": "api_key", "id": "anthropic-api"})
        launcher.launch(d)
        self.assertEqual(launcher.calls, [("opencode", "anthropic-api", "hand-model")])
        # Without the hand pick the same task goes to the first matching rule.
        task.pop("hand_pick")
        d = router.decide(self.cfg, SENTRY, task)
        self.assertEqual((d["reason"], d["rule"], d["engine"], d["model"]), ("config", "review-to-codex", "codex", "codex-model-b"))
        # And a task no rule matches gets the default, with the model routes.json configures.
        d = router.decide(self.cfg, SENTRY, {"task_kind": "chat"})
        self.assertEqual((d["reason"], d["engine"], d["model"]), ("default", "claude_code", "claude-model-a"))

    def test_hand_pick_beats_every_rule_order_and_default(self) -> None:
        # 6 rule orders x 3 hand-picked engines = 18 cases; each is decided AND launched, and only the
        # hand-picked route is launched, once.
        launcher = Recorder()
        n = 0
        for perm in itertools.permutations(CONFIG["rules"]):
            raw = copy.deepcopy(CONFIG)
            raw["rules"] = list(perm)
            cfg = config(raw)
            for eng in ("claude_code", "codex", "opencode"):
                before = list(launcher.calls)
                d = router.decide(cfg, SENTRY, {"task_kind": "review", "tags": ["cheap"], "hand_pick": {"engine": eng, "model": None}})
                self.assertEqual((d["reason"], d["engine"], d["login"]), ("hand_pick", eng, LOGIN_OF[eng]), (perm, eng))
                launcher.launch(d)
                self.assertEqual(launcher.calls[len(before):], [(eng, CONNECTION_OF[eng], d["model"])], (perm, eng))
                n += 1
        self.assertEqual(n, 18)
        self.assertEqual(len(launcher.calls), 18)

    def test_hand_pick_of_undetected_engine_is_refused_not_replaced(self) -> None:
        launcher = Recorder()
        for eng, reason in (("pi", "not_on_path"), ("hermes", "signin_unverifiable")):
            d = router.decide(self.cfg, SENTRY, {"task_kind": "review", "hand_pick": {"engine": eng, "model": None}})
            self.assertFalse(d["ok"])
            self.assertEqual((d["code"], d["engine"], d["reason"]), ("hand_pick_engine_unavailable", eng, reason))
            self.assertEqual([c["skipped"] for c in d["candidates"]], [reason])
            self.assertFalse(launcher.launch(d)["launched"])
        self.assertEqual(launcher.calls, [])

    def test_hand_pick_of_an_engine_with_no_login_is_refused(self) -> None:
        raw = copy.deepcopy(CONFIG)
        raw["logins"] = [lg for lg in raw["logins"] if lg["id"] != "xai-sub"]
        cfg = config(raw)
        d = router.decide(cfg, SENTRY, {"hand_pick": {"engine": "hermes", "model": None}})
        self.assertEqual((d["ok"], d["code"], d["engine"]), (False, "hand_pick_engine_without_login", "hermes"))

    def test_bad_task_or_hand_pick_is_refused(self) -> None:
        for task, code in ((None, "bad_task"), ([], "bad_task"), ({"task_kind": ""}, "bad_task"), ({"task_kind": 3}, "bad_task"),
                           ({"tags": "cheap"}, "bad_task"), ({"tags": ["cheap", ""]}, "bad_task"),
                           ({"hand_pick": "codex"}, "bad_hand_pick"), ({"hand_pick": {"engine": "codex-cli"}}, "bad_hand_pick"),
                           ({"hand_pick": {"engine": "codex", "login": "x"}}, "bad_hand_pick"),
                           ({"hand_pick": {"model": "m"}}, "bad_hand_pick"),
                           ({"hand_pick": {"engine": "codex", "model": "has space"}}, "bad_hand_pick"),
                           ({"hand_pick": {"engine": "codex", "model": "x" * 129}}, "bad_hand_pick"),
                           ({"hand_pick": {"engine": ["codex"]}}, "bad_hand_pick")):
            d = router.decide(self.cfg, SENTRY, task)
            self.assertEqual((d["ok"], d["code"]), (False, code), task)
        # A hand pick with no model key reads as model null: the model routes.json configures.
        d = router.decide(self.cfg, SENTRY, {"hand_pick": {"engine": "codex"}})
        self.assertEqual((d["engine"], d["model"], d["hand_pick"]), ("codex", "codex-model-a", {"engine": "codex", "model": None}))


class DetectedOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = config()

    def test_rule_to_undetected_engine_is_skipped_then_default(self) -> None:
        d = router.decide(self.cfg, SENTRY, {"task_kind": "search"})
        self.assertEqual((d["reason"], d["engine"]), ("default", "claude_code"))
        self.assertEqual(d["candidates"], [
            {"source": "rule:search-to-pi", "engine": "pi", "model": "xai/model-c", "skipped": "not_on_path"},
            {"source": "default", "engine": "claude_code", "model": "claude-model-a", "skipped": None}])

    def test_rule_to_undetected_engine_refuses_when_configured(self) -> None:
        raw = copy.deepcopy(CONFIG)
        raw["undetected_rule"] = "refuse"
        d = router.decide(config(raw), SENTRY, {"task_kind": "search"})
        self.assertEqual((d["ok"], d["code"], d["rule"], d["reason"]), (False, "rule_engine_unavailable", "search-to-pi", "not_on_path"))

    def test_undetected_default_refuses_and_never_picks_another_engine(self) -> None:
        det = detection({"codex", "opencode"}, {"claude_code": "signed_out"})
        d = router.decide(self.cfg, det, {"task_kind": "chat"})
        self.assertEqual((d["ok"], d["code"], d["reason"], d["engine"]), (False, "default_engine_unavailable", "signed_out", "claude_code"))

    def test_routable_engine_without_a_configured_route_is_not_routed(self) -> None:
        # The snapshot calls pi routable and its key is in the key store, but this machine's routes.json has
        # no pi entry. (A sign-in login needs its route: the catalog reads sign-ins from routes.json, so
        # without the hermes entry its login would not load at all; test_config covers that.)
        routes = copy.deepcopy(ROUTES)
        del routes["pi"]
        cfg = config(routes=routes)
        everything = detection(set(rc.ENGINES))
        d = router.decide(cfg, everything, {"hand_pick": {"engine": "pi", "model": None}})
        self.assertEqual((d["ok"], d["code"], d["reason"], d["candidates"][0]["model"]),
                         (False, "hand_pick_engine_unavailable", "route_not_configured", None))
        d = router.decide(cfg, everything, {"task_kind": "search"})
        self.assertEqual((d["reason"], d["engine"], d["candidates"][0]["skipped"]), ("default", "claude_code", "route_not_configured"))

    def test_missing_or_malformed_detection_fails_closed(self) -> None:
        launcher = Recorder()
        for det in (None, {}, [], {"engines": None}, {"engines": {"codex": {"routable": "yes"}}}):
            for task in ({"task_kind": "chat"}, {"task_kind": "review"}, {"task_kind": "chat", "hand_pick": {"engine": "codex", "model": None}}):
                d = router.decide(self.cfg, det, task)
                self.assertFalse(d["ok"], (det, task))
                launcher.launch(d)
        self.assertEqual(launcher.calls, [])

    def test_adversarial_snapshots_refuse_everything_without_raising(self) -> None:
        # Each snapshot is SENTRY with one defect, next to a well-formed routable claude_code entry, so a
        # check entry by entry would still route the default; the whole snapshot must be refused.
        def with_extra(key, entry):
            s = copy.deepcopy(SENTRY)
            s["engines"][key] = entry
            return s

        def patched(engine, **fields):
            s = copy.deepcopy(SENTRY)
            s["engines"][engine].update(fields)
            return s

        def dropped(engine, key):
            s = copy.deepcopy(SENTRY)
            del s["engines"][engine][key]
            return s

        cases = {
            "unknown engine id": with_extra("not_an_engine", {"routable": True, "reason": None}),
            "hyphenated engine id": with_extra("claude-code", {"routable": True, "reason": None}),
            "engine key not a string": with_extra(1, {"routable": True, "reason": None}),
            "entry not a mapping": with_extra("codex", "yes"),
            "engines a list": {"engines": [{"engine": "codex", "routable": True, "reason": None}]},
            "engines a string": {"engines": "codex"},
            "routable not a bool (1)": patched("pi", routable=1),
            "routable missing": dropped("pi", "routable"),
            "routable but reason set": patched("pi", routable=True),
            "routable but reason empty": patched("codex", reason=""),
            "not routable and no reason": patched("hermes", reason=None),
            "not routable, reason key missing": dropped("hermes", "reason"),
            "not routable with empty reason": patched("hermes", reason=""),
            "reason not a code word": patched("hermes", reason="Signed Out"),
            "reason not a string": patched("hermes", reason=True),
        }
        tasks = ({"task_kind": "chat"}, {"task_kind": "review"}, {"task_kind": "search"}, {"task_kind": "chat", "tags": ["cheap"]},
                 {"hand_pick": {"engine": "claude_code", "model": None}}, {"hand_pick": {"engine": "pi", "model": None}})
        launcher = Recorder()
        n = 0
        for name, det in cases.items():
            self.assertEqual(router.snapshot(det)[0], None, name)
            for task in tasks:
                d = router.decide(self.cfg, det, task)  # must not raise
                self.assertFalse(d["ok"], (name, task))
                self.assertIn(d["reason"], ("detection_malformed", "detection_unavailable"), (name, task))
                launcher.launch(d)
                n += 1
        self.assertEqual(n, len(cases) * len(tasks))
        self.assertEqual(launcher.calls, [])
        # Control: the unmodified snapshot still routes, so the refusals come from the defects.
        self.assertEqual({e for e, v in router.snapshot(SENTRY)[0].items() if v["routable"]}, {"claude_code", "codex", "opencode"})
        self.assertTrue(router.decide(self.cfg, SENTRY, {"task_kind": "chat"})["ok"])

    def test_exhaustive_every_routable_subset_routes_only_inside_it(self) -> None:
        tasks = [{"task_kind": "chat"}, {"task_kind": "review"}, {"task_kind": "search"}, {"task_kind": "chat", "tags": ["cheap"]}]
        tasks += [{"task_kind": "review", "hand_pick": {"engine": e, "model": None}} for e in rc.ENGINES]
        n_ok = n_refused = 0
        for k in range(len(rc.ENGINES) + 1):
            for subset in itertools.combinations(rc.ENGINES, k):
                det = detection(set(subset))
                for t in tasks:
                    d = router.decide(self.cfg, det, t)
                    if d["ok"]:
                        n_ok += 1
                        self.assertIn(d["engine"], subset, (subset, t))
                        if "hand_pick" in t:
                            self.assertEqual(d["engine"], t["hand_pick"]["engine"], (subset, t))
                    else:
                        n_refused += 1
                        self.assertNotIn(d["engine"], subset, (subset, t))
        self.assertEqual(n_ok + n_refused, 2 ** len(rc.ENGINES) * len(tasks))
        self.assertGreater(n_ok, 0)
        self.assertGreater(n_refused, 0)


class RuleMatchingTests(unittest.TestCase):
    """What a rule's conditions mean: every condition given must hold, every listed tag, any listed kind."""

    def engine(self, rules, task, **extra):
        raw = copy.deepcopy(CONFIG)
        raw["rules"] = rules
        raw.update(extra)
        return router.decide(config(raw), SENTRY, task)["engine"]

    def test_tags_all_needs_every_tag(self) -> None:
        rules = [{"id": "both", "when": {"tags_all": ["cheap", "offline"]}, "route": {"engine": "codex"}}]
        for tags in ([], ["cheap"], ["offline"], ["cheap", "other"]):
            self.assertEqual(self.engine(rules, {"tags": tags}), "claude_code", tags)
        self.assertEqual(self.engine(rules, {"tags": ["offline", "extra", "cheap"]}), "codex")

    def test_task_kind_and_tags_all_must_both_hold(self) -> None:
        rules = [{"id": "both", "when": {"task_kind": ["review"], "tags_all": ["approved"]}, "route": {"engine": "codex"}}]
        for task in ({"task_kind": "review"}, {"tags": ["approved"]}, {"task_kind": "chat", "tags": ["approved"]}, {}):
            self.assertEqual(self.engine(rules, task), "claude_code", task)
        self.assertEqual(self.engine(rules, {"task_kind": "review", "tags": ["approved"]}), "codex")

    def test_any_listed_task_kind_matches(self) -> None:
        rules = [{"id": "either", "when": {"task_kind": ["review", "audit"]}, "route": {"engine": "codex"}}]
        for kind in ("review", "audit"):
            self.assertEqual(self.engine(rules, {"task_kind": kind}), "codex", kind)
        self.assertEqual(self.engine(rules, {"task_kind": "chat"}), "claude_code")

    def test_the_first_matching_rule_wins_in_the_users_order(self) -> None:
        first = {"id": "first", "when": {"tags_all": ["cheap"]}, "route": {"engine": "opencode"}}
        second = {"id": "second", "when": {"task_kind": ["review"]}, "route": {"engine": "codex"}}
        task = {"task_kind": "review", "tags": ["cheap"]}
        self.assertEqual(self.engine([first, second], task), "opencode")
        self.assertEqual(self.engine([second, first], task), "codex")

    def test_an_explicit_model_wins_over_the_configured_one(self) -> None:
        raw = copy.deepcopy(CONFIG)
        raw["default"]["model"] = "explicit-default"
        d = router.decide(config(raw), SENTRY, {"task_kind": "chat"})
        self.assertEqual((d["engine"], d["model"]), ("claude_code", "explicit-default"))
        self.assertEqual(router.decide(config(), SENTRY, {"task_kind": "chat"})["model"], "claude-model-a")


class NoAlternationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = config()

    def test_limit_hits_never_rotate_to_a_second_login_or_engine(self) -> None:
        # claude_code's connection answers a limit every time. Twenty starts all decide the same route;
        # a second Claude subscription on the machine (claude-second) is never reached, nor anything else.
        launcher = Recorder(limit_on={("claude_code", "claude-main")})
        decisions = set()
        for _ in range(20):
            d = router.decide(self.cfg, SENTRY, {"task_kind": "chat"})
            decisions.add((d["engine"], d["login"], d["connection"]["id"]))
            self.assertEqual(launcher.launch(d)["code"], "plan_limit")
        self.assertEqual(decisions, {("claude_code", "anthropic-sub", "claude-main")})
        self.assertEqual(launcher.count("claude_code", "claude-main"), 20)
        self.assertEqual(launcher.count("claude_code", "claude-second"), 0)
        self.assertEqual(len(launcher.calls), 20)

    def test_second_login_of_one_vendor_cannot_be_configured(self) -> None:
        from recursion_core.routing.tests.fixtures import routes_with_second_claude_login
        for kind, connection, eng in (("subscription", "claude-second", "claude_code"), ("api_key", "anthropic-api-2", "pi")):
            raw = copy.deepcopy(CONFIG)
            raw["logins"].append({"id": "anthropic-2", "vendor": "anthropic", "kind": kind, "connection_id": connection,
                                  "engines": [eng]})
            with self.assertRaises(rc.ConfigError) as cm:
                config(raw, routes_with_second_claude_login())
            self.assertEqual(cm.exception.code, "second_login_same_vendor", (kind, connection))

    def test_decision_has_no_history_input(self) -> None:
        task = {"task_kind": "review"}
        first = router.decide(self.cfg, SENTRY, task)
        launcher = Recorder(limit_on={("codex", "chatgpt-main")})
        for _ in range(5):
            self.assertEqual(launcher.launch(router.decide(self.cfg, SENTRY, task))["code"], "plan_limit")
        self.assertEqual(router.decide(self.cfg, SENTRY, task), first)
        self.assertEqual(launcher.count("codex", "chatgpt-main"), 5)
        self.assertEqual(launcher.count("claude_code", "claude-main"), 0)


class PurityTests(unittest.TestCase):
    """In place of the prototype's two loopback-gate tests: nothing a decision does leaves its inputs."""

    def test_decide_and_load_open_no_socket_file_or_process(self) -> None:
        reached: list[str] = []

        def refuse(name):
            def call(*args, **kwargs):
                reached.append(name)
                raise AssertionError(f"routing reached {name}")
            return call

        tasks = [{"task_kind": "chat"}, {"task_kind": "review", "tags": ["cheap"]}, {"task_kind": "search"},
                 {"hand_pick": {"engine": "pi", "model": None}}, {"hand_pick": {"engine": "codex", "model": "m-1"}}]
        targets = ["builtins.open", "io.open", "os.open", "socket.socket", "socket.create_connection", "subprocess.Popen",
                   "os.system", "os.fork", "os.posix_spawn", "os.execv"]
        with contextlib.ExitStack() as stack:
            for target in targets:
                module, name = target.rsplit(".", 1)
                if hasattr(__import__(module), name):  # os.fork and os.posix_spawn are not on every platform
                    stack.enter_context(mock.patch(target, refuse(target)))
            cfg = config()
            for k in range(len(rc.ENGINES) + 1):
                for subset in itertools.combinations(rc.ENGINES, k):
                    for t in tasks:
                        router.decide(cfg, detection(set(subset)), t)
        self.assertEqual(reached, [])
        self.assertIs(socket.socket, socket.socket)  # the patches are gone
        self.assertTrue(callable(subprocess.Popen) and callable(os.open))

    def test_decide_leaves_its_inputs_unchanged_and_shares_nothing(self) -> None:
        cfg = config()
        det = copy.deepcopy(SENTRY)
        task = {"task_kind": "search", "tags": ["cheap"], "hand_pick": None}
        before = (copy.deepcopy(det), copy.deepcopy(task), copy.deepcopy(dict(cfg.routes)))
        d = router.decide(cfg, det, task)
        self.assertEqual((det, task, dict(cfg.routes)), before)
        # Changing what one decision returned changes nothing for the next one.
        d["candidates"].append({"source": "x"})
        d["connection"]["id"] = "changed"
        again = router.decide(cfg, det, task)
        self.assertEqual(again["connection"], {"kind": "api_key", "id": "anthropic-api"})
        self.assertEqual([c["source"] for c in again["candidates"]], ["rule:search-to-pi", "rule:cheap-to-opencode"])
        hp = {"engine": "codex", "model": "m-1"}
        d = router.decide(cfg, det, {"hand_pick": hp})
        d["hand_pick"]["model"] = "changed"
        self.assertEqual(hp, {"engine": "codex", "model": "m-1"})


class RouteDecisionTests(unittest.TestCase):
    """``route_decision`` gives the control schema's shape (common.v1 route_decision in SPINE's draft #368)."""

    def test_route_decision_has_the_control_schemas_shape(self) -> None:
        cfg = config()
        for task, reason in (({"hand_pick": {"engine": "opencode", "model": "hand-model"}, "task_kind": "review"}, "hand_pick"),
                             ({"task_kind": "search", "tags": ["cheap"]}, "config"),
                             ({"task_kind": "chat"}, "default")):
            rd = router.route_decision(router.decide(cfg, SENTRY, task))
            self.assertEqual(set(rd), {"chosen", "reason", "candidates", "hand_pick"})
            self.assertEqual(set(rd["chosen"]), {"engine", "model", "connection"})
            self.assertEqual(set(rd["chosen"]["connection"]), {"kind", "id"})
            self.assertEqual(rd["reason"], reason)
            self.assertIn(rd["reason"], router.REASONS)
            for c in rd["candidates"]:
                self.assertEqual(set(c), {"engine", "model", "skipped"})
            self.assertEqual([c["skipped"] for c in rd["candidates"]][-1], None)
            self.assertTrue(all(c["skipped"] for c in rd["candidates"][:-1]))
            if reason == "hand_pick":
                self.assertEqual(rd["hand_pick"], {"engine": "opencode", "model": "hand-model"})
            else:
                self.assertIsNone(rd["hand_pick"])
        rd = router.route_decision(router.decide(cfg, SENTRY, {"task_kind": "search", "tags": ["cheap"]}))
        self.assertEqual(rd, {"chosen": {"engine": "opencode", "model": "anthropic/model-b",
                                         "connection": {"kind": "api_key", "id": "anthropic-api"}},
                              "reason": "config",
                              "candidates": [{"engine": "pi", "model": "xai/model-c", "skipped": "not_on_path"},
                                             {"engine": "opencode", "model": "anthropic/model-b", "skipped": None}],
                              "hand_pick": None})
        self.assertIsNone(router.route_decision(router.decide(cfg, SENTRY, {"hand_pick": {"engine": "pi", "model": None}})))


if __name__ == "__main__":
    unittest.main()
