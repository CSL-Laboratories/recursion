"""GUI page tests. Transports and device doors here are local fakes.

A lone surrogate gets a page answer, a venue switch stays beside its data, and an
unreadable panel mapping becomes a contract refusal.

Run from the repository root with PYTHONPATH=core python -B <this file> -v.
"""
import io
import json
import unittest
from collections.abc import Mapping
from urllib.parse import urlencode

from recursion_core.gui.client import ControlClient, ControlError, READS
from recursion_core.gui.panels import REFUSALS, panel_registry
from recursion_core.gui.surface import Application

HOST = "localhost:8765"
ORIGIN = "http://localhost:8765"


class GuiSweepRegressions(unittest.TestCase):
    def make_app(self, reply, **kwargs):
        self.calls = []
        def transport(method, path, body):
            self.calls.append(path)
            return reply
        return Application(ControlClient(transport), lambda env: True, {HOST}, {ORIGIN}, **kwargs)

    def request(self, app, path="/", *, query="", form=None):
        raw = urlencode(form).encode() if form else b""
        env = {"REQUEST_METHOD": "POST" if form else "GET", "PATH_INFO": path,
               "QUERY_STRING": query, "HTTP_HOST": HOST, "HTTP_ORIGIN": ORIGIN,
               "CONTENT_TYPE": "application/x-www-form-urlencoded", "CONTENT_LENGTH": str(len(raw)),
               "wsgi.input": io.BytesIO(raw)}
        statuses, headers = [], []
        body = b"".join(app(env, lambda s, h: (statuses.append(s), headers.extend(h))))
        self.assertEqual(len(statuses), 1)
        self.assertEqual(int(dict(headers)["Content-Length"]), len(body))
        return statuses[0], dict(headers), body.decode("utf-8")

    def test_f1_json_surrogates_get_an_encoded_response_with_page_headers(self):
        replies = [
            ("/read", "command=" + READS[0], None, (200, b'{"status_code":"MARKER\\ud800"}')),
            ("/", "", None, (200, b'{"routes":[{"class":"engine","engine":"codex","sign_in":"MARKER\\ud800"}]}')),
            ("/stop", "", {"run_id": "run-1", "confirm": "explicit"}, (200, b'{"state":"MARKER\\ud800"}')),
        ]
        for path, query, form, reply in replies:
            with self.subTest(path=path):
                status, headers, body = self.request(self.make_app(reply), path, query=query, form=form)
                self.assertEqual(status, "500 Internal Server Error")
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
                self.assertIn("does not know whether", body)
                self.assertIn("UnicodeEncodeError", body)
                self.assertNotIn("MARKER", body)

    def test_f2_enabled_switch_survives_venue_filtering(self):
        panel = {"id": "p", "title": "Paper", "reads": [READS[0]], "venue": "paper"}
        for switch in ("off", None, "on"):
            response = {"status_code": "ok", "result": {
                "venue_data": {"venue": "paper", "symbol": "ORDINARY-ROW"}}}
            if switch is not None:
                response["result"]["venue_data_switch"] = switch
            with self.subTest(switch=switch):
                app = self.make_app((200, json.dumps(response).encode()), panels=[panel])
                status, _, body = self.request(app, "/panel", query="id=p")
                self.assertEqual(status, "200 OK")
                if switch == "on":
                    self.assertIn("ORDINARY-ROW", body)
                    self.assertNotIn("venue data switch is off", body)
                else:
                    self.assertNotIn("ORDINARY-ROW", body)

    def test_f3_stop_daemon_refusal_is_not_called_a_missing_answer(self):
        app = self.make_app((409, b'{"error":"run_not_found"}'))
        _, _, body = self.request(app, "/stop", form={"run_id": "run-1", "confirm": "explicit"})
        self.assertEqual(self.calls, ["/v1/runs/stop"])
        self.assertIn("run_not_found", body)
        self.assertNotIn("The control API did not answer", body)
        self.assertIn("refused", body.lower())

    def test_f3_local_stop_validation_is_reported_before_sending(self):
        app = self.make_app((200, b'{}'))
        _, _, body = self.request(app, "/stop", form={"run_id": "bad id", "confirm": "explicit"})
        self.assertEqual(self.calls, [])
        self.assertIn("run_id_invalid", body)
        self.assertNotIn("The control API did not answer", body)
        self.assertNotIn("Stop state: unknown", body)

    def test_f4_unreadable_panel_mapping_has_a_contract_refusal(self):
        class UnreadablePanel(Mapping):
            def __iter__(self):
                return iter(("id", "title", "reads"))
            def __len__(self):
                return 3
            def __getitem__(self, key):
                if key == "title":
                    raise OSError("declaration source is unavailable")
                return {"id": "p", "reads": [READS[0]]}[key]
        with self.assertRaises(ValueError) as caught:
            panel_registry([UnreadablePanel()])
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(caught.exception.args, (REFUSALS[0],))
        self.assertIs(type(caught.exception.__cause__), OSError)
        self.assertEqual(caught.exception.__cause__.args, ("declaration source is unavailable",))

    def test_f5_installed_requirements_are_isolated_from_the_callers_mutations(self):
        required = {"venue_use": ["paper:own_trading"]}
        app = self.make_app((200, b'{"routes":[]}'), required=required)
        required["venue_use"].append("paper:model_fed_read")
        required["engine"] = ["plugin_engine"]
        status, _, body = self.request(app)
        self.assertEqual(status, "200 OK")
        self.assertIn("gap:venue_use:paper:own_trading", body)
        self.assertNotIn("gap:venue_use:paper:model_fed_read", body)
        self.assertNotIn("gap:engine:plugin_engine", body)

    def test_f6_malformed_transport_status_is_a_control_error(self):
        for status in (None, "503"):
            with self.subTest(status=status):
                client = ControlClient(lambda *args, status=status: (status, b'{"error":"starting"}'))
                with self.assertRaises(ControlError) as caught:
                    client.routes()
                self.assertIn(caught.exception.code, ("control_api_response_invalid", "control_api_unavailable"))
                self.assertIsNone(caught.exception.refused)

    def test_f3_module_notes_describe_tests_of_this_tree(self):
        # The notes used to say these tests fail on a reviewed commit and need a patch applied first.
        import recursion_core.gui.tests.test_s7_gui as s7

        notes = {"test_gui_sweep_regressions": __doc__, "test_s7_gui": s7.__doc__}
        for name, text in notes.items():
            self.assertIsInstance(text, str, name)
            self.assertNotIn("reviewed commit", text, name)
            self.assertNotIn("expected to fail", text, name)
            self.assertNotIn("accompanying patch", text, name)
            self.assertNotIn("S2b", text, name)
            self.assertIn("repository root", text, name)


if __name__ == "__main__":
    unittest.main()
