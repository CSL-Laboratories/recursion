# Written by Claude Opus 5.5 (claude-opus-5-5), Claude Code builder lane ENGINE-E5, for Lucas.
"""Antigravity's ACP drive as a route adapter (``AntigravityAcpAdapter``): one turn of ``agy acp`` per run,
against the fake ACP agent. Nothing is registered; every key and path here is made up.

Run: PYTHONPATH=core <python> -m unittest \\
    recursion_core.routes.engines.fixtures.antigravity.test_antigravity_acp_adapter
"""

from __future__ import annotations

import json
import pathlib
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
        self.handed: list = []                  # what each launch was handed: (positional count, keyword names)

        def launcher(*args, **kw):
            self.handed.append((len(args), sorted(kw)))
            self.launches.append({"argv": list(args[0]), "env": dict(kw["env"]), "shell": kw.get("shell", False)})
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
    """The folder the agent is told is the caller's: refused without one, and that folder, with no fallback, is
    what the agent is told (the Liaison's ruling, Nov 15). Where the process starts is the launch boundary's."""

    def test_a_run_without_an_absolute_cwd_is_refused_before_anything_starts(self) -> None:
        for cwd in (..., None, "", "relative/dir", "/var/tmp/a" + chr(0) + "b", b"/var/tmp/x", 5):
            with self.subTest(cwd=repr(cwd)):
                rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
                res = rig.run(request(cwd=cwd))
                self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, "cwd"))
                self.assertEqual((rig.launches, rig.custody, rig.asked, rig.agent.received), ([], [], [], []))

    def test_no_other_folder_is_ever_told(self) -> None:
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
            # with no prepared home the precondition refuses first (#411 round 3); the one-shot's own
            # NOT_PREPARED for it is held by the one-shot tests
            "no prepared home": (dict(home=None), request(), ag.NO_RUN_FOLDER),
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
    def test_the_factory_takes_only_binary_and_refuses_to_be_registered(self) -> None:
        for option in ("prepared_home", "output_boundary", "cwd", "env"):
            with self.subTest(option=option), self.assertRaises(TypeError):
                ag.acp_factory(None, None, None, PARENT, **{option: None})
        launches = []
        for options in ({}, {"binary": "/opt/agy"}):
            with self.subTest(options=options), self.assertRaises(c.RouteError) as caught:
                ag.acp_factory(lambda *a, **k: launches.append(a), lambda: 0.0, lambda *a: KEY, PARENT, **options)
            self.assertEqual((caught.exception.code, caught.exception.detail), (c.LAUNCH_REFUSED, ag.NO_RUN_FOLDER))
        self.assertEqual(launches, [])

    def test_an_adapter_with_no_prepared_home_refuses_every_launch(self) -> None:
        launches = []
        made = ag.AntigravityAcpAdapter(lambda *a, **k: launches.append(a), lambda: 0.0, lambda *a: KEY, PARENT)
        res = made.run(request("oauth-business", "subscription", None), 60.0)
        self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, ag.NO_RUN_FOLDER))
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


class TwoFacedItem(TwoFaced):
    """A TwoFaced that lies on [] as well (items() still tells the truth)."""

    def __getitem__(self, key):
        return self._lie[key] if key in self._lie else super().__getitem__(key)


class Unreadable(dict):
    """A route that cannot be read as a mapping."""

    def items(self):
        raise RuntimeError("synthetic")


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


class ReviewRoundTwo411Test(unittest.TestCase):
    """DeepSeek's round-2 read of #411 at 16fd5d50 (MEDIUM F1, LOW F3, F4, F6; F2 and F5 are #408's, pinned
    there by its round-4 answer). Each fails on that head."""

    def test_f1_the_launch_boundary_is_handed_no_working_directory(self) -> None:
        # The fact, pinned: argv, env and shell, nothing else. The agent is told the request's cwd; where the
        # process starts is the launch boundary's (in the daemon as started, the engine's home), not the run's.
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
        self.assertIsNone(rig.run(request())["code"])
        self.assertEqual(rig.handed, [(1, ["env", "shell"])])
        self.assertEqual([m["params"]["cwd"] for m in rig.sent("session/new")], [CWD])

    def test_f1_the_precondition_is_written_where_the_adapter_is_read(self) -> None:
        sentence = "MUST NOT BE REGISTERED UNTIL THE LAUNCH BOUNDARY STARTS THE CHILD IN THE RUN'S FOLDER"
        def flat(text):                                   # 3.13 and later strip a docstring's indentation
            return " ".join(text.split())

        self.assertIn(sentence, flat(ag.AntigravityAcpAdapter.__doc__))
        self.assertIn(sentence.lower(), flat(ag.acp_factory.__doc__).lower())
        readme = pathlib.Path(ag.__file__).parent / "fixtures" / "antigravity" / "README.md"
        self.assertIn(sentence.lower(), readme.read_text(encoding="utf-8").lower())
        for untrue in ("never the daemon's own working directory", "no fallback of any kind"):
            self.assertNotIn(untrue, ag.AntigravityAcpAdapter.__doc__)
            self.assertNotIn(untrue, readme.read_text(encoding="utf-8"))

    def test_f3_a_lone_surrogate_in_a_client_failures_detail_leaves_the_result_writable(self) -> None:
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]], methods=("gemini-api-key",))
        rig.agent._accept = set()                                 # the agent refuses authenticate
        read = rig.agent.read_line

        def read_line(timeout_s):
            line = read(timeout_s)
            if line is not None and '"error"' in line:              # the refusal carries a lone surrogate
                message = json.loads(line)
                message["error"]["message"] = "bad " + chr(0xD800) + " credential"
                line = json.dumps(message)
            return line

        rig.agent.read_line = read_line
        res = rig.run(request())
        self.assertIn(res["code"], c.CODES)
        self.assertNotIn(chr(0xD800), res["detail"])
        json.dumps(res, ensure_ascii=False).encode("utf-8")
        self.assertEqual(len(rig.agent.fenced), 1)

    def test_f4_a_lone_surrogate_in_a_failed_turns_detail_leaves_the_result_writable(self) -> None:
        rig = Rig([[("error", -32603, "refused " + chr(0xD800))]])
        res = rig.run(request())
        self.assertEqual(res["code"], c.ENGINE_ERROR)
        self.assertNotIn(chr(0xD800), res["detail"])
        self.assertIn(chr(0xFFFD), res["detail"])
        json.dumps(res, ensure_ascii=False).encode("utf-8")
        self.assertEqual(len(rig.agent.fenced), 1)

    def test_f2_a_route_and_a_connection_that_answer_two_ways_are_read_once(self) -> None:
        # (the lane's removal probes found this unpinned below the top level)
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
        req = request()
        req["route"] = TwoFaced(req["route"], {"auth_method": "agent-platform"})
        self.assertIsNone(rig.run(req)["code"])
        self.assertEqual([m["params"]["methodId"] for m in rig.sent("authenticate")], ["gemini-api-key"])
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
        req = request()
        req["route"]["connection"] = TwoFacedItem(req["route"]["connection"], {"id": "conn-B"})
        self.assertIsNone(rig.run(req)["code"])
        self.assertEqual(rig.custody, [("conn-A", "GEMINI_API_KEY")])

    def test_f6_a_refusal_of_the_request_keeps_the_callers_request_id(self) -> None:
        for path in (("tenant",), ("prompt",), ("contract",), ("route", "model"), ("route", "connection", "id")):
            with self.subTest(field=".".join(path)):
                rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
                req = request()
                holder = req
                for key in path[:-1]:
                    holder = holder[key]
                holder[path[-1]] = LyingStr(holder[path[-1]])
                res = rig.run(req)
                self.assertEqual((res["request_id"], res["code"], res["detail"]),
                                 ("r1", c.REQUEST_INVALID, ".".join(path)))
                self.assertEqual(rig.launches, [])
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
        req = request()
        req["route"] = Unreadable(req["route"])
        res = rig.run(req)
        self.assertEqual((res["request_id"], res["code"], res["detail"]), ("r1", c.REQUEST_INVALID, "request"))
        req = request()
        req["request_id"] = LyingStr("r1")                          # not exactly a str: never echoed
        res = rig.run(req)
        self.assertEqual((type(res["request_id"]), res["request_id"], res["detail"]), (str, "", "request_id"))
        self.assertEqual(rig.launches, [])


class ReviewRoundThree411Test(unittest.TestCase):
    """DeepSeek's round-3 read of #411 at ea339644 (F1r-1 to F1r-4, OBS-1). Each fails on that head but
    OBS-1's, a guard: the order it pins already held."""

    ROUTES = {"gemini-api-key": ("api_key", "GEMINI_API_KEY"), "agent-platform": ("api_key", "GOOGLE_API_KEY"),
              "oauth-business": ("subscription", None), "oauth-personal": ("subscription", None)}

    def test_f1r4_the_class_registered_through_the_engine_table_refuses_every_run(self) -> None:
        from recursion_core.routes.engines import EngineTable

        table = EngineTable()
        table.register("antigravity", ag.AntigravityAcpAdapter)     # registrable: the table checks callable
        table.freeze()
        factory = dict(table.items())["antigravity"]
        launches, custody = [], []
        made = factory(lambda *a, **k: launches.append(a), lambda: 0.0,
                       lambda *a: custody.append(a) or KEY, PARENT)
        self.assertEqual(set(self.ROUTES), set(ag.METHODS))
        for method, (kind, cred) in self.ROUTES.items():
            with self.subTest(method=method):
                res = made.run(request(method, kind, cred), 60.0)
                self.assertEqual((res["request_id"], res["code"], res["detail"]),
                                 ("r1", c.LAUNCH_REFUSED, ag.NO_RUN_FOLDER))
        self.assertEqual((launches, custody), ([], []))
        readme = pathlib.Path(ag.__file__).parent / "fixtures" / "antigravity" / "README.md"
        self.assertIn("The class itself is registrable", readme.read_text(encoding="utf-8"))

    def test_f1r1_a_request_id_that_cannot_be_written_is_refused_and_never_echoed(self) -> None:
        for lying in (None, "tenant"):
            with self.subTest(also_lying=lying):
                rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
                req = request()
                req["request_id"] = "r" + chr(0xD800)
                if lying:
                    req[lying] = LyingStr(req[lying])
                res = rig.run(req)
                self.assertEqual((res["request_id"], res["code"], res["detail"]), ("", c.REQUEST_INVALID, "request_id"))
                json.dumps(res, ensure_ascii=False).encode("utf-8")
                self.assertEqual(rig.launches, [])

    def test_f1r3_the_docstring_says_this_head_and_370_apart(self) -> None:
        doc = " ".join(ag.AntigravityAcpAdapter.__doc__.split())
        self.assertIn("At this head no launcher for this engine can be built", doc)
        self.assertIn("Once #370 lands, its S4", doc)
        self.assertNotIn("with #370's S4 where", doc)
        self.assertNotIn("NO fallback of any kind", CwdTest.__doc__)

    def test_obs1_the_first_field_in_order_is_the_one_named(self) -> None:
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
        req = request()
        req["tenant"], req["prompt"] = LyingStr(req["tenant"]), LyingStr(req["prompt"])
        self.assertEqual(rig.run(req)["detail"], "tenant")


class CodexRoundOne411Test(unittest.TestCase):
    """#411's first Codex reads at 15b4b2ee (astra R1-R3, sol R1-R5). Each fails on 15b4b2ee unless marked a
    guard (a guard holds a rule a removal probe needs)."""

    SPLIT_KEY = "synthetic-a\ufffdb"              # made up; custody's contract admits any non-empty str
    AGENT_SPELLING = "synthetic-a" + chr(0xD800) + "b"

    def refused_authenticate(self, rig, message):
        rig.agent._accept = set()
        read = rig.agent.read_line

        def read_line(timeout_s):
            line = read(timeout_s)
            if line is not None and '"error"' in line:
                parsed = json.loads(line)
                parsed["error"]["message"] = message
                line = json.dumps(parsed)
            return line
        rig.agent.read_line = read_line

    def test_r1_a_replaced_surrogate_never_spells_the_key_on_any_branch(self) -> None:
        branches = {
            "a turn's text": ([[("chunk", self.AGENT_SPELLING), ("end", "end_turn")]], "text"),
            "a failed turn's detail": ([[("error", -32603, self.AGENT_SPELLING)]], "detail"),
            "a client failure's detail": ([[("chunk", "hi"), ("end", "end_turn")]], "detail"),
        }
        for name, (scripts, field) in branches.items():
            with self.subTest(name):
                rig = Rig(scripts, key=self.SPLIT_KEY, methods=("gemini-api-key",))
                if name == "a client failure's detail":
                    self.refused_authenticate(rig, self.AGENT_SPELLING)
                res = rig.run(request())
                self.assertNotIn(self.SPLIT_KEY, res[field] or "")
                json.dumps(res, ensure_ascii=False).encode("utf-8")

    def test_r2_a_deadline_crossed_while_the_text_is_finished_is_deadline_exceeded_and_fenced(self) -> None:
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
        deadline = rig.agent.clock() + 60
        close = rig.agent.close_stdin

        def slow_close():                             # the input's closing takes the run past its deadline
            close()
            rig.agent.clock.advance(61)
        rig.agent.close_stdin = slow_close
        res = rig.run(request(), deadline)
        self.assertEqual(res["code"], c.DEADLINE_EXCEEDED)
        self.assertEqual(rig.agent.fenced, ["deadline"])
        self.assertNotIn("text", {k for k, v in res.items() if v})

    def test_r2_guard_a_turn_finished_in_time_succeeds(self) -> None:
        rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
        res = rig.run(request())
        self.assertEqual((res["code"], res["text"], rig.agent.fenced), (None, "hi", []))

    def test_r3_the_shared_request_checks_come_before_the_acp_preconditions(self) -> None:
        bad = {
            "an empty model": (("route", "model"), "", "route.model"),
            "a wrong contract": (("contract",), "route.v0-not", "contract"),
            "an empty tenant": (("tenant",), "", "tenant"),
            "an empty prompt": (("prompt",), "", "prompt"),
            "a bad deadline_s": (("deadline_s",), 0, "deadline_s"),
            "no connection id": (("route", "connection", "id"), "", "route.connection"),
        }
        for home in (prepared, None):
            for name, (path, value, detail) in bad.items():
                with self.subTest(name, home=home is not None):
                    rig = Rig([[("chunk", "hi"), ("end", "end_turn")]], home=home)
                    req = request(cwd=...)                        # no cwd either: the shared check comes first
                    holder = req
                    for key in path[:-1]:
                        holder = holder[key]
                    holder[path[-1]] = value
                    res = rig.run(req)
                    self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, detail))
                    self.assertEqual((rig.launches, rig.custody), ([], []))
        with self.subTest("a bad deadline_at"):
            rig = Rig([[("chunk", "hi"), ("end", "end_turn")]], home=None)
            res = rig.run(request(), float("nan"))
            self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, "deadline_at"))

    def test_r3_a_request_id_that_is_not_a_str_is_refused_and_never_echoed(self) -> None:
        for rid in (5, True, {"id": "r1"}, ["r1"], None):
            for home, cwd in ((prepared, ...), (None, CWD), (prepared, CWD)):
                with self.subTest(rid=rid, home=home is not None, cwd=cwd is not ...):
                    rig = Rig([[("chunk", "hi"), ("end", "end_turn")]], home=home)
                    req = request(cwd=cwd)
                    req["request_id"] = rid
                    res = rig.run(req)
                    self.assertEqual((res["code"], res["detail"], res["request_id"]),
                                     (c.REQUEST_INVALID, "request_id", ""))
                    self.assertEqual(rig.launches, [])

    def test_r3_guard_a_well_formed_run_built_by_the_engine_table_keeps_its_precondition(self) -> None:
        for method, kind, cred in (("gemini-api-key", "api_key", "GEMINI_API_KEY"),
                                   ("oauth-business", "subscription", None)):
            with self.subTest(method=method):
                rig = Rig([[("chunk", "hi"), ("end", "end_turn")]], home=None)
                res = rig.run(request(method, kind, cred))
                self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, ag.NO_RUN_FOLDER))

    def test_r4_the_connections_kind_and_credential_name_are_read_as_exactly_a_str(self) -> None:
        for name in ("kind", "credential_env"):
            with self.subTest(field=name):
                rig = Rig([[("chunk", "hi"), ("end", "end_turn")]])
                req = request()
                req["route"]["connection"][name] = LyingStr(req["route"]["connection"][name])
                res = rig.run(req)
                self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, "route.connection." + name))
                self.assertEqual((rig.launches, rig.custody), ([], []))


class ReviewRoundFour411Test(unittest.TestCase):
    """DeepSeek's round-4 read of #411 at a6a6ada3. F1 fails on that head: a refusal detail keeps a lone
    surrogate. F3 and F4 are the sentences the read asked for. F5 puts each test module on its own line."""

    def test_f1_a_surrogate_in_the_connection_id_never_makes_a_refusal_unwritable(self) -> None:
        bad = "conn-" + chr(0xD800)
        for key in ("", "made-up\x00key"):
            with self.subTest(key=repr(key)):
                rig = Rig([[("chunk", "hi"), ("end", "end_turn")]], key=key)
                req = request()
                req["route"]["connection"]["id"] = bad
                res = rig.run(req)
                json.dumps(res, ensure_ascii=False).encode("utf-8")
                self.assertEqual(res["code"], c.LAUNCH_REFUSED)
                self.assertNotIn(chr(0xD800), res["detail"])
                self.assertEqual(rig.launches, [])

    def test_f3_the_fence_is_named_on_launched_and_the_read_comes_first(self) -> None:
        doc = " ".join(ag.AntigravityAcpAdapter.__doc__.split())
        self.assertIn("fenced by ``_launched``, which both adapters' ``run`` calls", doc)
        refused = " ".join(ag.AntigravityOneShotAdapter._refused.__doc__.split())
        self.assertIn("reads the request once first, and that read is a shape check too", refused)

    def test_f5_the_readme_command_gives_each_test_module_its_own_line(self) -> None:
        text = (pathlib.Path(ag.__file__).parent / "fixtures" / "antigravity" / "README.md").read_text(
            encoding="utf-8")
        command = text.split("Run the tests by module name", 1)[1].split("`unittest discover", 1)[0]
        for line in command.splitlines():
            self.assertLessEqual(line.count("antigravity.test_"), 1)


if __name__ == "__main__":
    unittest.main()
