"""What the desktop's frontend may not contain, read from its source (desktop/ui).

It opens no connection and makes no HTTP request: every request goes through the proxy, and the page's
policy allows no connection at all. It writes no markup from data. It sends nothing when it closes. A theme
cannot reach the approval region, and nothing can be drawn above it.
"""

from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "ui"
SCRIPTS = sorted((UI / "js").glob("*.js"))
HTML = (UI / "index.html").read_text(encoding="utf-8")
CSS = (UI / "style.css").read_text(encoding="utf-8")
CSP = {
    "default-src": ["'none'"], "script-src": ["'self'"], "style-src": ["'self'"], "img-src": ["'self'"],
    "font-src": ["'self'"], "connect-src": ["'none'"], "object-src": ["'none'"], "base-uri": ["'none'"],
    "form-action": ["'none'"],
}
MARKUP_SINKS = re.compile(r"\binnerHTML\b|\bouterHTML\b|insertAdjacentHTML|document\.write|\beval\s*\(|new\s+Function\b"
                          r"|createContextualFragment|DOMParser|srcdoc")
NETWORK = re.compile(r"\bfetch\s*\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon|\bimport\s*\(|https?://|"
                     r"RTCPeerConnection|\bWorker\s*\(|navigator\.serviceWorker|window\.open")
CLOSING = re.compile(r"""addEventListener\(\s*["'](?:beforeunload|unload|pagehide|visibilitychange|freeze)["']"""
                     r"|\bon(?:beforeunload|unload|pagehide|visibilitychange)\b")


def code_of(path: Path) -> str:
    """The source with comments removed, so a sentence about a rule does not count as breaking it. A '//'
    right after ':' is kept, so a URL is never taken for a comment."""
    text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
    return re.sub(r"(?<!:)//.*$", "", text, flags=re.M)


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict, list[str]]] = []
        self.stack: list[str] = []
        self.script_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs), list(self.stack)))
        if tag not in ("meta", "link", "input", "br", "img"):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        while self.stack and self.stack.pop() != tag:
            pass

    def handle_data(self, data):
        if self.stack and self.stack[-1] in ("script", "style") and data.strip():
            self.script_text.append(data)


def page() -> Page:
    parser = Page()
    parser.feed(HTML)
    return parser


def css_rules() -> list[tuple[str, str]]:
    body = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    return [(sel.strip(), block) for sel, block in re.findall(r"([^{}]+)\{([^{}]*)\}", body)]


class ScriptTest(unittest.TestCase):
    def test_there_are_scripts_to_check(self) -> None:
        self.assertGreaterEqual({p.name for p in SCRIPTS},
                                {"app.js", "actions.js", "model.js", "sanitize.js", "transport.js", "views.js"})

    def test_no_script_writes_markup(self) -> None:
        for path in SCRIPTS:
            with self.subTest(path.name):
                self.assertIsNone(MARKUP_SINKS.search(code_of(path)))

    def test_no_script_opens_a_connection_or_makes_a_request(self) -> None:
        for path in SCRIPTS:
            with self.subTest(path.name):
                self.assertIsNone(NETWORK.search(code_of(path)))

    def test_no_script_acts_when_the_window_closes(self) -> None:
        for path in SCRIPTS:
            with self.subTest(path.name):
                self.assertIsNone(CLOSING.search(code_of(path)))

    def test_scripts_import_only_each_other(self) -> None:
        names = {p.name for p in SCRIPTS}
        for path in SCRIPTS:
            for target in re.findall(r"""^\s*import\b[^;]*?from\s+["']([^"']+)["']""", path.read_text(encoding="utf-8"),
                                     flags=re.M | re.S):
                with self.subTest(f"{path.name} -> {target}"):
                    self.assertTrue(target.startswith("./"))
                    self.assertIn(target[2:], names)

    def test_sources_are_plain_ascii(self) -> None:
        for path in [*SCRIPTS, UI / "index.html", UI / "style.css"]:
            with self.subTest(path.name):
                data = path.read_bytes()
                self.assertTrue(all(b == 0x0A or 0x20 <= b <= 0x7E for b in data))


class PageTest(unittest.TestCase):
    def test_the_policy_allows_no_connection_and_no_inline_code(self) -> None:
        metas = [a for t, a, _ in page().tags if t == "meta" and a.get("http-equiv") == "Content-Security-Policy"]
        self.assertEqual(len(metas), 1)
        directives = {}
        for part in metas[0]["content"].split(";"):
            name, *values = part.split()
            directives[name] = values
        self.assertEqual(directives, CSP)

    def test_scripts_load_from_files_and_no_element_carries_a_handler(self) -> None:
        parsed = page()
        self.assertEqual(parsed.script_text, [])
        scripts = [a for t, a, _ in parsed.tags if t == "script"]
        self.assertEqual(scripts, [{"type": "module", "src": "js/app.js"}])
        for tag, attrs, _ in parsed.tags:
            with self.subTest(tag):
                self.assertFalse([k for k in attrs if k.startswith("on")])
                self.assertNotIn("style", attrs)

    def test_nothing_is_loaded_from_elsewhere(self) -> None:
        self.assertIsNone(re.search(r"https?://|//[a-z]", HTML + CSS))

    def test_the_approval_region_is_the_cores_and_outside_the_theme(self) -> None:
        parsed = page()
        regions = [(a, parents) for t, a, parents in parsed.tags if a.get("id") == "core-approval"]
        self.assertEqual(len(regions), 1)
        attrs, parents = regions[0]
        self.assertEqual(attrs.get("aria-live"), "assertive")
        self.assertNotIn("main", parents)
        themed = [a for t, a, _ in parsed.tags if "data-theme-scope" in a]
        self.assertEqual([a.get("id") for a in themed], ["workspace"])

    def test_the_stop_confirmation_is_the_cores_and_outside_every_view(self) -> None:
        # A Stop asked from any view is confirmed in the bar every view shares, so it is never inside a hidden view.
        found = [(a, parents) for t, a, parents in page().tags if a.get("id") == "stop-confirm"]
        self.assertEqual(len(found), 1)
        attrs, parents = found[0]
        self.assertIn("header", parents)
        self.assertNotIn("main", parents)
        self.assertNotIn("section", parents)
        self.assertIn("hidden", attrs)

    def test_the_stop_answers_are_the_cores_and_outside_every_view(self) -> None:
        # A Stop's answer is drawn in the same bar, so a view's refresh cannot replace it and no view hides it.
        tags = page().tags
        found = [(a, parents) for t, a, parents in tags if a.get("id") == "stop-answers"]
        self.assertEqual(len(found), 1)
        attrs, parents = found[0]
        self.assertIn("header", parents)
        self.assertNotIn("main", parents)
        self.assertNotIn("section", parents)
        self.assertEqual(attrs.get("role"), "status")
        for inner in ("stop-answers-list", "stop-answers-clear"):
            with self.subTest(inner):
                self.assertEqual([p for t, a, p in tags if a.get("id") == inner], [[*parents, "div"]])

    def test_the_window_says_it_is_beta_and_that_closing_stops_nothing(self) -> None:
        self.assertIn('class="beta"', HTML)
        self.assertIn("Closing this window stops nothing", HTML)


class StyleTest(unittest.TestCase):
    def test_theme_tokens_are_read_only_inside_the_workspace(self) -> None:
        for selector, block in css_rules():
            if "--theme-" in block:
                with self.subTest(selector):
                    for one in selector.split(","):
                        self.assertTrue(one.strip().startswith("[data-theme-scope]"), one)

    def test_the_approval_region_uses_core_tokens_only(self) -> None:
        rules = [(s, b) for s, b in css_rules() if "core-approval" in s]
        self.assertTrue(rules)
        for selector, block in rules:
            with self.subTest(selector):
                self.assertNotIn("--theme-", block)
                self.assertNotIn("data-theme-scope", selector)

    def test_nothing_but_the_approval_region_sets_a_layer_or_hides_it(self) -> None:
        for selector, block in css_rules():
            with self.subTest(selector):
                if "z-index" in block or "position: fixed" in block:
                    self.assertEqual(selector, "#core-approval")
        region = dict(re.findall(r"([a-z-]+)\s*:\s*([^;]+);", dict(css_rules())["#core-approval"]))
        self.assertEqual(region["z-index"].strip(), "2147483647")
        self.assertEqual(region["position"].strip(), "fixed")

    def test_no_rule_outside_the_region_can_select_it(self) -> None:
        for selector, _ in css_rules():
            if "core-approval" in selector:
                continue
            with self.subTest(selector):
                self.assertNotRegex(selector, r"\baside\b|^\*|\s\*|:not\(|:has\(|\[aria-live")


if __name__ == "__main__":
    unittest.main()
