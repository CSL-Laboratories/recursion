"""T08 and T09: an explicit empty CLI client key, and MCP arguments that are not objects.

Run from the repository root with PYTHONPATH=core.
"""
from __future__ import annotations

import io
import json
import unittest

from recursion_core.control import cli, mcp_tools
from recursion_core.control.api import ControlAPI
from recursion_core.control.registry import EMPTY, InMemoryRunRegistry


def make_api():
    launches = []
    ids = iter(range(1, 10))

    class Cleanup:
        def request_stop(self, cgroup):
            raise AssertionError("these witnesses never stop a run")

        def confirm(self, cgroup, timeout_s):
            return EMPTY

    def starter(params):
        launches.append(dict(params))
        return {"cgroup": "/fake/" + params["run_id"]}

    api = ControlAPI(InMemoryRunRegistry(), Cleanup(), starter,
                     clock=lambda: 1000.0, new_run_id=lambda: "run-" + str(next(ids)))
    return api, launches


class RecordingAPI:
    """Records each call and returns one constant answer. A stand-in, not a ControlAPI."""

    def __init__(self, answer=None):
        self.calls = []
        self.answer = {"code": "ok"} if answer is None else answer

    def call(self, method, params=None):
        self.calls.append((method, params))
        return self.answer


_START_ANSWER = {"code": "started", "run": {"run_id": "run-1"}}
_START_ARGV = ["start", "--tenant", "t", "--engine", "codex", "--prompt", "hi"]
_START_PARAMS = {"tenant": "t", "engine": "codex", "prompt": "hi"}
_HOSTILE_ARGUMENTS = ([1], [], False, 7)


def empty_key_observation():
    params = {"tenant": "t", "engine": "codex", "prompt": "hello", "client_key": ""}
    direct, direct_launches = make_api()
    direct_ids = [direct.call("run.start", params)["run"]["run_id"] for _ in range(2)]
    through_cli, cli_launches = make_api()
    cli_ids = []
    argv = ["start", "--tenant", "t", "--engine", "codex", "--prompt", "hello", "--client-key", ""]
    for _ in range(2):
        out = io.StringIO()
        assert cli.main(argv, through_cli, out, io.StringIO()) == 0
        cli_ids.append(json.loads(out.getvalue())["run"]["run_id"])
    return {"api_launches": len(direct_launches), "api_ids": direct_ids,
            "cli_launches": len(cli_launches), "cli_ids": cli_ids}


def mcp_argument_observation():
    observations = []
    for arguments in ([1], [], False, 7):
        api, launches = make_api()
        try:
            answer = mcp_tools.call_tool(api, "recursion_run_list", arguments)
            observations.append({"arguments": arguments, "answer": answer})
        except Exception as exc:
            observations.append({"arguments": arguments, "exception": type(exc).__name__})
        assert not launches
    return observations


class SweepS2bRegressionTest(unittest.TestCase):
    def test_f02_empty_client_key_preserves_api_retry_identity(self):
        observed = empty_key_observation()
        self.assertEqual(observed["api_launches"], 1)
        self.assertEqual(observed["cli_launches"], 1, observed)
        self.assertEqual(observed["cli_ids"][0], observed["cli_ids"][1])

    def test_f04_mcp_rejects_non_object_arguments_with_a_typed_result(self):
        for observed in mcp_argument_observation():
            with self.subTest(arguments=observed["arguments"]):
                self.assertNotIn("exception", observed)
                answer = observed["answer"]
                self.assertTrue(answer["isError"], observed)
                self.assertEqual(answer["structuredContent"]["code"], "request_invalid")

    def test_omitted_client_key_is_absent_from_start_params(self):
        # F1: a start with no --client-key sends no client_key member. Sending None would pass every
        # lane test that only supplies a non-empty key.
        api = RecordingAPI(_START_ANSWER)
        out = io.StringIO()
        self.assertEqual(cli.main(list(_START_ARGV), api, out, io.StringIO()), 0)
        self.assertEqual(api.calls, [("run.start", dict(_START_PARAMS))])
        self.assertNotIn("client_key", api.calls[0][1])

    def test_empty_client_key_is_the_empty_string(self):
        # F4: --client-key "" is the empty string itself, and it is a different run from a "_" key.
        api = RecordingAPI(_START_ANSWER)
        argv = list(_START_ARGV) + ["--client-key", ""]
        self.assertEqual(cli.main(argv, api, io.StringIO(), io.StringIO()), 0)
        self.assertEqual(api.calls, [("run.start", {**_START_PARAMS, "client_key": ""})])

        live, launches = make_api()
        codes = []
        outs = []
        for key in ("", "_"):
            out = io.StringIO()
            codes.append(cli.main(list(_START_ARGV) + ["--client-key", key], live, out, io.StringIO()))
            outs.append(out.getvalue())
        self.assertEqual(codes, [0, 0])
        ids = [json.loads(text)["run"]["run_id"] for text in outs]
        self.assertEqual(len(launches), 2, launches)
        self.assertNotEqual(ids[0], ids[1])
        self.assertEqual(launches[0].get("client_key"), "")
        self.assertEqual(launches[1].get("client_key"), "_")

    def test_hostile_arguments_do_not_call_the_api(self):
        # F2: a present non-dict is request_invalid and call_tool does not call the API. A read such as
        # run.list leaves no launch, so the witness is the recording double, including on run.start.
        for arguments in _HOSTILE_ARGUMENTS:
            for tool in ("recursion_run_list", "recursion_run_start"):
                spy = RecordingAPI()
                answer = mcp_tools.call_tool(spy, tool, arguments)
                self.assertEqual(spy.calls, [], (tool, arguments))
                self.assertTrue(answer["isError"], (tool, arguments))
                self.assertEqual(answer["structuredContent"]["code"], "request_invalid")
                self.assertEqual(answer["structuredContent"]["detail"], "arguments")
            live, launches = make_api()
            answer = mcp_tools.call_tool(live, "recursion_run_start", arguments)
            self.assertEqual(launches, [], arguments)
            self.assertTrue(answer["isError"], arguments)
            self.assertEqual(answer["structuredContent"]["code"], "request_invalid")

    def test_omitted_arguments_are_an_empty_object(self):
        # F3: arguments=None is legal and is the same as {}. run.list answers ok; run.stop and
        # run.start are the API's own refusal (a missing field), which means the API was called.
        spy = RecordingAPI()
        listed = mcp_tools.call_tool(spy, "recursion_run_list", None)
        self.assertEqual(spy.calls, [("run.list", {})])
        self.assertFalse(listed["isError"])
        self.assertEqual(listed["structuredContent"]["code"], "ok")

        live, launches = make_api()
        listed = mcp_tools.call_tool(live, "recursion_run_list", None)
        self.assertFalse(listed["isError"])
        self.assertEqual(listed["structuredContent"]["code"], "ok")
        self.assertEqual(listed["structuredContent"]["runs"], [])
        self.assertEqual(launches, [])

        stopped = mcp_tools.call_tool(live, "recursion_run_stop", None)
        self.assertTrue(stopped["isError"])
        self.assertEqual(stopped["structuredContent"], {"code": "request_invalid", "detail": "run_id"})

        started = mcp_tools.call_tool(live, "recursion_run_start", None)
        self.assertEqual(launches, [])
        self.assertTrue(started["isError"])
        self.assertEqual(started["structuredContent"], {"code": "request_invalid", "detail": "tenant"})

        start_spy = RecordingAPI()
        mcp_tools.call_tool(start_spy, "recursion_run_start", None)
        self.assertEqual(start_spy.calls, [("run.start", {})])

    def test_call_tool_words_name_a_dict(self):
        # F5: the words match the refusal, which is isinstance(..., dict), not every object.
        call_words = " ".join(mcp_tools.call_tool.__doc__.split())
        module_words = " ".join(mcp_tools.__doc__.split())
        self.assertIn("Arguments are a ``dict``", call_words)
        self.assertIn("not a ``dict``", call_words)
        self.assertIn("is not a ``dict``", module_words)
        self.assertNotIn("adds nothing", module_words)


if __name__ == "__main__":
    unittest.main()
