"""Detection: an engine is routable only with a launchable route, a connection that is not fenced and, for a
key route or recursion_loop, a key that is present and opted in.

The sources are plain functions here; the daemon's breaker is the route contract's in-memory
``ConnectionBreaker`` (the same class the daemon's durable breaker wraps), and the daemon's own pre-launch
refusals are the real ``route_unsupported`` and ``route_unbindable`` where the daemon imports.
"""

from __future__ import annotations

import copy
import unittest

from recursion_core.routes import contract as c
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routing import config as rc
from recursion_core.routing import detect, router
from recursion_core.routing.tests.fixtures import CONFIG, KEYS, ROUTES, config

READY = {"anthropic-api": "ready", "xai-api": "ready", "openai-api": "ready"}


def keys(states):
    return lambda key_id: states.get(key_id, "absent")


def loop_config(kind="api_key"):
    """CONFIG plus recursion_loop on an OpenAI key (or, for kind="subscription", on the Codex sign-in)."""
    raw = copy.deepcopy(CONFIG)
    routes = copy.deepcopy(ROUTES)
    if kind == "api_key":
        raw["logins"].append({"id": "openai-key", "vendor": "openai", "kind": "api_key", "connection_id": "openai-api",
                              "engines": ["recursion_loop"]})
        conn = {"kind": "api_key", "id": "openai-api", "credential_env": "OPENAI_API_KEY"}
    else:
        raw["logins"][1]["engines"] = ["codex", "recursion_loop"]
        conn = {"kind": "subscription", "id": "chatgpt-main"}
    routes["recursion_loop"] = {"route": {"engine": "recursion_loop", "model": "openai/model-e", "connection": conn},
                                "deadline_s": 600.0, "binary": None}
    raw["rules"].append({"id": "loop-for-tools", "when": {"tags_all": ["tools"]}, "route": {"engine": "recursion_loop"}})
    return config(raw, routes)


class DetectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = config()

    def reasons(self, snap):
        return {eng: entry["reason"] for eng, entry in snap["engines"].items()}

    def test_every_engine_is_answered_and_the_snapshot_is_one_decide_accepts(self) -> None:
        snap = detect.detect(self.cfg, key_state=keys(READY))
        self.assertEqual(set(snap["engines"]), set(rc.ENGINES))
        self.assertEqual(router.snapshot(snap)[1], None)
        self.assertEqual(self.reasons(snap), {"claude_code": None, "codex": None, "opencode": None, "pi": None,
                                              "hermes": None, "recursion_loop": "route_not_configured"})
        for entry in snap["engines"].values():
            self.assertEqual(entry["routable"], entry["reason"] is None)

    def test_a_key_route_needs_its_key_present_and_opted_in(self) -> None:
        self.assertEqual(self.reasons(detect.detect(self.cfg))["opencode"], "key_state_unknown")  # no key source: never
        states = {"anthropic-api": "absent", "xai-api": "not_opted_in"}
        r = self.reasons(detect.detect(self.cfg, key_state=keys(states)))
        self.assertEqual((r["opencode"], r["pi"]), ("key_absent", "key_not_opted_in"))
        self.assertEqual(self.reasons(detect.detect(self.cfg, key_state=lambda k: "maybe"))["opencode"], "key_state_unknown")
        # A sign-in route asks no key source at all.
        asked = []
        detect.detect(self.cfg, key_state=lambda k: asked.append(k) or "ready")
        self.assertEqual(sorted(asked), ["anthropic-api", "xai-api"])

    def test_an_open_or_unreadable_breaker_fences_its_connection(self) -> None:
        seen = []

        def breaker(key):
            seen.append(key)
            return {"subscription:chatgpt-main": "open", "subscription:grok-sign-in": "unknown",
                    "subscription:claude-main": "half_open"}.get(key, "closed")
        r = self.reasons(detect.detect(self.cfg, breaker_state=breaker, key_state=keys(READY)))
        self.assertEqual((r["codex"], r["hermes"], r["claude_code"], r["opencode"]), ("breaker_open", "breaker_unknown", None, None))
        self.assertIn("api_key:anthropic-api", seen)  # the connection key: kind and id, never the engine
        self.assertEqual(self.reasons(detect.detect(self.cfg, breaker_state=lambda k: "tripped", key_state=keys(READY)))["codex"],
                         "breaker_unknown")

    def test_a_route_the_daemon_would_refuse_is_not_routable(self) -> None:
        refusals = {"codex": "breaker_model_unrepresentable", "pi": "no credential custody: a passthrough route is not launched"}
        r = self.reasons(detect.detect(self.cfg, route_refusal=lambda e: refusals.get(e["route"]["engine"]), key_state=keys(READY)))
        self.assertEqual((r["codex"], r["pi"], r["claude_code"]), ("breaker_model_unrepresentable", "route_refused", None))

    def test_an_engine_the_engine_table_cannot_launch_is_not_routable(self) -> None:
        r = self.reasons(detect.detect(self.cfg, active=("claude_code", "codex"), key_state=keys(READY)))
        self.assertEqual({e for e, why in r.items() if why is None}, {"claude_code", "codex"})
        self.assertEqual((r["opencode"], r["recursion_loop"]), ("engine_not_active", "engine_not_active"))

    def test_a_failing_source_makes_its_engine_unroutable_and_no_other(self) -> None:
        def breaker(key):
            if key == "subscription:chatgpt-main":
                raise RuntimeError("store gone")
            return "closed"
        r = self.reasons(detect.detect(self.cfg, breaker_state=breaker, key_state=keys(READY)))
        self.assertEqual(r["codex"], "detection_source_failed")
        self.assertIsNone(r["claude_code"])

    def test_recursion_loop_is_routable_only_on_a_present_opted_in_key(self) -> None:
        cfg = loop_config()
        self.assertEqual(cfg.engine_login["recursion_loop"], "openai-key")
        self.assertIsNone(self.reasons(detect.detect(cfg, key_state=keys(READY)))["recursion_loop"])
        self.assertEqual(self.reasons(detect.detect(cfg, key_state=keys({**READY, "openai-api": "not_opted_in"})))["recursion_loop"],
                         "key_not_opted_in")
        sub = loop_config("subscription")
        self.assertEqual(self.reasons(detect.detect(sub, key_state=keys(READY)))["recursion_loop"], "recursion_loop_needs_key")
        # And the decision follows: a task tagged "tools" goes to the own loop only when it is routable.
        task = {"task_kind": "chat", "tags": ["tools"]}
        self.assertEqual(router.decide(cfg, detect.detect(cfg, key_state=keys(READY)), task)["engine"], "recursion_loop")
        d = router.decide(cfg, detect.detect(cfg, key_state=keys({**READY, "openai-api": "absent"})), task)
        self.assertEqual((d["engine"], d["candidates"][0]["skipped"]), ("claude_code", "key_absent"))

    def test_decide_on_a_detected_snapshot_skips_a_fenced_rule(self) -> None:
        snap = detect.detect(self.cfg, breaker_state=lambda k: "open" if k == "subscription:chatgpt-main" else "closed",
                             key_state=keys(READY))
        d = router.decide(self.cfg, snap, {"task_kind": "review"})
        self.assertEqual((d["reason"], d["engine"], d["candidates"][0]["skipped"]), ("default", "claude_code", "breaker_open"))
        d = router.decide(self.cfg, snap, {"hand_pick": {"engine": "codex", "model": None}})
        self.assertEqual((d["ok"], d["code"], d["reason"]), (False, "hand_pick_engine_unavailable", "breaker_open"))


class SourceAdapterTests(unittest.TestCase):
    def test_breaker_view_reads_the_route_contracts_breaker(self) -> None:
        now = [1000.0]
        breaker = ConnectionBreaker(lambda: now[0], cooldown_s=300)
        view = detect.breaker_view(breaker)
        self.assertEqual(view("subscription:chatgpt-main"), "closed")
        breaker.record("subscription:chatgpt-main", "codex-model-a", "t1",
                       {"code": c.PLAN_LIMIT, "limit": c.limit_signal("connection", "test.typed")})
        self.assertEqual(view("subscription:chatgpt-main"), "open")
        self.assertEqual(view("subscription:claude-main"), "closed")
        now[0] += 301
        self.assertEqual(view("subscription:chatgpt-main"), "half_open")
        cfg = config()
        now[0] = 1000.0
        breaker.record("subscription:chatgpt-main", "codex-model-a", "t1",
                       {"code": c.PLAN_LIMIT, "limit": c.limit_signal("connection", "test.typed")})
        r = detect.detect(cfg, breaker_state=view, key_state=keys(READY))["engines"]
        self.assertEqual(r["codex"], {"routable": False, "reason": "breaker_open"})

    def test_breaker_view_reads_a_stored_trip_as_route_list_does(self) -> None:
        class Durable:
            def __init__(self, state, tripped):
                self._state, self._tripped = state, tripped

            def state(self, key):
                return self._state

            def tripped(self, key):
                return self._tripped
        self.assertEqual(detect.breaker_view(Durable("closed", True))("k"), "open")
        self.assertEqual(detect.breaker_view(Durable("closed", None))("k"), "unknown")
        self.assertEqual(detect.breaker_view(Durable("half_open", False))("k"), "half_open")
        self.assertEqual(detect.breaker_view(Durable("weird", False))("k"), "unknown")

    def test_key_state_from_reads_presence_and_the_stores_own_opt_in(self) -> None:
        meta = {"anthropic-api": {"provider": "anthropic", "spend": {"opt_in": True}},
                "xai-api": {"provider": "xai", "spend": {"opt_in": False}}, "odd": {"provider": "openai"}}
        state = detect.key_state_from(meta, opted_in=lambda m: m.get("spend", {}).get("opt_in"))
        self.assertEqual([state(k) for k in ("anthropic-api", "xai-api", "odd", "missing")],
                         ["ready", "not_opted_in", "not_opted_in", "absent"])
        self.assertEqual(detect.key_state_from(KEYS, opted_in=lambda m: "yes")("anthropic-api"), "not_opted_in")

    def test_daemon_route_refusal_uses_the_daemons_own_functions(self) -> None:
        try:
            refusal = detect.daemon_route_refusal()
        except ImportError as exc:  # pragma: no cover - the daemon imports on Linux only
            self.skipTest(f"daemon not importable here: {exc}")
        self.assertIsNone(refusal(ROUTES["claude_code"]))
        passthrough = copy.deepcopy(ROUTES["claude_code"])
        passthrough["route"]["connection"] = {"kind": "passthrough", "id": "bedrock", "method": "bedrock"}
        self.assertEqual(refusal(passthrough), c.LAUNCH_REFUSED)
        helper = copy.deepcopy(ROUTES["claude_code"])
        helper["route"]["connection"] = {"kind": "api_key", "id": "k", "key_source": "api_key_helper"}
        self.assertEqual(refusal(helper), c.LAUNCH_REFUSED)

        class Unkeyable:
            def representable(self, connection):
                return False

            def model_representable(self, model):
                return True
        self.assertEqual(detect.daemon_route_refusal(Unkeyable())(ROUTES["codex"]), "breaker_connection_unrepresentable")


if __name__ == "__main__":
    unittest.main()
