# Written by Claude Opus 5.5 (claude-opus-5-5), Claude Code builder lane ENGINE-E5, for Lucas.
"""Antigravity's ACP drive as a route adapter (``AntigravityAcpAdapter``): one turn of ``agy acp`` per run,
against the fake ACP agent. Nothing is registered; every key and path here is made up.

Run: PYTHONPATH=core <python> -m unittest \\
    recursion_core.routes.engines.fixtures.antigravity.test_antigravity_acp_adapter
"""

from __future__ import annotations

import json
import unittest

from recursion_core.routes import contract as c
from recursion_core.routes.engines import antigravity as ag
from recursion_core.routes.engines.fixtures.antigravity.fake_acp_agent import FakeAcpAgent

KEY = "r8-made-up-custody-value-41d"   # invented here; no real key, custody or auth file is read
CWD = "/var/tmp/workspace-for-this-run"
PARENT = {"PATH": "/usr/bin", "HOME": "/home/operator", "LANG": "C.UTF-8", "PWD": "/home/operator/somewhere",
          "GEMINI_API_KEY": "parent-made-up", "GOOGLE_API_KEY": "parent-made-up", "AGY_ADC_AUTH": "true"}


def request(method="gemini-api-key", kind="api_key", cred="GEMINI_API_KEY", cwd=CWD, prompt="say hi") -> dict:
    connection = {"kind": kind, "id": "conn-A"}
    if cred is not None:
        connection["credential_env"] = cred
    req = {"contract": c.CONTRACT_VERSION, "request_id": "r1", "tenant": "t1", "prompt": prompt, "deadline_s": 60.0,
           "route": {"engine": "antigravity", "model": "gemini-3.6-flash", "connection": connection,
                     "auth_method": method}}
    if cwd is not ...:
        req["cwd"] = cwd
    return req


def prepared(route, method_id) -> str:
    return f"/var/tmp/route-homes/antigravity-{method_id}"


class Rig:
    """An adapter over the fake agent: the launcher records each launch and hands back the agent."""

    def __init__(self, scripts=(), *, parent=None, key=KEY, boundary=True, home=prepared, methods=None):
        kwargs = {} if methods is None else {"methods": methods}
        self.agent = FakeAcpAgent(scripts, **kwargs)
        self.launches: list = []
        self.asked: list = []
        self.custody: list = []

        def launcher(argv, *, env, shell=False, **kw):
            self.launches.append({"argv": list(argv), "env": dict(env), "shell": shell})
            return self.agent

        def boundary_fn(values):
            self.asked.append(tuple(values))
            return launcher

        def credentials(conn_id, name):
            self.custody.append((conn_id, name))
            return key

        self.adapter = ag.AntigravityAcpAdapter(launcher, self.agent.clock, credentials,
                                                PARENT if parent is None else parent, prepared_home=home,
                                                output_boundary=boundary_fn if boundary else None)

    def run(self, req, deadline=None):
        return self.adapter.run(req, self.agent.clock() + 60 if deadline is None else deadline)

    def sent(self, method):
        return [m for m in self.agent.received if m.get("method") == method]


class CwdTest(unittest.TestCase):
    """The run's folder is the caller's: refused without one, and NO fallback of any kind (the Liaison's
    ruling, Nov 15)."""

    def test_a_run_without_an_absolute_cwd_is_refused_before_anything_starts(self) -> None:
        for cwd in (..., None, "", "relative/dir", "/var/tmp/a" + chr(0) + "b", b"/var/tmp/x", 5):
            with self.subTest(cwd=repr(cwd)):
                rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
                res = rig.run(request(cwd=cwd))
                self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, "cwd"))
                self.assertEqual((rig.launches, rig.custody, rig.asked, rig.agent.received), ([], [], [], []))

    def test_no_fallback_folder_is_ever_used(self) -> None:
        # the daemon's working directory (PWD), the route home and the user's home are all at hand: none is used
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
        res = rig.run(request(cwd=...))
        self.assertEqual(res["detail"], "cwd")
        self.assertEqual(rig.launches, [])
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
        self.assertIsNone(rig.run(request())["code"])
        self.assertEqual([m["params"]["cwd"] for m in rig.sent("session/new")], [CWD])
        for other in (PARENT["PWD"], PARENT["HOME"], prepared(None, "gemini-api-key")):
            self.assertNotIn(other, json.dumps(rig.agent.received))


class EnvironmentTest(unittest.TestCase):
    """The STRICT child request is the one-shot adapter's, shared and unchanged."""

    def test_the_child_environment_is_exactly_the_one_shot_adapters(self) -> None:
        for method, kind, cred in (("gemini-api-key", "api_key", "GEMINI_API_KEY"),
                                   ("agent-platform", "api_key", "GOOGLE_API_KEY"),
                                   ("agent-platform", "passthrough", None), ("oauth-business", "subscription", None)):
            with self.subTest(method=method, kind=kind):
                rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
                req = request(method, kind, cred)
                res = rig.run(req)
                self.assertIsNone(res["code"])
                expected = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "HOME": prepared(None, method),
                            "AGY_CLI_DISABLE_AUTO_UPDATE": "true"}
                if cred:
                    expected[cred] = KEY
                self.assertEqual(rig.launches[0]["env"], expected)
                self.assertEqual(rig.launches[0]["argv"], ["agy", "acp"])
                self.assertIs(rig.launches[0]["shell"], False)
                one_shot = ag.AntigravityOneShotAdapter(None, rig.agent.clock, lambda *a: KEY, PARENT,
                                                        prepared_home=prepared, output_boundary=lambda v: None)
                self.assertEqual(rig.launches[0]["env"], one_shot.env(req))

    def test_the_strict_refusals_hold_before_launch(self) -> None:
        cases = {
            "the personal account": (dict(), request("oauth-personal", "subscription", None),
                                     ag.PERSONAL_ACCOUNT_OFF),
            "a key route without a boundary": (dict(boundary=False), request(), ag.OUTPUT_UNPROTECTED),
            "the daemon's own HOME": (dict(home=lambda r, m: "/home/operator/.config"), request(),
                                      ag.ROUTE_HOME_IS_DAEMON_HOME),
            "a parent with no HOME": (dict(parent={"PATH": "/usr/bin"}), request(), ag.DAEMON_HOME_UNKNOWN),
            "no prepared home": (dict(home=None), request(), ag.NOT_PREPARED),
        }
        for name, (kwargs, req, detail) in cases.items():
            with self.subTest(case=name):
                rig = Rig([[("chunk", "hi"), ("end", "end_turn")]], **kwargs)
                res = rig.run(req)
                self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, detail))
                self.assertEqual(rig.launches, [])

    def test_a_binary_option_is_argv0(self) -> None:
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
        rig.adapter._binary = "/opt/agy/bin/agy"
        rig.run(request())
        self.assertEqual(rig.launches[0]["argv"], ["/opt/agy/bin/agy", "acp"])


class TurnTest(unittest.TestCase):
    def test_a_turn_answered_end_turn_is_its_text_and_the_child_is_not_fenced(self) -> None:
        rig = Rig([[("chunk", "hel"), ("chunk", "lo"), ("end", "end_turn")]])
        res = rig.run(request())
        self.assertEqual((res["code"], res["text"]), (None, "hello"))
        self.assertEqual(rig.agent.fenced, [])
        self.assertTrue(rig.agent.stdin_closed)                     # the agent's input ends: it finishes
        self.assertEqual([m["params"]["methodId"] for m in rig.sent("authenticate")], ["gemini-api-key"])
        self.assertEqual(rig.asked, [(KEY,)])

    def test_a_key_split_across_chunks_never_reaches_the_result(self) -> None:
        half = len(KEY) // 2
        rig = Rig([[("chunk", "the key "), ("chunk", KEY[:half]), ("chunk", KEY[half:]), ("end", "end_turn")]])
        res = rig.run(request())
        self.assertIsNone(res["code"])
        self.assertNotIn(KEY, json.dumps(res))
        self.assertTrue(res["text"].startswith("the key "))

    def test_every_turn_that_does_not_succeed_is_fenced_with_its_code(self) -> None:
        cases = {
            "the agent stops the turn (cancelled)": ([[("end", "cancelled")]], c.ENGINE_ERROR, None),
            "the agent goes away": ([[("chunk", "x"), ("die",)]], c.ENGINE_EXITED, None),
            "the deadline": ([[("wait_cancel",), ("end", "cancelled")]], c.DEADLINE_EXCEEDED, 10.0),
            "a line that is not JSON-RPC": ([[("raw", "{not json"), ("wait_cancel",), ("end", "cancelled")]],
                                            c.PROTOCOL_ERROR, None),
            "a refusal answer naming the key": ([[("error", -32603, "refused " + KEY)]], c.ENGINE_ERROR, None),
        }
        for name, (scripts, code, deadline_in) in cases.items():
            with self.subTest(case=name):
                rig = Rig(scripts)
                deadline = None if deadline_in is None else rig.agent.clock() + deadline_in
                res = rig.run(request(), deadline)
                self.assertEqual(res["code"], code)
                self.assertEqual(len(rig.agent.fenced), 1)
                self.assertNotIn(KEY, json.dumps(res))

    def test_a_method_the_agent_does_not_offer_is_refused_and_fenced(self) -> None:
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]], methods=("oauth-business",))
        res = rig.run(request())
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertEqual(len(rig.agent.fenced), 1)
        self.assertEqual(rig.sent("authenticate"), [])

    def test_whatever_escapes_once_the_child_runs_fences_it_first(self) -> None:
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
        calls = []
        real = rig.agent.clock

        def clock():
            calls.append(1)
            if len(calls) > 1:
                raise KeyboardInterrupt("synthetic")
            return real()

        rig.adapter._clock = clock
        with self.assertRaises(KeyboardInterrupt):
            rig.run(request(), real() + 60)
        self.assertEqual(rig.agent.fenced, ["unexpected"])


class FactoryTest(unittest.TestCase):
    def test_the_factory_takes_only_binary_and_its_adapter_refuses_every_launch(self) -> None:
        for option in ("prepared_home", "output_boundary", "cwd", "env"):
            with self.subTest(option=option), self.assertRaises(TypeError):
                ag.acp_factory(None, None, None, PARENT, **{option: None})
        launches = []
        made = ag.acp_factory(lambda *a, **k: launches.append(a), lambda: 0.0, lambda *a: KEY, PARENT)
        res = made.run(request("oauth-business", "subscription", None), 60.0)
        self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, ag.NOT_PREPARED))
        self.assertEqual(launches, [])

    def test_nothing_registers_the_acp_adapter(self) -> None:
        source = ag.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn(".register(", text)



class LyingStr(str):
    """A str whose checks lie: it claims to be absolute and to hold no NUL whatever it holds."""

    def startswith(self, *args, **kwargs):
        return True

    def __contains__(self, item):
        return False


class TwoFaced(dict):
    """A Mapping that answers .get one way and [] (and so items()) another."""

    def __init__(self, data, lie):
        super().__init__(data)
        self._lie = lie

    def get(self, key, default=None):
        return self._lie.get(key, super().get(key, default))


class ReviewRoundOne411Test(unittest.TestCase):
    """DeepSeek's round-1 read of #411 at 78ed4424 (F1 STRICT, F2, F3, F5). Each fails on that head."""

    def test_f1_a_cwd_that_is_not_exactly_a_str_is_refused(self) -> None:
        for cwd in (LyingStr("relative/dir"), LyingStr("/var/tmp/a" + chr(0) + "b"), LyingStr(CWD)):
            with self.subTest(cwd=str.__repr__(cwd)):
                rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
                res = rig.run(request(cwd=cwd))
                self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, "cwd"))
                self.assertEqual((rig.launches, rig.custody, rig.agent.received), ([], [], []))

    def test_f2_every_str_field_is_read_as_exactly_a_str(self) -> None:
        for field in ("request_id", "tenant", "prompt", "contract"):
            with self.subTest(field=field):
                rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
                req = request()
                req[field] = LyingStr(req[field])
                res = rig.run(req)
                self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, field))
                self.assertEqual(rig.launches, [])
        for path in (("route", "model"), ("route", "auth_method"), ("route", "connection", "id")):
            with self.subTest(field=".".join(path)):
                rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
                req = request()
                holder = req
                for key in path[:-1]:
                    holder = holder[key]
                holder[path[-1]] = LyingStr(holder[path[-1]])
                res = rig.run(req)
                self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, ".".join(path)))
                self.assertEqual(rig.launches, [])

    def test_f2_a_mapping_that_answers_two_ways_is_read_once(self) -> None:
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
        res = rig.run(TwoFaced(request(cwd="relative/dir"), {"cwd": CWD}))
        self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, "cwd"))
        self.assertEqual(rig.launches, [])
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
        res = rig.run(TwoFaced(request(), {"cwd": "relative/dir"}))   # the use reads what the check read
        self.assertIsNone(res["code"])
        self.assertEqual([m["params"]["cwd"] for m in rig.sent("session/new")], [CWD])
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
        req = request()
        req["request_id"] = 5
        res = rig.run(TwoFaced(req, {"request_id": "r1"}))
        self.assertEqual(res["code"], c.REQUEST_INVALID)
        self.assertNotEqual(res["request_id"], 5)
        self.assertEqual(rig.launches, [])

    def test_f3_a_client_failure_naming_the_key_is_masked(self) -> None:
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]], methods=("gemini-api-key",))
        rig.agent._accept = set()                                 # the agent refuses authenticate
        read = rig.agent.read_line

        def read_line(timeout_s):
            line = read(timeout_s)
            if line is not None and '"error"' in line:              # the refusal names the key
                message = json.loads(line)
                message["error"]["message"] = "bad credential " + KEY
                line = json.dumps(message)
            return line

        rig.agent.read_line = read_line
        res = rig.run(request())
        self.assertIn(res["code"], c.CODES)
        self.assertNotIn(KEY, json.dumps(res))
        self.assertEqual(len(rig.agent.fenced), 1)

    def test_f5_a_lone_surrogate_in_the_turns_text_leaves_the_result_writable(self) -> None:
        rig = Rig([[("chunk", "a" + chr(0xD800) + "b"), ("end", "end_turn")]])
        res = rig.run(request())
        self.assertIsNone(res["code"])
        json.dumps(res, ensure_ascii=False).encode("utf-8")
        self.assertEqual(res["text"], "a" + chr(0xFFFD) + "b")

if __name__ == "__main__":
    unittest.main()
