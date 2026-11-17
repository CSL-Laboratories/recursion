"""GUI page tests that use only local in-memory adapters.

Blank or extra fields are refused before dispatch, a lone surrogate stays inside the
response, and a venue switch opens only beside venue data the filter kept.

Run from the repository root:
PYTHONPATH=core python -B -m unittest recursion_core.gui.tests.test_s7_gui -v
"""
import io
import json
import unittest
from collections.abc import Mapping

from recursion_core.gui.client import ACTIONS, READS, ControlClient, ControlError
from recursion_core.gui.panels import REFUSALS, filter_venue_rows, panel_registry
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
            ({"state": "MARKER\ud800"}, 200, "POST", "/stop", b"run_id=run-1&confirm=explicit"),
            ({"status_code": "MARKER\ud800"}, 200, "GET", "/read", b""),
            ({"error": "MARKER\ud800"}, 503, "GET", "/read", b""),
            ({"routes": [{"id": "r", "class": "engine", "engine": "codex", "sign_in": "MARKER\ud800"}]},
             200, "GET", "/", b""),
        ):
            with self.subTest(path=path, payload=list(payload)):
                app, calls = make_app(payload, status=status)
                statuses, headers, page = request(app, method, path, body=body)
                self.assertEqual(statuses, ["500 Internal Server Error"])
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("Content-Security-Policy", headers)
                self.assertEqual(int(headers["Content-Length"]), len(page.encode("utf-8")))
                self.assertEqual(len(calls), 1)
                self.assertIn("does not know whether", page)
                self.assertIn("UnicodeEncodeError", page)
                self.assertNotIn("MARKER", page)
                self.assertNotIn("\ud800", page)

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
        self.assertIs(type(caught.exception.__cause__), OSError)
        self.assertEqual(caught.exception.__cause__.args, ("synthetic declaration read failure",))

    def test_f5_noninteger_transport_status_is_a_typed_invalid_response(self):
        for status in ("503", None, 503.0, True):
            with self.subTest(status_type=type(status).__name__):
                client = ControlClient(lambda *args: (status, b'{"error":"unavailable"}'))
                with self.assertRaises(ControlError) as caught:
                    client.routes()
                self.assertEqual(caught.exception.code, "control_api_response_invalid")
                self.assertIsNone(caught.exception.refused)

    def test_f1_a_sole_blank_run_id_is_refused_before_dispatch(self):
        # Mutant: drop `or v[0] == ""` in _form. A duplicate blank is already 400 by length.
        app, calls = make_app({"state": "pending"})
        statuses, _, page = request(app, "POST", "/stop", body=b"run_id=&confirm=explicit")
        self.assertEqual(statuses, ["400 Bad Request"])
        self.assertEqual(calls, [])
        self.assertIn("Bad request: fields", page)
        self.assertNotIn("did not answer", page)

    def test_f3_an_on_switch_stays_only_beside_venue_data_the_filter_kept(self):
        # Mutant M10 keeps the switch whenever it saw "on". Mutant M12 drops the switch
        # and does not record it as withheld. Both show up on the no-data shape.
        panel = {"id": "p", "title": "Paper", "reads": [READS[0]], "venue": "paper"}
        cases = {
            "no venue data": {"venue_data_switch": "on"},
            "other venue": {"venue_data_switch": "on",
                            "venue_data": {"rows": [{"venue": "kalshi", "symbol": "SECRET-ROW"}]}},
            "unlabelled scalar": {"venue_data_switch": "on", "venue_data": "SECRET-SCALAR"},
        }
        for name, payload in cases.items():
            with self.subTest(shape=name):
                kept, withheld = filter_venue_rows(payload, "paper")
                self.assertNotIn("venue_data_switch", kept)
                self.assertIs(withheld, True)
                self.assertNotIn("SECRET", repr(kept))
                if name == "no venue data":
                    self.assertEqual((kept, withheld), ({}, True))
                app, _ = make_app(payload, panels=[panel])
                statuses, _, page = request(app, path="/panel", query="id=p")
                self.assertEqual(statuses, ["200 OK"])
                self.assertIn("Withheld:", page)
                self.assertNotIn("venue_data_switch", page)
                self.assertNotIn("SECRET", page)

    def test_f3_a_string_subclass_does_not_open_the_venue_switch(self):
        # Mutant M08: isinstance(item, str) instead of type(item) is str.

        class ClaimsOn(str):
            def __eq__(self, other):
                return other == "on"

        payload = {"venue_data_switch": ClaimsOn("no"),
                   "venue_data": {"venue": "paper", "symbol": "VISIBLE-SAMPLE"}}
        kept, withheld = filter_venue_rows(payload, "paper")
        self.assertEqual(kept, {"venue_data": {"venue": "paper", "symbol": "VISIBLE-SAMPLE"}})
        self.assertIs(withheld, True)
        self.assertNotIn("venue_data_switch", kept)

    def test_f3_another_keys_on_does_not_open_a_switch_that_is_off_or_missing(self):
        # Mutant V07: drop `key == "venue_data_switch"`, so any unlabelled key whose
        # value is exactly "on" counts as the switch. The panel then shows venue_data
        # whose own switch is off or absent.
        panel = {"id": "p", "title": "Paper", "reads": [READS[0]], "venue": "paper"}
        owned = {"venue": "paper", "symbol": "X"}
        cases = {
            "switch off": {"venue_data_switch": "off", "status": "on",
                           "venue_data": {"rows": [owned]}},
            "switch missing": {"status": "on", "venue_data": {"rows": [dict(owned)]}},
        }
        for name, payload in cases.items():
            with self.subTest(shape=name):
                kept, _withheld = filter_venue_rows(payload, "paper")
                self.assertNotIn("venue_data_switch", kept)
                app, _calls = make_app(payload, panels=[panel])
                statuses, _, page = request(app, path="/panel", query="id=p")
                self.assertEqual(statuses, ["200 OK"])
                self.assertNotIn("X", page)
                self.assertIn("withheld: venue data switch is off", page)

    def test_f4_a_read_error_that_looks_like_a_refusal_is_the_first_refusal(self):
        # Mutant: tell a registry refusal from a read error by comparing text.

        class Sub(ValueError):
            pass

        seen = []

        def declarations():
            yield _RaisesOnTitle(ValueError("invalid panel id"))
            seen.append("later")
            yield {"id": "later", "title": "Later", "reads": [READS[0]]}

        with self.assertRaises(ValueError) as caught:
            panel_registry(declarations())
        self.assertEqual(seen, [])
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(caught.exception.args, (REFUSALS[0],))
        self.assertIs(type(caught.exception.__cause__), ValueError)
        self.assertEqual(caught.exception.__cause__.args, ("invalid panel id",))
        self.assertIsNone(caught.exception.__cause__.__cause__)

        with self.assertRaises(ValueError) as caught:
            panel_registry([_RaisesOnTitle(Sub("invalid panel id"))])
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(caught.exception.args, (REFUSALS[0],))
        self.assertIs(type(caught.exception.__cause__), Sub)
        self.assertEqual(caught.exception.__cause__.args, ("invalid panel id",))

        with self.assertRaises(ValueError) as caught:
            panel_registry([{"id": "has space", "title": "T", "reads": [READS[0]]}])
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(caught.exception.args, ("invalid panel id",))
        self.assertIsNone(caught.exception.__cause__)

    def test_f4_a_valueerror_while_reading_is_the_first_refusal_with_its_cause(self):
        # Mutant M5c re-raises the read error. Mutant M5d drops the cause (`from None`).
        for text in ("other", "boom"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as caught:
                    panel_registry([_RaisesOnTitle(ValueError(text))])
                self.assertIs(type(caught.exception), ValueError)
                self.assertEqual(caught.exception.args, (REFUSALS[0],))
                self.assertIs(type(caught.exception.__cause__), ValueError)
                self.assertEqual(caught.exception.__cause__.args, (text,))

        with self.assertRaises(ValueError) as caught:
            panel_registry([_RaisesOnTitle(ValueError())])
        self.assertEqual(caught.exception.args, (REFUSALS[0],))
        self.assertIs(type(caught.exception.__cause__), ValueError)
        self.assertEqual(caught.exception.__cause__.args, ())

    def test_f4_an_uncomparable_read_error_is_still_the_first_refusal(self):
        # Mutant: `exc.args[0] in REFUSALS` calls the argument's equality and that raise escapes.

        class Unequal:
            def __eq__(self, other):
                raise RuntimeError("comparison exploded")

        with self.assertRaises(ValueError) as caught:
            panel_registry([_RaisesOnTitle(ValueError(Unequal()))])
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(caught.exception.args, (REFUSALS[0],))
        self.assertIs(type(caught.exception.__cause__), ValueError)
        self.assertIsInstance(caught.exception.__cause__.args[0], Unequal)

    def test_f5_a_noninteger_status_is_invalid_before_the_body_is_read(self):
        # Mutant: judge the status only after json.loads, so a bad body hides it.
        for status in (None, "503"):
            with self.subTest(status=status):
                client = ControlClient(lambda *args, status=status: (status, b"not json"))
                with self.assertRaises(ControlError) as caught:
                    client.routes()
                self.assertEqual(caught.exception.code, "control_api_response_invalid")
                self.assertIsNone(caught.exception.refused)
        client = ControlClient(lambda *args: (200, b"not json"))
        with self.assertRaises(ControlError) as caught:
            client.routes()
        self.assertEqual(caught.exception.code, "control_api_unavailable")
        self.assertIsNone(caught.exception.refused)


class _RaisesOnTitle(Mapping):
    """A declaration whose title cannot be read. The other keys are ordinary."""

    def __init__(self, error):
        self._error = error

    def __iter__(self):
        return iter(("id", "title", "reads"))

    def __len__(self):
        return 3

    def __getitem__(self, key):
        if key == "title":
            raise self._error
        return {"id": "p", "reads": [READS[0]]}[key]


if __name__ == "__main__":
    unittest.main()
