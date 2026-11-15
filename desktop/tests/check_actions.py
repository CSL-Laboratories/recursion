"""Each desktop action sends the CLI's request, and only methods the desktop may call (fixtures/actions.json).

Run from the repository root (see desktop/README.md):

    PYTHONPATH=core python -m unittest discover -s desktop/tests -t desktop/tests -p 'check_*.py'
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from fake_socket import FakeDaemon
from recursion_core.control import cli
from recursion_core.control.client import SocketAPI
from recursion_core.control_transport.framing import encode_frame
from recursion_core.tui import protocol

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ACTIONS = json.loads((FIXTURES / "actions.json").read_text(encoding="utf-8"))
ROWS = json.loads((FIXTURES / "spine-rows.json").read_text(encoding="utf-8"))
COMMAND_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# The actions that end, stop or answer something are sent only after the operator confirms.
CONFIRMED = {"run.stop", "session.stop", "approval.answer"}
FIRST_PAGE = {"code": "ok", "runs": [{"run_id": "run-1", "tenant": "t1", "engine": "codex", "state": "running"}],
              "next_after": "run-1"}
LAST_PAGE = {"code": "ok", "runs": []}


def actions(*kinds: str) -> list[dict]:
    return [a for a in ACTIONS["actions"] if not kinds or a["source"]["kind"] in kinds]


def frame(action: dict) -> dict:
    return {**ACTIONS["envelope"], "method": action["method"], "params": action["params"]}


class Recorder:
    """``call(method, params)`` like ``SocketAPI``; records each call and answers from a script."""

    def __init__(self, answers: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._answers = answers or {}

    def call(self, method: str, params: dict | None = None) -> dict:
        self.calls.append((method, dict(params or {})))
        answer = self._answers.get(method, {"code": "ok"})
        return answer(len(self.calls)) if callable(answer) else answer


def walk(n: int) -> dict:
    return FIRST_PAGE if n == 1 else LAST_PAGE


class CliParityTest(unittest.TestCase):
    def test_each_cli_action_is_the_request_the_cli_sends(self) -> None:
        for action in actions("cli"):
            with self.subTest(action["action"]):
                api = Recorder()
                cli.main(action["source"]["argv"], api, io.StringIO(), io.StringIO())
                self.assertEqual(api.calls, [(action["method"], action["params"])])

    def test_the_next_page_is_the_request_the_cli_list_sends_for_it(self) -> None:
        for action in actions("cli-walk"):
            with self.subTest(action["action"]):
                api = Recorder({"run.list": walk})
                cli.main(action["source"]["argv"], api, io.StringIO(), io.StringIO())
                page = action["source"]["page"]
                self.assertEqual(api.calls[page - 1], (action["method"], action["params"]))
                self.assertEqual(api.calls[page - 2][1].get("after"), None if page == 2 else action["source"]["after"])

    def test_every_cli_command_that_sends_a_request_has_a_desktop_action(self) -> None:
        sent = {c for c in protocol.COMMANDS if c != protocol.CONDITIONS}
        covered = {a["source"]["argv"][0] for a in actions("cli")}
        self.assertEqual(covered, sent)
        self.assertEqual({a["method"] for a in actions("cli")}, set(protocol.ALLOWED_METHODS))


class WireParityTest(unittest.TestCase):
    """The frame the CLI's socket client writes for each action, read on a real socket, is the golden frame."""

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="desktop-wire-")

    def test_the_cli_over_the_socket_writes_the_golden_frame(self) -> None:
        for action in actions("cli", "cli-walk"):
            with self.subTest(action["action"]):
                pages = iter([FIRST_PAGE, LAST_PAGE])
                answers = {"run.list": lambda p: next(pages), "run.status": {"code": "ok"},
                           "run.stop": {"code": "stopped"}, "run.start": {"code": "started"}}
                with FakeDaemon(tempfile.mkdtemp(dir=self.dir), answers) as daemon:
                    cli.main(action["source"]["argv"], SocketAPI(daemon.path), io.StringIO(), io.StringIO())
                    page = action["source"].get("page", 1)
                    got = daemon.frames[page - 1]
                self.assertEqual(got, frame(action))
                # Same keys in the same order, so the same bytes on the wire.
                self.assertEqual(encode_frame(got), encode_frame(frame(action)))

    def test_the_socket_client_writes_the_golden_frame_for_every_method_the_daemon_has(self) -> None:
        for action in [a for a in ACTIONS["actions"] if a["on_master"]]:
            with self.subTest(action["action"]):
                with FakeDaemon(tempfile.mkdtemp(dir=self.dir), {action["method"]: {"code": "ok"}}) as daemon:
                    answer = SocketAPI(daemon.path).call(action["method"], action["params"])
                self.assertEqual(answer, {"code": "ok"})
                self.assertEqual(daemon.frames, [frame(action)])


class AllowedMethodsTest(unittest.TestCase):
    def test_every_method_has_a_row_the_page_may_call_and_none_is_of_the_manage_scope(self) -> None:
        for action in ACTIONS["actions"]:
            with self.subTest(action["action"]):
                row = ROWS["rows"].get(action["method"])
                self.assertIsNotNone(row, f"{action['method']} has no recorded row")
                self.assertTrue(row["page_allowed"])
                self.assertNotEqual(row["scope"], "manage")

    def test_the_recorded_rows_are_exactly_the_methods_the_actions_use(self) -> None:
        self.assertEqual(set(ROWS["rows"]), {a["method"] for a in ACTIONS["actions"]})
        self.assertEqual(ROWS["spine_head"], ACTIONS["spine_head"])

    def test_params_are_within_the_recorded_params_of_their_method(self) -> None:
        for action in ACTIONS["actions"]:
            with self.subTest(action["action"]):
                spec = ROWS["rows"][action["method"]]["params"]
                self.assertFalse(spec["additionalProperties"])
                self.assertLessEqual(set(action["params"]), set(spec["properties"]))
                self.assertLessEqual(set(spec["required"]), set(action["params"]))

    def test_a_keyed_method_carries_a_command_id_the_desktop_minted(self) -> None:
        self.assertRegex(ACTIONS["command_id_example"], COMMAND_ID)
        for action in ACTIONS["actions"]:
            keyed = ROWS["rows"][action["method"]]["idempotency"] == "command_id"
            with self.subTest(action["action"]):
                self.assertEqual("command_id" in action["params"], keyed)
                if keyed:
                    self.assertEqual(action["params"]["command_id"], ACTIONS["command_id_example"])
                    self.assertIn("command_id", action["deltas"])

    def test_a_surface_the_schema_does_not_list_is_declared_as_a_gap(self) -> None:
        for action in ACTIONS["actions"]:
            if "surface" not in action["params"]:
                continue
            family = "approval" if action["method"].startswith("approval.") else "surface.prefs"
            listed = action["params"]["surface"] in ROWS["surfaces"][family]
            with self.subTest(action["action"]):
                self.assertEqual("schema_gap" in action, not listed)
                self.assertEqual(action["params"]["surface"], "desktop")

    def test_stops_and_answers_are_confirmed_first(self) -> None:
        for action in ACTIONS["actions"]:
            with self.subTest(action["action"]):
                self.assertEqual(action.get("confirm", False), action["method"] in CONFIRMED)

    def test_every_spine_source_is_pinned(self) -> None:
        for action in actions("spine"):
            with self.subTest(action["action"]):
                self.assertRegex(action["source"]["sha256"], SHA256)
                self.assertTrue(action["source"]["file"].endswith(".json"))
                self.assertLessEqual(set(action["deltas"]), set(action["params"]))
                self.assertTrue(all(isinstance(why, str) and why for why in action["deltas"].values()))

    def test_action_names_are_unique(self) -> None:
        names = [a["action"] for a in ACTIONS["actions"]]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
