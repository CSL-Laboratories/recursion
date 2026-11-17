"""G1-b proof: a user's layout draws a third-party panel, and the core region stays put.

The page proof (column 2, N5, N6, N12, and the core-region bytes) runs on every
interpreter. The daemon proof calls ``main(plugin_dirs=...)`` with the signed fixture
package. That load needs ``cryptography``, which this machine's 3.12 gate interpreter
does not have; the loader then refuses every plugin, so that one test is skipped there.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock
from http.client import HTTPConnection

from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.gui.client import READS, ControlClient
from recursion_core.gui.layouts import (
    MAX_BYTES, SCHEMA, LayoutRefused, load_user_layout, parse_layout, parse_layout_text, read_layout,
)
from recursion_core.gui.panels import panel_registry
from recursion_core.gui.surface import Application
from recursion_core.gui.tests.fakes import wsgi_transport
from recursion_core.gui.tests.test_surface import HOST, ORIGIN, GuiHarness
from recursion_core.plugin_security.signing import backend_available, key_id

try:
    import jsonschema
except ImportError:  # the free-threaded interpreter has no jsonschema
    jsonschema = None

ROOT = Path(__file__).resolve().parents[4]
SCHEMA_DIR = ROOT / "schemas" / "gui" / "v1"
FIXTURES = SCHEMA_DIR / "fixtures" / "layout"
PLUGIN = Path(__file__).resolve().parent / "fixtures" / "g1_plugin"

# Bytes the layout must not change. Taken from the unthemed page's header and footer.
HEADER = ("<header><h1>Recursion operator</h1><p>Every route is BETA unless all six bars pass with "
          "recorded evidence, and a route the control API tags beta stays BETA. No live demos. "
          "This page cannot place, approve or size orders.</p></header>")
FOOTER = ("<footer><p>Results are shown as the control API returned them. A stop that is not "
          "confirmed shows as pending.</p></footer>")
SKIP = '<a class="skip" href="#main">Skip to main content</a>'
FOCUS_RULE = ":focus{outline:3px solid #1a5fb4;outline-offset:2px}"
TARGET_RULE = "a,button,input{min-height:24px;min-width:24px}"
NARROW = "@media (max-width:767px)"

CORE_REGIONS = ("stop", "approval", "header", "footer")


def _layout(panel, *, columns=1, collapsed=False, column=1, span=1, order=0, **extra):
    slot = {"panel": panel, "column": column, "span": span, "order": order, "collapsed": collapsed}
    body = {"schema": SCHEMA, "name": "sample", "title": "Sample", "columns": columns,
            "slots": [] if panel is None else [slot]}
    body.update(extra)
    return body


def core_region(html: str) -> str:
    """Header, skip link, stop form, footer, and the core floor rules, in that order."""
    stop = re.search(r'<section aria-labelledby="stop-h">.*?</section>', html, re.S)
    if stop is None:
        raise AssertionError("no stop section")
    style = re.search(r"<style>(.*?)</style>", html, re.S)
    if style is None:
        raise AssertionError("no style")
    rules = style.group(1)
    for rule in (FOCUS_RULE, TARGET_RULE):
        if rule not in rules:
            raise AssertionError(rule)
    return "\n".join((SKIP, HEADER, stop.group(0), FOOTER, FOCUS_RULE, TARGET_RULE))


def element_at(html: str, marker: str) -> str:
    """The element whose start tag contains ``marker``, including its nested tags."""
    start = html.index(marker)
    open_at = html.rfind("<", 0, start)
    tag = re.match(r"<([A-Za-z0-9]+)", html[open_at:]).group(1)
    depth = 0
    token = re.compile(r"</?%s\b[^>]*>" % tag)
    for match in token.finditer(html, open_at):
        depth += -1 if match.group(0).startswith("</") else 1
        if depth == 0:
            return html[open_at:match.end()]
    raise AssertionError("unclosed %s" % tag)


class LayoutFixtureTests(unittest.TestCase):
    """The schema fixtures and the parser agree, including the refusals a schema cannot state."""

    def test_schema_name_and_fixture_index_match_the_files(self) -> None:
        schema = json.loads((SCHEMA_DIR / "layout.v1.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema"]["const"], SCHEMA)
        index = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
        listed = [item["file"] for item in index["fixtures"]]
        on_disk = sorted(str(path.relative_to(FIXTURES))
                         for path in FIXTURES.glob("*/*.json"))
        self.assertEqual(sorted(listed), on_disk)
        self.assertEqual(len(listed), len(set(listed)))
        for path in FIXTURES.glob("*/*.json"):
            text = path.read_text(encoding="utf-8")
            shown = str(path.relative_to(FIXTURES))
            self.assertNotIn("gui.theme/1", text, shown)
            self.assertIn("gui.layout/1", text, shown)

    def test_good_fixtures_parse_and_bad_fixtures_are_refused(self) -> None:
        schema = json.loads((SCHEMA_DIR / "layout.v1.json").read_text(encoding="utf-8"))
        validator = None
        if jsonschema is not None:
            validator = jsonschema.Draft202012Validator(schema)
            validator.check_schema(schema)
        known = frozenset({"fixture.panel"})
        good = sorted((FIXTURES / "good").glob("*.json"))
        bad = sorted((FIXTURES / "bad").glob("*.json"))
        self.assertTrue(good and bad)
        for path in good:
            data = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(path=path.name):
                parsed = parse_layout(data, known_panels=known)
                self.assertEqual(parsed["schema"], SCHEMA)
                self.assertEqual(parsed["columns"], data["columns"])
                if validator is not None:
                    validator.validate(data)
        for path in bad:
            data = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(path=path.name):
                with self.assertRaises(LayoutRefused):
                    parse_layout(data, known_panels=known | set(CORE_REGIONS))
                if validator is not None:
                    self.assertFalse(validator.is_valid(data), path.name)

    def test_core_region_names_unknown_panels_and_duplicates_are_refused(self) -> None:
        known = frozenset({"fixture.panel", *CORE_REGIONS})
        for region in CORE_REGIONS:
            with self.subTest(region=region):
                with self.assertRaises(LayoutRefused) as caught:
                    parse_layout(_layout(region), known_panels=known)
                self.assertEqual(caught.exception.reason, "panel")
        with self.assertRaises(LayoutRefused) as caught:
            parse_layout(_layout("missing.panel"), known_panels=known)
        self.assertEqual(caught.exception.reason, "panel")
        body = _layout("fixture.panel", columns=2, column=2)
        body["slots"].append(dict(body["slots"][0]))
        with self.assertRaises(LayoutRefused) as caught:
            parse_layout(body, known_panels=known)
        self.assertEqual(caught.exception.reason, "panel")
        for key in ("z-index", "pixels", "overlay", "hide-core"):
            with self.subTest(key=key):
                with self.assertRaises(LayoutRefused) as caught:
                    parse_layout(_layout("routes", **{key: 1}), known_panels=known)
                self.assertEqual(caught.exception.reason, "unknown_key")
        raw = ('{"schema":"gui.layout/1","schema":"gui.layout/1","name":"a","title":"A",'
               '"columns":1,"slots":[]}').encode()
        with self.assertRaises(LayoutRefused) as caught:
            parse_layout_text(raw, known_panels=known)
        self.assertEqual(caught.exception.reason, "duplicate_key")

    def test_slots_sort_by_column_then_order_and_the_mapping_is_read_only(self) -> None:
        body = _layout("reads", columns=2, column=2, order=5)
        body["slots"].insert(0, {"panel": "routes", "column": 2, "span": 1, "order": 1,
                                  "collapsed": False})
        body["slots"].append({"panel": "interventions", "column": 1, "span": 1, "order": 0,
                               "collapsed": False})
        parsed = parse_layout(body, known_panels=frozenset())
        self.assertEqual([slot["panel"] for slot in parsed["slots"]],
                         ["interventions", "routes", "reads"])
        with self.assertRaises(TypeError):
            parsed["slots"][0]["panel"] = "stop"  # type: ignore[index]


class LayoutPageTests(GuiHarness):
    """What GET / does with a layout file, including N5, N6 and N12."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="g1b-layout-"))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._previous = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp)
        self.addCleanup(self._restore_config)
        self.gui_dir = self.tmp / "recursion" / "gui"
        self.gui_dir.mkdir(parents=True, mode=0o700)
        for path in (self.tmp, self.tmp / "recursion", self.gui_dir):
            os.chmod(path, 0o700)
        self.layout_path = self.gui_dir / "layout.json"

    def _restore_config(self) -> None:
        if self._previous is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._previous

    def mount(self, panels) -> None:
        self.app = Application(ControlClient(wsgi_transport(self.api)), self.door, frozenset({HOST}),
                               frozenset({ORIGIN}), panels=panels)

    def write_layout(self, data, *, mode=0o600) -> Path:
        self.layout_path.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(self.layout_path, mode)
        return self.layout_path

    def home(self):
        return self.request("GET", "/")

    def test_n5_a_core_region_layout_is_refused_and_an_empty_one_keeps_stop(self) -> None:
        self.mount([{"id": "fixture.panel", "title": "Fixture panel", "reads": [READS[0]]}])
        default = self.home()[2]
        self.assertIn(HEADER, default)
        self.assertIn(FOOTER, default)
        self.assertNotIn("data-column=", default)
        for region in CORE_REGIONS:
            self.write_layout(_layout(region, columns=2, column=2))
            status, _, refused = self.home()
            with self.subTest(region=region):
                self.assertEqual(status, "200 OK")
                self.assertEqual(core_region(refused), core_region(default))
                self.assertNotIn('data-panel="%s"' % region, refused)
                self.assertNotIn("data-column=", refused)
        self.write_layout(_layout(None, columns=1))
        status, _, empty = self.home()
        self.assertEqual(status, "200 OK")
        self.assertIn(HEADER, empty)
        self.assertIn('id="stop-h"', empty)
        self.assertIn("I confirm stopping this run", empty)
        self.assertIn('action="/stop"', empty)
        self.assertEqual(core_region(empty), core_region(default))
        self.assertIn('class="layout cols-1"', empty)

    def test_n6_a_collapsed_panel_does_not_hide_a_shown_views_visibility(self) -> None:
        self.mount([
            {"id": "shown.panel", "title": "Shown view", "reads": [READS[0]]},
            {"id": "hidden.panel", "title": "Hidden view", "reads": [READS[0]]},
        ])
        self.write_layout({
            "schema": SCHEMA, "name": "collapse", "title": "Collapse one", "columns": 2,
            "slots": [
                {"panel": "hidden.panel", "column": 1, "span": 1, "order": 0, "collapsed": True},
                {"panel": "shown.panel", "column": 2, "span": 1, "order": 0, "collapsed": False},
            ],
        })
        status, _, page = self.home()
        self.assertEqual(status, "200 OK")
        shown = element_at(page, 'data-panel="shown.panel"')
        collapsed = element_at(page, 'data-panel="hidden.panel"')
        self.assertTrue(shown.startswith("<section"), shown[:80])
        self.assertIn('data-column="2"', shown)
        self.assertIn('data-collapsed="false"', shown)
        self.assertIn("Visibility reported by the control API", shown)
        self.assertIn("Shown view", shown)
        self.assertTrue(collapsed.startswith("<details"), collapsed[:80])
        self.assertNotRegex(collapsed.split(">", 1)[0], r"\sopen[\s>]")
        self.assertNotIn('data-panel="shown.panel"', collapsed)
        self.assertNotIn(shown, collapsed)
        # The stop form and the disclaimer are not inside the collapsed panel.
        self.assertNotIn('id="stop-h"', collapsed)
        self.assertIn(HEADER, page)

    def test_n7_action_url_html_or_script_keys_are_refused(self) -> None:
        base = {"id": "p", "title": "Panel", "reads": [READS[0]]}
        for key in ("action", "url", "html", "script"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as caught:
                    panel_registry([{**base, key: "nope"}])
                self.assertEqual(str(caught.exception), "panel only declares reads")
                with self.assertRaises(ValueError):
                    self.mount([{**base, key: "nope"}])

    def test_n12_narrow_width_keeps_stop_on_one_column_without_a_wide_min_width(self) -> None:
        self.mount([{"id": "fixture.panel", "title": "Fixture panel", "reads": [READS[0]]}])
        self.write_layout(_layout("fixture.panel", columns=3, column=3, span=1))
        status, headers, page = self.home()
        self.assertEqual(status, "200 OK")
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', page)
        style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
        self.assertIn(NARROW, style)
        self.assertIn(".slot-2-1{grid-column:2}", style)
        narrow = style.split(NARROW, 1)[1]
        self.assertIn("grid-template-columns:minmax(0,1fr)", narrow)
        self.assertIn(".slot-2-1", narrow)
        self.assertIn("grid-column:auto", narrow)
        for match in re.finditer(r"min-width:\s*([0-9]+)px", style):
            self.assertLessEqual(int(match.group(1)), 24, match.group(0))
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        layout = element_at(page, 'class="layout ')
        stop = re.search(r'<section aria-labelledby="stop-h">.*?</section>', page, re.S).group(0)
        self.assertNotIn('id="stop-h"', layout)
        self.assertLess(page.index('class="layout '), page.index('id="stop-h"'))
        self.assertIn("I confirm stopping this run", stop)
        self.assertIn(TARGET_RULE, style)
        self.assertIn(FOCUS_RULE, style)

    def test_user_layout_draws_the_panel_inline_in_column_2_and_the_core_matches(self) -> None:
        self.mount([{"id": "fixture.panel", "title": "Fixture <panel>", "reads": [READS[0]]}])
        self.api.calls.clear()
        default_status, _, default = self.home()
        self.assertEqual(default_status, "200 OK")
        self.assertEqual([call for call in self.api.calls if call[1] == "/v1/operator"], [])
        self.assertIn("/panel?id=fixture.panel", default)
        self.assertNotIn("data-column=", default)
        self.write_layout(_layout("fixture.panel", columns=2, column=2))
        self.api.calls.clear()
        status, _, laid = self.home()
        self.assertEqual(status, "200 OK")
        self.assertEqual([call[2]["command"] for call in self.api.calls if call[1] == "/v1/operator"],
                         [READS[0]])
        slot = element_at(laid, 'data-panel="fixture.panel"')
        self.assertIn('class="layout cols-2"', laid)
        self.assertIn('data-column="2"', slot)
        self.assertIn("Fixture &lt;panel&gt;", slot)
        self.assertIn("Read-only plugin panel.", slot)
        self.assertIn("<pre>", slot)
        self.assertNotIn("<script", slot.lower())
        self.assertEqual(core_region(laid), core_region(default))
        self.assertIn(HEADER, laid)
        self.assertIn(FOOTER, laid)
        # A second request sees a replaced file. Nothing is cached from the first.
        self.write_layout(_layout("fixture.panel", columns=1, column=1, collapsed=True))
        replaced = self.home()[2]
        collapsed = element_at(replaced, 'data-panel="fixture.panel"')
        self.assertTrue(collapsed.startswith("<details"))
        self.assertIn('data-column="1"', collapsed)
        self.assertEqual(core_region(replaced), core_region(default))

    def test_an_untrusted_or_oversized_or_unreadable_layout_leaves_the_page(self) -> None:
        self.mount([{"id": "fixture.panel", "title": "Fixture panel", "reads": [READS[0]]}])
        default = self.home()[2]
        self.write_layout(_layout("fixture.panel", columns=2, column=2), mode=0o666)
        self.assertIsNone(load_user_layout(frozenset({"fixture.panel"})))
        writable = self.home()[2]
        self.assertEqual(core_region(writable), core_region(default))
        self.assertNotIn("data-column=", writable)
        os.chmod(self.layout_path, 0o600)
        os.chmod(self.gui_dir, 0o775)
        self.assertIsNone(load_user_layout(frozenset({"fixture.panel"})))
        os.chmod(self.gui_dir, 0o700)
        self.layout_path.write_bytes(b" " * (MAX_BYTES + 1))
        os.chmod(self.layout_path, 0o600)
        with self.assertRaises(LayoutRefused) as caught:
            read_layout(self.layout_path, known_panels=frozenset())
        self.assertEqual(caught.exception.reason, "too_large")
        self.assertEqual(core_region(self.home()[2]), core_region(default))
        self.layout_path.write_text("{", encoding="utf-8")
        os.chmod(self.layout_path, 0o600)
        self.assertEqual(self.home()[0], "200 OK")
        self.assertNotIn("data-column=", self.home()[2])

    def test_dusk_holds_pinned_safety_text_on_an_opaque_canvas(self) -> None:
        self.mount([])
        themes = self.gui_dir / "themes"
        themes.mkdir(mode=0o700)
        os.chmod(themes, 0o700)
        dusk = {
            "schema": "gui.theme/1", "name": "dusk", "title": "Dusk",
            "tokens": {
                "bg": "#101418", "fg": "#f2f4f7", "muted": "#b8c0cc",
                "accent": "#7cc4ff", "border": "#3a4450",
            },
        }
        theme_path = themes / "dusk.json"
        theme_path.write_text(json.dumps(dusk), encoding="utf-8")
        os.chmod(theme_path, 0o600)
        selection = self.gui_dir / "selection.json"
        selection.write_text(json.dumps({"theme": "dusk"}), encoding="utf-8")
        os.chmod(selection, 0o600)
        status, _, page = self.home()
        self.assertEqual(status, "200 OK")
        self.assertIn('<style id="theme">', page)
        self.assertIn("--theme-bg:#101418", page)
        match = re.search(
            r'section\[aria-label="Visibility"\].*?'
            r'section\[aria-labelledby="actions-h"\] \*'
            r'\{color:canvastext;background:([^;}]+)\}',
            page,
        )
        self.assertIsNotNone(match, page)
        self.assertEqual(match.group(1), "canvas")
        self.assertNotIn("background:transparent", match.group(0))

    def test_a_lone_surrogate_title_is_refused_and_the_page_stays_default(self) -> None:
        body = _layout(None)
        body["title"] = "\ud800"
        with self.assertRaises(LayoutRefused) as caught:
            parse_layout(body, known_panels=frozenset())
        self.assertEqual(caught.exception.reason, "title")
        emoji = "\U0001f600"
        kept = _layout(None)
        kept["title"] = emoji
        parsed = parse_layout(kept, known_panels=frozenset())
        self.assertEqual(parsed["title"], emoji)
        self.mount([])
        self.write_layout(body)
        status, _, page = self.home()
        self.assertEqual(status, "200 OK")
        self.assertNotIn("data-column=", page)

    def test_later_columns_start_on_the_first_row(self) -> None:
        self.mount([])
        document = json.loads((FIXTURES / "good" / "h01-two-columns.json").read_text(encoding="utf-8"))
        self.write_layout(document)
        status, _, page = self.home()
        self.assertEqual(status, "200 OK")
        style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
        self.assertIn(
            ".layout{display:grid;grid-auto-flow:dense;grid-template-columns:minmax(0,1fr)}",
            style,
        )
        narrow = style.split(NARROW, 1)[1]
        self.assertIn("grid-column:auto", narrow)
        interventions = element_at(page, 'data-panel="interventions"')
        self.assertIn('data-column="2"', interventions)

    def test_a_symlink_layout_is_not_applied(self) -> None:
        self.mount([{"id": "fixture.panel", "title": "Fixture panel", "reads": [READS[0]]}])
        outside = self.tmp / "real-layout.json"
        outside.write_text(
            json.dumps(_layout("fixture.panel", columns=2, column=2)), encoding="utf-8")
        os.chmod(outside, 0o600)
        os.symlink(outside, self.layout_path)
        self.assertIsNone(load_user_layout(frozenset({"fixture.panel"})))
        status, _, page = self.home()
        self.assertEqual(status, "200 OK")
        self.assertNotIn("data-column=", page)

    def test_h01_draws_the_three_core_views_without_an_operator_read(self) -> None:
        self.mount([])
        document = json.loads((FIXTURES / "good" / "h01-two-columns.json").read_text(encoding="utf-8"))
        self.write_layout(document)
        self.api.calls.clear()
        status, _, page = self.home()
        self.assertEqual(status, "200 OK")
        self.assertIn("Routes from the control API", page)
        self.assertIn('id="reads-h"', page)
        self.assertIn("I confirm this request", page)
        self.assertEqual([call for call in self.api.calls if call[1] == "/v1/operator"], [])


@unittest.skipUnless(sys.platform.startswith("linux"), "the daemon entry point is Linux-only")
@unittest.skipUnless(backend_available(),
                     "cryptography is not installed for this interpreter, so the loader refuses every plugin")
class MainPluginProofTests(unittest.TestCase):
    """``main(plugin_dirs=[fixture])`` serves the signed panel in column 2."""

    def test_main_plugin_dirs_shows_fixture_panel_in_column_2(self) -> None:
        from recursion_core.daemon import __main__ as entry

        tmp = Path(tempfile.mkdtemp(prefix="g1b-main-"))
        os.chmod(tmp, 0o700)
        self.addCleanup(shutil.rmtree, tmp, True)
        config_home, runtime, state = tmp / "config", tmp / "rt", tmp / "state"
        for path in (config_home, runtime, state):
            path.mkdir(mode=0o700)
            os.chmod(path, 0o700)
        plugin_dir = tmp / "g1_plugin"
        shutil.copytree(PLUGIN, plugin_dir)
        os.chmod(plugin_dir, 0o700)
        for name in ("manifest.json", "manifest.sig", "plugin.py"):
            os.chmod(plugin_dir / name, 0o600)
        manifest = json.loads((plugin_dir / "manifest.json").read_text(encoding="utf-8"))
        source = (plugin_dir / "plugin.py").read_bytes()
        public = manifest["developer_key"]
        self.assertEqual(hashlib.sha256(source).hexdigest(), manifest["artifact_sha256"])
        fx.write_install(config_home / "recursion", trusted={"test": public}, grants=[{
            "plugin_id": manifest["plugin_id"],
            "artifact_sha256": manifest["artifact_sha256"],
            "signer_key_id": key_id(public),
            "permissions": list(manifest["permissions"]),
        }])
        gui_dir = config_home / "recursion" / "gui"
        gui_dir.mkdir(mode=0o700)
        os.chmod(config_home / "recursion", 0o700)
        os.chmod(gui_dir, 0o700)
        layout_path = gui_dir / "layout.json"
        layout_path.write_text(json.dumps(_layout("fixture.panel", columns=2, column=2)), encoding="utf-8")
        os.chmod(layout_path, 0o600)
        environ = {
            "XDG_CONFIG_HOME": str(config_home), "XDG_RUNTIME_DIR": str(runtime),
            "XDG_STATE_HOME": str(state), "HOME": str(tmp), "RECURSION_ENGINE_USER": "",
        }
        seen = {}

        def fetch(gui, token):
            last = None
            for _ in range(30):
                try:
                    connection = HTTPConnection(*gui.address, timeout=20)
                    try:
                        connection.request("GET", "/", headers={"Authorization": "Bearer " + token})
                        response = connection.getresponse()
                        return response.status, response.read().decode("utf-8")
                    finally:
                        connection.close()
                except OSError as exc:
                    last = exc
                    time.sleep(0.05)
            raise AssertionError(last)

        def wait(_timeout=None):
            gui = seen["gui"]
            token = (runtime / "recursion" / "gui-device-token").read_text(encoding="utf-8").strip()
            seen["laid"] = fetch(gui, token)
            layout_path.unlink()
            seen["plain"] = fetch(gui, token)
            return True

        started = {}
        real_start = entry._start_gui

        def start_gui(*args, **kwargs):
            gui = real_start(*args, **kwargs)
            started["gui"] = gui
            seen["gui"] = gui
            return gui

        with unittest.mock.patch.object(entry, "require_free_threaded"), \
             unittest.mock.patch.object(entry, "check_operator_linger"), \
             unittest.mock.patch.object(entry.signal, "signal"), \
             unittest.mock.patch.object(entry, "_start_gui", start_gui), \
             unittest.mock.patch.object(entry, "threading") as threads, \
             unittest.mock.patch.dict(os.environ, environ, clear=False):
            threads.Event.return_value.wait.side_effect = wait
            entry.main(plugin_dirs=(plugin_dir,), port=0, sweep_seconds=0.05)
        self.assertIn("gui", seen)
        status, laid = seen["laid"]
        self.assertEqual(status, 200, laid[:800])
        slot = element_at(laid, 'data-panel="fixture.panel"')
        self.assertIn('data-column="2"', slot)
        self.assertIn("Fixture panel", slot)
        self.assertIn("Read-only plugin panel.", slot)
        self.assertNotIn("<script", slot.lower())
        plain_status, plain = seen["plain"]
        self.assertEqual(plain_status, 200, plain[:800])
        self.assertNotIn("data-column=", plain)
        self.assertIn("/panel?id=fixture.panel", plain)
        self.assertEqual(core_region(laid), core_region(plain))
        self.assertIn(HEADER, laid)
        self.assertIn(FOOTER, laid)
        self.assertIn("I confirm stopping this run", laid)
