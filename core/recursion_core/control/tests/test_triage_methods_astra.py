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


if __name__ == "__main__":
    unittest.main()
