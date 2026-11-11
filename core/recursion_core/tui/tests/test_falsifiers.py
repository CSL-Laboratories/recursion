"""The O7 TUI decision packet's two falsifiers (the operator plugin repository's
docs/replacement/outcomes/O7/tui-decision-packet.json at 05918cea) and the CSL-799 brief's two, as tests:

O7-1  The TUI hides a refusal, uncertainty, stale or unreconciled state that the shared API exposes.
O7-2  The TUI submits an order or widens a profile (here: any request outside the CLI's control API set).
B-1   A TUI-only command the CLI lacks.
B-2   Attaching or closing changes run lifetime or starts a model call.
"""

from __future__ import annotations

import ast
import io
import json
import random
import unittest

from recursion_core.control import cli
from recursion_core.tui import protocol as p
from recursion_core.tui.app import TuiApp
from recursion_core.tui.tests.fakes import Clock, FakeDriver, ScriptedAPI, fixture
from recursion_core.tui.tests.test_imports import production_files

ANSWERS = fixture("answers.json")["cases"]


def cli_argv(method: str, params: dict) -> list[str]:
    cmd = {v: k for k, v in p.METHODS.items()}[method]
    if cmd == "list":
        return ["list"]
    if cmd in ("status", "stop"):
        return [cmd, params["run_id"]]
    argv = ["start", "--tenant", params["tenant"], "--engine", params["engine"], "--prompt", params["prompt"]]
    return argv + (["--client-key", params["client_key"]] if "client_key" in params else [])


def visible_everywhere(app: TuiApp, labels: set[str]) -> list[str]:
    rows, missing = [t for t, _ in app.frame()], []
    for label in labels:  # a condition row of its own, not only a mention in the error row
        if not any(r.startswith(f"{label}: ") for r in rows):
            missing.append(f"{label} not on screen")
    return missing


class O7Falsifiers(unittest.TestCase):
    def test_o7_1_no_exposed_condition_is_hidden(self) -> None:
        for case in ANSWERS:
            for environ in ({"NO_COLOR": "1"}, {}):
                with self.subTest(case["name"], colour=not environ):
                    api = ScriptedAPI({"run.list": {"code": "ok", "runs": [{"run_id": "run-1", "state": "running"}]},
                                       case["method"]: case["answer"]})
                    app = TuiApp(api, clock=Clock(), environ=environ, width=140)
                    app.open()
                    app.attach("run-1") if case["method"] == "run.status" else None
                    app.request(case["method"], {"run_id": "run-1"} if case["method"] != "run.list" else {})
                    exposed = {lab for lab, _ in p.answer_conditions(case["answer"])}
                    for i in range(3000):  # an output flood does not push conditions off screen
                        app.on_event({"run_id": "run-1", "text": f"noise {i}\n"})
                    self.assertEqual(visible_everywhere(app, exposed), [])
                    self.assertIn(f"status={case['answer']['code']}", app.frame()[1][0])

    def test_o7_1_detector_catches_a_hiding_view(self) -> None:
        api = ScriptedAPI({"run.list": {"code": "ok", "runs": []}, "run.stop": {"code": "run_unknown", "run_id": "x"}})
        app = TuiApp(api, clock=Clock(), environ={})
        app.open()
        app.command("stop x")
        app._current = lambda: {}  # mutant: a view that drops conditions
        self.assertNotEqual(visible_everywhere(app, {p.REFUSED}), [])

    def test_o7_2_and_b_1_every_reachable_request_is_a_cli_request(self) -> None:
        rng = random.Random(799)
        keys = list("jkarsy:qnYS ") + ["enter", "esc", "backspace", "up", "down"]
        words = ["list", "status run-1", "stop run-1", "start --tenant t --engine e --prompt p",
                 "conditions", "approve run-1", "deny run-1", "order buy", "submit", "kill run-1", "steer run-1 x"]
        for trial in range(200):
            api = ScriptedAPI({"run.list": {"code": "ok", "runs": [{"run_id": "run-1", "state": "running"}]}})
            app = TuiApp(api, clock=Clock(), environ={})
            app.open()
            for _ in range(40):
                if rng.random() < 0.2:
                    app.command(rng.choice(words))
                else:
                    app.press(rng.choice(keys))
                if app.closed:
                    break
            for method, params in api.calls:
                self.assertIn(method, p.ALLOWED_METHODS)
                cli_api = ScriptedAPI({})
                cli.main(cli_argv(method, params), cli_api, io.StringIO(), io.StringIO())
                self.assertEqual(cli_api.calls, [(method, params)], f"trial {trial}")

    def test_b_1_tui_commands_equal_cli_commands(self) -> None:
        sub = next(a for a in cli._parser()._actions if a.dest == "command")
        self.assertEqual(set(p.COMMANDS), set(sub.choices))

    def test_o7_2_no_order_approval_or_broker_identifier_in_production_code(self) -> None:
        banned = ("order", "approve", "approval", "deny", "submit", "broker", "gateway", "profile", "grant")
        for f in production_files():
            tree = ast.parse(f.read_text(encoding="utf-8"))
            idents = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    idents.add(node.name)
                elif isinstance(node, ast.Name):
                    idents.add(node.id)
                elif isinstance(node, ast.Attribute):
                    idents.add(node.attr)
                elif isinstance(node, ast.arg):
                    idents.add(node.arg)
            for ident in idents:
                with self.subTest(f.name, ident=ident):
                    self.assertFalse(any(b in ident.lower() for b in banned), ident)


class BriefFalsifiers(unittest.TestCase):
    def test_b_2_open_attach_close_cycles_change_nothing(self) -> None:
        driver = FakeDriver()
        a, b = driver.start_run(), driver.start_run()
        driver.api.call("run.stop", {"run_id": b})
        before = json.dumps(driver.registry.list(), sort_keys=True)
        calls = (driver.model_calls, len(driver.kill.requests), len(driver.kill.confirms))
        for _ in range(50):
            app = TuiApp(driver.api, clock=Clock(), environ={})
            app.open()
            app.attach(a)
            app.attach(b)
            app.press("r")
            app.close()
        self.assertEqual(json.dumps(driver.registry.list(), sort_keys=True), before)
        self.assertEqual((driver.model_calls, len(driver.kill.requests), len(driver.kill.confirms)), calls)


if __name__ == "__main__":
    unittest.main()
