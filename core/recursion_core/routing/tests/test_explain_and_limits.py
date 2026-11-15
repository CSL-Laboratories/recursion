"""explain() says why a decision came out as it did; next_after_limit() never moves a task to a route the user
did not list (lane ROUTING, slice 2)."""

from __future__ import annotations

import copy
import itertools
import re
import unittest
from collections.abc import Mapping

from recursion_core import routing
from recursion_core.routes import contract as c
from recursion_core.routes import limits as route_limits
from recursion_core.routing import config as rc
from recursion_core.routing import detect, router
from recursion_core.routing.tests.fixtures import CONFIG, ROUTES, SENTRY, config, detection

CODE_WORD = re.compile(r"[a-z][a-z0-9_]{0,63}")

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


class _ReadCounting(Mapping):
    """A task whose task_kind reads "review" the first time and "chat" after (a caller still writing it)."""

    def __init__(self) -> None:
        self.reads = 0

    def __getitem__(self, key):
        if key != "task_kind":
            raise KeyError(key)
        self.reads += 1
        return "review" if self.reads == 1 else "chat"

    def __contains__(self, key) -> bool:  # asking whether a member is there is not a read of its value
        return key == "task_kind"

    def __iter__(self):
        return iter(("task_kind",))

    def __len__(self) -> int:
        return 1


class ReviewRegressionTests(unittest.TestCase):
    """The first reads of #399 (05a5d13d): Codex gpt-6.1-sol R1-R2, DeepSeek V4.1 Flash F1-F10."""

    def test_wait_uses_breaker_retry_when_vendor_reset_is_null(self) -> None:  # sol R1, ds F1
        cfg = with_limits({"on_limit": "wait"})
        decision = router.decide(cfg, SENTRY, {})
        result = c.result("review", c.PLAN_LIMIT, limit=c.limit_signal("connection", "review.typed"), retry_at=7000.0)
        self.assertEqual(router.next_after_limit(cfg, decision, result)["until"], 7000.0)
        zero = c.result("review", c.PLAN_LIMIT, limit=c.limit_signal("connection", "review.typed", reset_at=0.0),
                        retry_at=7000.0)
        self.assertEqual(router.next_after_limit(cfg, decision, zero)["until"], 0.0)  # an explicit zero is kept

    def test_primary_not_reoffered_without_notice(self) -> None:  # sol R2
        primary = copy.deepcopy(ROUTES["claude_code"]["route"])
        cfg = with_limits({"on_limit": "fallback", "fallback": [primary, CODEX]})
        decision = router.decide(cfg, SENTRY, {})
        answer = router.next_after_limit(cfg, decision, LIMIT)
        self.assertEqual(answer["route"]["engine"], "codex")
        self.assertEqual(answer["skipped"][0]["skipped"], "already_tried")

    def test_same_engine_and_connection_other_model_still_allowed(self) -> None:  # sol R2
        other = copy.deepcopy(ROUTES["claude_code"]["route"])
        other["model"] = "claude-model-b"
        cfg = with_limits({"on_limit": "fallback", "fallback": [other, CODEX]})
        decision = router.decide(cfg, SENTRY, {})
        self.assertEqual(router.next_after_limit(cfg, decision, LIMIT)["route"], other)

    def test_a_route_with_no_model_anywhere_is_not_routable(self) -> None:  # ds F2
        routes = copy.deepcopy(ROUTES)
        del routes["claude_code"]["route"]["model"]
        cfg = config(routes=routes)
        d = router.decide(cfg, SENTRY, {"task_kind": "chat"})
        self.assertEqual((d["ok"], d["code"], d["reason"]), (False, "default_engine_unavailable", "route_no_model"))
        self.assertIsNone(router.route_decision(d))
        self.assertEqual(router.explain_answer(cfg, SENTRY, {"task_kind": "chat"}),
                         {"code": "request_invalid", "detail": "default_engine_unavailable:route_no_model"})
        self.assertEqual(router.decide(cfg, SENTRY, {"hand_pick": {"engine": "claude_code", "model": None}})["reason"],
                         "route_no_model")
        picked = router.decide(cfg, SENTRY, {"hand_pick": {"engine": "claude_code", "model": "m-picked"}})
        self.assertEqual((picked["ok"], picked["model"]), (True, "m-picked"))
        raw = copy.deepcopy(CONFIG)
        raw["default"]["model"] = "m-default"
        self.assertEqual(router.decide(config(raw, routes=routes), SENTRY, {})["model"], "m-default")

    def test_every_refusal_detail_is_code_words(self) -> None:  # ds F3
        cases = [{"hand_pick": {"engine": "bogus", "model": None}}, {"hand_pick": {"engine": "pi", "model": ""}},
                 {"hand_pick": {"engine": "pi", "model": "two words"}}, {"task_kind": 5}, {"tags": ["a", ""]},
                 {"task_kind": None}, {"hand_pick": {"engine": "pi", "model": None}},
                 {"hand_pick": {"engine": "recursion_loop", "model": None}}, {"engine": "codex"},
                 {"engine": "codex", "hand_pick": {"engine": "opencode", "model": None}}, {"repo": "x"}, {1: "x"}]
        for params in cases:
            with self.subTest(params):
                answer = router.explain_answer(config(), SENTRY, params)
                self.assertEqual(answer["code"], "request_invalid")
                self.assertTrue(all(CODE_WORD.fullmatch(part) for part in answer["detail"].split(":")), answer["detail"])

    def test_explain_reads_the_task_once(self) -> None:  # ds F4
        task = _ReadCounting()
        out = router.explain(config(), SENTRY, task)
        self.assertEqual(task.reads, 1)
        self.assertEqual((out["decision"]["rule"], out["decision"]["engine"]), ("review-to-codex", "codex"))
        chosen = [r["id"] for r in out["rules"] if r["outcome"] == "chosen"]
        self.assertEqual((chosen, out["default"]["outcome"]), (["review-to-codex"], "not_reached"))

    def test_a_bad_hand_pick_is_explained_as_not_reached(self) -> None:  # ds F5
        out = router.explain(config(), SENTRY, {"hand_pick": {"engine": "codex", "model": None, "x": 1}})
        rows = {(r["outcome"], r["reason"]) for r in out["rules"]} | {(out["default"]["outcome"], out["default"]["reason"])}
        self.assertEqual(rows, {("not_reached", "bad_hand_pick")})

    def test_next_after_limit_takes_decides_answer_only(self) -> None:  # ds F6
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {})
        for wrong in (router.route_decision(d), None, {}, "decision"):
            self.assertEqual(router.next_after_limit(cfg, wrong, LIMIT), {"action": "none", "reason": "no_decision"}, wrong)
        self.assertEqual(router.next_after_limit(cfg, d, LIMIT)["action"], "switch")

    def test_a_snapshot_that_cannot_be_walked_is_malformed(self) -> None:  # ds F10
        class Hostile(Mapping):
            def __getitem__(self, key):
                raise KeyError(key)

            def __iter__(self):
                raise RuntimeError("cannot walk")

            def __len__(self):
                return 1
        self.assertEqual(router.snapshot({"engines": Hostile()}), (None, "detection_malformed"))
        d = router.decide(config(), {"engines": Hostile()}, {})
        self.assertEqual((d["ok"], d["reason"]), (False, "detection_malformed"))

    def test_small_interface_gaps(self) -> None:  # ds F10; second reads: sol C2, C3
        self.assertEqual(routing.EXPLAIN_PARAMS, ("session_id", "engine", "hand_pick", "task_kind", "tags"))
        self.assertIn("EXPLAIN_PARAMS", routing.__all__)
        for active in ("codex", b"codex"):
            with self.assertRaises(TypeError):
                detect.detect(config(), active=active)

    def test_a_rule_without_model_is_skipped_or_refused(self) -> None:  # second reads: sol C1
        routes = copy.deepcopy(ROUTES)
        del routes["codex"]["route"]["model"]
        raw = copy.deepcopy(CONFIG)
        del raw["rules"][0]["route"]["model"]
        out = router.explain(config(raw, routes=routes), SENTRY, {"task_kind": "review"})
        self.assertEqual((out["decision"]["ok"], out["decision"]["engine"]), (True, "claude_code"))
        self.assertEqual((out["rules"][0]["outcome"], out["rules"][0]["reason"]), ("skipped", "route_no_model"))
        raw["undetected_rule"] = "refuse"
        d = router.decide(config(raw, routes=routes), SENTRY, {"task_kind": "review"})
        self.assertEqual((d["ok"], d["code"], d["reason"]), (False, "rule_engine_unavailable", "route_no_model"))
        raw["rules"][0]["route"]["model"] = "explicit-model"
        d = router.decide(config(raw, routes=routes), SENTRY, {"task_kind": "review"})
        self.assertEqual((d["ok"], d["model"]), (True, "explicit-model"))


class ReviewTwoRegressionTests(unittest.TestCase):
    """The second reads of #399 (d857ebb5): DeepSeek V4.1 Flash R1-R7."""

    def test_a_null_hand_pick_is_refused_alike_everywhere(self) -> None:  # ds R1
        cfg = config()
        task = {"task_kind": "review", "hand_pick": None}
        d = router.decide(cfg, SENTRY, task)
        self.assertEqual((d["ok"], d["code"], d["reason"]), (False, "bad_hand_pick", "hand_pick"))
        self.assertEqual(router.explain(cfg, SENTRY, task)["decision"], d)
        self.assertEqual(router.explain_answer(cfg, SENTRY, task), {"code": "request_invalid", "detail": "hand_pick"})

    def test_a_null_attempts_list_and_a_decision_in_name_only(self) -> None:  # ds R2
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {})
        out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": {"attempts": None}})
        self.assertEqual((out["action"], out["route"]["engine"]), ("switch", "codex"))
        for partial in ({"ok": True}, {"ok": True, "reason": "config", "engine": "codex"},
                        {**d, "connection": None}, {**d, "connection": {"kind": "subscription"}},
                        {k: v for k, v in d.items() if k != "rule"}):
            self.assertEqual(router.next_after_limit(cfg, partial, LIMIT), {"action": "none", "reason": "no_decision"}, partial)
        # Third reads, sol 1: each member alone, and a connection that is not a mapping or has no kind.
        for missing in ("reason", "rule", "engine", "model", "connection"):
            partial = {k: v for k, v in d.items() if k != missing}
            self.assertEqual(router.next_after_limit(cfg, partial, LIMIT), {"action": "none", "reason": "no_decision"}, missing)
        for connection in ({"id": "claude-main"}, ["kind", "id"]):
            self.assertEqual(router.next_after_limit(cfg, {**d, "connection": connection}, LIMIT),
                             {"action": "none", "reason": "no_decision"}, connection)
        refused = router.decide(cfg, SENTRY, {"hand_pick": {"engine": "pi", "model": None}})
        self.assertEqual(router.next_after_limit(cfg, refused, LIMIT), {"action": "none", "reason": "no_route"})

    def test_an_invalid_routes_json_limits_block_falls_back_to_ask(self) -> None:  # ds R3
        for bad in ({"on_limit": "sometimes"}, {"on_limit": "ask", "x": 1}, []):
            with self.subTest(bad):
                routes = copy.deepcopy(ROUTES)
                routes["claude_code"]["route"]["limits"] = bad
                cfg = config(routes=routes)
                d = router.decide(cfg, SENTRY, {})
                self.assertEqual(router.next_after_limit(cfg, d, LIMIT),
                                 {"action": "ask", "reason": "on_limit_ask", "choices": []})

    def test_an_exhausted_list_names_each_listed_route_once_in_order(self) -> None:  # ds R4
        pi = copy.deepcopy(ROUTES["pi"]["route"])
        order = [CODEX, OPENCODE, pi]
        cfg = with_limits({"on_limit": "fallback", "fallback": order})
        d = router.decide(cfg, SENTRY, {})
        notice = {"attempts": [route_limits.describe(r) for r in order]}
        out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": notice})
        self.assertEqual((out["action"], out["reason"]), ("ask", "fallback_exhausted"))
        self.assertEqual(out["skipped"], [{"place": n, **route_limits.describe(r), "skipped": "already_tried"}
                                          for n, r in enumerate(order, 1)])

    def test_a_task_that_cannot_be_read_is_refused(self) -> None:  # ds R5
        class Hostile(dict):
            def __init__(self, broken: str) -> None:
                super().__init__(task_kind="review")
                self.broken = broken

            def __contains__(self, key):
                if self.broken == "contains":
                    raise RuntimeError("contains")
                return super().__contains__(key)

            def get(self, key, default=None):
                if self.broken == "get":
                    raise RuntimeError("get")
                return super().get(key, default)
        for broken in ("contains", "get"):
            d = router.decide(config(), SENTRY, Hostile(broken))
            self.assertEqual((d["ok"], d["code"], d["reason"]), (False, "bad_task", "task"), broken)
            self.assertEqual(router.explain(config(), SENTRY, Hostile(broken))["decision"], d)

    def test_route_decision_of_anything_but_a_routed_decision_is_none(self) -> None:  # ds R7; third reads N4
        for value in (None, 5, "x", {}, {"ok": False}, {"ok": True}):
            self.assertIsNone(router.route_decision(value), value)
        d = router.decide(config(), SENTRY, {})
        for missing in ("reason", "engine", "model", "connection", "candidates", "hand_pick"):
            self.assertIsNone(router.route_decision({k: v for k, v in d.items() if k != missing}), missing)
        self.assertEqual(router.route_decision(d)["chosen"]["engine"], "claude_code")

    def test_a_hand_pick_that_cannot_be_read_is_refused(self) -> None:  # third reads, sol 2
        class HostileGet(dict):
            def get(self, key, default=None):
                raise RuntimeError("hand_pick get")

        class HostileIter(dict):
            def __iter__(self):
                raise RuntimeError("hand_pick iter")

        for hand in (HostileGet(engine="codex", model=None), HostileIter(engine="codex", model=None)):
            task = {"hand_pick": hand}
            d = router.decide(config(), SENTRY, task)
            self.assertEqual((d["ok"], d["code"], d["reason"]), (False, "bad_hand_pick", "hand_pick"))
            self.assertEqual(router.explain(config(), SENTRY, task)["decision"], d)


class _Unreadable(Mapping):
    """A mapping every read of which raises: a caller's object that is broken."""

    def __init__(self, broken: str) -> None:
        self.broken = broken

    def __contains__(self, key):
        if self.broken in ("contains", "all"):
            raise RuntimeError("contains")
        return key in ("engines", "task_kind")  # so that reading its value is what breaks

    def __getitem__(self, key):
        raise RuntimeError("getitem")

    def get(self, key, default=None):
        raise RuntimeError("get")

    def __iter__(self):
        if self.broken in ("iter", "all"):
            raise RuntimeError("iter")
        return iter(("engines",))

    def __len__(self) -> int:
        return 1


class ReviewThreeRegressionTests(unittest.TestCase):
    """The third reads of #399 (1bf3254d): DeepSeek V4.1 Flash N1-N5 (Sol's two are beside the code they pin)."""

    def test_a_detection_object_that_cannot_be_read_refuses_every_task(self) -> None:  # ds N1
        for broken in ("contains", "getitem"):
            self.assertEqual(router.snapshot(_Unreadable(broken)), (None, "detection_malformed"), broken)
            d = router.decide(config(), _Unreadable(broken), {})
            self.assertEqual((d["ok"], d["code"], d["reason"]), (False, "default_engine_unavailable", "detection_malformed"))

    def test_explain_answer_refuses_params_it_cannot_read(self) -> None:  # ds N2
        for broken in ("contains", "iter", "getitem"):
            self.assertEqual(router.explain_answer(config(), SENTRY, _Unreadable(broken)),
                             {"code": "request_invalid", "detail": "params"}, broken)

        class HostileHand(dict):
            def __iter__(self):
                raise RuntimeError("iter")
        self.assertEqual(router.explain_answer(config(), SENTRY, {"hand_pick": HostileHand(engine="codex", model=None)}),
                         {"code": "request_invalid", "detail": "hand_pick"})

    def test_a_decision_with_an_engine_decide_never_gives_is_no_decision(self) -> None:  # ds N3
        cfg = config()  # the default route has no limits of its own, so routes.json's are looked up by engine
        d = router.decide(cfg, SENTRY, {})
        for odd in ({**d, "engine": ["codex"]}, {**d, "engine": None}, {**d, "model": ["m"]}):
            self.assertEqual(router.next_after_limit(cfg, odd, LIMIT), {"action": "none", "reason": "no_decision"}, odd)
        self.assertEqual(router.next_after_limit(cfg, d, LIMIT)["action"], "ask")

    def test_a_limit_result_that_cannot_be_read_is_not_a_limit(self) -> None:  # ds N5; fourth reads E, sol's later N5
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {})
        self.assertEqual(router.next_after_limit(cfg, d, _Unreadable("all")), {"action": "none", "reason": "not_a_limit"})

        class FailsOn(dict):
            """A limit result whose get answers ``code`` and fails on one other member."""

            def __init__(self, member: str, **values) -> None:
                super().__init__(code=c.PLAN_LIMIT, **values)
                self.member = member

            def get(self, key, default=None):
                if key == self.member:
                    raise RuntimeError(key)
                return super().get(key, default)

        class Broken(dict):
            def get(self, key, default=None):
                raise RuntimeError(key)
        wait = with_limits({"on_limit": "wait"})
        dw = router.decide(wait, SENTRY, {})
        not_a_limit = {"action": "none", "reason": "not_a_limit"}
        for member in ("limit", "retry_at"):  # the wait path's reads
            self.assertEqual(router.next_after_limit(wait, dw, FailsOn(member)), not_a_limit, member)
        self.assertEqual(router.next_after_limit(cfg, d, FailsOn("limit_notice")), not_a_limit)  # the switch path's
        # The nested reads too: the vendor's signal and the notice. An attempt in it that cannot be read is dropped
        # like any other non-attempt (sixth read, F2), so the limit itself still leads where the user listed.
        self.assertEqual(router.next_after_limit(wait, dw, {"code": c.PLAN_LIMIT, "limit": Broken()}), not_a_limit)
        self.assertEqual(router.next_after_limit(cfg, d, {"code": c.PLAN_LIMIT, "limit_notice": Broken()}), not_a_limit)
        self.assertEqual(router.next_after_limit(cfg, d, {"code": c.PLAN_LIMIT, "limit_notice": {"attempts": [Broken()]}})["action"],
                         "switch")


class ReviewFourRegressionTests(unittest.TestCase):
    """The fourth reads of #399 (477fa308): DeepSeek V4.1 Flash A-F and I (E is beside N5's test)."""

    def test_route_decision_of_an_unreadable_or_wrong_typed_decision_is_none(self) -> None:  # ds A, B
        class UnreadableDecision(dict):
            def __getitem__(self, key):
                raise RuntimeError("getitem")
        d = router.decide(config(), SENTRY, {})
        for broken in ("contains", "getitem", "all"):
            self.assertIsNone(router.route_decision(_Unreadable(broken)), broken)
        self.assertIsNone(router.route_decision(UnreadableDecision(d)))
        for member, value in (("connection", ["a", "b"]), ("connection", None), ("connection", "x:y"),
                              ("candidates", [None]), ("candidates", 5), ("hand_pick", "codex"), ("hand_pick", 5),
                              ("reason", ["config"]), ("reason", "learned"), ("engine", ["codex"]), ("model", ["m"]),
                              ("model", None)):
            self.assertIsNone(router.route_decision({**d, member: value}), (member, value))
        self.assertEqual(router.route_decision(d)["chosen"]["model"], "claude-model-a")

    def test_next_after_limit_answers_no_decision_for_a_decision_it_cannot_read(self) -> None:  # ds C, D
        class UnreadableDecision(dict):
            def __getitem__(self, key):
                raise RuntimeError("getitem")

        class UnwalkableConnection(dict):
            def __iter__(self):
                raise RuntimeError("connection iter")
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {})
        no_decision = {"action": "none", "reason": "no_decision"}
        self.assertEqual(router.next_after_limit(cfg, _Unreadable("contains"), LIMIT), no_decision)
        self.assertEqual(router.next_after_limit(cfg, UnreadableDecision(d), LIMIT), no_decision)
        connection = UnwalkableConnection(kind="subscription", id="claude-main")
        self.assertEqual(router.next_after_limit(cfg, {**d, "connection": connection}, LIMIT), no_decision)

    def test_a_missing_snapshot_is_detection_unavailable(self) -> None:  # ds F
        for missing in (None, 5, "engines", ["engines"]):
            self.assertEqual(router.snapshot(missing), (None, "detection_unavailable"), missing)
        d = router.decide(config(), None, {})
        self.assertEqual((d["ok"], d["code"], d["reason"]), (False, "default_engine_unavailable", "detection_unavailable"))
        self.assertEqual(router.snapshot({"engines": []}), (None, "detection_malformed"))

    def test_an_attempt_with_a_structured_connection_counts_as_tried(self) -> None:  # ds I
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {})
        structured = {"engine": "codex", "model": CODEX["model"], "connection": dict(CODEX["connection"])}
        out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": {"attempts": [structured]}})
        self.assertEqual((out["action"], out["reason"], out["skipped"][0]["skipped"]), ("ask", "fallback_exhausted", "already_tried"))


class ReviewFiveRegressionTests(unittest.TestCase):
    """The fifth read of #399 (30073846): DeepSeek V4.1 Flash A-G."""

    def setUp(self) -> None:
        self.cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        self.d = router.decide(self.cfg, SENTRY, {})

    def test_next_after_limit_reads_each_member_inside_the_guard(self) -> None:  # ds A
        class FailsOn(dict):
            def __init__(self, member: str, values) -> None:
                super().__init__(values)
                self.member = member

            def __getitem__(self, key):
                if key == self.member:
                    raise RuntimeError(key)
                return super().__getitem__(key)
        rule = router.decide(self.cfg, SENTRY, {"task_kind": "review"})  # a decision from a rule, which reads its rule
        for member, decision in (("engine", self.d), ("model", self.d), ("reason", self.d), ("rule", rule)):
            self.assertEqual(router.next_after_limit(self.cfg, FailsOn(member, decision), LIMIT),
                             {"action": "none", "reason": "no_decision"}, member)

    def test_a_connection_that_iterates_but_cannot_be_indexed_is_no_decision(self) -> None:  # ds B
        class Unindexable(Mapping):
            def __getitem__(self, key):
                raise RuntimeError("connection getitem")

            def __iter__(self):
                return iter(("kind", "id"))

            def __len__(self):
                return 2
        self.assertEqual(router.next_after_limit(self.cfg, {**self.d, "connection": Unindexable()}, LIMIT),
                         {"action": "none", "reason": "no_decision"})

    def test_route_decision_refuses_members_dict_would_accept(self) -> None:  # ds C
        class Row:  # indexable like a candidate, but not a mapping
            def __getitem__(self, key):
                return "x"
        for member, value in (("connection", [("kind", "subscription"), ("id", "claude-main")]),
                              ("hand_pick", [("engine", "codex"), ("model", "m")]),
                              ("candidates", (c for c in self.d["candidates"])), ("candidates", [Row()])):
            self.assertIsNone(router.route_decision({**self.d, member: value}), member)

    def test_a_bare_connection_word_in_attempts_counts_as_tried(self) -> None:  # ds D
        word = route_limits.describe(CODEX)["connection"]
        for attempts in ([word], word):  # a list of words, or the one word on its own
            out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": attempts}})
            self.assertEqual((out["action"], out["reason"], out["skipped"][0]["skipped"]),
                             ("ask", "fallback_exhausted", "already_tried"), attempts)
        out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": ["subscription:other"]}})
        self.assertEqual((out["action"], out["route"]["engine"]), ("switch", "codex"))

    def test_a_wait_until_is_a_time_or_the_retry_time(self) -> None:  # ds E
        cfg = with_limits({"on_limit": "wait"})
        d = router.decide(cfg, SENTRY, {})

        def until(reset_at, retry_at):
            signal = {"scope": "connection", "model": None, "reset_at": reset_at, "source": "test.typed"}
            return router.next_after_limit(cfg, d, {"code": c.PLAN_LIMIT, "limit": signal, "retry_at": retry_at})["until"]
        self.assertEqual(until(["x"], 7000.0), 7000.0)
        self.assertEqual(until({"a": 1}, 7000.0), 7000.0)
        self.assertIsNone(until(True, "soon"))
        self.assertEqual(until(0.0, 7000.0), 0.0)
        self.assertEqual(until(5000, 7000.0), 5000)

    def test_ok_must_be_exactly_true_or_false(self) -> None:  # ds F
        for ok in (1, "yes", None):
            self.assertIsNone(router.route_decision({**self.d, "ok": ok}), ok)
            self.assertEqual(router.next_after_limit(self.cfg, {**self.d, "ok": ok}, LIMIT),
                             {"action": "none", "reason": "no_decision"}, ok)
        self.assertEqual(router.next_after_limit(self.cfg, {**self.d, "ok": False}, LIMIT), {"action": "none", "reason": "no_route"})

    def test_attempts_that_are_not_a_list_are_no_attempts(self) -> None:  # ds G
        for notice in ({"attempts": 5}, {"attempts": {"a": 1}}, {"attempts": [5, None]}, ["x"]):
            out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": notice})
            self.assertEqual((out["action"], out["route"]["engine"]), ("switch", "codex"), notice)


class _EvilEq:
    """A value that raises when compared."""
    __hash__ = object.__hash__

    def __eq__(self, other):
        raise RuntimeError("eq")


class _EvilFormat:
    """A value that raises when formatted."""

    def __format__(self, spec):
        raise RuntimeError("format")


class _EvilEqStr(str):
    """A str whose comparing raises (its hash is str's)."""
    __hash__ = str.__hash__

    def __eq__(self, other):
        raise RuntimeError("eq")

    def __ne__(self, other):
        raise RuntimeError("ne")


class _EvilFormatStr(str):
    """A str whose formatting raises."""

    def __format__(self, spec):
        raise RuntimeError("format")


class _AlwaysEqStr(str):
    """A str equal to anything."""
    __hash__ = str.__hash__

    def __eq__(self, other):
        return True


class _EvilFloatInt(int):
    """An int whose conversion to a float raises."""

    def __float__(self):
        raise RuntimeError("float")


class ReviewSixRegressionTests(unittest.TestCase):
    """The sixth read of #399 (7bbd59be): DeepSeek V4.1 Flash F1-F6."""

    def setUp(self) -> None:
        self.cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        self.d = router.decide(self.cfg, SENTRY, {})
        self.word = route_limits.describe(CODEX)["connection"]

    def test_values_that_cannot_be_compared_or_formatted_are_no_decision(self) -> None:  # ds F1
        rule = router.decide(self.cfg, SENTRY, {"task_kind": "review"})
        no_decision = {"action": "none", "reason": "no_decision"}
        for decision in ({**self.d, "reason": _EvilEq()}, {**rule, "rule": _EvilEq()}, {**self.d, "reason": ["config"]},
                         {**self.d, "rule": ["review-to-codex"]},
                         {**self.d, "connection": {"kind": _EvilFormat(), "id": "claude-main"}},
                         {**self.d, "connection": {"kind": "subscription", "id": _EvilFormat()}}):
            self.assertEqual(router.next_after_limit(self.cfg, decision, LIMIT), no_decision, decision)
        self.assertIsNone(router.route_decision({**self.d, "reason": _EvilEq()}))

    def test_an_attempt_whose_parts_are_not_words_is_not_an_attempt(self) -> None:  # ds F1
        for entry in ({"engine": _EvilEq(), "model": "y", "connection": "z"},
                      {"engine": _EvilEq(), "model": CODEX["model"], "connection": self.word},
                      {"engine": "codex", "model": CODEX["model"], "connection": 5}):
            out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": [entry]}})
            self.assertEqual((out["action"], out["route"]["engine"]), ("switch", "codex"), entry)

    def test_an_unreadable_attempts_entry_is_dropped_not_the_limit(self) -> None:  # ds F2
        class GetRaises(dict):
            def get(self, key, default=None):
                raise RuntimeError("get")
        unreadable = GetRaises(engine="codex", model=CODEX["model"], connection=self.word)
        out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": [unreadable]}})
        self.assertEqual((out["action"], out["route"]["engine"]), ("switch", "codex"))
        out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": [unreadable, self.word]}})
        self.assertEqual((out["action"], out["reason"]), ("ask", "fallback_exhausted"))

    def test_a_wait_until_is_finite(self) -> None:  # ds F3
        cfg = with_limits({"on_limit": "wait"})
        d = router.decide(cfg, SENTRY, {})

        def until(reset_at, retry_at):
            signal = {"scope": "connection", "model": None, "reset_at": reset_at, "source": "test.typed"}
            return router.next_after_limit(cfg, d, {"code": c.PLAN_LIMIT, "limit": signal, "retry_at": retry_at})["until"]
        # NaN and the infinities; an int too large for a float (valid JSON) and an int whose conversion raises,
        # which a finiteness check alone would raise on.
        for odd in (float("nan"), float("inf"), float("-inf"), 10**400, -10**400, _EvilFloatInt(5000)):
            self.assertEqual(until(odd, 7000.0), 7000.0, type(odd))
            self.assertIsNone(until(odd, float("nan")), type(odd))
            self.assertIsNone(until(None, odd), type(odd))
        self.assertEqual(until(5000, None), 5000)

    def test_route_decision_needs_the_connections_kind_and_id(self) -> None:  # ds F5
        for connection in ({}, {"kind": "subscription"}, {"id": "claude-main"}):
            self.assertIsNone(router.route_decision({**self.d, "connection": connection}), connection)

    def test_an_attempt_naming_only_its_connection_counts_as_tried(self) -> None:  # ds F6
        out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": [{"connection": self.word}]}})
        self.assertEqual((out["action"], out["reason"], out["skipped"][0]["skipped"]), ("ask", "fallback_exhausted", "already_tried"))

    # F1's class one step further: a member that IS a str (a subclass) passes an isinstance check and can still
    # raise, or answer anything, when compared or formatted outside the guard.
    def test_a_decision_word_that_is_a_str_subclass_is_no_decision(self) -> None:  # ds F1
        rule = router.decide(self.cfg, SENTRY, {"task_kind": "review"})
        conn = self.d["connection"]
        for decision in ({**self.d, "reason": _AlwaysEqStr("config")}, {**rule, "rule": _EvilEqStr(rule["rule"])},
                         {**self.d, "connection": {**conn, "kind": _EvilFormatStr(conn["kind"])}},
                         {**self.d, "connection": {**conn, "id": _EvilFormatStr(conn["id"])}},
                         {**self.d, "engine": _EvilEqStr(self.d["engine"])}, {**self.d, "model": _AlwaysEqStr("x")}):
            self.assertEqual(router.next_after_limit(self.cfg, decision, LIMIT), {"action": "none", "reason": "no_decision"},
                             decision)

    def test_an_attempt_word_that_is_a_str_subclass_is_read_as_its_text(self) -> None:  # ds F1
        m, conn = CODEX["model"], CODEX["connection"]
        for entry in ({"engine": _EvilEqStr("codex"), "model": m, "connection": self.word},
                      {"engine": "codex", "model": _EvilEqStr(m), "connection": self.word},
                      {"engine": "codex", "model": m, "connection": _EvilEqStr(self.word)},
                      {"engine": "codex", "model": m, "connection": {"kind": _EvilFormatStr(conn["kind"]), "id": conn["id"]}},
                      _EvilEqStr(self.word)):
            out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": [entry]}})
            self.assertEqual((out["action"], out["reason"], out["skipped"][0]["skipped"]),
                             ("ask", "fallback_exhausted", "already_tried"), entry)

    def test_a_limit_code_that_is_a_str_subclass_is_not_a_limit(self) -> None:  # ds F1
        for code in (_EvilEqStr(c.PLAN_LIMIT), _AlwaysEqStr("x")):
            self.assertEqual(router.next_after_limit(self.cfg, self.d, {**LIMIT, "code": code}),
                             {"action": "none", "reason": "not_a_limit"}, type(code))

    def test_route_decision_needs_plain_words(self) -> None:  # ds F5: what next_after_limit refuses is not published
        conn = self.d["connection"]
        for odd in ({"connection": {**conn, "kind": 5}}, {"connection": {**conn, "id": None}},
                    {"connection": {**conn, "kind": _EvilFormatStr(conn["kind"])}}, {"reason": _AlwaysEqStr("x")},
                    {"engine": _EvilEqStr(self.d["engine"])}, {"model": _EvilEqStr(self.d["model"])}):
            decision = {**self.d, **odd}
            self.assertIsNone(router.route_decision(decision), odd)
            self.assertEqual(router.next_after_limit(self.cfg, decision, LIMIT), {"action": "none", "reason": "no_decision"},
                             odd)


if __name__ == "__main__":
    unittest.main()
