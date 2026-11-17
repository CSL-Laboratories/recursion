"""gui.theme/1 on the daemon page: tokens on the workspace, core region left alone.

Run: PYTHONPATH=core python3 -B -m unittest recursion_core.gui.tests.test_themes
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from recursion_core.gui.surface import Application
from recursion_core.gui.tests.test_surface import HOST, ORIGIN, GuiHarness

ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "schemas" / "gui" / "v1" / "fixtures"
SCHEMA_PATH = ROOT / "schemas" / "gui" / "v1" / "theme.v1.json"

# The core stylesheet as it stood before a theme floor was appended. Phone layout
# (max width, the 24px target, the focus outline) is this prefix.
CORE_CSS_PREFIX = (
    "body{font:16px/1.5 system-ui,sans-serif;margin:0 auto;max-width:72rem;padding:0 1rem}"
    ".skip{position:absolute;left:-999px}.skip:focus{left:1rem;top:1rem;background:#fff;padding:.5rem}"
    "a,button,input{min-height:24px;min-width:24px}button{padding:.25rem .75rem}"
    ":focus{outline:3px solid #1a5fb4;outline-offset:2px}table{border-collapse:collapse}"
    "th,td{border:1px solid #666;padding:.25rem .5rem;text-align:left}"
    ".badge{font-weight:700;border:2px solid currentColor;padding:0 .25rem}"
)

PAPER = {
    "bg": "#ffffff", "fg": "#1a1a1a", "muted": "#595959", "accent": "#1a5fb4", "border": "#666666",
}
DUSK = {
    "schema": "gui.theme/1",
    "name": "dusk",
    "title": "Dusk",
    "tokens": {
        "bg": "#101418", "fg": "#f2f4f7", "muted": "#b8c0cc", "accent": "#7cc4ff", "border": "#3a4450",
    },
}
DUSK_CSS = (
    "[data-theme-scope]{--theme-bg:#101418;--theme-fg:#f2f4f7;--theme-muted:#b8c0cc;"
    "--theme-accent:#7cc4ff;--theme-border:#3a4450;}"
)

# Nearest 8-bit greys under the floors. #777777 on #ffffff is 4.478:1 (the brief's 4.49:1
# has no 8-bit sRGB pair). #959595 on #ffffff is 2.995:1 (the brief's 2.99:1).
UNDER_TEXT = "#777777"
UNDER_ACCENT = "#959595"
ON_TEXT = "#767676"      # 4.542:1 on white, must be kept
ON_ACCENT = "#949494"    # 3.033:1 on white, must be kept

_MAIN = re.compile(r'<main id="main"[^>]*>(.*)</main>', re.S)
_THEME_STYLE = re.compile(r'<style id="theme">(.*?)</style>', re.S)
_ANY_STYLE = re.compile(r'<style(?: id="theme")?>(.*?)</style>', re.S)
_DECL = re.compile(
    r"--theme-(?:bg|fg|muted|accent|border):#[0-9a-f]{6}"
    r"|--theme-density:(?:compact|normal|roomy)"
    r"|--theme-radius:(?:0|2|4|8)px"
)


def _parser():
    try:
        from recursion_core.gui import themes
    except ImportError as exc:
        raise AssertionError("theme parser is absent, so a bad theme cannot be refused (%s)" % exc) from None
    return themes


def _main(body: str) -> str:
    match = _MAIN.search(body)
    if match is None:
        raise AssertionError("page has no main")
    return match.group(1)


def _core(body: str) -> str:
    """Page bytes outside the theme style and outside the themed main."""
    return _MAIN.sub("", _THEME_STYLE.sub("", body))


def _styles(body: str) -> list[str]:
    return _ANY_STYLE.findall(body)


def _directives(header: str) -> dict:
    out = {}
    for part in header.split(";"):
        words = part.split()
        if words:
            out[words[0].lower()] = words[1:]
    return out


def _own(path: Path, mode: int) -> None:
    os.chmod(path, mode)


def _drop_os(*names: str) -> list:
    """Remove POSIX-only ``os`` names for one test, the way the simulated-Windows child does.

    A name that is already absent stays absent. The caller puts the saved values back.
    """
    saved = []
    for name in names:
        if hasattr(os, name):
            saved.append((name, getattr(os, name)))
            delattr(os, name)
    return saved


def _restore_os(saved: list) -> None:
    for name, value in reversed(saved):
        setattr(os, name, value)


class _ShiftedUid:
    """Stat fields the loader reads, with the user id one higher.

    This is not an ``os.stat_result``. The simulated-Windows child hides the
    posix module, and building a ``stat_result`` then faults the child.
    """

    def __init__(self, info) -> None:
        self.st_mode = info.st_mode
        self.st_uid = info.st_uid + 1
        self.st_gid = info.st_gid
        self.st_size = info.st_size


def _other_uid(info):
    return _ShiftedUid(info)


def _write_gui(directory: Path, themes: dict, selection) -> Path:
    """An operator-owned gui directory: themes/*.json and selection.json."""
    gui = Path(directory)
    folder = gui / "themes"
    folder.mkdir(parents=True, exist_ok=True)
    _own(gui, 0o700)
    _own(folder, 0o700)
    for name, payload in themes.items():
        path = folder / name
        if isinstance(payload, bytes):
            path.write_bytes(payload)
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")
        _own(path, 0o600)
    if selection is not None:
        chosen = gui / "selection.json"
        raw = selection if isinstance(selection, bytes) else json.dumps(selection).encode("utf-8")
        chosen.write_bytes(raw)
        _own(chosen, 0o600)
    return gui


def _paper_with(**tokens) -> dict:
    document = {
        "schema": "gui.theme/1",
        "name": "bad",
        "title": "Bad",
        "tokens": dict(PAPER),
    }
    document["tokens"].update(tokens)
    return document


class ThemeParserTests(unittest.TestCase):
    def themes(self):
        return _parser()

    def test_n1_unknown_or_core_token_is_refused(self) -> None:
        themes = self.themes()
        for label, document in (
            ("unknown", _paper_with(shadow="#112233")),
            ("core-bg", _paper_with(**{"core-bg": "#000000"})),
            ("core-star", _paper_with(**{"core-*": "#000000"})),
            ("core-fg", _paper_with(**{"core-fg": "#ffffff"})),
        ):
            with self.subTest(token=label):
                with self.assertRaises(themes.ThemeError) as caught:
                    themes.parse_theme(document)
                self.assertEqual(caught.exception.code, "theme_token_unknown")

    def test_n2_each_bad_value_form_is_refused(self) -> None:
        themes = self.themes()
        forms = {
            "url": "url(http://127.0.0.1/a)",
            "var": "var(--core-bg)",
            "semicolon": "#ffffff;color:red",
            "brace": "#ffffff}",
            "named": "red",
            "alpha": "#ffffff80",
            "expression": "expression(alert(1))",
            "short": "#fff",
            "space": " #ffffff",
        }
        for label, value in forms.items():
            with self.subTest(form=label):
                with self.assertRaises(themes.ThemeError) as caught:
                    themes.parse_theme(_paper_with(fg=value))
                self.assertEqual(caught.exception.code, "theme_colour")

    def test_n3_contrast_below_the_floor_is_refused_and_the_boundary_is_kept(self) -> None:
        themes = self.themes()
        refused = {
            "fg": _paper_with(fg=UNDER_TEXT),
            "muted": _paper_with(muted=UNDER_TEXT),
            "accent": _paper_with(accent=UNDER_ACCENT),
        }
        for label, document in refused.items():
            with self.subTest(below=label):
                with self.assertRaises(themes.ThemeError) as caught:
                    themes.parse_theme(document)
                self.assertEqual(caught.exception.code, "theme_contrast")
        kept = themes.parse_theme(_paper_with(fg=ON_TEXT, muted=ON_TEXT, accent=ON_ACCENT))
        self.assertIn("--theme-fg:%s" % ON_TEXT, kept.css)
        self.assertIn("--theme-accent:%s" % ON_ACCENT, kept.css)

    def test_a_document_compiles_to_theme_properties_only(self) -> None:
        themes = self.themes()
        parsed = themes.parse_theme(DUSK)
        self.assertEqual(parsed.css, DUSK_CSS)
        self.assertRegex(parsed.css, r"^\[data-theme-scope\]\{(?:(?:%s);)+\}$" % _DECL.pattern)
        self.assertNotIn("url(", parsed.css)
        self.assertNotIn("var(", parsed.css)
        self.assertNotIn("expression", parsed.css)
        caps = themes.parse_theme(json.loads((FIXTURES / "good" / "g05-caps.json").read_text(encoding="utf-8")))
        self.assertIn("--theme-bg:#ffffff", caps.css)
        self.assertNotIn("#FFFFFF", caps.css)
        with_optional = themes.parse_theme(json.loads(
            (FIXTURES / "good" / "g03-compact.json").read_text(encoding="utf-8")))
        self.assertIn("--theme-density:compact", with_optional.css)
        self.assertIn("--theme-radius:2px", with_optional.css)

    def test_checked_in_fixtures_match_the_parser_and_the_schema(self) -> None:
        themes = self.themes()
        good = sorted((FIXTURES / "good").glob("*.json"))
        bad = sorted((FIXTURES / "bad").glob("*.json"))
        self.assertGreaterEqual(len(good), 6)
        self.assertGreaterEqual(len(bad), 9)
        schema_errors = _schema_errors_or_none()
        for path in good:
            with self.subTest(path=path.name):
                document = json.loads(path.read_text(encoding="utf-8"))
                parsed = themes.parse_theme(document)
                self.assertRegex(parsed.css, r"^\[data-theme-scope\]\{(?:(?:%s);)+\}$" % _DECL.pattern)
                if schema_errors is not None:
                    self.assertEqual(schema_errors(document), [], path.name)
        # Contrast floors, and radius 4.0, which a schema checker treats as the integer 4.
        parser_only = {
            "b10-fg-contrast.json",
            "b11-muted-contrast.json",
            "b12-accent-contrast.json",
            "b20-radius-float.json",
        }
        self.assertIn("b20-radius-float.json", parser_only)
        for path in bad:
            with self.subTest(path=path.name):
                raw = path.read_bytes()
                if path.name == "b21-duplicate-member.json":
                    with self.assertRaises(ValueError):
                        json.loads(raw.decode("utf-8"), object_pairs_hook=themes.no_duplicate_members)
                    continue
                document = json.loads(raw.decode("utf-8"))
                with self.assertRaises(themes.ThemeError):
                    themes.parse_theme(document)
                if schema_errors is not None and path.name in parser_only:
                    self.assertEqual(schema_errors(document), [], path.name)

    def test_other_whole_document_defects_are_refused(self) -> None:
        themes = self.themes()
        samples = [
            ("top", {**DUSK, "css": "x"}),
            ("schema", {**DUSK, "schema": "gui.theme/2"}),
            ("name", {**DUSK, "name": "Dusk"}),
            ("title", {**DUSK, "title": "bad\nline"}),
            ("empty-title", {**DUSK, "title": ""}),
            ("radius-bool", _paper_with(radius=True)),
            ("radius-string", _paper_with(radius="4")),
            ("density", _paper_with(density="tight")),
            ("not-object", ["dusk"]),
            ("missing", {"schema": "gui.theme/1"}),
        ]
        for label, document in samples:
            with self.subTest(defect=label):
                with self.assertRaises(themes.ThemeError):
                    themes.parse_theme(document)

    def test_a_trailing_newline_is_refused_by_the_schema_and_the_parser(self) -> None:
        # Draft 2020-12 searches the pattern, and ``$`` matches before a final newline.
        # The page uses ``fullmatch``, which does not. Both must refuse the document.
        themes = self.themes()
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        samples = {
            "name": (
                schema["properties"]["name"]["pattern"],
                {**DUSK, "name": "dusk\n"},
                "dusk\n",
                "theme_name",
            ),
            "title": (
                schema["properties"]["title"]["pattern"],
                {**DUSK, "title": "Dusk\n"},
                "Dusk\n",
                "theme_title",
            ),
            "colour": (
                schema["$defs"]["colour"]["pattern"],
                _paper_with(fg="#1a1a1a\n"),
                "#1a1a1a\n",
                "theme_colour",
            ),
        }
        schema_errors = _schema_errors_or_none()
        for label, (pattern, document, value, code) in samples.items():
            with self.subTest(field=label):
                self.assertIsNone(
                    re.search(pattern, value),
                    "%s pattern still matches a trailing newline" % label)
                with self.assertRaises(themes.ThemeError) as caught:
                    themes.parse_theme(document)
                self.assertEqual(caught.exception.code, code)
                if schema_errors is not None:
                    self.assertTrue(schema_errors(document), label)

    def test_radius_float_is_parser_only_and_the_schema_names_it(self) -> None:
        themes = self.themes()
        document = json.loads((FIXTURES / "bad" / "b20-radius-float.json").read_text(encoding="utf-8"))
        with self.assertRaises(themes.ThemeError) as caught:
            themes.parse_theme(document)
        self.assertEqual(caught.exception.code, "theme_radius")
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertIn("4.0", schema["description"])
        schema_errors = _schema_errors_or_none()
        if schema_errors is not None:
            self.assertEqual(schema_errors(document), [], "b20-radius-float.json")

    def test_config_dir_refuses_a_relative_base(self) -> None:
        themes = self.themes()
        self.assertEqual(
            themes.gui_config_dir({"XDG_CONFIG_HOME": "/cfg", "HOME": "/home/op"}),
            Path("/cfg/recursion/gui"))
        self.assertEqual(
            themes.gui_config_dir({"XDG_CONFIG_HOME": "", "HOME": "/home/op"}),
            Path("/home/op/.config/recursion/gui"))
        self.assertIsNone(themes.gui_config_dir({"XDG_CONFIG_HOME": "relative", "HOME": "/home/op"}))
        self.assertIsNone(themes.gui_config_dir({}))
        self.assertIsNone(themes.gui_config_dir({"XDG_CONFIG_HOME": 5, "HOME": "/home/op"}))


class ThemePageTests(GuiHarness):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="gui-theme-")
        self.addCleanup(self._tmp.cleanup)

    def _gui(self, themes: dict, selection) -> Path:
        # A fresh directory per call, so one case cannot leave a file for the next.
        self._gui_n = getattr(self, "_gui_n", 0) + 1
        return _write_gui(Path(self._tmp.name) / ("gui-%d" % self._gui_n), themes, selection)

    def _app(self, root: Path | None):
        try:
            return Application(self.app._client, self.door, frozenset({HOST}), frozenset({ORIGIN}),
                               theme_root=root)
        except TypeError as exc:
            self.fail("the page does not accept an operator theme directory (%s)" % exc)

    def _render(self, app, method="GET", path="/", form=None, query="", headers=None):
        previous = self.app
        self.app = app
        try:
            return self.request(method, path, form, query=query, headers=headers)
        finally:
            self.app = previous

    def _plain(self):
        """A page whose theme directory is empty: the core default."""
        root = self._gui({}, {"theme": None})
        return self._render(self._app(root))

    def test_a_good_theme_sets_workspace_tokens_and_a_second_csp_hash(self) -> None:
        root = self._gui({"dusk.json": DUSK}, {"theme": "dusk"})
        status, headers, body = self._render(self._app(root))
        self.assertEqual(status, "200 OK")
        self.assertIn('<main id="main" data-theme-scope', body)
        styles = _styles(body)
        self.assertEqual(len(styles), 2, "a good theme adds a second style element")
        self.assertEqual(styles[1], DUSK_CSS)
        self.assertTrue(styles[0].startswith(CORE_CSS_PREFIX), "phone-width core rules changed")
        policy = _directives(headers["Content-Security-Policy"])
        import base64
        import hashlib
        hashes = ["'sha256-%s'" % base64.b64encode(hashlib.sha256(sheet.encode()).digest()).decode()
                  for sheet in styles]
        self.assertEqual(policy["style-src"], hashes)
        self.assertEqual(len(hashes), 2)
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertNotIn("#101418", styles[0])
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', body)
        self.assertNotIn("position:fixed", styles[0])
        self.assertEqual(styles[0].count("min-width:"), 1)

    def test_n1_unknown_or_core_token_leaves_the_page_unchanged(self) -> None:
        plain = self._plain()
        for label, document in (
            ("unknown", _paper_with(shadow="#112233")),
            ("core-bg", _paper_with(**{"core-bg": "#000000"})),
            ("core-star", _paper_with(**{"core-*": "#000000"})),
        ):
            with self.subTest(token=label):
                root = self._gui({"bad.json": document}, {"theme": "bad"})
                got = self._render(self._app(root))
                self.assertEqual(got[2], plain[2])
                self.assertEqual(got[1]["Content-Security-Policy"], plain[1]["Content-Security-Policy"])
                self.assertNotIn("<style id=\"theme\">", got[2])

    def test_n2_each_bad_value_form_leaves_the_page_unchanged(self) -> None:
        plain = self._plain()
        forms = {
            "url": "url(http://127.0.0.1/a)",
            "var": "var(--core-bg)",
            "semicolon": "#ffffff;color:red",
            "brace": "#ffffff}",
            "named": "red",
            "alpha": "#ffffff80",
            "expression": "expression(alert(1))",
        }
        for label, value in forms.items():
            with self.subTest(form=label):
                root = self._gui({"bad.json": _paper_with(fg=value)}, {"theme": "bad"})
                got = self._render(self._app(root))
                self.assertEqual(got[2], plain[2])
                self.assertEqual(got[1]["Content-Security-Policy"], plain[1]["Content-Security-Policy"])

    def test_n3_contrast_below_the_floor_leaves_the_page_unchanged(self) -> None:
        plain = self._plain()
        for label, document in (
            ("fg-4.49", _paper_with(fg=UNDER_TEXT)),
            ("muted-4.49", _paper_with(muted=UNDER_TEXT)),
            ("accent-2.99", _paper_with(accent=UNDER_ACCENT)),
        ):
            with self.subTest(below=label):
                root = self._gui({"bad.json": document}, {"theme": "bad"})
                got = self._render(self._app(root))
                self.assertEqual(got[2], plain[2])
                self.assertNotIn(UNDER_TEXT, got[2])
                self.assertNotIn(UNDER_ACCENT, got[2])

    def test_n4_good_themes_keep_core_region_bytes_and_compiled_css_is_theme_tokens_only(self) -> None:
        body = self.request("GET", "/")[2]
        main = _main(body)
        core = _core(body)
        self.assertNotIn("Request stop", main, "Stop is still inside the themed main")
        self.assertIn("Request stop", core)
        self.assertIn('<aside id="core-approval"', core)
        self.assertRegex(body, r'<aside id="core-approval"[^>]*></aside>')
        self.assertNotIn("core-approval", main)
        self.assertIn("Every route is BETA", core)
        self.assertNotIn("Every route is BETA", main)
        self.assertIn("confirmed shows as pending", core)
        self.assertNotIn("confirmed shows as pending", main)
        styles = _styles(body)
        self.assertTrue(styles[0].startswith(CORE_CSS_PREFIX))
        self.assertIn(':focus{outline:3px solid #1a5fb4', styles[0])
        self.assertIn("min-height:24px", styles[0])
        self.assertIn("min-width:24px", styles[0])
        self.assertIn('section[aria-label="Visibility"]', styles[0])
        self.assertIn('section[aria-labelledby="actions-h"]', styles[0])
        self.assertIn("color:canvastext", styles[0])
        plain = self._plain()
        self.assertEqual(_core(plain[2]), core)
        documents = [json.loads(path.read_text(encoding="utf-8"))
                     for path in sorted((FIXTURES / "good").glob("*.json"))]
        self.assertGreaterEqual(len(documents), 1)
        for document in documents:
            with self.subTest(theme=document["name"]):
                root = self._gui({"theme.json": document}, {"theme": document["name"]})
                status, headers, themed = self._render(self._app(root))
                self.assertEqual(status, "200 OK")
                self.assertEqual(_core(themed), core)
                match = _THEME_STYLE.search(themed)
                self.assertIsNotNone(match, document["name"])
                css = match.group(1)
                decls = [part for part in css.removeprefix("[data-theme-scope]{").removesuffix("}").split(";") if part]
                self.assertTrue(decls)
                self.assertTrue(all(_DECL.fullmatch(part) for part in decls), css)
                self.assertNotIn("url(", css)
                self.assertNotIn("var(", css)
                self.assertNotIn("expression", css)
                self.assertNotIn("<", css)
                bg = document["tokens"]["bg"].lower()
                outside = _THEME_STYLE.sub("", themed)
                self.assertNotIn(bg, outside, document["name"])
                self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])

    def test_n9_every_response_csp_has_no_unsafe_inline_and_only_core_hashes(self) -> None:
        self._assert_csp_responses(self.app)
        root = self._gui({"dusk.json": DUSK}, {"theme": "dusk"})
        try:
            themed = Application(self.app._client, self.door, frozenset({HOST}), frozenset({ORIGIN}),
                                 theme_root=root)
        except TypeError:
            # Master has no theme directory. The unthemed half above is the whole check there.
            return
        self._assert_csp_responses(themed)
        _status, headers, body = self._render(themed)
        self.assertEqual(len(_styles(body)), 2)
        self.assertEqual(len(_directives(headers["Content-Security-Policy"])["style-src"]), 2)

    def _assert_csp_responses(self, app) -> None:
        import base64
        import hashlib
        previous = self.app
        self.app = app
        try:
            cases = {
                "200 OK": lambda: self.request("GET", "/"),
                "400 Bad Request": lambda: self.request("GET", "/read", query="command=not-a-command"),
                "403 Forbidden": lambda: self.request("GET", "/", headers={"HTTP_HOST": "evil.example"}),
                "404 Not Found": lambda: self.request("GET", "/nowhere"),
                "405 Method Not Allowed": lambda: self.request("POST", "/panel", {"id": "x"}),
            }
            responses = {status: make() for status, make in cases.items()}
            self.door.ok = False
            responses["401 Unauthorized"] = self.request("GET", "/")
            self.door.ok = True
        finally:
            self.app = previous
        for expected, (status, headers, body) in responses.items():
            with self.subTest(status=expected):
                self.assertEqual(status, expected)
                policy = _directives(headers["Content-Security-Policy"])
                styles = _styles(body)
                self.assertGreaterEqual(len(styles), 1)
                hashes = ["'sha256-%s'" % base64.b64encode(hashlib.sha256(sheet.encode()).digest()).decode()
                          for sheet in styles]
                self.assertEqual(policy["default-src"], ["'none'"])
                self.assertEqual(policy["style-src"], hashes)
                self.assertNotIn("style-src-attr", policy)
                raw = headers["Content-Security-Policy"]
                self.assertNotIn("unsafe-inline", raw)
                self.assertNotIn("unsafe-eval", raw)
                self.assertNotIn("unsafe-hashes", raw)
                self.assertNotIn("<script", body.lower())
                for values in policy.values():
                    self.assertNotIn("*", values)

    def test_only_the_selected_owned_theme_is_applied(self) -> None:
        present = self._gui({"dusk.json": DUSK}, None)
        self.assertNotIn("<style id=\"theme\">", self._render(self._app(present))[2])
        unknown = self._gui({"dusk.json": DUSK}, {"theme": "missing"})
        self.assertNotIn("<style id=\"theme\">", self._render(self._app(unknown))[2])
        default = self._gui({"dusk.json": DUSK}, {"theme": None})
        self.assertNotIn("<style id=\"theme\">", self._render(self._app(default))[2])
        extra = self._gui({"dusk.json": DUSK}, {"theme": "dusk", "layout": "wide"})
        self.assertNotIn("<style id=\"theme\">", self._render(self._app(extra))[2])
        marked = deepcopy(DUSK)
        marked["title"] = "Dusk <script>"
        root = self._gui({"dusk.json": marked}, {"theme": "dusk"})
        body = self._render(self._app(root))[2]
        self.assertNotIn("<script>", body)
        self.assertNotIn("Dusk <script>", body)
        self.assertIn(DUSK_CSS, body)
        slipped = self._gui({"dusk.json": DUSK}, {"theme": "../dusk"})
        self.assertNotIn("<style id=\"theme\">", self._render(self._app(slipped))[2])
        other = deepcopy(DUSK)
        other["title"] = "Second"
        twins = self._gui({"a.json": DUSK, "b.json": other}, {"theme": "dusk"})
        self.assertNotIn("<style id=\"theme\">", self._render(self._app(twins))[2])

    def test_a_directory_over_the_cap_is_not_applied(self) -> None:
        themes = self.themes() if False else None  # cap is enforced by the loader, not the parser
        del themes
        many = {}
        for number in range(65):
            document = deepcopy(DUSK)
            document["name"] = "n%02d" % number
            many["n%02d.json" % number] = document
        over = self._gui(many, {"theme": "n00"})
        self.assertNotIn("<style id=\"theme\">", self._render(self._app(over))[2])
        kept = dict(list(many.items())[:64])
        under = self._gui(kept, {"theme": "n00"})
        body = self._render(self._app(under))[2]
        self.assertIn("--theme-bg:#101418", body)
        self.assertIn("<style id=\"theme\">", body)

    def test_untrusted_files_and_a_second_caller(self) -> None:
        root = self._gui({"dusk.json": DUSK}, {"theme": "dusk"})
        chosen = root / "selection.json"
        _own(chosen, 0o666)
        self.assertNotIn("<style id=\"theme\">", self._render(self._app(root))[2])
        _own(chosen, 0o600)
        link = root / "themes" / "alias.json"
        outside = Path(self._tmp.name) / "outside.json"
        outside.write_text(json.dumps(_paper_with()), encoding="utf-8")
        os.symlink(outside, link)
        body = self._render(self._app(root))[2]
        self.assertIn(DUSK_CSS, body)
        self.assertNotIn("url(", body)
        _own(root / "themes", 0o777)
        self.assertNotIn("<style id=\"theme\">", self._render(self._app(root))[2])
        _own(root / "themes", 0o700)
        huge = root / "themes" / "huge.json"
        huge.write_bytes(b"{" + b" " * 9000)
        _own(huge, 0o600)
        self.assertIn(DUSK_CSS, self._render(self._app(root))[2])
        nested = b'{"a":' * 1200 + b"1" + b"}" * 1200
        self.assertLess(len(nested), 8192)
        (root / "themes" / "deep.json").write_bytes(nested)
        _own(root / "themes" / "deep.json", 0o600)
        self.assertIn(DUSK_CSS, self._render(self._app(root))[2])
        found: list = []
        app = self._app(root)

        def once() -> None:
            env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "QUERY_STRING": "", "HTTP_HOST": HOST,
                   "CONTENT_LENGTH": "0", "CONTENT_TYPE": "application/x-www-form-urlencoded",
                   "wsgi.input": io.BytesIO(b""), "wsgi.errors": sys.stderr}
            status: list = []
            page = b"".join(app(env, lambda s, h: status.append(s))).decode("utf-8")
            found.append((status[0], page))

        threads = [threading.Thread(target=once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0], found[1])
        self.assertIn(DUSK_CSS, found[0][1])
        self.assertNotIn("deep", found[0][1])

    def test_pinned_safety_text_uses_an_opaque_canvas_background(self) -> None:
        sheet = _styles(self.request("GET", "/")[2])[0]
        self._assert_pinned_safety_background(sheet)

    def _assert_pinned_safety_background(self, sheet: str) -> None:
        # Visibility and intervention text sit on canvas, the same pair as Stop
        # and the approval region. A transparent background lets --theme-bg through.
        match = re.search(
            r'section\[aria-label="Visibility"\].*?'
            r'section\[aria-labelledby="actions-h"\] \*'
            r'\{color:canvastext;background:([^;}]+)\}',
            sheet,
        )
        self.assertIsNotNone(match, sheet)
        self.assertEqual(match.group(1), "canvas")
        self.assertNotIn("background:transparent", match.group(0))
        self.assertIn(
            'header,footer,nav,#core-approval,section[aria-labelledby="stop-h"]'
            "{color:canvastext;background:canvas}",
            sheet,
        )

    def test_the_daemon_path_reads_the_operator_directory(self) -> None:
        xdg = Path(self._tmp.name) / "xdg-daemon"
        _write_gui(xdg / "recursion" / "gui", {"dusk.json": DUSK}, {"theme": "dusk"})
        saved = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(xdg)
        try:
            app = Application(
                self.app._client, self.door, frozenset({HOST}), frozenset({ORIGIN}))
            status, headers, body = self._render(app)
        finally:
            if saved is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = saved
        self.assertEqual(status, "200 OK")
        self.assertIn('<style id="theme">', body)
        self.assertIn(DUSK_CSS, body)
        self.assertEqual(len(_directives(headers["Content-Security-Policy"])["style-src"]), 2)

    def test_a_selected_symlink_is_not_applied(self) -> None:
        root = self._gui({}, {"theme": "dusk"})
        outside = Path(self._tmp.name) / "real-dusk.json"
        outside.write_text(json.dumps(DUSK), encoding="utf-8")
        _own(outside, 0o600)
        os.symlink(outside, root / "themes" / "dusk.json")
        body = self._render(self._app(root))[2]
        self.assertNotIn('<style id="theme">', body)
        self.assertNotIn(DUSK_CSS, body)

    def test_a_repeated_member_applies_nothing(self) -> None:
        selected = self._gui({"dusk.json": DUSK}, None)
        choice = selected / "selection.json"
        choice.write_bytes(b'{"theme": null, "theme": "dusk"}')
        _own(choice, 0o600)
        self.assertNotIn('<style id="theme">', self._render(self._app(selected))[2])
        # Last-wins would keep the dusk bg. The repeated member must refuse the file.
        raw = (
            b'{"schema":"gui.theme/1","name":"dusk","title":"Dusk","tokens":{'
            b'"bg":"#ffffff","fg":"#f2f4f7","muted":"#b8c0cc","accent":"#7cc4ff","border":"#3a4450",'
            b'"bg":"#101418"}}'
        )
        doubled = self._gui({}, {"theme": "dusk"})
        path = doubled / "themes" / "dusk.json"
        path.write_bytes(raw)
        _own(path, 0o600)
        body = self._render(self._app(doubled))[2]
        self.assertNotIn('<style id="theme">', body)
        self.assertNotIn(DUSK_CSS, body)

    def test_a_valid_theme_over_the_byte_cap_is_not_applied(self) -> None:
        # 8193 is one byte over the cap. Sizing from the constant would hide a raised cap.
        body = json.dumps(DUSK, separators=(",", ":")).encode("utf-8")
        pad = 8193 - len(body)
        self.assertGreater(pad, 0)
        over = b"{" + b" " * pad + body[1:]
        under = b"{" + b" " * 20 + body[1:]
        self.assertEqual(len(over), 8193)
        self.assertLess(len(under), 8192)
        huge = self._gui({}, {"theme": "dusk"})
        huge_path = huge / "themes" / "dusk.json"
        huge_path.write_bytes(over)
        _own(huge_path, 0o600)
        self.assertNotIn('<style id="theme">', self._render(self._app(huge))[2])
        small = self._gui({}, {"theme": "dusk"})
        small_path = small / "themes" / "dusk.json"
        small_path.write_bytes(under)
        _own(small_path, 0o600)
        rendered = self._render(self._app(small))[2]
        self.assertIn('<style id="theme">', rendered)
        self.assertIn(DUSK_CSS, rendered)

    def test_another_owners_theme_is_not_applied(self) -> None:
        themes = _parser()
        root = self._gui({"dusk.json": DUSK}, {"theme": "dusk"})
        real_lstat = themes.os.lstat
        real_fstat = themes.os.fstat

        def other_lstat(path):
            return _other_uid(real_lstat(path))

        def other_fstat(fd):
            return _other_uid(real_fstat(fd))

        with mock.patch.object(themes.os, "lstat", side_effect=other_lstat):
            self.assertNotIn('<style id="theme">', self._render(self._app(root))[2])
        with mock.patch.object(themes.os, "fstat", side_effect=other_fstat):
            self.assertNotIn('<style id="theme">', self._render(self._app(root))[2])

    def test_a_missing_theme_directory_does_not_fail_the_page(self) -> None:
        status, _headers, body = self._render(self._app(Path(self._tmp.name) / "absent"))
        self.assertEqual(status, "200 OK")
        self.assertNotIn("<style id=\"theme\">", body)
        self.assertIn("Request stop", _core(body))

    def test_an_owned_theme_applies_when_geteuid_is_absent(self) -> None:
        # os.geteuid is POSIX-only. The simulated-Windows child deletes it, and a
        # Windows host has no such call. An owned theme must still reach the page.
        # Group write and another uid must still apply nothing.
        root = self._gui({"dusk.json": DUSK}, {"theme": "dusk"})
        saved = _drop_os("geteuid")
        try:
            body = self._render(self._app(root))[2]
            self.assertIn(DUSK_CSS, body)
            self.assertIn('<style id="theme">', body)
            chosen = root / "selection.json"
            _own(chosen, 0o666)
            self.assertNotIn('<style id="theme">', self._render(self._app(root))[2])
            _own(chosen, 0o600)
            themes = _parser()
            real_fstat = themes.os.fstat

            def other_fstat(fd):
                return _other_uid(real_fstat(fd))

            with mock.patch.object(themes.os, "fstat", side_effect=other_fstat):
                self.assertNotIn('<style id="theme">', self._render(self._app(root))[2])
        finally:
            _restore_os(saved)

    def test_a_symlink_theme_is_refused_when_o_nofollow_is_absent(self) -> None:
        # os.O_NOFOLLOW is POSIX-only. Without it, os.open follows a symlink.
        # The selected theme must still apply nothing; a real file still applies.
        root = self._gui({}, {"theme": "dusk"})
        outside = Path(self._tmp.name) / "real-dusk.json"
        outside.write_text(json.dumps(DUSK), encoding="utf-8")
        _own(outside, 0o600)
        os.symlink(outside, root / "themes" / "dusk.json")
        real = self._gui({"dusk.json": DUSK}, {"theme": "dusk"})
        saved = _drop_os("O_NOFOLLOW")
        try:
            body = self._render(self._app(root))[2]
            self.assertNotIn('<style id="theme">', body)
            self.assertNotIn(DUSK_CSS, body)
            kept = self._render(self._app(real))[2]
            self.assertIn(DUSK_CSS, kept)
        finally:
            _restore_os(saved)


def _schema_errors_or_none():
    try:
        import jsonschema
    except ImportError:
        return None
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)

    def errors(document):
        return list(validator.iter_errors(document))

    return errors


if __name__ == "__main__":
    unittest.main()
