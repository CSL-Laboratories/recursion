"""The CLI is a surface over the control API: no Stop path of its own (S6)."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import pathlib
import unittest

from recursion_core.control import cli
from recursion_core.control.registry import EMPTY, POPULATED
from recursion_core.control.tests.fakes import FakeKillTree, make_api, started

CLI_SOURCE = pathlib.Path(cli.__file__).read_text()


class FakeAPI:
    def __init__(self, answer: dict) -> None:
        self.answer, self.calls = answer, []

    def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        return self.answer


def run(argv, api):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, api, out, err)
    return code, out.getvalue(), err.getvalue()


class CliThroughApiTest(unittest.TestCase):
    def test_stop_goes_through_the_api_and_prints_its_state_verbatim(self) -> None:
        answer = {"code": "pending", "run_id": "run-9", "state": "pending", "reason": "cgroup_populated"}
        api = FakeAPI(answer)
        code, out, _ = run(["stop", "run-9"], api)
        self.assertEqual(api.calls, [("run.stop", {"run_id": "run-9"})])
        self.assertEqual(json.loads(out), answer)
        self.assertEqual(out.strip(), json.dumps(answer, sort_keys=True))
        self.assertEqual(code, cli.EXIT_PENDING)

    def test_exit_codes_distinguish_stopped_pending_and_error(self) -> None:
        cases = {"stopped": cli.EXIT_OK, "pending": cli.EXIT_PENDING, "run_unknown": cli.EXIT_UNKNOWN,
                 "request_invalid": cli.EXIT_ERROR, "something_else": cli.EXIT_ERROR}
        for api_code, exit_code in cases.items():
            self.assertEqual(run(["stop", "r"], FakeAPI({"code": api_code}))[0], exit_code, api_code)
        self.assertEqual(len({cli.EXIT_OK, cli.EXIT_PENDING, cli.EXIT_ERROR, cli.EXIT_UNKNOWN}), 4)

    def test_other_commands_map_to_api_methods(self) -> None:
        api = FakeAPI({"code": "ok"})
        run(["list"], api)
        run(["status", "run-1"], api)
        run(["start", "--tenant", "t", "--engine", "codex", "--prompt", "hi", "--client-key", "k"], api)
        self.assertEqual(api.calls, [("run.list", {}), ("run.status", {"run_id": "run-1"}),
                                     ("run.start", {"tenant": "t", "engine": "codex", "prompt": "hi", "client_key": "k"})])

    def test_usage_error_makes_no_call(self) -> None:
        api = FakeAPI({"code": "ok"})
        self.assertEqual(run(["stop"], api)[0], cli.EXIT_USAGE)
        self.assertEqual(run(["kill", "r"], api)[0], cli.EXIT_USAGE)
        self.assertEqual(api.calls, [])

    def test_against_real_api_with_fake_cleanup(self) -> None:
        api, _, kill, _ = make_api(FakeKillTree([POPULATED, EMPTY]))
        rid = started(api)
        code, out, _ = run(["stop", rid], api)
        self.assertEqual((code, json.loads(out)["state"]), (cli.EXIT_PENDING, "pending"))
        code, out, _ = run(["stop", rid], api)
        self.assertEqual((code, json.loads(out)["state"]), (cli.EXIT_OK, "stopped"))
        self.assertEqual(len(kill.requests), 1)

    def test_no_factory_is_a_typed_error(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.run(["list"], None), cli.EXIT_ERROR)
        self.assertEqual(json.loads(buf.getvalue())["code"], "control_api_unavailable")


class CliStructureTest(unittest.TestCase):
    def test_cli_imports_no_process_or_signal_access(self) -> None:
        banned = {"os", "signal", "subprocess", "ctypes", "multiprocessing", "resource", "pty", "socket"}
        tree = ast.parse(CLI_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertFalse(names & banned, names)
        for word in ("kill", "SIGINT", "SIGKILL", "pidfd", "cgroup", "__import__", "importlib"):
            self.assertNotIn(word, CLI_SOURCE.replace("verbatim", ""), word)

    def test_cli_does_not_import_the_registry_or_cleanup_layer(self) -> None:
        self.assertNotIn("registry", CLI_SOURCE)
        self.assertNotIn("KillTree", CLI_SOURCE)


if __name__ == "__main__":
    unittest.main()
