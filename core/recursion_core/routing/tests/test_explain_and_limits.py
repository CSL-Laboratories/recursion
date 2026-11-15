"""explain() says why a decision came out as it did; next_after_limit() never moves a task to a route the user
did not list (lane ROUTING, slice 2)."""

from __future__ import annotations

import copy
import itertools
import unittest

from recursion_core.routes import contract as c
from recursion_core.routes import limits as route_limits
from recursion_core.routing import config as rc
from recursion_core.routing import router
from recursion_core.routing.tests.fixtures import CONFIG, ROUTES, SENTRY, config, detection

TASKS = [{"task_kind": "chat"}, {"task_kind": "review"}, {"task_kind": "search"}, {"task_kind": "search", "tags": ["cheap"]},
         {"task_kind": "review", "tags": ["cheap"]}, {}, {"tags": ["cheap"]}, None, {"tags": "x"},
         *({"task_kind": "review", "hand_pick": {"engine": e, "model": None}} for e in rc.ENGINES)]


class ExplainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = config()

    def rows(self, out):
        return [(r["id"], r["outcome"], r["reason"]) for r in out["rules"]] + [("default", out["default"]["outcome"], out["default"]["reason"])]

    def test_every_rule_and_the_default_say_why(self) -> None:
        out = router.explain(self.cfg, SENTRY, {"task_kind": "search", "tags": ["cheap"]})
        self.assertEqual(self.rows(out), [("review-to-codex", "no_match", "task_kind"), ("search-to-pi", "skipped", "not_on_path"),
                                          ("cheap-to-opencode", "chosen", None), ("default", "not_reached", None)])
        self.assertEqual(out["route_decision"]["chosen"]["engine"], "opencode")
        self.assertEqual(out["rules"][1]["model"], "xai/model-c")  # the model routes.json configures for pi
        out = router.explain(self.cfg, SENTRY, {"task_kind": "chat", "tags": ["dear"]})
        self.assertEqual(self.rows(out), [("review-to-codex", "no_match", "task_kind"), ("search-to-pi", "no_match", "task_kind"),
                                          ("cheap-to-opencode", "no_match", "tags_all"), ("default", "chosen", None)])

    def test_a_hand_pick_considers_no_rule_and_no_default(self) -> None:
        out = router.explain(self.cfg, SENTRY, {"task_kind": "review", "hand_pick": {"engine": "opencode", "model": "m-1"}})
        self.assertEqual({r[1] for r in self.rows(out)}, {"hand_pick"})
        self.assertEqual((out["decision"]["engine"], out["hand_pick"]), ("opencode", {"engine": "opencode", "model": "m-1"}))

    def test_refusals_are_explained(self) -> None:
        raw = copy.deepcopy(CONFIG)
        raw["undetected_rule"] = "refuse"
        out = router.explain(config(raw), SENTRY, {"task_kind": "search", "tags": ["cheap"]})
        self.assertEqual(self.rows(out), [("review-to-codex", "no_match", "task_kind"), ("search-to-pi", "refused", "not_on_path"),
                                          ("cheap-to-opencode", "not_reached", None), ("default", "not_reached", None)])
        out = router.explain(self.cfg, detection({"codex"}, {"claude_code": "signed_out"}), {"task_kind": "chat"})
        self.assertEqual(out["default"], {"engine": "claude_code", "model": "claude-model-a", "outcome": "refused", "reason": "signed_out"})
        self.assertIsNone(out["route_decision"])
        out = router.explain(self.cfg, SENTRY, {"tags": "cheap"})
        self.assertEqual({r[1:] for r in self.rows(out)}, {("not_reached", "bad_task")})

    def test_explain_agrees_with_decide_everywhere(self) -> None:
        for undetected in ("skip", "refuse"):
            raw = copy.deepcopy(CONFIG)
            raw["undetected_rule"] = undetected
            cfg = config(raw)
            for k in range(len(rc.ENGINES) + 1):
                for subset in itertools.combinations(rc.ENGINES, k):
                    det = detection(set(subset))
                    for task in TASKS:
                        out = router.explain(cfg, det, task)
                        d = router.decide(cfg, det, task)
                        self.assertEqual(out["decision"], d)
                        outcomes = [r["outcome"] for r in out["rules"]] + [out["default"]["outcome"]]
                        chosen = outcomes.count("chosen")
                        self.assertEqual(chosen, 1 if d["ok"] and d["reason"] != "hand_pick" else 0, (subset, task))
                        self.assertEqual(outcomes.count("refused"), 0 if d["ok"] or d["code"] in ("bad_task", "bad_hand_pick")
                                         or d["hand_pick"] is not None else 1, (subset, task))
                        skipped = [(r["engine"], r["reason"]) for r in out["rules"] if r["outcome"] in ("skipped", "refused")]
                        if out["default"]["outcome"] == "refused":
                            skipped.append((out["default"]["engine"], out["default"]["reason"]))
                        if d["hand_pick"] is None:
                            self.assertEqual(skipped, [(c_["engine"], c_["skipped"]) for c_ in d["candidates"] if c_["skipped"]])


def with_limits(limits, where="default"):
    raw = copy.deepcopy(CONFIG)
    target = raw["default"] if where == "default" else raw["rules"][0]["route"]
    target["limits"] = limits
    return config(raw)


CODEX = copy.deepcopy(ROUTES["codex"]["route"])
OPENCODE = copy.deepcopy(ROUTES["opencode"]["route"])
LIMIT = {"code": c.PLAN_LIMIT, "limit": c.limit_signal("connection", "test.typed", reset_at=5000.0)}


class NextAfterLimitTests(unittest.TestCase):
    def test_the_next_listed_route_in_the_users_order(self) -> None:
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX, OPENCODE]})
        d = router.decide(cfg, SENTRY, {"task_kind": "chat"})
        out = router.next_after_limit(cfg, d, LIMIT)
        self.assertEqual((out["action"], out["place"], out["route"], out["source"]), ("switch", 1, CODEX, "routing"))
        # The route service already tried the first fallback in this request: the next one.
        notice = {"attempts": [route_limits.describe(ROUTES["claude_code"]["route"]), route_limits.describe(CODEX)]}
        out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": notice})
        self.assertEqual((out["action"], out["place"], out["route"]["engine"]), ("switch", 2, "opencode"))
        self.assertEqual(out["skipped"], [{"place": 1, **route_limits.describe(CODEX), "skipped": "already_tried"}])
        notice["attempts"].append(route_limits.describe(OPENCODE))
        out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": notice})
        self.assertEqual((out["action"], out["reason"]), ("ask", "fallback_exhausted"))
        self.assertEqual([ch["engine"] for ch in out["choices"]], ["codex", "opencode"])

    def test_a_listed_route_whose_engine_is_not_routable_is_passed_over(self) -> None:
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX, OPENCODE]})
        d = router.decide(cfg, SENTRY, {"task_kind": "chat"})
        det = detection({"claude_code", "opencode"}, {"codex": "signed_out"})
        out = router.next_after_limit(cfg, d, LIMIT, det)
        self.assertEqual((out["action"], out["place"]), ("switch", 2))
        self.assertEqual(out["skipped"][0]["skipped"], "signed_out")
        out = router.next_after_limit(cfg, d, LIMIT, detection({"claude_code"}))
        self.assertEqual((out["action"], out["reason"]), ("ask", "fallback_exhausted"))

    def test_only_a_limit_leads_anywhere(self) -> None:
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {"task_kind": "chat"})
        for code in (c.ENGINE_ERROR, c.AUTH_FAILED, c.SHORT_FAULT, c.ENGINE_EXITED, c.DEADLINE_EXCEEDED, c.PROTOCOL_ERROR, None, "x"):
            self.assertEqual(router.next_after_limit(cfg, d, {"code": code}), {"action": "none", "reason": "not_a_limit"}, code)
        for code in (c.PLAN_LIMIT, c.BREAKER_OPEN, c.PROBE_IN_FLIGHT):
            self.assertEqual(router.next_after_limit(cfg, d, {"code": code})["action"], "switch", code)
        refused = router.decide(cfg, SENTRY, {"hand_pick": {"engine": "pi", "model": None}})
        self.assertEqual(router.next_after_limit(cfg, refused, LIMIT), {"action": "none", "reason": "no_route"})

    def test_ask_and_wait_are_the_users_settings(self) -> None:
        cfg = with_limits({"on_limit": "ask", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {"task_kind": "chat"})
        self.assertEqual(router.next_after_limit(cfg, d, LIMIT),
                         {"action": "ask", "reason": "on_limit_ask", "choices": [route_limits.describe(CODEX)]})
        cfg = with_limits({"on_limit": "wait"})
        d = router.decide(cfg, SENTRY, {"task_kind": "chat"})
        self.assertEqual(router.next_after_limit(cfg, d, LIMIT), {"action": "wait", "until": 5000.0, "choices": []})
        self.assertEqual(router.next_after_limit(cfg, d, {"code": c.BREAKER_OPEN, "retry_at": 7.0})["until"], 7.0)
        # Nothing configured anywhere: the user's default, show and ask.
        d = router.decide(config(), SENTRY, {"task_kind": "chat"})
        self.assertEqual(router.next_after_limit(config(), d, LIMIT), {"action": "ask", "reason": "on_limit_ask", "choices": []})

    def test_a_hand_pick_is_never_switched_away_from(self) -> None:
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {"hand_pick": {"engine": "claude_code", "model": None}})
        self.assertEqual(router.next_after_limit(cfg, d, LIMIT)["action"], "ask")
        self.assertEqual(router.next_after_limit(cfg, d, LIMIT)["reason"], "hand_pick")

    def test_routes_json_limits_apply_when_the_routing_file_gives_none(self) -> None:
        routes = copy.deepcopy(ROUTES)
        routes["codex"]["route"]["limits"] = {"on_limit": "fallback", "fallback": [copy.deepcopy(ROUTES["claude_code"]["route"])]}
        cfg = config(routes=routes)
        d = router.decide(cfg, SENTRY, {"task_kind": "review"})
        out = router.next_after_limit(cfg, d, LIMIT)
        self.assertEqual((out["action"], out["route"]["engine"], out["source"]), ("switch", "claude_code", "routes"))
        # The routing file's own limits for a route win over routes.json's.
        raw = copy.deepcopy(CONFIG)
        raw["rules"][0]["route"]["limits"] = {"on_limit": "ask"}
        d = router.decide(config(raw, routes), SENTRY, {"task_kind": "review"})
        self.assertEqual(router.next_after_limit(config(raw, routes), d, LIMIT)["action"], "ask")

    def test_never_a_route_the_user_did_not_list(self) -> None:
        listed = [CODEX, OPENCODE, copy.deepcopy(ROUTES["pi"]["route"])]
        n = 0
        for k in range(1, len(listed) + 1):
            for order in itertools.permutations(listed, k):
                cfg = with_limits({"on_limit": "fallback", "fallback": [copy.deepcopy(r) for r in order]})
                for subset in itertools.combinations(rc.ENGINES, 3):
                    det = detection(set(subset) | {"claude_code"})
                    d = router.decide(cfg, det, {"task_kind": "chat"})
                    for tried in range(len(order) + 1):
                        notice = {"attempts": [route_limits.describe(r) for r in order[:tried]]}
                        out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": notice}, det)
                        self.assertIn(out["action"], ("switch", "ask"))
                        if out["action"] == "switch":
                            self.assertIn(out["route"], list(order))
                            self.assertEqual(out["route"], order[out["place"] - 1])
                            self.assertIn(out["route"]["engine"], set(subset) | {"claude_code"})
                            self.assertGreater(out["place"], tried)
                        n += 1
        self.assertGreater(n, 500)


class ExplainAnswerTests(unittest.TestCase):
    """route.explain's answer, its published shape only (methods.v1 answer.route.explain, #368 at 16dea486)."""

    def setUp(self) -> None:
        self.cfg = config()

    def test_a_routed_task_answers_ok_and_the_route_decision_only(self) -> None:
        for params in ({"task_kind": "review"}, {"task_kind": "search", "tags": ["cheap"]}, {},
                       {"task_kind": "chat", "hand_pick": {"engine": "opencode", "model": "m-1"}},
                       {"session_id": "s-1", "task_kind": "review"}):
            with self.subTest(params):
                task = {k: v for k, v in params.items() if k != "session_id"}
                answer = router.explain_answer(self.cfg, SENTRY, params)
                self.assertEqual(answer, {"code": "ok", "decision": router.route_decision(router.decide(self.cfg, SENTRY, task))})
                self.assertEqual(set(answer["decision"]), {"chosen", "reason", "candidates", "hand_pick"})

    def test_a_refused_decision_answers_request_invalid_with_routings_code(self) -> None:
        for params, det, detail in (
                ({"hand_pick": {"engine": "pi", "model": None}}, SENTRY, "hand_pick_engine_unavailable:not_on_path"),
                ({"task_kind": "chat"}, detection({"codex"}, {"claude_code": "signed_out"}), "default_engine_unavailable:signed_out"),
                ({"tags": ["cheap", ""]}, SENTRY, "bad_task:tags")):
            with self.subTest(params):
                self.assertEqual(router.explain_answer(self.cfg, det, params), {"code": "request_invalid", "detail": detail})

    def test_the_h33_example_is_refused_never_answered_by_the_config(self) -> None:
        # #368's good/methods.v1/h33-route-explain-ok.json answers these params with a config decision, hand_pick
        # null and codex skipped breaker_open. Routing never replaces a hand pick (grill Q45): it refuses.
        det = detection({"claude_code", "opencode"}, {"codex": "breaker_open"})
        answer = router.explain_answer(self.cfg, det, {"hand_pick": {"engine": "codex", "model": None}, "task_kind": "code_edit"})
        self.assertEqual(answer, {"code": "request_invalid", "detail": "hand_pick_engine_unavailable:breaker_open"})

    def test_an_engine_and_a_hand_pick(self) -> None:
        hp = {"engine": "opencode", "model": None}
        self.assertEqual(router.explain_answer(self.cfg, SENTRY, {"engine": "codex", "hand_pick": hp}),
                         {"code": "request_invalid", "detail": "engine_and_hand_pick_differ"})
        same = router.explain_answer(self.cfg, SENTRY, {"engine": "opencode", "hand_pick": hp})
        self.assertEqual((same["code"], same["decision"]["reason"], same["decision"]["hand_pick"]), ("ok", "hand_pick", hp))
        self.assertEqual(router.explain_answer(self.cfg, SENTRY, {"engine": "codex"}),
                         {"code": "request_invalid", "detail": "engine_named_not_routed"})

    def test_params_outside_the_published_ones_are_refused(self) -> None:
        for params, detail in (({"task_kind": "review", "repo": "x"}, "repo"), (None, "params"), (["review"], "params"),
                               ({"hand_pick": {"engine": "codex"}}, "hand_pick"), ({"hand_pick": None}, "hand_pick"),
                               ({"hand_pick": "codex"}, "hand_pick"), ({"Task Kind": "x"}, "params")):
            with self.subTest(params):
                self.assertEqual(router.explain_answer(self.cfg, SENTRY, params), {"code": "request_invalid", "detail": detail})


if __name__ == "__main__":
    unittest.main()
