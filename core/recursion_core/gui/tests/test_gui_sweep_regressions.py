"""Proposed GUI regressions for S2b; all transports and device doors are local fakes.

Run from the repository root with PYTHONPATH=core python -B <this file> -v.
These tests assert corrected behavior and are expected to fail on the reviewed commit.
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
            ("/read", "command=" + READS[0], None, (200, b'{"status_code":"\\ud800"}')),
            ("/", "", None, (200, b'{"routes":[{"class":"engine","engine":"codex","sign_in":"\\ud800"}]}')),
            ("/stop", "", {"run_id": "run-1", "confirm": "explicit"}, (200, b'{"state":"\\ud800"}')),
        ]
        for path, query, form, reply in replies:
            with self.subTest(path=path):
                status, headers, body = self.request(self.make_app(reply), path, query=query, form=form)
                self.assertIn(status[:3], ("200", "500", "502"))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("default-src 'none'", headers["Content-Security-Policy"])

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

    def test_f6_malformed_transport_status_is_a_control_error(self):
        for status in (None, "503"):
            with self.subTest(status=status):
                client = ControlClient(lambda *args, status=status: (status, b'{"error":"starting"}'))
                with self.assertRaises(ControlError) as caught:
                    client.routes()
                self.assertIn(caught.exception.code, ("control_api_response_invalid", "control_api_unavailable"))
                self.assertIsNone(caught.exception.refused)


if __name__ == "__main__":
    unittest.main()
