"""Item 10 GUI: door, no order path, forwarded Stop, O7 parity, beta rubric, coverage, a11y.

Run: ``PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/gui/tests -t core -v``.
Everything goes through WSGI fakes; no socket is opened, no provider or venue is called.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

from recursion_core.gui.client import ACTIONS, COMMANDS, READS, ControlClient, ControlError, o7_command
from recursion_core.gui.inventory import BARS, REQUIRED, SIGN_IN_REJECTED, assess, coverage_gaps, project
from recursion_core.gui.surface import Application, _sign_in_cell, withhold_venue_data
from recursion_core.gui.tests.fakes import PARITY_MATRIX, FakeControlAPI, broken_transport, wsgi_transport

HOST = "127.0.0.1:8765"
ORIGIN = "http://127.0.0.1:8765"
GUI_DIR = Path(__file__).resolve().parents[1]


def _bars(**overrides) -> dict:
    bars = {bar: {"pass": True, "evidence": "fixture-evidence-" + bar} for bar in BARS}
    bars.update(overrides)
    return bars


def _route(rid: str, cls: str, maturity: str = "beta", bars=None, **extra) -> dict:
    return {"id": rid, "class": cls, "maturity": maturity, "bars": bars if bars is not None else {},
            "configured": True, "blocker": "", **extra}


# JSON values for sign_in other than a string. Numbers, bools, lists and dicts include a
# falsy value: the Sign-in cell used to treat that as missing and to crash on the others.
_SIGN_IN_NON_STRINGS = (
    ("number", 0),
    ("number", 2),
    ("number", 1.5),
    ("bool", True),
    ("bool", False),
    ("none", None),
    ("list", ["chatgpt"]),
    ("list", []),
    ("dict", {"provider": "chatgpt"}),
    ("dict", {}),
)


def _sign_in_column(body: str, route_id: str) -> str:
    row = re.search(r'<tr><th scope="row">%s</th>.*?</tr>' % re.escape(route_id), body)
    if row is None:
        raise AssertionError("no route row for %s" % route_id)
    return re.findall(r"<td>(.*?)</td>", row.group(0))[2]


class _Door:
    def __init__(self, ok: bool = True) -> None:
        self.ok, self.seen = ok, 0

    def __call__(self, env: dict) -> bool:
        self.seen += 1
        return self.ok


class GuiHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeControlAPI(routes=[_route("engine:codex:chatgpt", "engine", engine="codex", sign_in="chatgpt")])
        self.door = _Door()
        self.app = Application(ControlClient(wsgi_transport(self.api)), self.door,
                               frozenset({HOST}), frozenset({ORIGIN}))

    def request(self, method: str, path: str, form: dict | list | None = None, *, query: str = "",
                headers: dict | None = None, raw: bytes | None = None) -> tuple[str, dict, str]:
        body = raw if raw is not None else (urlencode(form, doseq=True).encode() if form is not None else b"")
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "HTTP_HOST": HOST,
               "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": "application/x-www-form-urlencoded",
               "wsgi.input": io.BytesIO(body), "wsgi.errors": sys.stderr}
        if method == "POST":
            env["HTTP_ORIGIN"] = ORIGIN
            env["HTTP_SEC_FETCH_SITE"] = "same-origin"
        for key, value in (headers or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        status, hdrs = [], {}
        out = b"".join(self.app(env, lambda s, h: (status.append(s), hdrs.update(h))))
        return status[0], hdrs, out.decode("utf-8")


class DoorTests(GuiHarness):
    def test_get_without_origin_is_a_normal_navigation(self) -> None:
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.door.seen, 1)

    def test_refusals(self) -> None:
        cases = [
            ("GET", {"HTTP_HOST": "evil.example"}, "403", "host_not_allowed"),
            ("GET", {"HTTP_ORIGIN": "http://evil.example"}, "403", "origin_not_allowed"),
            ("GET", {"HTTP_ORIGIN": "null"}, "403", "origin_not_allowed"),
            ("POST", {"HTTP_ORIGIN": ORIGIN + ", http://evil.example"}, "403", "origin_not_allowed"),
            ("POST", {"HTTP_ORIGIN": None}, "403", "origin_required"),
            ("POST", {"HTTP_SEC_FETCH_SITE": "cross-site"}, "403", "cross_site_request"),
            ("GET", {"HTTP_SEC_FETCH_SITE": "same-site"}, "403", "cross_site_request"),
        ]
        for method, headers, status, code in cases:
            with self.subTest(headers=headers):
                path = "/" if method == "GET" else "/stop"
                form = None if method == "GET" else {"run_id": "run-1", "confirm": "explicit"}
                got, _, body = self.request(method, path, form, headers=headers)
                self.assertTrue(got.startswith(status), got)
                self.assertIn(code, body)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.door.seen, 0)  # refused before the device check

    def test_device_door_belongs_to_the_core_adapter(self) -> None:
        self.door.ok = False
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, "401 Unauthorized")
        self.assertIn("device_not_authenticated", body)
        self.assertEqual(self.api.calls, [])
        source = (GUI_DIR / "surface.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"HTTP_AUTHORIZATION|HTTP_COOKIE|token\s*=")

    def test_no_socket_binding_or_deploy_code(self) -> None:
        for path in GUI_DIR.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotRegex(text, r"^\s*(import|from)\s+(socket|wsgiref|http\.server|socketserver)",)
                self.assertNotIn("0.0.0.0", text)


class NoOrderPathTests(GuiHarness):
    def test_command_set_is_the_o7_contract(self) -> None:
        matrix = json.loads(PARITY_MATRIX.read_text(encoding="utf-8"))
        self.assertEqual(sorted(COMMANDS), sorted(row["command"] for row in matrix["rows"]))
        self.assertEqual((len(READS), len(ACTIONS)), (11, 3))
        self.assertTrue(all(row["order_path"] == "none" for row in matrix["rows"]))

    def test_disallowed_orders_and_commands_refused_before_the_api(self) -> None:
        for command in ("operator.orders.submit", "place_order", "operator.approve", "submit", ""):
            with self.subTest(command=command):
                status, _, _ = self.request("POST", "/action", {"command": command, "confirm": "explicit"})
                self.assertEqual(status, "400 Bad Request")
                self.assertRaises(ControlError, o7_command, command, "gui", True)
        for path in ("/order", "/orders", "/approve", "/submit"):
            self.assertEqual(self.request("POST", path, {"x": "1"})[0], "404 Not Found")
        status, _, _ = self.request("GET", "/read", query="command=operator.interventions.request_stop")
        self.assertEqual(status, "400 Bad Request")  # actions are never a GET
        self.assertEqual([c for c in self.api.calls if c[1] != "/v1/routes"], [])

    def test_gui_imports_no_order_or_paper_code(self) -> None:
        for path in GUI_DIR.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path.relative_to(GUI_DIR))):
                self.assertNotRegex(text, r"(import|from)\s+\S*(paper|gateway|broker|order)")

    def test_interventions_need_explicit_confirmation(self) -> None:
        for form in ({"command": ACTIONS[0]}, {"command": ACTIONS[0], "confirm": "yes"}):
            with self.subTest(form=form):
                self.assertEqual(self.request("POST", "/action", form)[0], "400 Bad Request")
        status, _, body = self.request("POST", "/action", {"command": ACTIONS[0], "confirm": "explicit"})
        self.assertEqual(status, "200 OK")
        self.assertIn("<strong>ok</strong>", body)
        sent = self.api.calls[-1][2]
        self.assertEqual((sent["surface"], sent["confirmation"], sent["arguments"]),
                         ("gui", "explicit", {"scope": "operator_session"}))

    def test_strict_form_parsing(self) -> None:
        cases = [
            dict(form=[("run_id", "a"), ("run_id", "b"), ("confirm", "explicit")]),
            dict(form={"run_id": "a", "confirm": "explicit", "extra": "1"}),
            dict(form={"run_id": "a", "confirm": "explicit"}, headers={"CONTENT_TYPE": "text/plain"}),
            dict(raw=b"run_id=" + b"a" * 5000 + b"&confirm=explicit"),
            dict(raw=b"%%%"),
        ]
        for case in cases:
            with self.subTest(case=str(case)[:60]):
                self.assertEqual(self.request("POST", "/stop", **case)[0], "400 Bad Request")
        self.assertEqual([c for c in self.api.calls if c[1] == "/v1/runs/stop"], [])


class StopTests(GuiHarness):
    def test_stop_forwards_and_shows_pending_unchanged(self) -> None:
        status, _, body = self.request("POST", "/stop", {"run_id": "run-42", "confirm": "explicit"})
        self.assertEqual(status, "200 OK")
        self.assertEqual(self.api.calls[-1], ("POST", "/v1/runs/stop", {"run_id": "run-42"}))
        self.assertIn("Stop state reported by the control API: <strong>pending</strong>", body)
        main = body.split("<main", 1)[1].split("</main>", 1)[0].lower()
        for word in ("terminated", "stopped", "killed", "confirmed"):
            self.assertNotIn(word, main)

    def test_stop_state_is_never_invented(self) -> None:
        self.api.stop_result = {"detail": "accepted"}
        body = self.request("POST", "/stop", {"run_id": "run-1", "confirm": "explicit"})[2]
        self.assertIn("not reported by the control API (unknown)", body)
        self.api.stop_result = {"state": "terminated", "exit_observed": True}
        body = self.request("POST", "/stop", {"run_id": "run-1", "confirm": "explicit"})[2]
        self.assertIn("<strong>terminated</strong>", body)  # shown only because the API said so
        down = Application(ControlClient(broken_transport), _Door(), frozenset({HOST}), frozenset({ORIGIN}))
        self.app = down
        body = self.request("POST", "/stop", {"run_id": "run-1", "confirm": "explicit"})[2]
        self.assertIn("Stop state: unknown", body)
        self.assertNotIn("terminated", body.lower())

    def test_stop_needs_confirmation_and_a_valid_run_id(self) -> None:
        self.assertEqual(self.request("POST", "/stop", {"run_id": "run-1", "confirm": "no"})[0], "400 Bad Request")
        body = self.request("POST", "/stop", {"run_id": "bad id;", "confirm": "explicit"})[2]
        self.assertIn("run_id_invalid", body)
        self.assertEqual([c for c in self.api.calls if c[1] == "/v1/runs/stop"], [])

    def test_a_stop_whose_body_ends_before_its_content_length_is_refused(self) -> None:
        # The part that arrived can be a well-formed Stop of another run: "...run_id=run-12" here.
        whole = b"confirm=explicit&run_id=run-1234567890"
        status, _, _ = self.request("POST", "/stop", raw=whole[:-8], headers={"CONTENT_LENGTH": str(len(whole))})
        self.assertEqual(status, "400 Bad Request")
        self.assertEqual([c for c in self.api.calls if c[1] == "/v1/runs/stop"], [])
        # The whole body, for contrast: the same request is a Stop of the run it names.
        status, _, _ = self.request("POST", "/stop", raw=whole)
        self.assertEqual(status, "200 OK")
        self.assertEqual(self.api.calls[-1], ("POST", "/v1/runs/stop", {"run_id": "run-1234567890"}))


class ParityTests(GuiHarness):
    _SEMANTIC = ("api_version", "profile", "enforcement_route", "authority_effect", "order_path", "status_code",
                 "visibility", "result", "errors", "command")

    def test_gui_and_cli_get_the_same_semantics_for_every_command(self) -> None:
        transport = wsgi_transport(self.api)
        gui = ControlClient(transport)
        matrix = json.loads(PARITY_MATRIX.read_text(encoding="utf-8"))
        for row in matrix["rows"]:
            command = row["command"]
            with self.subTest(command=command):
                confirmed = command in ACTIONS
                status, raw = transport("POST", "/v1/operator", json.dumps(
                    o7_command(command, "cli", confirmed)).encode())
                cli = json.loads(raw)
                g = gui.operator(command, confirmed=confirmed)
                self.assertEqual((status, cli["surface"], g["surface"]), (200, "cli", "gui"))
                self.assertEqual({k: cli.get(k) for k in self._SEMANTIC}, {k: g.get(k) for k in self._SEMANTIC})
                for field in row["must_show"]:
                    self.assertIn(field, json.dumps(g))
                if command in READS:
                    body = self.request("GET", "/read", query=urlencode({"command": command}))[2]
                    self.assertIn("<strong>%s</strong>" % cli["status_code"], body)


class RubricTests(unittest.TestCase):
    def test_any_single_failing_or_missing_bar_gives_beta(self) -> None:
        self.assertEqual(assess(_route("r", "engine", "mature", _bars()))["maturity"], "mature")
        for bar in BARS:
            for broken in ({"pass": False, "evidence": "x"}, {"pass": True, "evidence": ""},
                           {"pass": True}, {"pass": "true", "evidence": "x"}, None):
                with self.subTest(bar=bar, broken=broken):
                    bars = _bars(**{bar: broken}) if broken is not None else {
                        k: v for k, v in _bars().items() if k != bar}
                    row = assess(_route("r", "engine", "mature", bars))
                    self.assertEqual((row["maturity"], row["claim"]), ("beta", "mature_claim_rejected"))
                    self.assertEqual(row["failed_bars"], [bar])

    def test_missing_evidence_or_maturity_field_is_beta(self) -> None:
        self.assertEqual(assess(_route("r", "engine", "beta", {}))["failed_bars"], list(BARS))
        record = _route("r", "engine", bars=_bars())
        del record["maturity"]
        self.assertEqual((assess(record)["maturity"], assess(record)["claim"]), ("beta", "missing_maturity"))
        self.assertEqual(assess("not a record")["maturity"], "beta")
        self.assertEqual(assess(_route("r", "engine", "MATURE", _bars()))["maturity"], "beta")

    def test_o50_beta_tag_keeps_a_passing_route_beta(self) -> None:
        tagged = assess(_route("r", "engine", "beta", _bars()))
        self.assertEqual((tagged["maturity"], tagged["claim"], tagged["beta_tag"]), ("beta", "ok", True))
        self.assertEqual(tagged["failed_bars"], [])
        grant = assess(_route("r", "engine", "mature", _bars(), beta=True))  # R2a grant: floor unset
        self.assertEqual((grant["maturity"], grant["claim"], grant["beta_tag"]), ("beta", "ok", True))
        for bad in ("true", 1, None, "beta"):
            with self.subTest(beta=bad):
                row = assess(_route("r", "engine", "mature", _bars(), beta=bad))
                self.assertEqual((row["maturity"], row["claim"]), ("beta", "beta_tag_invalid"))
        clean = assess(_route("r", "engine", "mature", _bars(), beta=False))
        self.assertEqual((clean["maturity"], clean["beta_tag"]), ("mature", False))

    def test_every_projection_carries_maturity(self) -> None:
        rows = [assess(_route("engine:%d" % i, "engine", m, b, engine="codex"))
                for i, (m, b) in enumerate([("beta", {}), ("mature", _bars()), ("mature", {})])]
        rows += coverage_gaps([])
        for row in rows:
            with self.subTest(row=row["id"]):
                self.assertIn("maturity=" + row["maturity"], project(row, "cli"))
                self.assertEqual(project(row, "mcp")["maturity"], row["maturity"])
                self.assertEqual(project(row, "gui"), row["maturity"].upper())

    def test_coverage_gaps_cover_every_required_class(self) -> None:
        gaps = coverage_gaps([])
        self.assertEqual(len(gaps), sum(len(v) for v in REQUIRED.values()))
        self.assertEqual(len(REQUIRED["engine"]), 9)
        self.assertEqual(REQUIRED["mcp"], ("2025-06-18", "2026-07-28"))
        self.assertTrue(all(g["maturity"] == "beta" and g["blocker"].startswith("upstream") for g in gaps))
        self.assertNotIn("venue_use", REQUIRED)  # venue uses are the paper-trading plugin's
        reported = [_route("e", "engine", engine="codex"), _route("m", "mcp", mcp_version="2025-06-18"),
                    _route("v", "venue_use", venue="kalshi", use="model_fed_read")]
        ids = {g["id"] for g in coverage_gaps(reported)}
        self.assertNotIn("gap:engine:codex", ids)
        self.assertNotIn("gap:mcp:2025-06-18", ids)
        self.assertIn("gap:mcp:2026-07-28", ids)
        plugin = {"venue_use": ("kalshi:model_fed_read", "kalshi:model_fed_propose")}
        self.assertEqual({g["id"] for g in coverage_gaps(reported, plugin)},
                         ids | {"gap:venue_use:kalshi:model_fed_propose"})

    def test_non_string_sign_in_is_rejected_by_assess(self) -> None:
        # The package notes do not say a non-string sign_in is malformed. It is not
        # coerced, and the rest of the record is still assessed.
        self.assertEqual(SIGN_IN_REJECTED, "rejected: not a string")
        missing = assess(_route("engine:sign:absent", "engine", "mature", _bars(), engine="codex"))
        self.assertEqual(missing["sign_in"], "")
        blank = assess(_route("engine:sign:blank", "engine", "mature", _bars(), engine="codex", sign_in=""))
        self.assertEqual(blank["sign_in"], "")
        text = assess(_route("engine:sign:text", "engine", "mature", _bars(), engine="codex",
                             sign_in="chat<gpt>"))
        self.assertEqual(text["sign_in"], "chat<gpt>")
        self.assertEqual((text["maturity"], text["claim"]), ("mature", "ok"))
        for kind, value in _SIGN_IN_NON_STRINGS:
            with self.subTest(path="assess", kind=kind, value=value):
                row = assess(_route("engine:sign:bad", "engine", "mature", _bars(), engine="codex",
                                    sign_in=value))
                self.assertEqual(row["sign_in"], "rejected: not a string")
                self.assertNotEqual(row["sign_in"], str(value))
                self.assertEqual((row["maturity"], row["claim"], row["class"]), ("mature", "ok", "engine"))

    def test_non_string_sign_in_is_rejected_by_the_cell(self) -> None:
        # escape(value or "-") crashes on a truthy non-string and shows a falsy one as
        # missing. assess() used to hide that by calling str() first. Both are rejected.
        self.assertEqual(_sign_in_cell(""), "-")
        self.assertEqual(_sign_in_cell("chat<gpt>"), "chat&lt;gpt&gt;")
        for kind, value in _SIGN_IN_NON_STRINGS:
            with self.subTest(path="cell", kind=kind, value=value):
                shown = _sign_in_cell(value)
                self.assertEqual(shown, "rejected: not a string")
                self.assertNotEqual(shown, str(value))
                self.assertNotEqual(shown, "-")


class PageTests(GuiHarness):
    def test_routes_shown_with_beta_badges_and_gaps(self) -> None:
        self.api.routes = [_route("engine:codex:chatgpt", "engine", "mature", _bars(xf_verify={"pass": False}),
                                  engine="codex", sign_in="chatgpt"),
                           _route("engine:claude_code:subscription", "engine", "mature", _bars(),
                                  engine="claude_code", sign_in="subscription")]
        body = self.request("GET", "/")[2]
        self.assertIn("mature_claim_rejected", body)
        self.assertIn("gap:engine:hermes", body)
        self.assertIn("upstream: control API did not report this route", body)
        self.assertEqual(body.count('<span class="badge">MATURE</span>'), 1)
        self.assertEqual(body.count('<span class="badge">BETA</span>'),
                         1 + sum(len(v) for v in REQUIRED.values()) - 2)  # two engines reported

    def test_o50_beta_tag_is_shown_on_the_route_badge(self) -> None:
        self.api.routes = [_route("engine:codex:chatgpt", "engine", "mature", _bars(), engine="codex",
                                  sign_in="chatgpt", beta=True),
                           _route("engine:claude_code:subscription", "engine", "mature", _bars(),
                                  engine="claude_code", sign_in="subscription", beta=False)]
        body = self.request("GET", "/")[2]
        row = re.search(r"<tr><th scope=\"row\">engine:codex:chatgpt</th>.*?</tr>", body).group(0)
        self.assertIn('<span class="badge">BETA</span> (beta tag)', row)
        row = re.search(r"<tr><th scope=\"row\">engine:claude_code:subscription</th>.*?</tr>", body).group(0)
        self.assertIn('<span class="badge">MATURE</span>', row)
        self.assertNotIn("(beta tag)", row)

    def test_plugin_supplied_required_classes(self) -> None:
        self.app = Application(ControlClient(wsgi_transport(self.api)), _Door(), frozenset({HOST}),
                               frozenset({ORIGIN}), required={"venue_use": ("kalshi:model_fed_read",)})
        body = self.request("GET", "/")[2]
        self.assertIn("gap:venue_use:kalshi:model_fed_read", body)
        self.assertIn("gap:engine:hermes", body)

    def test_routes_unavailable_is_reported(self) -> None:
        self.app = Application(ControlClient(broken_transport), _Door(), frozenset({HOST}), frozenset({ORIGIN}))
        body = self.request("GET", "/")[2]
        self.assertIn("Route inventory unavailable: control_api_unavailable", body)
        self.assertNotIn("MATURE</span>", body)

    def test_route_outage_is_not_reported_as_missing_inventory(self) -> None:
        # Devin r4505409118: a failed GET /v1/routes must not list required routes as gaps.
        outages = {"transport": broken_transport,
                   "error_status": lambda m, p, b: (503, b'{"error": "starting"}'),
                   "bad_json": lambda m, p, b: (200, b"not json"),
                   "routes_not_a_list": lambda m, p, b: (200, b'{"routes": {}}')}
        for name, transport in outages.items():
            with self.subTest(outage=name):
                self.app = Application(ControlClient(transport), _Door(), frozenset({HOST}), frozenset({ORIGIN}))
                status, _, body = self.request("GET", "/")
                self.assertEqual(status, "200 OK")
                self.assertIn("Route inventory unavailable:", body)
                self.assertIn("Route coverage is unknown", body)
                self.assertNotIn("gap:", body)
                self.assertNotIn("did not report this route", body)
                self.assertNotIn('<span class="badge">', body)

    def test_malformed_route_records_render_as_flagged_rows(self) -> None:
        # Devin r4505409084: malformed /v1/routes records must never crash the overview.
        good = _route("engine:codex:chatgpt", "engine", "mature", _bars(), engine="codex", sign_in="chatgpt")
        malformed = [None, 7, "engine", [], {}, {"id": "no-class"}, {"id": "list-class", "class": []},
                     {"id": "dict-class", "class": {"a": 1}}, _route("engine:list", "engine", engine=[]),
                     _route("mcp:dict", "mcp", mcp_version={"v": 1}),
                     _route("venue:bad", "venue_use", venue="kalshi", use=None)]
        for record in malformed:
            with self.subTest(record=record):
                row = assess(record)
                self.assertEqual((row["maturity"], row["claim"], row["class"]),
                                 ("beta", "malformed_route_record", "invalid"))
                for key in ("id", "subject", "sign_in", "configured", "beta_tag", "failed_bars", "blocker"):
                    self.assertIn(key, row)
                self.assertEqual(project(row, "gui"), "BETA")
        # Malformed records cover nothing, so they never hide a required-route gap.
        self.assertEqual(coverage_gaps(malformed), coverage_gaps([]))
        self.assertEqual(assess(good)["maturity"], "mature")  # well-formed records are unaffected
        self.assertEqual(assess(_route("engine:no-subject", "engine"))["claim"], "ok")  # missing is not malformed
        self.api.routes = [good] + malformed
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, "200 OK")
        self.assertEqual(body.count("(malformed_route_record)"), len(malformed))
        self.assertIn("malformed route record from the control API", body)
        self.assertEqual(body.count('<span class="badge">MATURE</span>'), 1)
        self.assertIn("gap:engine:hermes", body)
        self.assertNotIn("gap:engine:codex", body)

    def test_non_string_sign_in_is_rejected_on_the_overview(self) -> None:
        # Through the control-API JSON read and the route table. A string is escaped
        # for HTML; that is the cell builder, not a claim about script injection.
        self.api.routes = [
            _route("engine:sign:text", "engine", "mature", _bars(), engine="codex", sign_in="chat<gpt>"),
            _route("engine:sign:blank", "engine", "mature", _bars(), engine="opencode", sign_in=""),
        ]
        for kind, value in _SIGN_IN_NON_STRINGS:
            self.api.routes.append(_route("engine:sign:%s:%s" % (kind, _SIGN_IN_NON_STRINGS.index((kind, value))),
                                          "engine", "mature", _bars(), engine="codex", sign_in=value))
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, "200 OK")
        self.assertEqual(_sign_in_column(body, "engine:sign:text"), "chat&lt;gpt&gt;")
        self.assertNotIn("<", _sign_in_column(body, "engine:sign:text"))
        self.assertEqual(_sign_in_column(body, "engine:sign:blank"), "-")
        for kind, value in _SIGN_IN_NON_STRINGS:
            route_id = "engine:sign:%s:%s" % (kind, _SIGN_IN_NON_STRINGS.index((kind, value)))
            with self.subTest(path="overview", kind=kind, value=value):
                cell = _sign_in_column(body, route_id)
                self.assertEqual(cell, "rejected: not a string")
                self.assertNotIn(str(value), cell)
                row = re.search(r'<tr><th scope="row">%s</th>.*?</tr>' % re.escape(route_id), body).group(0)
                self.assertIn(">MATURE</span>", row)
                self.assertNotIn("malformed_route_record", row)

    def test_lost_intervention_reply_is_uncertain_not_unsent(self) -> None:
        # Devin r4505409056: a sent intervention with no readable reply has an unknown outcome.
        sent = []

        def after_sending(reply):
            def transport(method, path, body):
                sent.append(path)
                return reply()
            return transport

        def timeout():
            raise TimeoutError("reply lost")
        failures = {"timeout": timeout,
                    "connection_reset": lambda: (_ for _ in ()).throw(ConnectionResetError("closed")),
                    "malformed_json": lambda: (200, b"{truncated"),
                    "non_object_reply": lambda: (200, b"[]"),
                    "error_status": lambda: (502, b'{"error": "upstream"}')}
        for name, reply in failures.items():
            with self.subTest(failure=name):
                sent.clear()
                self.app = Application(ControlClient(after_sending(reply)), _Door(), frozenset({HOST}),
                                       frozenset({ORIGIN}))
                status, _, body = self.request("POST", "/action", {"command": ACTIONS[2], "confirm": "explicit"})
                self.assertEqual((status, sent), ("200 OK", ["/v1/operator"]))
                self.assertIn("Intervention outcome: unknown", body)
                self.assertIn("Check the run and event log before retrying", body)
                self.assertNotIn("not sent", body.lower())

    def test_typed_daemon_refusal_of_an_intervention_says_not_sent(self) -> None:
        # ADV2-9: the control API answered with its own refusal before dispatching (4xx, 503).
        reason = 'unavailable: <b>"x"</b> & more'
        for status in (400, 404, 409, 503):
            with self.subTest(status=status):
                reply = (status, json.dumps({"error": reason}).encode())
                self.app = Application(ControlClient(lambda *a, reply=reply: reply), _Door(), frozenset({HOST}),
                                       frozenset({ORIGIN}))
                body = self.request("POST", "/action", {"command": ACTIONS[0], "confirm": "explicit"})[2]
                self.assertIn('<p role="status">Not sent: refused by the daemon '
                              "(unavailable: &lt;b&gt;&quot;x&quot;&lt;/b&gt; &amp; more); "
                              "run Stop is available</p>", body)
                self.assertNotIn("<b>", body)
                self.assertNotIn("outcome: unknown", body)
                self.assertNotIn("may have reached", body)

    def test_answers_that_are_not_typed_refusals_stay_outcome_unknown(self) -> None:
        # After a dispatch (502, 504), a server error, or a refusal status with no error string.
        replies = {"500": (500, b'{"error": "internal"}'), "502": (502, b'{"error": "plugin failed"}'),
                   "504": (504, b'{"error": "timed out"}'), "503_no_error": (503, b"{}"),
                   "503_non_string": (503, b'{"error": 7}'), "400_not_object": (400, b"[]")}
        for name, reply in replies.items():
            with self.subTest(reply=name):
                self.app = Application(ControlClient(lambda *a, reply=reply: reply), _Door(), frozenset({HOST}),
                                       frozenset({ORIGIN}))
                body = self.request("POST", "/action", {"command": ACTIONS[0], "confirm": "explicit"})[2]
                self.assertIn("Intervention outcome: unknown", body)
                self.assertIn("may have reached", body)
                self.assertNotIn("refused by the daemon", body)

    def test_intervention_refused_before_sending_says_not_sent(self) -> None:
        class RefusingClient(ControlClient):
            def operator(self, command, confirmed=False):
                raise ControlError("confirmation_required")  # as o7_command does, before any transport call
        self.app = Application(RefusingClient(broken_transport), _Door(), frozenset({HOST}), frozenset({ORIGIN}))
        body = self.request("POST", "/action", {"command": ACTIONS[0], "confirm": "explicit"})[2]
        self.assertIn("Request not sent: confirmation_required", body)
        self.assertNotIn("outcome: unknown", body)

    def test_venue_data_withheld_while_switch_off(self) -> None:
        secret_number = "4242.17"
        self.api.extra["operator.evidence.inspect"] = {"result": {
            **self.api.responses["operator.evidence.inspect"]["result"],
            "provenance": {"venue_data": {"kalshi_yes_price": secret_number}, "venue_data_switch": "off"}}}
        body = self.request("GET", "/read", query="command=operator.evidence.inspect")[2]
        self.assertNotIn(secret_number, body)
        self.assertIn("withheld: venue data switch is off", body)
        self.assertEqual(withhold_venue_data({"venue_data": 1})["venue_data"], "[withheld: venue data switch is off]")
        self.assertEqual(withhold_venue_data({"venue_data": 1, "venue_data_switch": "on"})["venue_data"], 1)
        self.assertEqual(withhold_venue_data([{"venue_data": 1, "venue_data_switch": True}])[0]["venue_data"],
                         "[withheld: venue data switch is off]")

    def test_semantic_landmarks_labels_and_keyboard(self) -> None:
        status, headers, body = self.request("GET", "/")
        audit = _Audit()
        audit.feed(body)
        self.assertEqual(audit.lang, "en")
        for tag in ("header", "nav", "main", "footer"):
            self.assertEqual(audit.counts.get(tag), 1, tag)
        self.assertTrue(audit.skip_link)
        self.assertEqual(audit.counts.get("h1"), 1)
        unlabeled = [i for i in audit.inputs if i.get("type") != "hidden" and i.get("id") not in audit.label_for]
        self.assertEqual(unlabeled, [])
        self.assertTrue(all(text.strip() for text in audit.button_text))
        self.assertEqual(audit.counts.get("script", 0), 0)
        self.assertNotIn("tabindex=\"1", body)  # no positive tabindex; DOM order is the keyboard order
        style = re.search(r"<style>(.*?)</style>", body, re.S).group(1)
        digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
        self.assertIn("'sha256-%s'" % digest, headers["Content-Security-Policy"])
        self.assertIn("min-height:24px", style)
        self.assertIn(":focus{outline", style)


class _Audit(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.counts: dict[str, int] = {}
        self.inputs: list[dict] = []
        self.label_for: set = set()
        self.button_text: list[str] = []
        self.lang = None
        self.skip_link = False
        self._in_button = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.counts[tag] = self.counts.get(tag, 0) + 1
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "input":
            self.inputs.append(a)
        if tag == "label" and a.get("for"):
            self.label_for.add(a["for"])
        if tag == "a" and a.get("href") == "#main":
            self.skip_link = True
        if tag == "button":
            self._in_button = True
            self.button_text.append("")

    def handle_endtag(self, tag):
        if tag == "button":
            self._in_button = False

    def handle_data(self, data):
        if self._in_button:
            self.button_text[-1] += data


def _directives(header: str) -> dict:
    out = {}
    for part in header.split(";"):
        words = part.split()
        if words:
            out[words[0].lower()] = words[1:]
    return out


class StrictDoorPins(GuiHarness):
    """Two door rules no earlier test held (first review of #384, GPT-6.1 Sol, R1)."""

    ROUTES = (
        ("GET", "/", None, ""),
        ("GET", "/read", None, "command=operator.positions.inspect"),
        ("GET", "/panel", None, "id=missing"),
        ("POST", "/action", {"command": ACTIONS[0], "confirm": "explicit"}, ""),
        ("POST", "/stop", {"run_id": "run-1", "confirm": "explicit"}, ""),
        ("GET", "/v1/control", None, ""),
        ("POST", "/v1/control", {"method": "run.list"}, ""),
    )

    def test_only_the_value_true_admits_a_device(self) -> None:
        # The core adapter's answer is taken only when it is the value True: a truthy number, text,
        # list or principal-shaped mapping is not an admission, on every path, known or not.
        for verdict in (False, None, 0, 1, "yes", [True], {"kind": "device", "id": "page"}):
            for method, path, form, query in self.ROUTES:
                with self.subTest(verdict=verdict, method=method, path=path):
                    self.door.ok = verdict
                    status, _, body = self.request(method, path, form, query=query)
                    self.assertEqual(status, "401 Unauthorized")
                    self.assertIn("device_not_authenticated", body)
        self.assertEqual(self.api.calls, [])
        self.door.ok = True
        self.assertEqual(self.request("GET", "/")[0], "200 OK")

    def test_every_response_class_denies_scripts_and_allows_only_the_page_stylesheet(self) -> None:
        page = self.request("GET", "/")[2]
        style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
        style_source = "'sha256-%s'" % base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
        door_says_no = _Door(False)
        cases = {
            "200 OK": lambda: self.request("GET", "/"),
            "400 Bad Request": lambda: self.request("GET", "/read", query="command=operator.orders.submit"),
            "403 Forbidden": lambda: self.request("GET", "/", headers={"HTTP_HOST": "evil.example"}),
            "404 Not Found": lambda: self.request("GET", "/nowhere"),
            "405 Method Not Allowed": lambda: self.request("POST", "/panel", {"id": "x"}),
        }
        responses = {status: make() for status, make in cases.items()}
        self.app._door = door_says_no
        responses["401 Unauthorized"] = self.request("GET", "/")
        for expected, (status, headers, body) in responses.items():
            with self.subTest(status=expected):
                self.assertEqual(status, expected)
                policy = _directives(headers["Content-Security-Policy"])
                self.assertEqual(policy["default-src"], ["'none'"])
                # What a browser applies to script elements and to script attributes, fallbacks included.
                for chain in (("script-src-elem", "script-src", "default-src"),
                              ("script-src-attr", "script-src", "default-src")):
                    effective = next(policy[name] for name in chain if name in policy)
                    self.assertEqual(effective, ["'none'"], chain)
                style_elem = next(policy[name] for name in ("style-src-elem", "style-src", "default-src") if name in policy)
                self.assertEqual(style_elem, [style_source])
                self.assertNotIn("style-src-attr", policy)
                for values in policy.values():
                    self.assertFalse({"'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'", "*"} & set(values))
                self.assertEqual(policy["base-uri"], ["'none'"])
                self.assertEqual(policy["frame-ancestors"], ["'none'"])
                self.assertNotIn("<script", body.lower())


def _deep(levels: int, leaf):
    value = leaf
    for _ in range(levels):
        value = {"next": value}
    return value


class BreakItPins(GuiHarness):
    """Findings F1 to F8 of the DeepSeek break-it read of #384 (round 3)."""

    TITLE = "</title><script>alert(1)</script>"

    def with_panel(self):
        self.app = Application(ControlClient(wsgi_transport(self.api)), self.door, frozenset({HOST}),
                               frozenset({ORIGIN}), panels=[{"id": "p", "title": self.TITLE, "reads": [READS[0]],
                                                            "venue": "paper"}])

    def headers_without_length(self, headers: dict) -> dict:
        return {k: v for k, v in headers.items() if k != "Content-Length"}

    def test_f1_a_deep_answer_is_drawn_with_a_marker_never_a_crash(self) -> None:
        deep = {"result": _deep(5000, "DEEP-LEAF"), "status_code": "ok"}
        self.app._client.operator = lambda *_a, **_k: deep
        status, headers, body = self.request("GET", "/read", query="command=" + READS[0])
        self.assertEqual(status, "200 OK")
        self.assertIn("nested too deeply to show", body)
        self.assertNotIn("DEEP-LEAF", body)
        self.with_panel()
        self.app._client.operator = lambda *_a, **_k: {"result": {"venue": "paper", "rows": _deep(5000, "DEEP-LEAF")},
                                                      "errors": [_deep(5000, "DEEP-ERROR")]}
        status, _, body = self.request("GET", "/panel", query="id=p")
        self.assertEqual(status, "200 OK")
        self.assertIn("Withheld: data not in the paper book", body)
        self.assertNotIn("DEEP-LEAF", body)
        self.assertNotIn("DEEP-ERROR", body)
        shallow = withhold_venue_data(_deep(60, "SHALLOW-LEAF"))
        for _ in range(60):
            shallow = shallow["next"]
        self.assertEqual(shallow, "SHALLOW-LEAF")

    def test_f2_a_panel_title_is_escaped_inside_the_title_element(self) -> None:
        self.with_panel()
        for path, query in (("/panel", "id=p"), ("/", "")):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path, query=query)
                self.assertEqual(status, "200 OK")
                self.assertNotIn("<script", body.lower())
                self.assertEqual(body.count("</title>"), 1)
        title = re.search(r"<title>(.*?)</title>", self.request("GET", "/panel", query="id=p")[2], re.S).group(1)
        self.assertTrue(title.startswith("&lt;/title&gt;&lt;script&gt;alert(1)&lt;/script&gt;"), title)

    def test_f3_the_read_and_panel_queries_take_exactly_one_name_once(self) -> None:
        self.with_panel()
        command = "command=" + READS[0]
        for path, query in (("/read", command + "&extra=1"), ("/read", command + "&" + command),
                            ("/read", ""), ("/panel", "id=p&extra=1"), ("/panel", "id=p&id=p"), ("/panel", "")):
            with self.subTest(path=path, query=query):
                self.assertEqual(self.request("GET", path, query=query)[0], "400 Bad Request")
        self.assertEqual(self.request("GET", "/read", query=command)[0], "200 OK")
        self.assertEqual(self.request("GET", "/panel", query="id=p")[0], "200 OK")

    def test_f4_a_length_of_zero_or_less_is_refused_and_nothing_is_read(self) -> None:
        body = b"run_id=run-1&confirm=explicit"
        for length in ("0", "-1", "-100"):
            with self.subTest(length=length):
                stream = io.BytesIO(body)
                env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/stop", "QUERY_STRING": "", "HTTP_HOST": HOST,
                       "HTTP_ORIGIN": ORIGIN, "HTTP_SEC_FETCH_SITE": "same-origin", "CONTENT_LENGTH": length,
                       "CONTENT_TYPE": "application/x-www-form-urlencoded", "wsgi.input": stream,
                       "wsgi.errors": sys.stderr}
                status = []
                self.app(env, lambda s, h: status.append(s))
                self.assertEqual(status, ["400 Bad Request"])
                self.assertEqual(stream.tell(), 0)
        self.assertEqual(self.api.calls, [])

    def test_f5_a_raising_door_refuses_and_a_raising_handler_answers_with_the_same_headers(self) -> None:
        ok_headers = self.headers_without_length(self.request("GET", "/")[1])
        for error in (TimeoutError("DOOR-SECRET"), RuntimeError("DOOR-SECRET"), RecursionError("DOOR-SECRET")):
            with self.subTest(door=type(error).__name__):
                def door(_env, error=error):
                    raise error
                self.app._door = door
                status, headers, body = self.request("GET", "/")
                self.assertEqual(status, "401 Unauthorized")
                self.assertIn("device_not_authenticated", body)
                self.assertNotIn("DOOR-SECRET", body)
                self.assertEqual(self.headers_without_length(headers), ok_headers)
        self.app._door = self.door
        for error in (TimeoutError("SECRET-TEXT"), RuntimeError("SECRET-TEXT"), RecursionError("SECRET-TEXT")):
            with self.subTest(handler=type(error).__name__):
                def boom(*_a, error=error, **_k):
                    raise error
                self.app._client.operator = boom
                self.app._client.stop_run = boom
                for method, path, form, query in (("GET", "/read", None, "command=" + READS[0]),
                                                  ("POST", "/stop", {"run_id": "run-1", "confirm": "explicit"}, "")):
                    status, headers, body = self.request(method, path, form, query=query)
                    self.assertEqual(status, "500 Internal Server Error")
                    self.assertIn("(%s)" % type(error).__name__, body)
                    self.assertIn("does not know whether anything that was sent took effect", body)
                    self.assertNotIn("SECRET-TEXT", body)
                    self.assertEqual(self.headers_without_length(headers), ok_headers)

    def test_f7_a_key_that_is_not_text_is_left_out_and_the_page_still_draws(self) -> None:
        shown = withhold_venue_data({1: "INT-KEY", (1, 2): "TUPLE-KEY", "b": 2, "nested": [{3: "x", "c": 1}]})
        self.assertEqual(shown, {"b": 2, "nested": [{"c": 1}]})
        self.assertEqual(json.loads(json.dumps(shown, sort_keys=True)), shown)
        self.app._client.operator = lambda *_a, **_k: {"result": {1: "INT-KEY", "symbol": "TEXT-KEY"}}
        status, _, body = self.request("GET", "/read", query="command=" + READS[0])
        self.assertEqual(status, "200 OK")
        self.assertIn("TEXT-KEY", body)
        self.assertNotIn("INT-KEY", body)

    def test_f8_a_host_or_origin_that_is_not_text_is_refused(self) -> None:
        for headers, code in (({"HTTP_HOST": [HOST]}, "host_not_allowed"), ({"HTTP_HOST": HOST.encode()}, "host_not_allowed"),
                              ({"HTTP_ORIGIN": [ORIGIN]}, "origin_not_allowed"),
                              ({"HTTP_ORIGIN": ORIGIN.encode()}, "origin_not_allowed")):
            for method in ("GET", "POST"):
                with self.subTest(headers=headers, method=method):
                    form = {"run_id": "run-1", "confirm": "explicit"} if method == "POST" else None
                    status, _, body = self.request(method, "/" if method == "GET" else "/stop", form, headers=headers)
                    self.assertEqual(status, "403 Forbidden")
                    self.assertIn(code, body)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.door.seen, 0)

    def test_round4_f1_a_request_line_that_is_not_text_is_refused_with_the_page_headers(self) -> None:
        ok_headers = self.headers_without_length(self.request("GET", "/")[1])
        self.api.calls.clear()  # the home page above reads the routes; the refused requests below read nothing
        for headers in ({"REQUEST_METHOD": ["GET"]}, {"PATH_INFO": ["/"]}, {"REQUEST_METHOD": ("GET",)},
                        {"REQUEST_METHOD": 7}, {"PATH_INFO": b"/"}):
            with self.subTest(headers=headers):
                status, got, body = self.request("GET", "/", headers=headers)
                self.assertEqual(status, "400 Bad Request")
                self.assertIn("Bad request: request line", body)
                self.assertEqual(self.headers_without_length(got), ok_headers)
        self.assertEqual(self.api.calls, [])

    def test_round4_f4_a_host_or_origin_must_be_a_str_exactly(self) -> None:
        class Lying(str):
            def __eq__(self, other):
                return True

            def __hash__(self):
                return hash(HOST) if self.startswith("evil") else str.__hash__(self)

        class LyingOrigin(Lying):
            def __hash__(self):
                return hash(ORIGIN)

        for headers, code in (({"HTTP_HOST": Lying("evil.example")}, "host_not_allowed"),
                              ({"HTTP_HOST": Lying(HOST)}, "host_not_allowed"),
                              ({"HTTP_ORIGIN": LyingOrigin("http://evil.example")}, "origin_not_allowed")):
            with self.subTest(headers=headers):
                status, _, body = self.request("GET", "/", headers=headers)
                self.assertEqual(status, "403 Forbidden")
                self.assertIn(code, body)
        self.assertEqual(self.door.seen, 0)

    def test_round5_no_untrusted_value_is_compared_before_it_is_a_plain_str(self) -> None:
        # DeepSeek round 5, F1, F2 and F4: the method, the path and Sec-Fetch-Site are checked to be a
        # str exactly before anything compares them; a value that raises or lies in a comparison is
        # refused with the page's headers and reaches nothing.
        class EqRaises:
            def __eq__(self, other):
                raise RuntimeError("EQ-BOOM")

            def __ne__(self, other):
                raise RuntimeError("NE-BOOM")

            def __hash__(self):
                raise RuntimeError("HASH-BOOM")

        class Lying(str):
            claim = ""

            def __eq__(self, other):
                return other == self.claim

            def __ne__(self, other):
                return other != self.claim

            def __hash__(self):
                return hash(self.claim)

        class LyingPost(Lying):
            claim = "POST"

        class LyingStop(Lying):
            claim = "/stop"

        class LyingSameOrigin(Lying):
            claim = "same-origin"

        ok_headers = self.headers_without_length(self.request("GET", "/")[1])
        self.api.calls.clear()
        stop_form = {"run_id": "run-1", "confirm": "explicit"}
        allowed = {"HTTP_ORIGIN": ORIGIN, "HTTP_SEC_FETCH_SITE": "same-origin"}
        cases = (
            ("GET", "/", None, {"REQUEST_METHOD": EqRaises()}, "400 Bad Request", "request line"),
            ("GET", "/", None, {"PATH_INFO": EqRaises()}, "400 Bad Request", "request line"),
            ("GET", "/stop", stop_form, {"REQUEST_METHOD": LyingPost("GET"), **allowed}, "400 Bad Request", "request line"),
            ("POST", "/", stop_form, {"PATH_INFO": LyingStop("/"), **allowed}, "400 Bad Request", "request line"),
            ("GET", "/", None, {"HTTP_SEC_FETCH_SITE": EqRaises()}, "403 Forbidden", "cross_site_request"),
            ("POST", "/stop", stop_form, {"HTTP_SEC_FETCH_SITE": LyingSameOrigin("cross-site")}, "403 Forbidden",
             "cross_site_request"),
        )
        for method, path, form, headers, expected, text in cases:
            with self.subTest(method=method, path=path, headers=sorted(headers)):
                status, got, body = self.request(method, path, form, headers=headers)
                self.assertEqual(status, expected)
                self.assertIn(text, body)
                self.assertEqual(self.headers_without_length(got), ok_headers)
        self.assertEqual(self.api.calls, [])

    def test_round6_each_request_value_is_read_once_and_the_door_alone_refuses_a_bad_method(self) -> None:
        # DeepSeek round 6, F1: the method that was checked is the method that is compared. An environ
        # whose second read of a key would answer with a raising object is read once per key and the
        # request is answered normally. F3: the door function on its own refuses a method that is not
        # a str exactly instead of admitting it.
        class Boom:
            def __eq__(self, other):
                raise RuntimeError("SECOND-READ-BOOM")

            __ne__ = __eq__
            __hash__ = None

        class OnceEnviron(dict):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.reads = {}

            def _count(self, key):
                self.reads[key] = self.reads.get(key, 0) + 1
                return self.reads[key]

            def get(self, key, default=None):
                if self._count(key) > 1 and key in ("REQUEST_METHOD", "PATH_INFO", "HTTP_HOST",
                                                    "HTTP_ORIGIN", "HTTP_SEC_FETCH_SITE"):
                    return Boom()
                return super().get(key, default)

            def __getitem__(self, key):
                self._count(key)
                return super().__getitem__(key)

        for method, path, body in (("GET", "/", b""), ("POST", "/stop", b"run_id=run-1&confirm=explicit")):
            with self.subTest(method=method):
                env = OnceEnviron({"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "", "HTTP_HOST": HOST,
                                   "HTTP_ORIGIN": ORIGIN, "HTTP_SEC_FETCH_SITE": "same-origin",
                                   "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": "application/x-www-form-urlencoded",
                                   "wsgi.input": io.BytesIO(body), "wsgi.errors": sys.stderr})
                status = []
                self.app(env, lambda s, h: status.append(s))
                self.assertEqual(status, ["200 OK"])
                for key in ("REQUEST_METHOD", "PATH_INFO", "HTTP_HOST", "HTTP_ORIGIN", "HTTP_SEC_FETCH_SITE"):
                    self.assertEqual(env.reads.get(key), 1, key)
        for bad in (7, None, ["GET"], Boom()):
            with self.subTest(standalone=type(bad).__name__):
                env = {"HTTP_HOST": HOST, "HTTP_ORIGIN": ORIGIN, "HTTP_SEC_FETCH_SITE": "same-origin"}
                self.assertIsNotNone(self.app._refusal(env, bad))


if __name__ == "__main__":
    unittest.main()
