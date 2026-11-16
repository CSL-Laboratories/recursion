"""Proposed GUI regressions for sweep S2b; use only local in-memory adapters.

From the repository root, after applying the accompanying patch:
PYTHONPATH=core python -B -m unittest recursion_core.gui.tests.test_s7_gui -v
"""
import io
import json
import unittest
from collections.abc import Mapping

from recursion_core.gui.client import ACTIONS, READS, ControlClient, ControlError
from recursion_core.gui.panels import panel_registry
from recursion_core.gui.surface import Application

HOST = "localhost:8123"
ORIGIN = "http://localhost:8123"


def make_app(payload, *, status=200, panels=(), required=None):
    calls = []
    raw = json.dumps(payload).encode("ascii")

    def transport(method, path, body):
        calls.append((method, path, body))
        return status, raw

    app = Application(ControlClient(transport), lambda env: True, {HOST}, {ORIGIN},
                      panels=panels, required=required)
    return app, calls


def request(app, method="GET", path="/read", *, body=b"", query=None):
    env = {"REQUEST_METHOD": method, "PATH_INFO": path,
           "QUERY_STRING": query if query is not None else "command=" + READS[0],
           "HTTP_HOST": HOST, "HTTP_ORIGIN": ORIGIN, "HTTP_SEC_FETCH_SITE": "same-origin",
           "CONTENT_TYPE": "application/x-www-form-urlencoded", "CONTENT_LENGTH": str(len(body)),
           "wsgi.input": io.BytesIO(body)}
    statuses, headers = [], {}
    raw = b"".join(app(env, lambda status, pairs: (statuses.append(status), headers.update(pairs))))
    return statuses, headers, raw.decode("utf-8")


class SweepGuiRegressions(unittest.TestCase):
    def test_f1_empty_duplicate_or_extra_form_fields_are_refused_before_dispatch(self):
        for path, base, suffixes in (
            ("/stop", b"run_id=run-1&confirm=explicit", (b"&run_id=", b"&confirm=", b"&extra=")),
            ("/action", ("command=" + ACTIONS[0] + "&confirm=explicit").encode(),
             (b"&command=", b"&confirm=", b"&extra=")),
        ):
            for suffix in suffixes:
                with self.subTest(path=path, suffix=suffix):
                    app, calls = make_app({"state": "pending"})
                    statuses, _, _ = request(app, "POST", path, body=base + suffix)
                    self.assertEqual(statuses, ["400 Bad Request"])
                    self.assertEqual(calls, [])

    def test_f1_empty_query_fields_do_not_bypass_exactly_one_name_once(self):
        panel = {"id": "p", "title": "Panel", "reads": [READS[0]]}
        for path, base, suffix in (
            ("/read", "command=" + READS[0], "&command="),
            ("/read", "command=" + READS[0], "&extra="),
            ("/panel", "id=p", "&id="),
            ("/panel", "id=p", "&extra="),
        ):
            with self.subTest(path=path, suffix=suffix):
                app, calls = make_app({"status_code": "ok"}, panels=[panel])
                statuses, _, _ = request(app, path=path, query=base + suffix)
                self.assertEqual(statuses, ["400 Bad Request"])
                self.assertEqual(calls, [])

    def test_f2_unpaired_surrogates_never_escape_the_wsgi_response_boundary(self):
        for payload, status, method, path, body in (
            ({"state": "\ud800"}, 200, "POST", "/stop", b"run_id=run-1&confirm=explicit"),
            ({"status_code": "\ud800"}, 200, "GET", "/read", b""),
            ({"error": "\ud800"}, 503, "GET", "/read", b""),
            ({"routes": [{"id": "r", "class": "engine", "engine": "codex", "sign_in": "\ud800"}]},
             200, "GET", "/", b""),
        ):
            with self.subTest(path=path, payload=list(payload)):
                app, calls = make_app(payload, status=status)
                statuses, headers, page = request(app, method, path, body=body)
                self.assertEqual(len(statuses), 1)
                self.assertIn(statuses[0], ("200 OK", "500 Internal Server Error"))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("Content-Security-Policy", headers)
                self.assertEqual(int(headers["Content-Length"]), len(page.encode("utf-8")))
                self.assertEqual(len(calls), 1)
                if method == "POST" and statuses == ["500 Internal Server Error"]:
                    self.assertIn("does not know whether", page)

    def test_f3_venue_filter_preserves_an_on_gate_for_owned_data_in_an_envelope(self):
        panel = {"id": "p", "title": "Paper", "reads": [READS[0]], "venue": "paper"}
        for switch in ("on", "off"):
            with self.subTest(switch=switch):
                payload = {"status_code": "ok", "result": {"venue_data_switch": switch,
                           "venue_data": {"rows": [{"venue": "paper", "symbol": "VISIBLE-SAMPLE"}]}}}
                app, _ = make_app(payload, panels=[panel])
                statuses, _, page = request(app, path="/panel", query="id=p")
                self.assertEqual(statuses, ["200 OK"])
                if switch == "on":
                    self.assertTrue("VISIBLE-SAMPLE" in page, "ON data for the owned venue was lost")
                    self.assertFalse("withheld: venue data switch is off" in page, "an ON gate was labelled OFF")
                else:
                    self.assertNotIn("VISIBLE-SAMPLE", page)
                    self.assertIn("withheld: venue data switch is off", page)

    def test_f4_unreadable_mapping_declarations_use_the_frozen_refusal(self):
        class Unreadable(Mapping):
            def __iter__(self):
                return iter(("id", "title", "reads"))

            def __len__(self):
                return 3

            def __getitem__(self, key):
                raise OSError("synthetic declaration read failure")

        with self.assertRaises(ValueError) as caught:
            panel_registry([Unreadable()])
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(caught.exception.args, ("panel needs id, title and reads",))

    def test_f5_noninteger_transport_status_is_a_typed_invalid_response(self):
        for status in ("503", None, 503.0, True):
            with self.subTest(status_type=type(status).__name__):
                client = ControlClient(lambda *args: (status, b'{"error":"unavailable"}'))
                with self.assertRaises(ControlError) as caught:
                    client.routes()
                self.assertEqual(caught.exception.code, "control_api_response_invalid")
                self.assertIsNone(caught.exception.refused)

    def test_f6_constructor_requirements_are_a_snapshot_of_caller_configuration(self):
        required = {"engine": ["plugin_engine"]}
        app, _ = make_app({"routes": []}, required=required)
        required["engine"][:] = ["replacement_engine"]
        required["venue_use"] = ["paper:own_trading"]
        statuses, _, page = request(app, path="/", query="")
        self.assertEqual(statuses, ["200 OK"])
        self.assertTrue("gap:engine:plugin_engine" in page, "the original requirements were not retained")
        self.assertNotIn("gap:engine:replacement_engine", page)
        self.assertNotIn("gap:venue_use:paper:own_trading", page)

    def test_f7_local_stop_validation_is_reported_as_not_sent(self):
        app, calls = make_app({"state": "pending"})
        statuses, _, page = request(app, "POST", "/stop", body=b"run_id=bad+id&confirm=explicit")
        self.assertEqual(calls, [])
        self.assertTrue("not sent" in page.lower(), "local validation was labelled as no API reply")
        self.assertIn("run_id_invalid", page)
        self.assertNotIn("The control API did not answer", page)

    def test_f7_typed_stop_refusal_is_distinguished_from_no_reply(self):
        app, calls = make_app({"error": "run_not_found"}, status=404)
        statuses, _, page = request(app, "POST", "/stop", body=b"run_id=run-1&confirm=explicit")
        self.assertEqual(len(calls), 1)
        self.assertTrue("refused" in page.lower(), "a typed API refusal was labelled as no reply")
        self.assertIn("run_not_found", page)
        self.assertNotIn("The control API did not answer", page)


if __name__ == "__main__":
    unittest.main()
