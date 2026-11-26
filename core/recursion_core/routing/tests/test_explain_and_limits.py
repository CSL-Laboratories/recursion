"""explain() says why a decision came out as it did; next_after_limit() never moves a task to a route the user
did not list."""

from __future__ import annotations

import copy
import inspect
import itertools
import json
import re
import sys
import unittest
from collections.abc import Mapping

from recursion_core import routing
from recursion_core.routes import contract as c
from recursion_core.routes import limits as route_limits
from recursion_core.routing import config as rc
from recursion_core.routing import detect, router
from recursion_core.routing.tests.fixtures import CONFIG, ROUTES, SENTRY, config, detection

CODE_WORD = re.compile(r"[a-z][a-z0-9_]{0,63}")
# The one value that leaves a source out on purpose; a source passed as None is read narrow.
NOT_CONSULTED = detect.NOT_CONSULTED

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
# A snapshot that calls every engine routable (each key opted in): what a test passes, said in the call, when it is
# about something other than detection. next_after_limit has no default snapshot.
ALL_ROUTABLE = detection(set(rc.ENGINES))


class NextAfterLimitTests(unittest.TestCase):
    def test_the_next_listed_route_in_the_users_order(self) -> None:
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX, OPENCODE]})
        d = router.decide(cfg, SENTRY, {"task_kind": "chat"})
        ready = detect.detect(cfg, route_refusal=NOT_CONSULTED, breaker_state=NOT_CONSULTED, key_state=lambda key_id: "ready", active=NOT_CONSULTED)
        out = router.next_after_limit(cfg, d, LIMIT, ready, route_refusal=NOT_CONSULTED)
        self.assertEqual((out["action"], out["place"], out["route"], out["source"]), ("switch", 1, CODEX, "routing"))
        # The route service already tried the first fallback in this request: the next one, a key route, because the
        # snapshot has seen its key opted in.
        notice = {"attempts": [route_limits.describe(ROUTES["claude_code"]["route"]), route_limits.describe(CODEX)]}
        out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": notice}, ready, route_refusal=NOT_CONSULTED)
        self.assertEqual((out["action"], out["place"], out["route"]["engine"]), ("switch", 2, "opencode"))
        self.assertEqual(out["skipped"], [{"place": 1, **route_limits.describe(CODEX), "skipped": "already_tried"}])
        # The same key route whose key is not opted in, or whose key state no source gave, is passed over: ask.
        for keys, why in ((lambda key_id: "not_opted_in", "key_not_opted_in"), (None, "key_state_unknown")):
            unseen = detect.detect(cfg, route_refusal=NOT_CONSULTED, breaker_state=NOT_CONSULTED, key_state=keys, active=NOT_CONSULTED)
            out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": notice}, unseen, route_refusal=NOT_CONSULTED)
            self.assertEqual((out["action"], out["reason"], out["skipped"][-1]["skipped"]), ("ask", "fallback_exhausted", why))
        notice["attempts"].append(route_limits.describe(OPENCODE))
        out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": notice}, ready, route_refusal=NOT_CONSULTED)
        self.assertEqual((out["action"], out["reason"]), ("ask", "fallback_exhausted"))
        self.assertEqual([ch["engine"] for ch in out["choices"]], ["codex", "opencode"])

    def test_a_listed_route_whose_engine_is_not_routable_is_passed_over(self) -> None:
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX, OPENCODE]})
        d = router.decide(cfg, SENTRY, {"task_kind": "chat"})
        det = detection({"claude_code", "opencode"}, {"codex": "signed_out"})
        out = router.next_after_limit(cfg, d, LIMIT, det, route_refusal=NOT_CONSULTED)
        self.assertEqual((out["action"], out["place"]), ("switch", 2))
        self.assertEqual(out["skipped"][0]["skipped"], "signed_out")
        out = router.next_after_limit(cfg, d, LIMIT, detection({"claude_code"}), route_refusal=NOT_CONSULTED)
        self.assertEqual((out["action"], out["reason"]), ("ask", "fallback_exhausted"))

    def test_only_a_limit_leads_anywhere(self) -> None:
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {"task_kind": "chat"})
        for code in (c.ENGINE_ERROR, c.AUTH_FAILED, c.SHORT_FAULT, c.ENGINE_EXITED, c.DEADLINE_EXCEEDED, c.PROTOCOL_ERROR, None, "x"):
            self.assertEqual(router.next_after_limit(cfg, d, {"code": code}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), {"action": "none", "reason": "not_a_limit"}, code)
        for code in (c.PLAN_LIMIT, c.BREAKER_OPEN, c.PROBE_IN_FLIGHT):
            self.assertEqual(router.next_after_limit(cfg, d, {"code": code}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)["action"], "switch", code)
        refused = router.decide(cfg, SENTRY, {"hand_pick": {"engine": "pi", "model": None}})
        self.assertEqual(router.next_after_limit(cfg, refused, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), {"action": "none", "reason": "no_route"})

    def test_ask_and_wait_are_the_users_settings(self) -> None:
        cfg = with_limits({"on_limit": "ask", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {"task_kind": "chat"})
        self.assertEqual(router.next_after_limit(cfg, d, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED),
                         {"action": "ask", "reason": "on_limit_ask", "choices": [route_limits.describe(CODEX)]})
        cfg = with_limits({"on_limit": "wait"})
        d = router.decide(cfg, SENTRY, {"task_kind": "chat"})
        self.assertEqual(router.next_after_limit(cfg, d, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), {"action": "wait", "until": 5000.0, "choices": []})
        self.assertEqual(router.next_after_limit(cfg, d, {"code": c.BREAKER_OPEN, "retry_at": 7.0}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)["until"], 7.0)
        # Nothing configured anywhere: the user's default, show and ask.
        d = router.decide(config(), SENTRY, {"task_kind": "chat"})
        self.assertEqual(router.next_after_limit(config(), d, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), {"action": "ask", "reason": "on_limit_ask", "choices": []})

    def test_a_hand_pick_is_never_switched_away_from(self) -> None:
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {"hand_pick": {"engine": "claude_code", "model": None}})
        self.assertEqual(router.next_after_limit(cfg, d, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)["action"], "ask")
        self.assertEqual(router.next_after_limit(cfg, d, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)["reason"], "hand_pick")

    def test_routes_json_limits_apply_when_the_routing_file_gives_none(self) -> None:
        routes = copy.deepcopy(ROUTES)
        routes["codex"]["route"]["limits"] = {"on_limit": "fallback", "fallback": [copy.deepcopy(ROUTES["claude_code"]["route"])]}
        cfg = config(routes=routes)
        d = router.decide(cfg, SENTRY, {"task_kind": "review"})
        out = router.next_after_limit(cfg, d, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)
        self.assertEqual((out["action"], out["route"]["engine"], out["source"]), ("switch", "claude_code", "routes"))
        # The routing file's own limits for a route win over routes.json's.
        raw = copy.deepcopy(CONFIG)
        raw["rules"][0]["route"]["limits"] = {"on_limit": "ask"}
        d = router.decide(config(raw, routes), SENTRY, {"task_kind": "review"})
        self.assertEqual(router.next_after_limit(config(raw, routes), d, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)["action"], "ask")

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
                        out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": notice}, det, route_refusal=NOT_CONSULTED)
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


class AnswerEdgeCaseTests(unittest.TestCase):
    """Edge cases of the answers: a wait on the breaker's retry time, a primary never offered again, a route with no
    model anywhere, refusal details that are code words, a task read once, and a snapshot that cannot be walked."""

    def test_wait_uses_breaker_retry_when_vendor_reset_is_null(self) -> None:
        cfg = with_limits({"on_limit": "wait"})
        decision = router.decide(cfg, SENTRY, {})
        result = c.result("review", c.PLAN_LIMIT, limit=c.limit_signal("connection", "review.typed"), retry_at=7000.0)
        self.assertEqual(router.next_after_limit(cfg, decision, result, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)["until"], 7000.0)
        zero = c.result("review", c.PLAN_LIMIT, limit=c.limit_signal("connection", "review.typed", reset_at=0.0),
                        retry_at=7000.0)
        self.assertEqual(router.next_after_limit(cfg, decision, zero, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)["until"], 0.0)  # an explicit zero is kept

    def test_primary_not_reoffered_without_notice(self) -> None:
        primary = copy.deepcopy(ROUTES["claude_code"]["route"])
        cfg = with_limits({"on_limit": "fallback", "fallback": [primary, CODEX]})
        decision = router.decide(cfg, SENTRY, {})
        answer = router.next_after_limit(cfg, decision, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)
        self.assertEqual(answer["route"]["engine"], "codex")
        self.assertEqual(answer["skipped"][0]["skipped"], "already_tried")

    def test_same_engine_and_connection_other_model_still_allowed(self) -> None:
        other = copy.deepcopy(ROUTES["claude_code"]["route"])
        other["model"] = "claude-model-b"
        cfg = with_limits({"on_limit": "fallback", "fallback": [other, CODEX]})
        decision = router.decide(cfg, SENTRY, {})
        self.assertEqual(router.next_after_limit(cfg, decision, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)["route"], other)

    def test_a_route_with_no_model_anywhere_is_not_routable(self) -> None:
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

    def test_every_refusal_detail_is_code_words(self) -> None:
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

    def test_explain_reads_the_task_once(self) -> None:
        task = _ReadCounting()
        out = router.explain(config(), SENTRY, task)
        self.assertEqual(task.reads, 1)
        self.assertEqual((out["decision"]["rule"], out["decision"]["engine"]), ("review-to-codex", "codex"))
        chosen = [r["id"] for r in out["rules"] if r["outcome"] == "chosen"]
        self.assertEqual((chosen, out["default"]["outcome"]), (["review-to-codex"], "not_reached"))

    def test_a_bad_hand_pick_is_explained_as_not_reached(self) -> None:
        out = router.explain(config(), SENTRY, {"hand_pick": {"engine": "codex", "model": None, "x": 1}})
        rows = {(r["outcome"], r["reason"]) for r in out["rules"]} | {(out["default"]["outcome"], out["default"]["reason"])}
        self.assertEqual(rows, {("not_reached", "bad_hand_pick")})

    def test_next_after_limit_takes_decides_answer_only(self) -> None:
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {})
        for wrong in (router.route_decision(d), None, {}, "decision"):
            self.assertEqual(router.next_after_limit(cfg, wrong, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), {"action": "none", "reason": "no_decision"}, wrong)
        self.assertEqual(router.next_after_limit(cfg, d, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)["action"], "switch")

    def test_a_snapshot_that_cannot_be_walked_is_malformed(self) -> None:
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

    def test_small_interface_gaps(self) -> None:
        self.assertEqual(routing.EXPLAIN_PARAMS, ("session_id", "engine", "hand_pick", "task_kind", "tags"))
        self.assertIn("EXPLAIN_PARAMS", routing.__all__)
        for active in ("codex", b"codex"):
            with self.assertRaises(TypeError):
                detect.detect(config(), active=active, route_refusal=NOT_CONSULTED, breaker_state=NOT_CONSULTED, key_state=None)

    def test_a_rule_without_model_is_skipped_or_refused(self) -> None:
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


class NullAndUnreadableInputTests(unittest.TestCase):
    """A null hand pick, a null attempts list, an invalid limits block, an exhausted fallback list and anything that
    cannot be read is refused or answered with a typed word, never raised."""

    def test_a_null_hand_pick_is_refused_alike_everywhere(self) -> None:
        cfg = config()
        task = {"task_kind": "review", "hand_pick": None}
        d = router.decide(cfg, SENTRY, task)
        self.assertEqual((d["ok"], d["code"], d["reason"]), (False, "bad_hand_pick", "hand_pick"))
        self.assertEqual(router.explain(cfg, SENTRY, task)["decision"], d)
        self.assertEqual(router.explain_answer(cfg, SENTRY, task), {"code": "request_invalid", "detail": "hand_pick"})

    def test_a_null_attempts_list_and_a_decision_in_name_only(self) -> None:
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {})
        out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": {"attempts": None}}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)
        self.assertEqual((out["action"], out["reason"]), ("ask", "attempts_unreadable"))  # a notice holds a list
        for partial in ({"ok": True}, {"ok": True, "reason": "config", "engine": "codex"},
                        {**d, "connection": None}, {**d, "connection": {"kind": "subscription"}},
                        {k: v for k, v in d.items() if k != "rule"}):
            self.assertEqual(router.next_after_limit(cfg, partial, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), {"action": "none", "reason": "no_decision"}, partial)
        # Each member alone, and a connection that is not a mapping or has no kind.
        for missing in ("reason", "rule", "engine", "model", "connection"):
            partial = {k: v for k, v in d.items() if k != missing}
            self.assertEqual(router.next_after_limit(cfg, partial, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), {"action": "none", "reason": "no_decision"}, missing)
        for connection in ({"id": "claude-main"}, ["kind", "id"], _LikeAMapping(("kind", "id"), "x")):
            self.assertEqual(router.next_after_limit(cfg, {**d, "connection": connection}, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED),
                             {"action": "none", "reason": "no_decision"}, connection)
        refused = router.decide(cfg, SENTRY, {"hand_pick": {"engine": "pi", "model": None}})
        self.assertEqual(router.next_after_limit(cfg, refused, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), {"action": "none", "reason": "no_route"})

    def test_an_invalid_routes_json_limits_block_falls_back_to_ask(self) -> None:
        for bad in ({"on_limit": "sometimes"}, {"on_limit": "ask", "x": 1}, []):
            with self.subTest(bad):
                routes = copy.deepcopy(ROUTES)
                routes["claude_code"]["route"]["limits"] = bad
                cfg = config(routes=routes)
                d = router.decide(cfg, SENTRY, {})
                self.assertEqual(router.next_after_limit(cfg, d, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED),
                                 {"action": "ask", "reason": "on_limit_ask", "choices": []})

    def test_an_exhausted_list_names_each_listed_route_once_in_order(self) -> None:
        pi = copy.deepcopy(ROUTES["pi"]["route"])
        order = [CODEX, OPENCODE, pi]
        cfg = with_limits({"on_limit": "fallback", "fallback": order})
        d = router.decide(cfg, SENTRY, {})
        notice = {"attempts": [route_limits.describe(r) for r in order]}
        out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": notice}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)
        self.assertEqual((out["action"], out["reason"]), ("ask", "fallback_exhausted"))
        self.assertEqual(out["skipped"], [{"place": n, **route_limits.describe(r), "skipped": "already_tried"}
                                          for n, r in enumerate(order, 1)])

    def test_a_task_that_cannot_be_read_is_refused(self) -> None:
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

    def test_route_decision_of_anything_but_a_routed_decision_is_none(self) -> None:
        for value in (None, 5, "x", {}, {"ok": False}, {"ok": True}):
            self.assertIsNone(router.route_decision(value), value)
        d = router.decide(config(), SENTRY, {})
        for missing in ("reason", "engine", "model", "connection", "candidates", "hand_pick"):
            self.assertIsNone(router.route_decision({k: v for k, v in d.items() if k != missing}), missing)
        self.assertEqual(router.route_decision(d)["chosen"]["engine"], "claude_code")

    def test_a_hand_pick_that_cannot_be_read_is_refused(self) -> None:
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


class UnreadableDetectionParamsAndDecisionTests(unittest.TestCase):
    """A detection object, params, a decision or a limit result that cannot be read gives a typed refusal or no
    decision, never an exception."""

    def test_a_detection_object_that_cannot_be_read_refuses_every_task(self) -> None:
        for broken in ("contains", "getitem"):
            self.assertEqual(router.snapshot(_Unreadable(broken)), (None, "detection_malformed"), broken)
            d = router.decide(config(), _Unreadable(broken), {})
            self.assertEqual((d["ok"], d["code"], d["reason"]), (False, "default_engine_unavailable", "detection_malformed"))

    def test_explain_answer_refuses_params_it_cannot_read(self) -> None:
        for broken in ("contains", "iter", "getitem"):
            self.assertEqual(router.explain_answer(config(), SENTRY, _Unreadable(broken)),
                             {"code": "request_invalid", "detail": "params"}, broken)

        class HostileHand(dict):
            def __iter__(self):
                raise RuntimeError("iter")
        self.assertEqual(router.explain_answer(config(), SENTRY, {"hand_pick": HostileHand(engine="codex", model=None)}),
                         {"code": "request_invalid", "detail": "hand_pick"})

    def test_a_decision_with_an_engine_decide_never_gives_is_no_decision(self) -> None:
        cfg = config()  # the default route has no limits of its own, so routes.json's are looked up by engine
        d = router.decide(cfg, SENTRY, {})
        for odd in ({**d, "engine": ["codex"]}, {**d, "engine": None}, {**d, "model": ["m"]}):
            self.assertEqual(router.next_after_limit(cfg, odd, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), {"action": "none", "reason": "no_decision"}, odd)
        self.assertEqual(router.next_after_limit(cfg, d, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)["action"], "ask")

    def test_a_limit_result_that_cannot_be_read_is_not_a_limit(self) -> None:
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {})
        self.assertEqual(router.next_after_limit(cfg, d, _Unreadable("all"), ALL_ROUTABLE, route_refusal=NOT_CONSULTED), {"action": "none", "reason": "not_a_limit"})

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
            self.assertEqual(router.next_after_limit(wait, dw, FailsOn(member), ALL_ROUTABLE, route_refusal=NOT_CONSULTED), not_a_limit, member)
        self.assertEqual(router.next_after_limit(cfg, d, FailsOn("limit_notice"), ALL_ROUTABLE, route_refusal=NOT_CONSULTED), not_a_limit)  # the switch path's
        # The nested reads too: the vendor's signal and the notice. An attempt in it that cannot be read makes the
        # history unreadable, like any other non-attempt: the limit still leads to ask, never to a switch.
        self.assertEqual(router.next_after_limit(wait, dw, {"code": c.PLAN_LIMIT, "limit": Broken()}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), not_a_limit)
        self.assertEqual(router.next_after_limit(cfg, d, {"code": c.PLAN_LIMIT, "limit_notice": Broken()}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), not_a_limit)
        self.assertEqual(router.next_after_limit(cfg, d, {"code": c.PLAN_LIMIT, "limit_notice": {"attempts": [Broken()]}},
                                                 ALL_ROUTABLE, route_refusal=NOT_CONSULTED)["action"], "ask")


class WrongTypedDecisionAndMissingSnapshotTests(unittest.TestCase):
    """A decision that cannot be read or has a wrong-typed member is no decision; a missing snapshot is
    detection_unavailable; an attempt with a structured connection counts as tried."""

    def test_route_decision_of_an_unreadable_or_wrong_typed_decision_is_none(self) -> None:
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

    def test_next_after_limit_answers_no_decision_for_a_decision_it_cannot_read(self) -> None:
        class UnreadableDecision(dict):
            def __getitem__(self, key):
                raise RuntimeError("getitem")

        class UnwalkableConnection(dict):
            def __iter__(self):
                raise RuntimeError("connection iter")
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {})
        no_decision = {"action": "none", "reason": "no_decision"}
        self.assertEqual(router.next_after_limit(cfg, _Unreadable("contains"), LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), no_decision)
        self.assertEqual(router.next_after_limit(cfg, UnreadableDecision(d), LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), no_decision)
        connection = UnwalkableConnection(kind="subscription", id="claude-main")
        self.assertEqual(router.next_after_limit(cfg, {**d, "connection": connection}, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), no_decision)

    def test_a_missing_snapshot_is_detection_unavailable(self) -> None:
        for missing in (None, 5, "engines", ["engines"]):
            self.assertEqual(router.snapshot(missing), (None, "detection_unavailable"), missing)
        d = router.decide(config(), None, {})
        self.assertEqual((d["ok"], d["code"], d["reason"]), (False, "default_engine_unavailable", "detection_unavailable"))
        self.assertEqual(router.snapshot({"engines": []}), (None, "detection_malformed"))

    def test_an_attempt_with_a_structured_connection_counts_as_tried(self) -> None:
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {})
        structured = {"engine": "codex", "model": CODEX["model"], "connection": dict(CODEX["connection"])}
        out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": {"attempts": [structured]}}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)
        self.assertEqual((out["action"], out["reason"], out["skipped"][0]["skipped"]), ("ask", "fallback_exhausted", "already_tried"))


class DecisionMembersAndAttemptShapeTests(unittest.TestCase):
    """Each member of a decision is read inside the guard and has the type decide gives it; a bare connection word in
    attempts counts as tried; a wait time is a time or the retry time; ok is exactly True or False; attempts that are
    not a list name no attempt."""

    def setUp(self) -> None:
        self.cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        self.d = router.decide(self.cfg, SENTRY, {})

    def test_next_after_limit_reads_each_member_inside_the_guard(self) -> None:
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
            self.assertEqual(router.next_after_limit(self.cfg, FailsOn(member, decision), LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED),
                             {"action": "none", "reason": "no_decision"}, member)

    def test_a_connection_that_iterates_but_cannot_be_indexed_is_no_decision(self) -> None:
        class Unindexable(Mapping):
            def __getitem__(self, key):
                raise RuntimeError("connection getitem")

            def __iter__(self):
                return iter(("kind", "id"))

            def __len__(self):
                return 2
        self.assertEqual(router.next_after_limit(self.cfg, {**self.d, "connection": Unindexable()}, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED),
                         {"action": "none", "reason": "no_decision"})

    def test_route_decision_refuses_members_dict_would_accept(self) -> None:
        class Row:  # indexable like a candidate, but not a mapping
            def __getitem__(self, key):
                return "x"
        for member, value in (("connection", [("kind", "subscription"), ("id", "claude-main")]),
                              ("hand_pick", [("engine", "codex"), ("model", "m")]),
                              ("candidates", (c for c in self.d["candidates"])), ("candidates", [Row()])):
            self.assertIsNone(router.route_decision({**self.d, member: value}), member)

    def test_a_bare_connection_word_in_attempts_counts_as_tried(self) -> None:
        word = route_limits.describe(CODEX)["connection"]
        for attempts in ([word], word):  # a list of words, or the one word on its own
            out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": attempts}}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)
            self.assertEqual((out["action"], out["reason"], out["skipped"][0]["skipped"]),
                             ("ask", "fallback_exhausted", "already_tried"), attempts)
        out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": ["subscription:other"]}}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)
        self.assertEqual((out["action"], out["route"]["engine"]), ("switch", "codex"))

    def test_a_wait_until_is_a_time_or_the_retry_time(self) -> None:
        cfg = with_limits({"on_limit": "wait"})
        d = router.decide(cfg, SENTRY, {})

        def until(reset_at, retry_at):
            signal = {"scope": "connection", "model": None, "reset_at": reset_at, "source": "test.typed"}
            return router.next_after_limit(cfg, d, {"code": c.PLAN_LIMIT, "limit": signal, "retry_at": retry_at}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)["until"]
        self.assertEqual(until(["x"], 7000.0), 7000.0)
        self.assertEqual(until({"a": 1}, 7000.0), 7000.0)
        self.assertIsNone(until(True, "soon"))
        self.assertEqual(until(0.0, 7000.0), 0.0)
        self.assertEqual(until(5000, 7000.0), 5000)

    def test_ok_must_be_exactly_true_or_false(self) -> None:
        for ok in (1, "yes", None):
            self.assertIsNone(router.route_decision({**self.d, "ok": ok}), ok)
            self.assertEqual(router.next_after_limit(self.cfg, {**self.d, "ok": ok}, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED),
                             {"action": "none", "reason": "no_decision"}, ok)
        self.assertEqual(router.next_after_limit(self.cfg, {**self.d, "ok": False}, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), {"action": "none", "reason": "no_route"})

    def test_attempts_that_are_not_a_list_are_no_attempts(self) -> None:
        # Not a list of attempts: the history cannot be read, so ask, never a switch.
        for notice in ({"attempts": 5}, {"attempts": {"a": 1}}, {"attempts": [5, None]}, ["x"]):
            out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": notice}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)
            self.assertEqual((out["action"], out["reason"]), ("ask", "attempts_unreadable"), notice)


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


class UncomparableValuesAndFiniteWaitTests(unittest.TestCase):
    """Values that cannot be compared or formatted, and words that are str subclasses, give no decision and no
    attempt; an unreadable attempts entry makes the history unreadable (ask), never the limit unread; a wait time
    is finite; a published connection has its kind and id."""

    def setUp(self) -> None:
        self.cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        self.d = router.decide(self.cfg, SENTRY, {})
        self.word = route_limits.describe(CODEX)["connection"]

    def test_values_that_cannot_be_compared_or_formatted_are_no_decision(self) -> None:
        rule = router.decide(self.cfg, SENTRY, {"task_kind": "review"})
        no_decision = {"action": "none", "reason": "no_decision"}
        for decision in ({**self.d, "reason": _EvilEq()}, {**rule, "rule": _EvilEq()}, {**self.d, "reason": ["config"]},
                         {**self.d, "rule": ["review-to-codex"]},
                         {**self.d, "connection": {"kind": _EvilFormat(), "id": "claude-main"}},
                         {**self.d, "connection": {"kind": "subscription", "id": _EvilFormat()}}):
            self.assertEqual(router.next_after_limit(self.cfg, decision, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), no_decision, decision)
        self.assertIsNone(router.route_decision({**self.d, "reason": _EvilEq()}))

    def test_an_attempt_whose_parts_are_not_words_is_not_an_attempt(self) -> None:
        for entry in ({"engine": _EvilEq(), "model": "y", "connection": "z"},
                      {"engine": _EvilEq(), "model": CODEX["model"], "connection": self.word},
                      {"engine": "codex", "model": CODEX["model"], "connection": 5}):
            out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": [entry]}}, ALL_ROUTABLE,
                                          route_refusal=NOT_CONSULTED)
            self.assertEqual((out["action"], out["reason"]), ("ask", "attempts_unreadable"), entry)

    def test_an_unreadable_attempts_entry_is_dropped_not_the_limit(self) -> None:
        class GetRaises(dict):
            def get(self, key, default=None):
                raise RuntimeError("get")
        unreadable = GetRaises(engine="codex", model=CODEX["model"], connection=self.word)
        out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": [unreadable]}}, ALL_ROUTABLE,
                                      route_refusal=NOT_CONSULTED)
        self.assertEqual((out["action"], out["reason"]), ("ask", "attempts_unreadable"))
        out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": [unreadable, self.word]}},
                                      ALL_ROUTABLE, route_refusal=NOT_CONSULTED)
        self.assertEqual((out["action"], out["reason"]), ("ask", "attempts_unreadable"))
        # A list or tuple that cannot be iterated makes the history unreadable like an entry, never the limit unread.
        for attempts in (_EvilIterList([self.word]), _EvilIterTuple((self.word,))):
            out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": attempts}}, ALL_ROUTABLE,
                                          route_refusal=NOT_CONSULTED)
            self.assertEqual((out["action"], out["reason"]), ("ask", "attempts_unreadable"), type(attempts))

    def test_a_wait_until_is_finite(self) -> None:
        cfg = with_limits({"on_limit": "wait"})
        d = router.decide(cfg, SENTRY, {})

        def until(reset_at, retry_at):
            signal = {"scope": "connection", "model": None, "reset_at": reset_at, "source": "test.typed"}
            return router.next_after_limit(cfg, d, {"code": c.PLAN_LIMIT, "limit": signal, "retry_at": retry_at}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)["until"]
        # NaN and the infinities; an int too large for a float (valid JSON) and an int whose conversion raises,
        # which a finiteness check alone would raise on.
        for odd in (float("nan"), float("inf"), float("-inf"), 10**400, -10**400, _EvilFloatInt(5000)):
            self.assertEqual(until(odd, 7000.0), 7000.0, type(odd))
            self.assertIsNone(until(odd, float("nan")), type(odd))
            self.assertIsNone(until(None, odd), type(odd))
        self.assertEqual(until(5000, None), 5000)
        self.assertEqual(until(-5.0, 7000.0), -5.0)  # a time already past is passed through as given

    def test_route_decision_needs_the_connections_kind_and_id(self) -> None:
        for connection in ({}, {"kind": "subscription"}, {"id": "claude-main"}):
            self.assertIsNone(router.route_decision({**self.d, "connection": connection}), connection)

    def test_an_attempt_naming_only_its_connection_counts_as_tried(self) -> None:
        out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": [{"connection": self.word}]}}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)
        self.assertEqual((out["action"], out["reason"], out["skipped"][0]["skipped"]), ("ask", "fallback_exhausted", "already_tried"))

    # One step further: a member that IS a str (a subclass) passes an isinstance check and can still
    # raise, or answer anything, when compared or formatted outside the guard.
    def test_a_decision_word_that_is_a_str_subclass_is_no_decision(self) -> None:
        rule = router.decide(self.cfg, SENTRY, {"task_kind": "review"})
        conn = self.d["connection"]
        for decision in ({**self.d, "reason": _AlwaysEqStr("config")}, {**rule, "rule": _EvilEqStr(rule["rule"])},
                         {**self.d, "connection": {**conn, "kind": _EvilFormatStr(conn["kind"])}},
                         {**self.d, "connection": {**conn, "id": _EvilFormatStr(conn["id"])}},
                         {**self.d, "engine": _EvilEqStr(self.d["engine"])}, {**self.d, "model": _AlwaysEqStr("x")}):
            self.assertEqual(router.next_after_limit(self.cfg, decision, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), {"action": "none", "reason": "no_decision"},
                             decision)

    def test_an_attempt_word_that_is_a_str_subclass_is_read_as_its_text(self) -> None:
        m, conn = CODEX["model"], CODEX["connection"]
        for entry in ({"engine": _EvilEqStr("codex"), "model": m, "connection": self.word},
                      {"engine": "codex", "model": _EvilEqStr(m), "connection": self.word},
                      {"engine": "codex", "model": m, "connection": _EvilEqStr(self.word)},
                      {"engine": "codex", "model": m, "connection": {"kind": _EvilFormatStr(conn["kind"]), "id": conn["id"]}},
                      _EvilEqStr(self.word)):
            out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": [entry]}}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)
            self.assertEqual((out["action"], out["reason"], out["skipped"][0]["skipped"]),
                             ("ask", "fallback_exhausted", "already_tried"), entry)

    def test_a_limit_code_that_is_a_str_subclass_is_not_a_limit(self) -> None:
        for code in (_EvilEqStr(c.PLAN_LIMIT), _AlwaysEqStr("x")):
            self.assertEqual(router.next_after_limit(self.cfg, self.d, {**LIMIT, "code": code}, ALL_ROUTABLE, route_refusal=NOT_CONSULTED),
                             {"action": "none", "reason": "not_a_limit"}, type(code))

    def test_route_decision_needs_plain_words(self) -> None:  # what next_after_limit refuses is not published
        conn = self.d["connection"]
        for odd in ({"connection": {**conn, "kind": 5}}, {"connection": {**conn, "id": None}},
                    {"connection": {**conn, "kind": _EvilFormatStr(conn["kind"])}}, {"reason": _AlwaysEqStr("x")},
                    {"engine": _EvilEqStr(self.d["engine"])}, {"model": _EvilEqStr(self.d["model"])}):
            decision = {**self.d, **odd}
            self.assertIsNone(router.route_decision(decision), odd)
            self.assertEqual(router.next_after_limit(self.cfg, decision, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), {"action": "none", "reason": "no_decision"},
                             odd)


class _EvilHashStr(str):
    """A str whose hashing raises."""

    def __hash__(self):
        raise RuntimeError("hash")


class _EvilIterList(list):
    """A list whose iterating raises."""

    def __iter__(self):
        raise RuntimeError("iter")


class _EvilIterTuple(tuple):
    """A tuple whose iterating raises."""

    def __iter__(self):
        raise RuntimeError("iter")


class _LikeAMapping:
    """Not a mapping: it iterates the names it is given and indexes every one of them to the same word."""

    def __init__(self, names, word):
        self.names, self.word = names, word

    def __iter__(self):
        return iter(self.names)

    def __getitem__(self, key):
        return self.word


class StrSubclassWordsAndJsonOnlyOutputTests(unittest.TestCase):
    """Words of the task and of the detection that are str subclasses are read as their text; tags that cannot be
    read are refused; route_decision publishes only what JSON takes; a mapping or a set of attempts is an
    unreadable history."""

    def setUp(self) -> None:
        self.cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        self.d = router.decide(self.cfg, SENTRY, {})
        self.word = route_limits.describe(CODEX)["connection"]

    def assert_plain(self, value) -> None:
        """Every str in ``value``, a key or a member at any depth, is a plain str."""
        if isinstance(value, Mapping):
            for key, member in value.items():
                self.assert_plain(key)
                self.assert_plain(member)
        elif isinstance(value, (list, tuple)):
            for member in value:
                self.assert_plain(member)
        elif isinstance(value, str):
            self.assertIs(type(value), str, repr(value))

    # The same fault one entry point further out: the task's own words.
    def test_a_task_word_that_is_a_str_subclass_is_read_as_its_text(self) -> None:
        cfg = config()
        for odd_type in (_EvilHashStr, _EvilEqStr, _AlwaysEqStr):
            for plain, odd in (({"task_kind": "review"}, {"task_kind": odd_type("review")}),
                               ({"tags": ["cheap"]}, {"tags": [odd_type("cheap")]}),
                               ({"task_kind": "search", "tags": ["x", "cheap"]},
                                {"task_kind": odd_type("search"), "tags": [odd_type("x"), odd_type("cheap")]}),
                               ({"hand_pick": {"engine": "codex", "model": None}},
                                {"hand_pick": {"engine": odd_type("codex"), "model": None}}),
                               ({"hand_pick": {"engine": "opencode", "model": "m-1"}},
                                {"hand_pick": {"engine": "opencode", "model": odd_type("m-1")}})):
                with self.subTest(odd_type=odd_type.__name__, task=plain):
                    decision = router.decide(cfg, SENTRY, odd)
                    self.assertTrue(decision["ok"])
                    self.assertEqual(decision, router.decide(cfg, SENTRY, plain))
                    self.assert_plain(decision)
                    self.assertEqual(router.explain(cfg, SENTRY, odd), router.explain(cfg, SENTRY, plain))
                    answer = router.explain_answer(cfg, SENTRY, odd)
                    self.assertEqual(answer, router.explain_answer(cfg, SENTRY, plain))
                    self.assert_plain(answer)

    def test_tags_that_cannot_be_read_are_refused(self) -> None:
        cfg, task = config(), {"tags": _EvilIterList(["cheap"])}
        decision = router.decide(cfg, SENTRY, task)
        self.assertEqual((decision["ok"], decision["code"], decision["reason"]), (False, "bad_task", "tags"))
        self.assertEqual(router.explain(cfg, SENTRY, task)["decision"], decision)
        self.assertEqual(router.explain_answer(cfg, SENTRY, task), {"code": "request_invalid", "detail": "bad_task:tags"})

    def test_explain_answer_never_compares_a_name_or_an_engine_it_has_not_checked(self) -> None:
        cfg, hand = config(), {"engine": "codex", "model": None}
        want = router.explain_answer(cfg, SENTRY, {"engine": "codex", "hand_pick": hand})
        self.assertEqual(want["code"], "ok")
        self.assertEqual(router.explain_answer(cfg, SENTRY, {"engine": _EvilEqStr("codex"), "hand_pick": hand}), want)
        self.assertEqual(router.explain_answer(cfg, SENTRY, {"engine": "codex",
                                                             "hand_pick": {"engine": _EvilEqStr("codex"), "model": None}}),
                         want)
        for engine in (_EvilEq(), 5, None):
            self.assertEqual(router.explain_answer(cfg, SENTRY, {"engine": engine, "hand_pick": hand}),
                             {"code": "request_invalid", "detail": "engine_and_hand_pick_differ"}, engine)
        self.assertEqual(router.explain_answer(cfg, SENTRY, {_EvilEq(): 1}), {"code": "request_invalid", "detail": "params"})
        answer = router.explain_answer(cfg, SENTRY, {_EvilEqStr("zz"): 1})
        self.assertEqual(answer, {"code": "request_invalid", "detail": "zz"})
        self.assert_plain(answer)

    def test_a_detection_word_that_is_a_str_subclass_is_read_as_its_text(self) -> None:
        cfg = config()
        others = {engine: entry for engine, entry in SENTRY["engines"].items() if engine != "pi"}
        self.assertEqual(SENTRY["engines"]["pi"], {"routable": False, "reason": "not_on_path"})
        for odd_type in (_EvilEqStr, _EvilFormatStr):
            odd = {"engines": {**others, odd_type("pi"): {"routable": False, "reason": odd_type("not_on_path")}}}
            with self.subTest(odd_type.__name__):
                self.assertEqual(router.snapshot(odd), router.snapshot(SENTRY))
                self.assert_plain(router.snapshot(odd))
                for task in ({"task_kind": "search"}, {}):
                    decision = router.decide(cfg, odd, task)
                    self.assertEqual(decision, router.decide(cfg, SENTRY, task))
                    self.assert_plain(decision)
                    json.dumps(router.route_decision(decision))
        signed_out = {"engines": {**SENTRY["engines"],
                                  "claude_code": {"routable": False, "reason": _EvilFormatStr("signed_out")}}}
        self.assertEqual(router.explain_answer(cfg, signed_out, {"task_kind": "chat"}),
                         {"code": "request_invalid", "detail": "default_engine_unavailable:signed_out"})

    # The record route_decision publishes is what the run record keeps and the event carries.
    def test_route_decision_publishes_only_what_json_takes(self) -> None:
        cfg = config()
        for task in ({}, {"task_kind": "search"}, {"hand_pick": {"engine": "opencode", "model": "m-1"}},
                     {"hand_pick": {"engine": "codex", "model": None}}):
            record = router.route_decision(router.decide(cfg, SENTRY, task))
            self.assertIsNotNone(record, task)
            self.assertEqual(json.loads(json.dumps(record)), record)
        # A candidate passed over for having no model is decide's own row, its model null: it is published.
        routes, raw = copy.deepcopy(ROUTES), copy.deepcopy(CONFIG)
        del routes["codex"]["route"]["model"]
        del raw["rules"][0]["route"]["model"]
        decision = router.decide(config(raw, routes=routes), SENTRY, {"task_kind": "review"})
        self.assertEqual((decision["candidates"][0]["model"], decision["candidates"][0]["skipped"]), (None, "route_no_model"))
        record = router.route_decision(decision)
        self.assertEqual(json.loads(json.dumps(record)), record)
        conn, row = self.d["connection"], self.d["candidates"][0]
        for label, odd in (("a connection with another member", {"connection": {**conn, "extra": "x"}}),
                           ("a connection key that is not a str", {"connection": {**conn, (1, 2): 3}}),
                           ("a connection that only iterates and indexes", {"connection": _LikeAMapping(("kind", "id"), "x")}),
                           ("a candidate skipped that is not a word", {"candidates": [{**row, "skipped": object()}]}),
                           ("a candidate model that is not a word", {"candidates": [{**row, "model": 5}]}),
                           ("a candidate engine that is a str subclass", {"candidates": [{**row, "engine": _EvilEqStr("x")}]}),
                           ("a candidate skipped that is a str subclass", {"candidates": [{**row, "skipped": _EvilFormatStr("x")}]}),
                           ("a hand pick with another member", {"hand_pick": {"engine": "codex", "model": "m", "x": object()}}),
                           ("a hand pick without its model", {"hand_pick": {"engine": "codex"}}),
                           ("a hand pick model that is not a word", {"hand_pick": {"engine": "codex", "model": object()}}),
                           ("a hand pick engine that is a str subclass", {"hand_pick": {"engine": _EvilEqStr("codex"), "model": None}}),
                           ("a hand pick that only iterates and indexes", {"hand_pick": _LikeAMapping(("engine", "model"), "codex")})):
            self.assertIsNone(router.route_decision({**self.d, **odd}), label)

    # What is not a list or tuple is an unreadable history, whatever it holds: ask, never a switch.
    def test_attempts_in_a_mapping_or_a_set_are_no_attempts(self) -> None:
        for attempts in ({self.word: 1}, {self.word}, frozenset({self.word})):
            out = router.next_after_limit(self.cfg, self.d, {**LIMIT, "limit_notice": {"attempts": attempts}}, ALL_ROUTABLE,
                                          route_refusal=NOT_CONSULTED)
            self.assertEqual((out["action"], out["reason"]), ("ask", "attempts_unreadable"), attempts)


class _NothingPresent(Mapping):
    """A task that says every member is absent, and raises if one is read all the same."""

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __contains__(self, key):
        return False

    def __getitem__(self, key):
        raise RuntimeError(f"getitem {key}")

    def get(self, key, default=None):
        raise RuntimeError(f"get {key}")


class _Engines(Mapping):
    """A detection's engines that answer their pairs as given, so two keys may spell one engine."""

    def __init__(self, pairs):
        self.pairs = list(pairs)

    def __iter__(self):
        return iter([key for key, _ in self.pairs])

    def __len__(self):
        return len(self.pairs)

    def __getitem__(self, key):
        raise KeyError(key)

    def items(self):
        return list(self.pairs)


class RaisingTaskAndDuplicateEngineKeyTests(unittest.TestCase):
    """A task whose reading raises is refused even for an absent member, and two snapshot keys that read as one
    engine make the snapshot unusable."""

    def test_a_task_whose_reading_raises_is_refused_even_for_an_absent_member(self) -> None:
        cfg = config()
        decision = router.decide(cfg, SENTRY, _NothingPresent())
        self.assertEqual((decision["ok"], decision["code"], decision["reason"]), (False, "bad_task", "task"))
        self.assertEqual(router.explain(cfg, SENTRY, _NothingPresent())["decision"], decision)
        self.assertTrue(router.decide(cfg, SENTRY, {})["ok"])  # the same task as a plain dict routes

    def test_two_keys_that_read_as_one_engine_make_the_snapshot_unusable(self) -> None:
        cfg = config()
        refused = {"routable": False, "reason": "signed_out"}
        routable = {"routable": True, "reason": None}
        claude = ("claude_code", {"routable": True, "reason": None})
        for pairs in ((("codex", refused), (_EvilHashStr("codex"), routable), claude),
                      ((_EvilHashStr("codex"), routable), ("codex", refused), claude),
                      (("codex", routable), ("codex", refused), claude)):
            detection = {"engines": _Engines(pairs)}
            with self.subTest(order=[(type(key).__name__, entry["routable"]) for key, entry in pairs[:2]]):
                self.assertEqual(router.snapshot(detection), (None, "detection_malformed"))
                decision = router.decide(cfg, detection, {"task_kind": "review"})
                self.assertEqual((decision["ok"], decision["reason"]), (False, "detection_malformed"))


class _GoesOnSecondAsking(dict):
    """A decision (or connection) whose one member goes the second time its presence is asked: a member deleted
    while the decision is read."""

    def __init__(self, values, key):
        super().__init__(values)
        self.key, self.asked = key, 0

    def __contains__(self, key):
        if key == self.key:
            self.asked += 1
            if self.asked > 1:
                self.pop(key, None)
                return False
        return super().__contains__(key)


class _UnformattableId(str):
    def __format__(self, spec):
        raise RuntimeError("connection id formatting")


class DisappearingMemberTests(unittest.TestCase):
    """A required member that goes while the decision is read makes it no decision, and a primary is not offered
    again when a member disappears."""

    def test_a_required_member_lost_while_reading_is_no_decision(self) -> None:
        cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        d = router.decide(cfg, SENTRY, {})
        for key in ("reason", "rule", "engine", "model"):
            with self.subTest(key=key):
                decision = _GoesOnSecondAsking(d, key)
                actual = router.next_after_limit(cfg, decision, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)
                self.assertEqual(decision.asked, 1)  # asked once: a member cannot go between two askings
                if not dict.__contains__(decision, key):
                    self.assertEqual(actual, {"action": "none", "reason": "no_decision"})
                else:
                    self.assertEqual(actual, router.next_after_limit(cfg, d, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED))

    def test_a_primary_cannot_be_reoffered_when_a_member_disappears(self) -> None:
        primary = copy.deepcopy(ROUTES["claude_code"]["route"])
        cfg = with_limits({"on_limit": "fallback", "fallback": [primary, CODEX]})
        d = router.decide(cfg, SENTRY, {})
        control = router.next_after_limit(cfg, d, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)
        self.assertEqual((control["action"], control["place"], control["route"]["engine"]), ("switch", 2, "codex"))
        for key in ("reason", "rule", "engine", "model"):
            with self.subTest(key=key):
                decision = _GoesOnSecondAsking(d, key)
                self.assertEqual(router.next_after_limit(cfg, decision, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), control)
                self.assertEqual(decision.asked, 1)  # each member's presence is asked for once
        for key in ("kind", "id"):  # the connection's members are walked once, and nothing asks for one again
            with self.subTest(connection=key):
                connection = _GoesOnSecondAsking(d["connection"], key)
                self.assertEqual(router.next_after_limit(cfg, {**d, "connection": connection}, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), control)


class StrSubclassConnectionIdAndUnknownReasonTests(unittest.TestCase):
    """route_decision refuses a connection id that is a str subclass; next_after_limit refuses a reason decide
    never gives."""

    def setUp(self) -> None:
        self.cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        self.decision = router.decide(self.cfg, SENTRY, {})

    def test_route_decision_refuses_a_connection_id_str_subclass(self) -> None:
        conn = self.decision["connection"]
        decision = {**self.decision, "connection": {**conn, "id": _UnformattableId(conn["id"])}}
        self.assertIsNone(router.route_decision(decision))

    def test_next_after_limit_refuses_unknown_reason(self) -> None:
        self.assertEqual(router.next_after_limit(self.cfg, {**self.decision, "reason": "learned"}, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED),
                         {"action": "none", "reason": "no_decision"})


class RulesAfterAChosenOrRefusedRuleTests(unittest.TestCase):
    """Rules after a chosen or refused rule are not reached, whatever their conditions."""

    def test_rules_after_a_chosen_rule_are_not_reached(self) -> None:
        out = router.explain(config(), SENTRY, {"task_kind": "review"})
        self.assertEqual(out["decision"]["engine"], "codex")
        self.assertEqual([row["outcome"] for row in out["rules"]], ["chosen", "not_reached", "not_reached"])

    def test_rules_after_a_refused_rule_are_not_reached_whatever_their_conditions(self) -> None:  # the same, refused
        raw = copy.deepcopy(CONFIG)
        raw["undetected_rule"] = "refuse"
        out = router.explain(config(raw), SENTRY, {"task_kind": "search"})  # no tags: cheap-to-opencode's would fail
        self.assertEqual([(row["id"], row["outcome"]) for row in out["rules"]],
                         [("review-to-codex", "no_match"), ("search-to-pi", "refused"), ("cheap-to-opencode", "not_reached")])


class _AskingDeletes(dict):
    """A decision (or a connection) that deletes one member the first time another one's presence is asked."""

    def __init__(self, values, asked, deleted):
        super().__init__(values)
        self.asked, self.deleted, self.done = asked, deleted, False

    def __contains__(self, key):
        if key == self.asked and not self.done:
            self.done = True
            self.pop(self.deleted, None)
        return super().__contains__(key)


class NonMappingConnectionAndSingleWalkTests(unittest.TestCase):
    """A connection that is not a mapping is never walked, and each member's value is read from one walk of the
    decision and of its connection."""

    def setUp(self) -> None:
        self.cfg = with_limits({"on_limit": "fallback", "fallback": [CODEX]})
        self.d = router.decide(self.cfg, SENTRY, {})
        self.control = router.next_after_limit(self.cfg, self.d, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)
        self.assertEqual(self.control["action"], "switch")

    def test_a_connection_that_is_not_a_mapping_is_never_walked(self) -> None:
        walked = []

        class Counted:
            def __iter__(self):
                for name in ("kind", "id"):
                    walked.append(name)
                    yield name

            def __getitem__(self, key):
                return "x"
        self.assertEqual(router.next_after_limit(self.cfg, {**self.d, "connection": Counted()}, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED),
                         {"action": "none", "reason": "no_decision"})
        self.assertEqual(walked, [])

    def test_each_members_value_is_read_where_its_presence_is_asked(self) -> None:
        decision = _AskingDeletes(self.d, asked="model", deleted="engine")  # engine is already read by then
        self.assertEqual(router.next_after_limit(self.cfg, decision, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), self.control)

    def test_the_connections_members_are_read_from_its_one_walk(self) -> None:
        connection = _AskingDeletes(self.d["connection"], asked="kind", deleted="id")  # nothing asks it again
        self.assertEqual(router.next_after_limit(self.cfg, {**self.d, "connection": connection}, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED), self.control)


class DaemonRefusedRouteIsPassedOverTests(unittest.TestCase):
    """A listed route the daemon refuses before launch is passed over for the next one."""

    def setUp(self) -> None:
        self.refused = {**copy.deepcopy(CODEX), "model": "codex-model-refused"}
        self.second = {**copy.deepcopy(CODEX), "model": "codex-model-b"}

    @staticmethod
    def refusal(entry):
        return "launch_refused" if entry["route"]["model"] == "codex-model-refused" else None

    def test_a_listed_route_the_daemon_refuses_is_passed_over_for_the_next(self) -> None:
        cfg = with_limits({"on_limit": "fallback", "fallback": [self.refused, self.second]})
        out = router.next_after_limit(cfg, router.decide(cfg, SENTRY, {}), LIMIT, SENTRY, route_refusal=self.refusal)
        self.assertEqual((out["action"], out["place"], out["route"]["model"]), ("switch", 2, "codex-model-b"))
        self.assertEqual([(row["place"], row["skipped"]) for row in out["skipped"]], [(1, "launch_refused")])
        cfg = with_limits({"on_limit": "fallback", "fallback": [self.refused]})  # none left: ask, never the refused one
        out = router.next_after_limit(cfg, router.decide(cfg, SENTRY, {}), LIMIT, SENTRY, route_refusal=self.refusal)
        self.assertEqual((out["action"], out["reason"]), ("ask", "fallback_exhausted"))

    def test_a_refusal_source_that_raises_passes_the_route_over(self) -> None:  # as detect has it
        def raising(entry):
            raise RuntimeError("made-up refusal source")
        cfg = with_limits({"on_limit": "fallback", "fallback": [self.second]})
        out = router.next_after_limit(cfg, router.decide(cfg, SENTRY, {}), LIMIT, SENTRY, route_refusal=raising)
        self.assertEqual((out["action"], out["skipped"][0]["skipped"]), ("ask", "detection_source_failed"))

    @unittest.skipUnless(sys.platform.startswith("linux"), "the daemon's own refusals are read on Linux only")
    def test_the_daemons_refusal_passes_a_helper_key_route_over(self) -> None:
        helper = {"engine": "claude_code", "model": "helper-model",
                  "connection": {"kind": "api_key", "id": "anthropic-helper", "key_source": "api_key_helper"}}
        raw = {"version": 1,
               "logins": [{"id": "openai-sub", "vendor": "openai", "kind": "subscription", "connection_id": "chatgpt-main",
                           "engines": ["codex"]},
                          {"id": "anthropic-helper", "vendor": "anthropic", "kind": "api_key",
                           "connection_id": "anthropic-helper", "engines": ["claude_code"]}],
               "default": {"engine": "codex", "limits": {"on_limit": "fallback", "fallback": [helper, self.second]}},
               "rules": []}
        routes = {"codex": copy.deepcopy(ROUTES["codex"]),
                  "claude_code": {"route": copy.deepcopy(helper), "deadline_s": 600, "binary": "claude"}}
        cfg = rc.load_config(raw, routes, {"anthropic-helper": {"provider": "anthropic"}})
        snap = detection({"claude_code", "codex"})
        decision = router.decide(cfg, snap, {})
        self.assertEqual(decision["engine"], "codex")
        daemon = detect.daemon_route_refusal(NOT_CONSULTED)
        out = router.next_after_limit(cfg, decision, LIMIT, snap, route_refusal=daemon)
        self.assertEqual((out["action"], out["place"], out["route"]["model"]), ("switch", 2, "codex-model-b"))
        self.assertEqual((out["skipped"][0]["place"], out["skipped"][0]["skipped"]), (1, "launch_refused"))
        self.assertIsNone(daemon({"route": out["route"]}))  # the route it switches to is one the daemon launches


# -- Fail closed: no default widens an answer, an unreadable history is never 'nothing tried', the walk ends ---------
# Made-up key states only: no key store is read.
NO_SOURCES = {"route_refusal": NOT_CONSULTED, "breaker_state": NOT_CONSULTED, "key_state": None,
              "active": NOT_CONSULTED}
CLAUDE = copy.deepcopy(ROUTES["claude_code"]["route"])
PI = copy.deepcopy(ROUTES["pi"]["route"])


def keys_answering(state):
    return lambda key_id: state


def detected(cfg, **given):
    """``detect.detect`` with every source the call does not give left out on purpose (``NOT_CONSULTED``) and no
    key source (None: a key route is never routable), said here in one place."""
    return detect.detect(cfg, **{**NO_SOURCES, **given})


def limited_to(fallback, routes=None, on_limit="fallback"):
    """The fixtures' config with the default route's limits set: on_limit and the fallback list, in order."""
    raw = copy.deepcopy(CONFIG)
    raw["default"] = {"engine": "claude_code", "model": None,
                      "limits": {"on_limit": on_limit, "fallback": copy.deepcopy(fallback)}}
    return config(raw, routes)


def width(answer):
    """How wide an answer of next_after_limit is: a switch (a run started on a route) is wider than ask or wait
    (nothing starts until the person chooses), and those are wider than none (a refusal)."""
    return {"switch": 3, "ask": 2, "wait": 2, "none": 1}[answer["action"]]


class _IterRaises(list):
    def __iter__(self):
        raise RuntimeError("iter")


class _GetRaisesEntry(dict):
    def get(self, *args):
        raise RuntimeError("get")


class FailClosedTests(unittest.TestCase):
    """A call that leaves a check out is refused with the check's name; a metered route whose key state the call has
    not seen opted in, or an engine routes.json does not configure, is never switched to."""

    def test_a_call_that_leaves_a_check_out_is_refused_with_its_name(self) -> None:
        cfg = limited_to([OPENCODE])
        d = router.decide(cfg, SENTRY, {})
        with self.assertRaisesRegex(TypeError, "detection"):
            router.next_after_limit(cfg, d, LIMIT, route_refusal=None)
        with self.assertRaisesRegex(TypeError, "route_refusal"):
            router.next_after_limit(cfg, d, LIMIT, ALL_ROUTABLE)
        for name in NO_SOURCES:
            with self.assertRaisesRegex(TypeError, name):
                detect.detect(cfg, **{k: v for k, v in NO_SOURCES.items() if k != name})
        with self.assertRaisesRegex(TypeError, "breaker"):
            detect.daemon_route_refusal()

    def test_a_metered_route_whose_key_is_not_opted_in_is_never_switched_to(self) -> None:
        cfg = limited_to([OPENCODE])  # opencode runs on an api_key connection: it spends
        d = router.decide(cfg, ALL_ROUTABLE, {"task_kind": "chat"})
        self.assertEqual((d["engine"], d["reason"]), ("claude_code", "default"))
        for det, why, words in ((None, "detection_unavailable", "run detection"), (5, "detection_unavailable", "run detection"),
                                ({"engines": []}, "detection_malformed", "run detection"),
                                (detected(cfg), "key_state_unknown", "opt the key in"),
                                (detected(cfg, key_state=keys_answering("not_opted_in")), "key_not_opted_in", "opt the key in"),
                                (detected(cfg, key_state=keys_answering("absent")), "key_absent", "add the key")):
            for refusal in (None, NOT_CONSULTED, lambda entry: None):
                out = router.next_after_limit(cfg, d, LIMIT, det, route_refusal=refusal)
                self.assertEqual((out["action"], out["reason"], [r["skipped"] for r in out["skipped"]]),
                                 ("ask", "fallback_exhausted", [why]), det)
                self.assertIn(words, router.next_steps(out)[0])
        out = router.next_after_limit(cfg, d, LIMIT, detected(cfg, key_state=keys_answering("ready")), route_refusal=NOT_CONSULTED)
        self.assertEqual((out["action"], out["route"]), ("switch", OPENCODE))

    def test_an_engine_routes_json_does_not_configure_is_never_switched_to(self) -> None:
        routes = copy.deepcopy(ROUTES)
        del routes["pi"]
        cfg = limited_to([PI], routes)
        d = router.decide(cfg, ALL_ROUTABLE, {"task_kind": "chat"})
        for det in (ALL_ROUTABLE, detected(cfg, key_state=keys_answering("ready"))):
            out = router.next_after_limit(cfg, d, LIMIT, det, route_refusal=NOT_CONSULTED)
            self.assertEqual((out["action"], [r["skipped"] for r in out["skipped"]]), ("ask", ["route_not_configured"]))
            self.assertIn("configure the engine", router.next_steps(out)[0])

    def test_no_call_names_a_route_the_user_did_not_list(self) -> None:
        # The reproduction's grid, its left-out arguments now passed as None or NOT_CONSULTED: 2628 calls.
        n = 0
        for k in range(4):
            for order in itertools.permutations([OPENCODE, PI, CODEX], k):
                for on_limit in ("fallback", "ask", "wait"):
                    if on_limit == "fallback" and not order:
                        continue
                    cfg = limited_to(list(order), on_limit=on_limit)
                    d = router.decide(cfg, ALL_ROUTABLE, {"task_kind": "chat"})
                    for det in (None, None, detected(cfg), detected(cfg, key_state=keys_answering("not_opted_in")),
                                detected(cfg, key_state=keys_answering("ready")), ALL_ROUTABLE):
                        engines = router.snapshot(det)[0] or {}
                        for refusal in (None, NOT_CONSULTED, lambda entry: None):
                            for tried in range(len(order) + 1):
                                notice = {"attempts": [route_limits.describe(r) for r in order[:tried]]}
                                out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": notice}, det,
                                                              route_refusal=refusal)
                                n += 1
                                if out["action"] == "switch":
                                    self.assertIn(out["route"], list(order))
                                    self.assertTrue(engines[out["route"]["engine"]]["routable"])  # the snapshot said so
        self.assertEqual(n, 2628)


class UnreadableHistoryTests(unittest.TestCase):
    """A history of routes tried that cannot be read is never read as nothing tried: ask, never a switch. A notice
    that is absent or null means the route service made none: only the decided route counts as tried."""

    def setUp(self) -> None:
        self.cfg = limited_to([CLAUDE, CODEX, OPENCODE])  # the decided route itself, then codex, then opencode
        self.d = router.decide(self.cfg, SENTRY, {"task_kind": "chat"})
        self.good = route_limits.describe(CODEX)
        self.word = self.good["connection"]

    def answer(self, notice, det=ALL_ROUTABLE, absent=False):
        limit = dict(LIMIT) if absent else {**LIMIT, "limit_notice": notice}
        return router.next_after_limit(self.cfg, self.d, limit, det, route_refusal=NOT_CONSULTED)

    def test_codex_switched_to_and_limited_is_never_offered_again_on_an_unreadable_history(self) -> None:
        def gen():
            yield self.good
        unreadable = [lambda: 5, lambda: {}, lambda: ["x"], lambda: {"attempts": None}, lambda: {"attempts": 5},
                      lambda: {"attempts": {self.word}}, lambda: {"attempts": frozenset({self.word})},
                      lambda: {"attempts": {self.word: self.good}}, lambda: {"attempts": [5, None]},
                      lambda: {"attempts": gen()}, lambda: {"attempts": _IterRaises([self.good])},
                      lambda: {"attempts": [("codex", CODEX["model"], self.word)]},
                      lambda: {"attempts": [_GetRaisesEntry(self.good)]}, lambda: {"attempts": [{**self.good, "engine": 5}]},
                      lambda: {"attempts": [{**self.good, "connection": 7}]}, lambda: {"attempts": [self.good, None]}]
        for make in unreadable:
            for det in (ALL_ROUTABLE, SENTRY):
                out = self.answer(make(), det)
                self.assertEqual((out["action"], out["reason"]), ("ask", "attempts_unreadable"), make())
                self.assertIn("could not be read", router.next_steps(out)[-1])
        for readable in ({"attempts": [self.good]}, {"attempts": (self.good,)}, {"attempts": self.word}, {"attempts": [self.word]}):
            out = self.answer(readable)
            self.assertEqual((out["action"], out["place"], out["route"]["engine"]), ("switch", 3, "opencode"), readable)
        # Absent or null: the route service made none. A readable empty list, and names of no listed route, name nothing.
        for notice, absent in ((None, True), (None, False), ({"attempts": []}, False),
                               ({"attempts": [{**self.good, "engine": "nosuch"}]}, False), ({"attempts": ["subscription:nosuch"]}, False),
                               ({"attempts": [{**self.good, "model": "zzz"}]}, False), ({"attempts": [self.word.upper()]}, False)):
            out = self.answer(notice, absent=absent)
            self.assertEqual((out["action"], out["place"], out["route"]["engine"]), ("switch", 2, "codex"), notice)

    def test_a_caller_that_feeds_the_history_back_never_gets_a_route_twice(self) -> None:
        steps = []
        for history in ([], {"set": True}):
            seen, out = [], None
            for _ in range(8):
                attempts = ([route_limits.describe(r) for r in seen] if isinstance(history, list)
                            else {route_limits.describe(r)["connection"] for r in seen} or {self.word})
                out = self.answer({"attempts": attempts})
                if out["action"] != "switch":
                    break
                self.assertNotIn(out["route"], seen)
                seen.append(out["route"])
            steps.append(([r["engine"] for r in seen], out["action"], out.get("reason")))
        self.assertEqual(steps, [(["codex", "opencode"], "ask", "fallback_exhausted"), ([], "ask", "attempts_unreadable")])


    def test_an_attempt_that_leaves_its_model_or_its_engine_out_counts_every_route_it_could_name(self) -> None:
        # An entry naming an engine and a connection but no model (absent or null), or a model and a connection but no
        # engine, reads the part it leaves out as any: every listed route it could name on that connection was tried.
        cfg = limited_to([CODEX])
        d = router.decide(cfg, SENTRY, {"task_kind": "chat"})
        for entry in ({"engine": "codex", "connection": self.word}, {"engine": "codex", "model": None, "connection": self.word},
                      {"model": CODEX["model"], "connection": self.word},
                      {"engine": None, "model": CODEX["model"], "connection": self.word}):
            out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": {"attempts": [entry]}}, ALL_ROUTABLE,
                                          route_refusal=NOT_CONSULTED)
            self.assertEqual((out["action"], out["reason"], [r["skipped"] for r in out["skipped"]]),
                             ("ask", "fallback_exhausted", ["already_tried"]), entry)
        # A part it names still has to match: another engine or model on that connection names no listed route.
        for entry in ({"engine": "pi", "connection": self.word}, {"model": "zzz", "connection": self.word}):
            out = router.next_after_limit(cfg, d, {**LIMIT, "limit_notice": {"attempts": [entry]}}, ALL_ROUTABLE,
                                          route_refusal=NOT_CONSULTED)
            self.assertEqual((out["action"], out["route"]), ("switch", CODEX), entry)


class TheWalkEndsTests(unittest.TestCase):
    """For every order, on_limit, snapshot, refusal and first history the grid holds, feeding each answer back as the
    history ends in ask, wait or none within the length of the list, never the same route twice."""

    def test_every_walk_ends_within_the_list_and_never_offers_a_route_twice(self) -> None:
        pool = [CODEX, OPENCODE, PI, {**CLAUDE, "model": "claude-model-b"}]
        firsts = ("absent", [], [route_limits.describe(CODEX)], {"attempts": {"x"}}, [route_limits.describe(CLAUDE)])
        walks = 0
        for k in range(4):
            for order in itertools.permutations(pool, k):
                for on_limit in ("fallback", "ask", "wait"):
                    if on_limit == "fallback" and not order:
                        continue
                    cfg = limited_to(list(order), on_limit=on_limit)
                    d = router.decide(cfg, ALL_ROUTABLE, {"task_kind": "chat"})
                    snaps = (None, ALL_ROUTABLE, detected(cfg), detected(cfg, key_state=keys_answering("ready")))
                    refusals = (None, NOT_CONSULTED,
                                lambda entry: "launch_refused" if entry["route"]["engine"] == "codex" else None)
                    for det, refusal, first in itertools.product(snaps, refusals, firsts):
                        seen, out = [], None
                        for _ in range(len(order) + 2):  # one more than a walk may take: a walk that does not end fails
                            if first == "absent" and not seen:
                                limit = dict(LIMIT)
                            elif first == "absent" or isinstance(first, list):
                                done = [] if first == "absent" else list(first)
                                limit = {**LIMIT, "limit_notice": {"attempts": done + [route_limits.describe(r) for r in seen]}}
                            else:
                                limit = {**LIMIT, "limit_notice": first}
                            out = router.next_after_limit(cfg, d, limit, det, route_refusal=refusal)
                            if out["action"] != "switch":
                                break
                            self.assertNotIn(out["route"], seen)
                            self.assertIn(out["route"], list(order))
                            seen.append(out["route"])
                        else:
                            self.fail(f"the walk did not end: {order} {on_limit} {seen}")
                        self.assertIn(out["action"], ("ask", "wait", "none"))
                        self.assertLessEqual(len(seen), len(order))
                        walks += 1
        self.assertGreater(walks, 1000)


class NoDefaultWidensAnAnswerTests(unittest.TestCase):
    """No public callable of the routing package has a defaulted argument that could turn a check off.

    Wider is decided by what an answer lets start: a switch is wider than ask or wait, and those are wider than none
    (a refusal); for a snapshot, more routable engines is wider. A defaulted argument is allowed only on the list
    below, each one a value that decides nothing; any other fails this test, so that an argument added later with a
    default that skips a check is caught here. Making it required (no default) is the way to pass."""

    DECIDES_NOTHING = {
        ("config.ConfigError", "detail"): "the error's words; nothing reads them to decide",
        ("config.Route", "model"): "None reads the model routes.json configures; a route with no model anywhere is "
                                   "refused route_no_model",
        ("config.Route", "limits"): "None reads the limits routes.json configures, else the user's default (ask)",
    }

    @staticmethod
    def defaulted():
        found = {}
        for module in (rc, router, detect):
            short = module.__name__.rsplit(".", 1)[-1]
            for name, obj in vars(module).items():
                if name.startswith("_") or getattr(obj, "__module__", None) != module.__name__:
                    continue
                if not (inspect.isfunction(obj) or inspect.isclass(obj)):
                    continue
                try:
                    signature = inspect.signature(obj)
                except (TypeError, ValueError):  # a class whose constructor is a builtin's: it takes no default of ours
                    continue
                for parameter in signature.parameters.values():
                    if parameter.default is not inspect.Parameter.empty:
                        found[(f"{short}.{name}", parameter.name)] = parameter.default
        return found

    def test_no_public_callable_has_a_default_that_could_turn_a_check_off(self) -> None:
        found = self.defaulted()
        self.assertEqual(sorted(set(found) - set(self.DECIDES_NOTHING)), [],
                         "a defaulted argument of a public routing callable: make it required, so that no call can leave "
                         "its check out by leaving it out")
        self.assertEqual(sorted(set(self.DECIDES_NOTHING) - set(found)), [])  # the list stays exact

    def test_the_callables_that_check_take_every_check_as_a_required_argument(self) -> None:
        for fn, names in ((router.next_after_limit, ("detection", "route_refusal")),
                          (detect.detect, ("route_refusal", "breaker_state", "key_state", "active")),
                          (detect.daemon_route_refusal, ("breaker",))):
            parameters = inspect.signature(fn).parameters
            for name in names:
                self.assertIs(parameters[name].default, inspect.Parameter.empty, (fn.__name__, name))

    def test_a_caller_with_no_source_gets_the_narrowest_answer(self) -> None:
        # The explicit call a caller without any source can make is never wider than any other explicit call.
        for order in itertools.permutations([OPENCODE, PI, CODEX], 2):
            cfg = limited_to(list(order))
            d = router.decide(cfg, ALL_ROUTABLE, {})
            nothing = router.next_after_limit(cfg, d, LIMIT, None, route_refusal=None)
            for det in (ALL_ROUTABLE, SENTRY, detected(cfg), detected(cfg, key_state=keys_answering("ready"))):
                for refusal in (None, NOT_CONSULTED, lambda entry: None, lambda entry: "launch_refused"):
                    self.assertLessEqual(width(nothing), width(router.next_after_limit(cfg, d, LIMIT, det, route_refusal=refusal)))
            self.assertEqual((nothing["action"], {r["skipped"] for r in nothing["skipped"]}), ("ask", {"detection_unavailable"}))


    def test_a_source_passed_as_none_is_never_wider_than_any_other_explicit_call(self) -> None:
        # Every source of detect: a None on one source, the others as they are, never leaves more engines routable
        # than any other value of that source; NOT_CONSULTED is the one road to the wide reading.
        cfg = limited_to([CODEX, OPENCODE])
        values = {"route_refusal": (None, NOT_CONSULTED, lambda entry: None, lambda entry: "launch_refused"),
                  "breaker_state": (None, NOT_CONSULTED, lambda key: "closed", lambda key: "open"),
                  "key_state": (None, NOT_CONSULTED, lambda key_id: "ready", lambda key_id: "absent"),
                  "active": (None, NOT_CONSULTED, tuple(rc.ENGINES), ())}
        names = tuple(values)

        def routable(snap):
            return {engine for engine, entry in snap["engines"].items() if entry["routable"]}
        snaps = {pick: routable(detect.detect(cfg, **{n: values[n][i] for n, i in zip(names, pick)}))
                 for pick in itertools.product(range(4), repeat=len(names))}
        for pick, got in snaps.items():
            for place, name in enumerate(names):
                if pick[place] == 0:
                    for other in range(1, 4):
                        self.assertLessEqual(got, snaps[pick[:place] + (other,) + pick[place + 1:]], (name, pick, other))
        ready = {**dict.fromkeys(names, NOT_CONSULTED), "key_state": lambda key_id: "ready"}
        self.assertTrue(routable(detect.detect(cfg, **ready)) >= {"codex", "opencode"})
        for name, why in (("route_refusal", "route_refused"), ("breaker_state", "breaker_unknown"), ("active", "engine_not_active")):
            snap = detect.detect(cfg, **{**ready, name: None})
            self.assertEqual((routable(snap), snap["engines"]["codex"]["reason"]), (set(), why), name)
        # next_after_limit: a refusal passed as None is never wider than any other, and passes every route over.
        d = router.decide(cfg, ALL_ROUTABLE, {})
        for det in (ALL_ROUTABLE, SENTRY, detected(cfg), detected(cfg, key_state=keys_answering("ready")), None):
            given_none = router.next_after_limit(cfg, d, LIMIT, det, route_refusal=None)
            self.assertNotEqual(given_none["action"], "switch", det)
            for refusal in (NOT_CONSULTED, lambda entry: None, lambda entry: "launch_refused"):
                self.assertLessEqual(width(given_none), width(router.next_after_limit(cfg, d, LIMIT, det, route_refusal=refusal)))
        out = router.next_after_limit(cfg, d, LIMIT, ALL_ROUTABLE, route_refusal=None)
        self.assertEqual((out["action"], out["reason"], [r["skipped"] for r in out["skipped"]]),
                         ("ask", "fallback_exhausted", ["source_not_given", "source_not_given"]))
        self.assertIn("NOT_CONSULTED", router.next_steps(out)[0])
        out = router.next_after_limit(cfg, d, LIMIT, ALL_ROUTABLE, route_refusal=NOT_CONSULTED)
        self.assertEqual((out["action"], out["route"]), ("switch", CODEX))
        # Both None at once: narrow on both.
        out = router.next_after_limit(cfg, d, LIMIT, None, route_refusal=None)
        self.assertEqual((out["action"], [r["skipped"] for r in out["skipped"]]),
                         ("ask", ["detection_unavailable", "detection_unavailable"]))

    def test_an_open_breaker_passed_as_none_is_not_switched_to(self) -> None:
        cfg = limited_to([CODEX, OPENCODE])
        d = router.decide(cfg, ALL_ROUTABLE, {"task_kind": "chat"})
        ready = keys_answering("ready")
        for breaker in (lambda key: "open" if key == "subscription:chatgpt-main" else "closed", None):
            snap = detected(cfg, key_state=ready, breaker_state=breaker)
            self.assertFalse(snap["engines"]["codex"]["routable"])
            out = router.next_after_limit(cfg, d, LIMIT, snap, route_refusal=NOT_CONSULTED)
            self.assertNotEqual((out["action"], (out.get("route") or {}).get("engine")), ("switch", "codex"))
        self.assertTrue(detected(cfg, key_state=ready, breaker_state=NOT_CONSULTED)["engines"]["codex"]["routable"])

    def test_not_consulted_is_one_exported_value(self) -> None:
        self.assertIn("NOT_CONSULTED", routing.__all__)
        self.assertIs(routing.NOT_CONSULTED, detect.NOT_CONSULTED)
        self.assertIs(router.NOT_CONSULTED, detect.NOT_CONSULTED)
        self.assertEqual(repr(detect.NOT_CONSULTED), "NOT_CONSULTED")


class NextStepsTests(unittest.TestCase):
    """Every ask, wait and none, and every listed route passed over, says what stopped the walk and what the person can
    do next (opt the key in, configure the engine, list another route, choose a route)."""

    def test_every_code_detection_or_the_walk_can_give_has_its_own_words(self) -> None:
        codes = set(re.findall(r'return "([a-z_]+)"', inspect.getsource(detect._engine_reason)))
        codes |= set(re.findall(r'return "([a-z_]+)"', inspect.getsource(router._state)))
        codes |= {"already_tried", "detection_source_failed", "detection_unavailable", "detection_malformed",
                  "route_no_model", "route_refused", "launch_refused", "source_not_given"}
        self.assertGreater(len(codes), 10)
        self.assertEqual(sorted(codes - set(router._SKIP_STEPS)), [])
        for code, words in router._SKIP_STEPS.items():
            self.assertTrue(": " in words and ("list another route" in words or "choose a route" in words), code)

    def test_every_answer_of_the_walk_names_what_the_person_can_do(self) -> None:
        reasons = set()
        for order in ([], [CODEX], [OPENCODE, CODEX]):
            for on_limit in ("fallback", "ask", "wait"):
                if on_limit == "fallback" and not order:
                    continue
                cfg = limited_to(order, on_limit=on_limit)
                for task in ({}, {"hand_pick": {"engine": "claude_code", "model": None}}):
                    d = router.decide(cfg, ALL_ROUTABLE, task)
                    for det in (None, ALL_ROUTABLE, detected(cfg)):
                        for limit in (LIMIT, {"code": "x"}, {**LIMIT, "limit_notice": {"attempts": 5}}):
                            for decision in (d, {"ok": False}, {"ok": 1}):
                                out = router.next_after_limit(cfg, decision, limit, det, route_refusal=NOT_CONSULTED)
                                steps = router.next_steps(out)
                                self.assertTrue(steps, out)
                                self.assertEqual(len(steps), len(out.get("skipped", [])) + 1)
                                if out["action"] in ("ask", "none"):
                                    reasons.add(out["reason"])
                                    self.assertEqual(steps[-1], router._ANSWER_STEPS[out["reason"]])
                                self.assertTrue(all(": " in s or "; " in s for s in steps), steps)
        self.assertTrue({"attempts_unreadable", "fallback_exhausted", "hand_pick", "on_limit_ask", "not_a_limit", "no_route",
                         "no_decision"} <= reasons, reasons)


    def test_the_wait_time_and_the_breaker_are_said_in_words_a_person_reads(self) -> None:
        steps = router.next_steps({"action": "wait", "until": 1764100000.0, "choices": []})
        self.assertIn("2025-11-25 19:46:40 UTC", steps[-1])
        self.assertNotIn("1764100000", steps[-1])
        self.assertIn("seconds after 1970-01-01 00:00 UTC", router.next_steps({"action": "wait", "until": 1e300, "choices": []})[-1])
        self.assertNotIn("reopen", router._SKIP_STEPS["breaker_open"])
        self.assertIn("refuses", router._SKIP_STEPS["breaker_open"])

    def test_a_refusal_that_is_not_a_code_word_is_skipped_as_detect_reads_it(self) -> None:
        cfg = limited_to([CODEX])
        d = router.decide(cfg, ALL_ROUTABLE, {})
        for given, code in (("", "route_refused"), ("Not A Code", "route_refused"), ("launch_refused", "launch_refused")):
            out = router.next_after_limit(cfg, d, LIMIT, ALL_ROUTABLE, route_refusal=lambda entry, g=given: g)
            self.assertEqual((out["action"], [r["skipped"] for r in out["skipped"]]), ("ask", [code]), given)
            snap = detected(cfg, key_state=keys_answering("ready"), route_refusal=lambda entry, g=given: g)
            self.assertEqual(snap["engines"]["codex"]["reason"], code, given)


if __name__ == "__main__":
    unittest.main()
