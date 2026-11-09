"""Read-only mount and additive plugin inventory regressions."""
import importlib
import re
import unittest
from collections.abc import Mapping

from recursion_core.gui import compose
from recursion_core.gui.client import ACTIONS, READS, ControlClient, ControlError
from recursion_core.gui.inventory import BARS, REQUIRED, assess, coverage_gaps
from recursion_core.gui.panels import filter_venue_rows
from recursion_core.gui.tests.fakes import wsgi_transport
from recursion_core.gui.tests.test_surface import GuiHarness, HOST, ORIGIN


class InventoryTests(unittest.TestCase):
    def test_plugin_requirements_extend_all_core_classes_and_same_class(self):
        original = {k: tuple(v) for k, v in REQUIRED.items()}
        gaps = coverage_gaps([], {"venue_use": ["paper:own_trading"], "engine": ["plugin_engine", "codex"]})
        ids = [g["id"] for g in gaps]
        for cls, subjects in original.items():
            for subject in subjects:
                self.assertIn(f"gap:{cls}:{subject}", ids)
        self.assertIn("gap:venue_use:paper:own_trading", ids)
        self.assertIn("gap:engine:plugin_engine", ids)
        self.assertEqual(ids.count("gap:engine:codex"), 1)
        self.assertEqual(REQUIRED, original)

    def test_gemini_floor_id_and_own_loop_permission_holds(self):
        self.assertIn("gemini", REQUIRED["engine"])
        self.assertNotIn("gemini_cli", REQUIRED["engine"])
        self.assertIn("xai", REQUIRED["own_loop"])
        for vendor in ("copilot", "nous"):
            gap = next(g for g in coverage_gaps([]) if g["id"] == f"gap:own_loop:{vendor}")
            self.assertTrue(gap["beta_tag"])
            self.assertIn("O61", gap["blocker"])
            row = assess({"id": vendor, "class": "own_loop", "vendor": vendor, "maturity": "mature",
                          "bars": {bar: {"pass": True, "evidence": "fixture"} for bar in BARS}})
            self.assertEqual((row["maturity"], row["beta_tag"]), ("beta", True))
            self.assertIn("O61", row["blocker"])


class PanelTests(GuiHarness):
    def mount(self, panels):
        self.app = compose(ControlClient(wsgi_transport(self.api)), self.door, {HOST}, {ORIGIN},
                           plugins=({"panels": panels},))

    def test_read_only_panel_renders_named_reads_and_escapes(self):
        panel = {"id": "example.panel", "title": "Example <panel>", "reads": list(READS[:2])}
        self.mount([panel])
        panel["reads"].append(ACTIONS[0])  # validated snapshot cannot be mutated by the plugin
        self.assertIn("/panel?id=example.panel", self.request("GET", "/")[2])
        self.api.calls.clear()
        status, _, body = self.request("GET", "/panel", query="id=example.panel")
        self.assertEqual(status, "200 OK")
        self.assertIn("Example &lt;panel&gt;", body)
        main = body.split('<main id="main"', 1)[1].split('</main>', 1)[0]
        self.assertNotIn('<form', main)
        self.assertNotIn('Request stop', main)
        self.assertEqual([c[2]["command"] for c in self.api.calls], list(READS[:2]))
        self.assertEqual(self.request("POST", "/panel", {"id": "example.panel"})[0], "405 Method Not Allowed")
        self.assertEqual(self.request("GET", "/panel", query="id=missing")[0], "400 Bad Request")

    def test_panel_declarations_refuse_actions_stop_orders_and_duplicates(self):
        for read in (*ACTIONS, "stop", "operator.orders.submit", ["bad"]):
            with self.subTest(read=read), self.assertRaises(ValueError):
                self.mount([{"id": "p", "title": "Panel", "reads": [read]}])
        p = {"id": "p", "title": "Panel", "reads": [READS[0]]}
        with self.assertRaises(ValueError):
            self.mount([p, p])
        with self.assertRaises(ValueError):
            self.mount([{**p, "stop": True}])

    def test_panel_filters_venue_data_and_shows_read_outage(self):
        self.mount([{"id": "p", "title": "Panel", "reads": [READS[0]], "withhold_venue_data": False}])
        self.api.extra[READS[0]] = {"result": {"venue_data": "not-for-display", "venue_data_switch": "off"}}
        body = self.request("GET", "/panel", query="id=p")[2]
        self.assertNotIn("not-for-display", body)
        self.assertIn("withheld", body)
        self.api.extra[READS[0]] = {"result": {"venue_data": "permitted", "venue_data_switch": "on"}}
        self.assertIn("permitted", self.request("GET", "/panel", query="id=p")[2])
        def unavailable(*_):
            raise ControlError("offline")
        self.app._client.operator = unavailable
        self.assertIn("Unavailable: offline", self.request("GET", "/panel", query="id=p")[2])

    def test_declared_venue_use_is_kept_shown_and_never_sent_as_a_scope(self):
        self.mount([{"id": "v", "title": "Venue panel", "reads": [READS[0]],
                     "venue": "kalshi", "venue_use": "kalshi:model_fed_read",
                     "withhold_venue_data": True},
                    {"id": "n", "title": "Plain panel", "reads": [READS[0]]}])
        self.assertEqual(self.app._panels["v"]["venue_use"], "kalshi:model_fed_read")
        self.assertEqual(self.app._panels["v"]["venue"], "kalshi")
        self.api.calls.clear()
        body = self.request("GET", "/panel", query="id=v")[2]
        self.assertIn("Declared venue use: <code>kalshi:model_fed_read</code> (a label)", body)
        self.assertIn("filtered to <code>kalshi</code>", body)
        self.assertEqual([c[2]["arguments"] for c in self.api.calls], [{}])  # O7 v0 has no venue argument
        self.assertNotIn("Declared venue use", self.request("GET", "/panel", query="id=n")[2])
        for bad in ({"venue_use": "kalshi"}, {"venue_use": ["kalshi:model_fed_read"]},
                    {"venue_use": "Kalshi:<x>"}, {"venue": "kalshi:model_fed_read"},
                    {"venue": "Kalshi"}, {"withhold_venue_data": "no"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.mount([{"id": "b", "title": "Bad", "reads": [READS[0]], **bad}])

    def test_stock_row_never_renders_on_a_kalshi_panel(self):
        self.mount([{"id": "k", "title": "Kalshi", "reads": [READS[0]],
                     "venue": "kalshi", "venue_use": "kalshi:model_fed_read"}])
        rows = [{"venue": "stocks", "symbol": "STOCK-SECRET"},
                {"venue": "kalshi", "symbol": "KALSHI-VISIBLE",
                 "details": [{"value": "inherited-kalshi"},
                             {"venue": "stocks", "value": "nested-stock-secret"}]},
                {"symbol": "UNATTRIBUTED-SECRET"}]
        self.api.extra[READS[0]] = {"result": {"rows": rows}}
        self.api.calls.clear()
        body = self.request("GET", "/panel", query="id=k")[2]
        self.assertIn("KALSHI-VISIBLE", body)
        self.assertIn("inherited-kalshi", body)
        for hidden in ("STOCK-SECRET", "nested-stock-secret", "UNATTRIBUTED-SECRET"):
            self.assertNotIn(hidden, body)
        self.assertEqual(len(rows), 3)  # display filtering does not alter the source
        self.assertEqual([c[2]["arguments"] for c in self.api.calls], [{}])

    def test_unlabelled_nested_dict_is_withheld_on_a_venue_panel(self):
        self.mount([{"id": "k", "title": "Kalshi", "reads": [READS[0]], "venue": "kalshi"}])
        self.api.extra[READS[0]] = {"result": {
            "positions": {"KXUNLABELLED": {"qty": 424242}},
            "rows": [{"venue": "kalshi", "symbol": "KALSHI-VISIBLE", "fills": {"qty": 7}}]}}
        body = self.request("GET", "/panel", query="id=k")[2]
        self.assertIn("KALSHI-VISIBLE", body)
        self.assertIn("&quot;qty&quot;: 7", body)  # nested data in a kalshi row inherits its label
        for hidden in ("KXUNLABELLED", "424242"):
            self.assertNotIn(hidden, body)
        self.assertIn("Withheld: data not labelled <code>kalshi</code>.", body)

    def test_cross_venue_and_other_venue_totals_are_withheld_on_a_venue_panel(self):
        self.mount([{"id": "k", "title": "Kalshi", "reads": [READS[0]], "venue": "kalshi"}])
        self.api.extra[READS[0]] = {"result": {
            "total": 919191, "totals": {"all_venues": 828282},
            "by_venue": [{"venue": "paper", "total": 737373}, {"venue": "kalshi", "total": 646464}]}}
        body = self.request("GET", "/panel", query="id=k")[2]
        self.assertIn("646464", body)
        for hidden in ("919191", "828282", "737373", "all_venues"):
            self.assertNotIn(hidden, body)
        self.assertIn("Withheld: data not labelled <code>kalshi</code>.", body)

    def test_unlabelled_response_level_errors_and_provenance_show_on_a_venue_panel(self):
        self.mount([{"id": "k", "title": "Kalshi", "reads": [READS[0]], "venue": "kalshi"}])
        self.api.extra[READS[0]] = {
            "errors": [{"code": "ERR-MARKER-515151"}], "provenance": {"source": "PROV-MARKER-626262"},
            "result": {"positions": {"KXUNLABELLED": {"qty": 424242}}, "total": 919191,
                       "errors": ["NESTED-ERR-313131"], "provenance": {"source": "NESTED-PROV-353535"},
                       "rows": [{"venue": "kalshi", "symbol": "KALSHI-VISIBLE"}]}}
        body = self.request("GET", "/panel", query="id=k")[2]
        for shown in ("ERR-MARKER-515151", "PROV-MARKER-626262", "KALSHI-VISIBLE"):
            self.assertIn(shown, body)
        for hidden in ("KXUNLABELLED", "424242", "919191", "NESTED-ERR-313131", "NESTED-PROV-353535"):
            self.assertNotIn(hidden, body)
        self.assertIn("Withheld: data not labelled <code>kalshi</code>.", body)
        self.api.extra[READS[0]]["provenance"] = {"venue": "paper", "source": "PAPER-PROV-747474"}
        self.assertNotIn("PAPER-PROV-747474", self.request("GET", "/panel", query="id=k")[2])

    def test_venue_data_switch_off_still_governs_with_and_without_a_venue_filter(self):
        self.mount([{"id": "k", "title": "Kalshi", "reads": [READS[0]], "venue": "kalshi"},
                    {"id": "n", "title": "Plain", "reads": [READS[0]]}])
        mixed = {"positions": {"KXUNLABELLED": {"qty": 424242}}, "total": 919191,
                 "rows": [{"venue": "kalshi", "symbol": "KALSHI-VISIBLE",
                           "venue_data": "kalshi-switch-gated", "venue_data_switch": "off"}]}
        self.api.extra[READS[0]] = {"result": mixed}
        plain = self.request("GET", "/panel", query="id=n")[2]  # no venue: unfiltered data renders
        for shown in ("KXUNLABELLED", "424242", "919191", "KALSHI-VISIBLE"):
            self.assertIn(shown, plain)
        self.assertNotIn("kalshi-switch-gated", plain)
        self.assertIn("withheld: venue data switch is off", plain)
        kalshi = self.request("GET", "/panel", query="id=k")[2]
        self.assertIn("KALSHI-VISIBLE", kalshi)
        for hidden in ("KXUNLABELLED", "424242", "919191", "kalshi-switch-gated"):
            self.assertNotIn(hidden, kalshi)
        self.assertIn("withheld: venue data switch is off", kalshi)
        mixed["rows"][0]["venue_data_switch"] = "on"
        self.assertIn("kalshi-switch-gated", self.request("GET", "/panel", query="id=k")[2])

    def test_o7_visibility_shows_on_a_venue_panel(self):
        self.mount([{"id": "k", "title": "Kalshi", "reads": ["operator.positions.inspect"],
                     "venue": "kalshi_demo"}])
        self.api.extra["operator.positions.inspect"] = {"visibility": {
            "uncertainty": "visible", "refusal": "visible", "stale_data": "visible",
            "reconciliation": "unreconciled_visible", "detail": {"venue": "stock", "px": "VIS-LEAK-1"}}}
        body = self.request("GET", "/panel", query="id=k")[2]
        for shown in ("stale_data: <strong>visible</strong>", "refusal: <strong>visible</strong>",
                      "uncertainty: <strong>visible</strong>",
                      "reconciliation: <strong>unreconciled_visible</strong>",
                      "Other fields: <strong>1 withheld, not in O7 visibility</strong>", "KXDEMO-1"):
            self.assertIn(shown, body)
        for hidden in ("AAPL", "VIS-LEAK-1", "detail"):
            self.assertNotIn(hidden, body)
        self.api.extra["operator.positions.inspect"] = {"visibility": None}
        self.assertIn("Visibility: not reported by the control API.",
                      self.request("GET", "/panel", query="id=k")[2])

    def test_o7_visibility_on_a_venue_panel_shows_only_the_four_keys_with_fixed_values(self):
        # X1a (t1430u): the visibility block is outside the venue filter, so it must not carry the
        # response's own text. Unknown keys and off-schema values never reach the page.
        self.mount([{"id": "k", "title": "Kalshi", "reads": ["operator.positions.inspect"],
                     "venue": "kalshi_demo"}])
        self.api.extra["operator.positions.inspect"] = {"visibility": {
            "uncertainty": "STOCK-AAPL-LEAK-171717", "refusal": "visible",
            "stale_data": ["visible"], "AAPL-KEY-LEAK-181818": "visible",
            "NOTE-KEY-LEAK-202020": "stock AAPL qty 191919", "reconciliation": "visible"}}
        body = self.request("GET", "/panel", query="id=k")[2]
        start = body.index('<section aria-label="Visibility">')
        block = body[start:body.index("</section>", start)]
        self.assertEqual(
            ["uncertainty: <strong>reported, not an O7 value</strong>",
             "refusal: <strong>visible</strong>",
             "stale_data: <strong>reported, not an O7 value</strong>",
             "reconciliation: <strong>reported, not an O7 value</strong>",
             "Other fields: <strong>2 withheld, not in O7 visibility</strong>"],
            re.findall(r"<li>(.*?)</li>", block))
        for hidden in ("STOCK-AAPL-LEAK-171717", "AAPL-KEY-LEAK-181818", "191919", "NOTE-KEY-LEAK-202020"):
            self.assertNotIn(hidden, body)
        self.api.extra["operator.positions.inspect"] = {"visibility": {"refusal": "absent_not_hidden"}}
        block = self.request("GET", "/panel", query="id=k")[2]
        for shown in ("uncertainty: <strong>not reported</strong>", "refusal: <strong>absent_not_hidden</strong>",
                      "stale_data: <strong>not reported</strong>", "reconciliation: <strong>not reported</strong>"):
            self.assertIn(shown, block)
        self.assertNotIn("Other fields", block)
        allowed = {"uncertainty": "absent_not_hidden", "refusal": "visible", "stale_data": "absent_not_hidden",
                   "reconciliation": "reconciled"}
        self.api.extra["operator.positions.inspect"] = {"visibility": dict(allowed)}
        block = self.request("GET", "/panel", query="id=k")[2]
        for key, value in allowed.items():
            self.assertIn("%s: <strong>%s</strong>" % (key, value), block)

    def test_o7_shaped_venue_rows_are_not_switch_gated_by_the_gui(self):
        # O7 v0 labels rows with `venue` and reports no switch state; the GUI only redacts
        # `venue_data` fields the control API marks, so clearance of these rows is the
        # control API's. This pins that behaviour and the panel note that states it.
        self.mount([{"id": "k", "title": "Kalshi", "reads": ["operator.positions.inspect"],
                     "venue": "kalshi_demo", "venue_use": "kalshi_demo:own_trading"}])
        self.api.calls.clear()
        body = self.request("GET", "/panel", query="id=k")[2]
        self.assertIn("KXDEMO-1", body)
        self.assertNotIn("AAPL", body)
        self.assertNotIn("withheld: venue data switch is off", body)
        self.assertIn("O7 v0 reports no switch state for venue-labelled rows", body)
        self.assertNotIn("venue data stays behind the venue-data switch", body)
        self.assertEqual([c[2]["arguments"] for c in self.api.calls], [{}])
        read = self.request("GET", "/read", query="command=operator.positions.inspect")[2]
        self.assertIn("KXDEMO-1", read)
        self.assertIn("stale_data: <strong>absent_not_hidden</strong>", read)

    def test_real_cockpit_registry_renders(self):
        try:
            plugin = importlib.import_module("cockpit.gui.panels")
        except ModuleNotFoundError as exc:
            if exc.name not in {"cockpit", "cockpit.gui", "cockpit.gui.panels"}:
                raise
            self.skipTest("run with Cockpit #318 engine on PYTHONPATH for cross-repo integration")
        self.app = compose(ControlClient(wsgi_transport(self.api)), self.door, {HOST}, {ORIGIN},
                           plugins=(plugin.registry(),))
        self.assertIsInstance(plugin.PANELS[0], Mapping)
        self.assertIn("gap:engine:hermes", self.request("GET", "/")[2])
        self.api.calls.clear()
        status, _, body = self.request("GET", "/panel", query="id=cockpit.paper_own_trading")
        self.assertEqual(status, "200 OK")
        self.assertIn(plugin.PANELS[0]["title"], body)
        self.assertEqual([c[2]["command"] for c in self.api.calls], list(plugin.PANELS[0]["reads"]))

    def test_paper_panel_keeps_paper_stock_and_paper_keys_as_one_book(self):
        # The page filter matches the paper book: paper rows, stock rows, and paper_* keys
        # (stock_* too, the other label's prefix). kalshi_demo and other venues stay out.
        # Unlabelled response-level errors and provenance still show.
        rows = [
            {"venue": "paper", "symbol": "PAPER-ROW-1"},
            {"venue": "stock", "symbol": "STOCK-ROW-1"},
            {"venue": "kalshi_demo", "symbol": "KXDEMO-SECRET"},
            {"venue": "kalshi", "symbol": "KALSHI-SECRET"},
            {"venue": "coinbase", "symbol": "COINBASE-SECRET"},
            {"venue": "gemini", "symbol": "GEMINI-SECRET"},
            {"venue": "Paper", "symbol": "CASE-SECRET"},
            {"symbol": "UNLABELLED-SECRET"},
        ]
        payload = {
            "errors": [{"code": "ERR-MARKER-515151"}],
            "provenance": {"source": "PROV-MARKER-626262"},
            "paper_cash_minor": 100000,
            "kalshi_demo_yes_price": "4242.17",
            "gemini_balance": "GEMINI-KEY-SECRET",
            "result": {
                "paper_open_orders": 2,
                "stock_buying_power_minor": 50000,
                "rows": rows,
                "paper_rows": [
                    {"venue": "kalshi_demo", "symbol": "NESTED-KX-SECRET"},
                    {"venue": "paper", "symbol": "NESTED-PAPER-ROW"},
                ],
            },
        }
        for declared, venue_use in (("stock", "paper:own_trading"), ("paper", None)):
            panel = {"id": "p", "title": "Paper", "reads": [READS[0]], "venue": declared}
            if venue_use is not None:
                panel["venue_use"] = venue_use
            self.mount([panel])
            self.api.extra[READS[0]] = payload
            kept, withheld = filter_venue_rows(payload, declared)
            self.assertTrue(withheld)
            self.assertEqual(kept["paper_cash_minor"], 100000)
            self.assertEqual(kept["result"]["paper_open_orders"], 2)
            self.assertEqual(kept["result"]["stock_buying_power_minor"], 50000)
            self.assertEqual([row["symbol"] for row in kept["result"]["rows"]],
                             ["PAPER-ROW-1", "STOCK-ROW-1"])
            self.assertEqual([row["symbol"] for row in kept["result"]["paper_rows"]],
                             ["NESTED-PAPER-ROW"])
            self.assertEqual(kept["errors"], payload["errors"])
            self.assertEqual(kept["provenance"], payload["provenance"])
            self.assertNotIn("kalshi_demo_yes_price", kept)
            self.assertNotIn("gemini_balance", kept)
            body = self.request("GET", "/panel", query="id=p")[2]
            for shown in ("PAPER-ROW-1", "STOCK-ROW-1", "NESTED-PAPER-ROW", "paper_cash_minor",
                          "paper_open_orders", "stock_buying_power_minor", "ERR-MARKER-515151",
                          "PROV-MARKER-626262", "filtered to the paper book",
                          "Withheld: data not in the paper book"):
                self.assertIn(shown, body)
            for hidden in ("KXDEMO-SECRET", "KALSHI-SECRET", "COINBASE-SECRET", "GEMINI-SECRET",
                           "CASE-SECRET", "UNLABELLED-SECRET", "NESTED-KX-SECRET", "4242.17",
                           "GEMINI-KEY-SECRET", "filtered to <code>%s</code>" % declared):
                self.assertNotIn(hidden, body)
            self.assertEqual(len(payload["result"]["rows"]), 8)
        labelled = dict(payload, provenance={"venue": "kalshi_demo", "source": "KX-PROV-SECRET"})
        self.mount([{"id": "p", "title": "Paper", "reads": [READS[0]], "venue": "stock"}])
        self.api.extra[READS[0]] = labelled
        self.assertNotIn("KX-PROV-SECRET", self.request("GET", "/panel", query="id=p")[2])
        paper_prov = dict(payload, provenance={"venue": "paper", "source": "PAPER-PROV-OK"})
        self.api.extra[READS[0]] = paper_prov
        self.assertIn("PAPER-PROV-OK", self.request("GET", "/panel", query="id=p")[2])

    def test_kalshi_panel_does_not_keep_the_paper_book(self):
        self.mount([{"id": "k", "title": "Kalshi", "reads": [READS[0]], "venue": "kalshi"}])
        payload = {
            "errors": [{"code": "ERR-MARKER-515151"}],
            "provenance": {"source": "PROV-MARKER-626262"},
            "paper_cash_minor": 100000,
            "result": {"paper_open_orders": 2, "rows": [
                {"venue": "paper", "symbol": "PAPER-ROW-1"},
                {"venue": "stock", "symbol": "STOCK-ROW-1"},
                {"venue": "kalshi", "symbol": "KALSHI-VISIBLE"},
                {"venue": "kalshi_demo", "symbol": "KXDEMO-SECRET"},
            ]},
        }
        self.api.extra[READS[0]] = payload
        kept, _ = filter_venue_rows(payload, "kalshi")
        self.assertEqual([row["symbol"] for row in kept["result"]["rows"]], ["KALSHI-VISIBLE"])
        self.assertNotIn("paper_cash_minor", kept)
        self.assertNotIn("paper_open_orders", kept["result"])
        self.assertEqual(kept["errors"], payload["errors"])
        self.assertEqual(kept["provenance"], payload["provenance"])
        body = self.request("GET", "/panel", query="id=k")[2]
        self.assertIn("KALSHI-VISIBLE", body)
        self.assertIn("filtered to <code>kalshi</code>", body)
        self.assertIn("Withheld: data not labelled <code>kalshi</code>.", body)
        self.assertIn("ERR-MARKER-515151", body)
        self.assertIn("PROV-MARKER-626262", body)
        for hidden in ("PAPER-ROW-1", "STOCK-ROW-1", "KXDEMO-SECRET", "paper_cash_minor",
                       "paper_open_orders", "100000"):
            self.assertNotIn(hidden, body)
