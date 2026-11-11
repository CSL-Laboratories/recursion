"""Required test 1: each TUI action sends the same control API request as the matching CLI command."""

from __future__ import annotations

import io
import unittest

from recursion_core.control import cli
from recursion_core.tui import protocol
from recursion_core.tui.app import TuiApp
from recursion_core.tui.tests.fakes import Clock, ScriptedAPI, fixture

SPEC = fixture("protocol.json")


def tui_requests(case: dict) -> list:
    api = ScriptedAPI({"run.list": {"code": "ok", "runs": SPEC["setup_runs"]}, "run.status": {"code": "ok"},
                       "run.stop": {"code": "stopped"}, "run.start": {"code": "started"}})
    app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"})
    app.open()
    opened = len(api.calls)
    if "command" in case["tui"]:
        app.command(case["tui"]["command"])
    for key in case["tui"].get("keys", []):
        app.press(key)
    return api.calls[opened:]


def cli_requests(argv: list) -> list:
    api = ScriptedAPI({})
    cli.main(argv, api, io.StringIO(), io.StringIO())
    return api.calls


class ParityTest(unittest.TestCase):
    def test_every_fixture_case_matches_the_cli_request(self) -> None:
        for case in SPEC["cases"]:
            with self.subTest(case["name"]):
                want = [(case["request"]["method"], case["request"]["params"])]
                self.assertEqual(tui_requests(case), want)
                self.assertEqual(cli_requests(case["cli"]), want)

    def test_fixture_covers_every_cli_command(self) -> None:
        covered = {case["cli"][0] for case in SPEC["cases"] + SPEC["reads_current_conditions"]}
        self.assertEqual(covered, set(protocol.COMMANDS))

    def test_conditions_is_on_both_surfaces_and_adds_no_method(self) -> None:
        for case in SPEC["reads_current_conditions"]:
            with self.subTest(case["name"]):
                self.assertEqual(tui_requests(case), [(r["method"], r["params"]) for r in case["tui_requests"]])
                self.assertEqual(cli_requests(case["cli"]), [(r["method"], r["params"]) for r in case["cli_requests"]])
                for r in case["cli_requests"]:
                    self.assertIn(r["method"], protocol.ALLOWED_METHODS)

    def test_view_only_actions_send_nothing(self) -> None:
        for case in SPEC["no_request"]:
            with self.subTest(case["name"]):
                self.assertEqual(tui_requests(case), [])

    def test_command_grammar_refuses_what_the_cli_refuses(self) -> None:
        for line in ("stop", "status", "kill run-1", "approve run-1", "deny run-1", "start --tenant t",
                     "stop run-1 extra", "list --all", "stop 'unbalanced", "conditions run-1"):
            with self.subTest(line):
                with self.assertRaises(protocol.CommandError):
                    protocol.parse_command(line)
                import shlex
                try:
                    argv = shlex.split(line)
                except ValueError:
                    continue
                self.assertEqual(cli.main(argv, ScriptedAPI({}), io.StringIO(), io.StringIO()), cli.EXIT_USAGE)

    def test_help_is_not_a_command(self) -> None:
        # The CLI prints argparse help and exits; the TUI shows a usage error and sends nothing.
        for line in ("--help", "start --help", "-h"):
            with self.assertRaises(protocol.CommandError):
                protocol.parse_command(line)


if __name__ == "__main__":
    unittest.main()
