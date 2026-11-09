"""Read-only mount and additive plugin inventory regressions."""
import importlib
import unittest
from collections.abc import Mapping

from recursion_core.gui import compose
from recursion_core.gui.client import ACTIONS, READS, ControlClient, ControlError
from recursion_core.gui.inventory import BARS, REQUIRED, assess, coverage_gaps
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
