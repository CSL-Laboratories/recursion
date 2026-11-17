"""Recursion's daemon-served operator GUI (O68, bar 4). Server-side HTML over WSGI, no JavaScript.

A presentation adapter over the control API, like the CLI: it reads routes, runs the
11 O7 reads, sends the three O7 interventions and forwards a run Stop. It has no order
path and no Stop of its own, and it shows every result as the control API returned it
(a Stop that is ``pending`` is shown as pending; the page never says "terminated" on
its own).

The door, in order, on every request:

0. ``REQUEST_METHOD`` and ``PATH_INFO`` are each read once and must be a ``str`` exactly,
   before anything compares them; otherwise the answer is 400. The checked method is the one
   the door's steps compare, and ``Host``, ``Origin`` and ``Sec-Fetch-Site`` must be a ``str``
   exactly too;
1. ``Host`` must be one of the configured hosts;
2. any ``Origin`` must be exactly one configured origin (a list or ``null`` is
   refused); a POST must carry one. ``Sec-Fetch-Site``, when sent, must be
   ``same-origin`` (or ``none`` for a typed-in GET);
3. the core's device-token adapter (``door``) must accept the request. Authentication
   belongs to the core adapter; this module never reads or stores a token.

``Referrer-Policy: same-origin`` is deliberate: ``no-referrer`` makes browsers send
``Origin: null`` on form posts, which broke every verb in the operator plugin's earlier
browser operator (csl512 gap6 record). That plugin's operator screens are plugin panels in this GUI,
not part of this module. Binding and remote pairing are the composition root's; nothing
here opens a socket. Accessibility: landmarks, labels, a skip link, visible
focus, text status, 24 px minimum targets. No WCAG conformance is claimed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from html import escape
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote

from .client import ACTIONS, READS, ControlClient, ControlError
from .inventory import BARS, SIGN_IN_REJECTED, assess, coverage_gaps, project
from .layouts import (
    MAX_BYTES as _LAYOUT_MAX_BYTES,
    LayoutRefused,
    load_user_layout,
    read_layout,
    user_layout_path,
)
from .panels import PAPER_BOOK, filter_venue_rows, panel_registry
from .themes import (
    MAX_THEME_FILES, _FILENAME, _load_theme, _trusted_dir, active_theme_css, gui_config_dir,
)

__all__ = ["Application", "withhold_venue_data"]

# Phone layout is the prefix: max-width 72rem, the 24px target, the focus outline.
# The layout grid and the theme floor follow that prefix in the one core sheet.
# A grid item's zero minimum and the summary target use min-inline-size, so this
# sheet still has one min-width: (the shared 24px target on a, button and input).
# Theme variables have no fallback, so an unset token leaves that declaration unused.
# Visibility, interventions, and the core chrome name canvas colours, never --theme-*.
# The appearance picker is core chrome as well: canvas, outside the themed workspace.
_CSS = ("body{font:16px/1.5 system-ui,sans-serif;margin:0 auto;max-width:72rem;padding:0 1rem}"
        ".skip{position:absolute;left:-999px}.skip:focus{left:1rem;top:1rem;background:#fff;padding:.5rem}"
        "a,button,input{min-height:24px;min-width:24px}button{padding:.25rem .75rem}"
        ":focus{outline:3px solid #1a5fb4;outline-offset:2px}table{border-collapse:collapse}"
        "th,td{border:1px solid #666;padding:.25rem .5rem;text-align:left}"
        ".badge{font-weight:700;border:2px solid currentColor;padding:0 .25rem}"
        "body{box-sizing:border-box}"
        "input,button,pre,table,.layout,.slot{max-width:100%;box-sizing:border-box}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere}"
        "table{display:block;overflow-x:auto}"
        "summary{min-height:24px;min-inline-size:24px}"
        ".layout{display:grid;grid-auto-flow:dense;grid-template-columns:minmax(0,1fr)}"
        ".cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}"
        ".cols-3{grid-template-columns:repeat(3,minmax(0,1fr))}"
        ".slot{min-inline-size:0;padding:.5rem}"
        ".slot-1-1{grid-column:1}.slot-2-1{grid-column:2}.slot-3-1{grid-column:3}"
        ".slot-1-2{grid-column:1/span 2}.slot-2-2{grid-column:2/span 2}.slot-1-3{grid-column:1/span 3}"
        "@media (max-width:767px){.layout,.cols-2,.cols-3{grid-template-columns:minmax(0,1fr)}"
        ".slot-1-1,.slot-2-1,.slot-3-1,.slot-1-2,.slot-2-2,.slot-1-3{grid-column:auto}}"
        "[data-theme-scope]{background:var(--theme-bg);color:var(--theme-fg)}"
        "[data-theme-scope] a{color:var(--theme-accent)}"
        '[data-theme-scope] [role="note"]{color:var(--theme-muted)}'
        "[data-theme-scope] th,[data-theme-scope] td{border-color:var(--theme-border)}"
        "[data-theme-scope] table{border-radius:var(--theme-radius)}"
        '[data-theme-scope] section[aria-label="Visibility"],'
        '[data-theme-scope] section[aria-label="Visibility"] *,'
        '[data-theme-scope] section[aria-labelledby="actions-h"],'
        '[data-theme-scope] section[aria-labelledby="actions-h"] *'
        "{color:canvastext;background:canvas}"
        'header,footer,nav,#core-approval,section[aria-labelledby="stop-h"]'
        "{color:canvastext;background:canvas}"
        "#core-appearance{color:canvastext;background:canvas}")
_CSS_HASH = base64.b64encode(hashlib.sha256(_CSS.encode()).digest()).decode()


def _content_security_policy(theme_css: str) -> str:
    """style-src is the core sheet, plus the compiled theme when one is embedded."""
    sources = ["'sha256-%s'" % _CSS_HASH]
    if theme_css:
        digest = base64.b64encode(hashlib.sha256(theme_css.encode("utf-8")).digest()).decode("ascii")
        sources.append("'sha256-%s'" % digest)
    return ("default-src 'none'; style-src %s; form-action 'self'; frame-ancestors 'none'; "
            "base-uri 'none'" % " ".join(sources))


# Stop sits in the page chrome, outside <main data-theme-scope>, on every response.
_STOP = ('<section aria-labelledby="stop-h"><h2 id="stop-h">Stop a run</h2>'
         '<form method="post" action="/stop"><p><label for="run-id">Run id</label> '
         '<input id="run-id" name="run_id" required autocomplete="off" maxlength="128" '
         'pattern="[A-Za-z0-9_.:\\-]{1,128}" aria-describedby="run-id-help"></p>'
         '<p id="run-id-help">The control API stops the run; the result shows as it reports it.</p>'
         '<p><input type="checkbox" id="confirm-stop" name="confirm" value="explicit" required>'
         '<label for="confirm-stop">I confirm stopping this run</label></p>'
         '<button type="submit">Request stop</button></form></section>')
# Fixed markup, outside <main data-theme-scope>. A loaded theme or layout cannot add, drop
# or restyle it, and the core-region bytes stay the same whichever names are loaded.
# Empty fields mean the core default. A name is accepted only when that theme or layout is loaded.
_APPEARANCE = (
    '<section id="core-appearance" aria-labelledby="appearance-h"><h2 id="appearance-h">Appearance</h2>'
    '<form method="post" action="/appearance"><p><label for="appearance-theme">Theme</label> '
    '<input id="appearance-theme" name="theme" maxlength="64" autocomplete="off"></p>'
    '<p><label for="appearance-layout">Layout</label> '
    '<input id="appearance-layout" name="layout" maxlength="64" autocomplete="off"></p>'
    '<button type="submit">Apply appearance</button></form>'
    '<form method="post" action="/appearance"><p><button type="submit" id="appearance-reset" '
    'name="reset" value="default">Reset appearance</button></p></form></section>')
# A catalog larger than the theme directory cap is not offered. One active layout file still is.
_LAYOUT_CATALOG_CAP = MAX_THEME_FILES
_MAX_BODY = 4096
# O7 operator-response.schema.json: the visibility object is closed to these four keys and values
# (the downstream plugin draft #318, docs/replacement/outcomes/O7). The GUI renders only these fixed strings.
_VISIBILITY = (
    ("uncertainty", ("visible", "absent_not_hidden")),
    ("refusal", ("visible", "absent_not_hidden")),
    ("stale_data", ("visible", "absent_not_hidden")),
    ("reconciliation", ("reconciled", "unreconciled_visible")),
)
_VISIBILITY_KEYS = frozenset(k for k, _ in _VISIBILITY)
# The status_code values of O7's oracle responses (tests/fixtures/o7-responses.json, from the same
# draft). Not checked against the schema's enum; any other status text is not shown on a venue panel.
_O7_STATUS = frozenset({"ok", "refused", "stale_or_unreconciled", "unavailable"})
# Codes the client raises before anything is sent; every other failure is after sending.
_REFUSED_BEFORE_SENDING = frozenset({"command_not_in_operator_contract", "confirmation_required"})
_LABELS = {
    "operator.evidence.inspect": "Evidence and provenance", "operator.strategies.inspect": "Strategies",
    "operator.positions.inspect": "Positions and collateral", "operator.decisions.inspect": "Decisions and refusals",
    "operator.uncertainty.inspect": "Uncertainty", "operator.health.inspect": "Health",
    "operator.budgets.inspect": "Budgets", "operator.profiles.inspect": "Compliance profiles",
    "operator.audit.export": "Audit export", "operator.recovery.inspect": "Recovery",
    "operator.export.bundle": "Export bundle",
    "operator.interventions.request_stop": "Stop request (operator intervention)",
    "operator.interventions.request_reconciliation": "Reconciliation request",
    "operator.interventions.request_revocation": "Revocation request",
}


def _sign_in_cell(value: object) -> str:
    """Sign-in column. A string is escaped; any other type is the fixed rejection.

    Empty text is a dash. ``escape`` is only called with a string, so a non-string
    cannot crash this cell and a falsy non-string is not shown as missing.
    """
    if type(value) is not str:
        value = SIGN_IN_REJECTED
    return escape(value) if value else "-"


def withhold_venue_data(value: object) -> object:
    """Hide venue data unless the control API marks its switch exactly ``on``.

    Any mapping is read as a dict and any list or tuple as a list, at every depth down to the
    bound below, and they come back as a dict and a list (C7 finding 4: a tuple of rows or a mapping proxy used to
    pass through unredacted). Any other value that is not a str, an int (bool included), a
    float or None (a set, a deque, any other object) is replaced by a fixed marker, never shown.
    A float is kept whatever its value, NaN and the infinities included, as ``json.loads``
    gives them; this is a redaction, not a strict-JSON sanitiser. An entry whose key is not a
    str (JSON gives only str keys) is left out. Anything nested deeper than ``_MAX_DEPTH``
    containers is replaced by a fixed marker, so a deep answer is drawn, never a crash.
    """
    return _withhold(value, 0)


_MAX_DEPTH = 64  # containers below the answer's top level; the same bound as gui/panels.py's filter


def _withhold(value: object, depth: int) -> object:
    if depth > _MAX_DEPTH and isinstance(value, (Mapping, list, tuple)):
        return "[withheld: nested too deeply to show]"
    if isinstance(value, Mapping):
        out = {k: _withhold(v, depth + 1) for k, v in value.items() if isinstance(k, str)}
        if "venue_data" in value and value.get("venue_data_switch") != "on":
            out["venue_data"] = "[withheld: venue data switch is off]"
        return out
    if isinstance(value, (list, tuple)):
        return [_withhold(v, depth + 1) for v in value]
    if value is None or isinstance(value, (str, int, float)):
        return value
    return "[withheld: not JSON data]"


def _venue_scope_sentence(venue: str) -> str:
    """How the panel note names the post-read filter for a declared venue id."""
    if venue in PAPER_BOOK:
        labels = " and ".join("<code>%s</code>" % escape(label) for label in sorted(PAPER_BOOK))
        prefixes = " and ".join("<code>%s_*</code>" % escape(label) for label in sorted(PAPER_BOOK))
        return ("filtered to the paper book (%s rows, and %s keys) before display and "
                "anything from another venue is withheld, except unlabelled response-level "
                "errors and provenance." % (labels, prefixes))
    return ("filtered to <code>%s</code> before display and anything not labelled "
            "with it is withheld, except unlabelled response-level errors and provenance."
            % escape(venue))


def _withheld_note(venue: str) -> str:
    if venue in PAPER_BOOK:
        parts = ["<code>%s</code>" % escape(label) for label in sorted(PAPER_BOOK)]
        parts += ["<code>%s_*</code>" % escape(label) for label in sorted(PAPER_BOOK)]
        return ('<p role="note">Withheld: data not in the paper book (%s).</p>'
                % ", ".join(parts))
    return '<p role="note">Withheld: data not labelled <code>%s</code>.</p>' % escape(venue)


def _canonical_layout(layout: Mapping) -> bytes:
    """JSON bytes ``read_layout`` will accept again. No author text is copied through."""

    def plain(value):
        if isinstance(value, Mapping):
            return {key: plain(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return [plain(item) for item in value]
        return value

    return json.dumps(plain(layout), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _read_nofollow(path: Path, size: int) -> bytes | None:
    """Bytes of a regular file, or None. A symlink is not opened."""
    if size < 0 or size > _LAYOUT_MAX_BYTES or not hasattr(os, "O_NOFOLLOW"):
        return None
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            return None
        view = bytearray()
        while len(view) < size:
            chunk = os.read(fd, size - len(view))
            if not chunk:
                return None
            view.extend(chunk)
        return bytes(view)
    except OSError:
        return None
    finally:
        os.close(fd)


def _dest_replaceable(path: Path) -> bool:
    """True when ``path`` is absent or a regular file. A symlink is left untouched."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def _snapshot(path: Path) -> tuple[str, bytes]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return ("absent", b"")
    except OSError:
        return ("blocked", b"")
    if not stat.S_ISREG(info.st_mode):
        return ("blocked", b"")
    raw = _read_nofollow(path, info.st_size)
    if raw is None:
        return ("blocked", b"")
    return ("file", raw)


def _replace_owned(path: Path, data: bytes) -> None:
    """Replace ``path`` with owner-only bytes. Never follow a symlink, never truncate first.

    The new bytes land in a temporary file in the same directory, then one rename. A failure
    before that rename leaves the previous file as it was.
    """
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "fchmod"):
        raise OSError("nofollow")
    parent = path.parent
    if not _trusted_dir(parent):
        raise OSError("untrusted")
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
             | getattr(os, "O_CLOEXEC", 0))
    fd = None
    tmp_path = None
    for _ in range(3):
        candidate = parent / (".%s.tmp-%d-%s" % (path.name, os.getpid(), os.urandom(4).hex()))
        try:
            fd = os.open(os.fspath(candidate), flags, 0o600)
            tmp_path = candidate
            break
        except FileExistsError:
            continue
    if fd is None or tmp_path is None:
        raise OSError("temp")
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            wrote = os.write(fd, view)
            if wrote <= 0:
                raise OSError("short write")
            view = view[wrote:]
        os.fsync(fd)
    except OSError:
        os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    os.close(fd)
    try:
        os.rename(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise OSError("mode")


def _restore(path: Path, snap: tuple[str, bytes]) -> None:
    kind, raw = snap
    if kind == "absent":
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a file")
        os.unlink(path)
        return
    if kind != "file":
        raise OSError("blocked")
    _replace_owned(path, raw)


def _remove_regular(path: Path) -> None:
    """Unlink a regular file. A symlink is not removed and not followed."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise OSError("symlink")
    os.unlink(path)


class Application:
    def __init__(self, client: ControlClient, door: Callable[[dict], bool], hosts: frozenset,
                 origins: frozenset, required: dict | None = None, panels=(), *, theme_root=None) -> None:
        self._client = client
        self._required = required
        self._panels = panel_registry(panels)
        self._door = door
        self._hosts = frozenset(hosts)
        self._origins = frozenset(origins)
        # None reads the operator gui directory. A caller may pass a directory for tests.
        self._theme_root = theme_root

    # -- door ---------------------------------------------------------------------------
    def _refusal(self, env: dict, method) -> str | None:
        # ``method`` is the request line's method as __call__ read it, once; the environ is not
        # asked again, so the value checked is the value compared (DeepSeek round 6).
        if type(method) is not str:
            return "request_line_invalid"
        host = env.get("HTTP_HOST")
        # A str exactly: a subclass could make its own equality and hash match a configured host.
        if type(host) is not str or host not in self._hosts:
            return "host_not_allowed"
        origin = env.get("HTTP_ORIGIN")
        if origin is not None and (type(origin) is not str or "," in origin or origin not in self._origins):
            return "origin_not_allowed"
        if method == "POST" and origin is None:
            return "origin_required"
        site = env.get("HTTP_SEC_FETCH_SITE")
        if site is not None and (type(site) is not str
                                 or (site != "same-origin" and not (method == "GET" and site == "none"))):
            return "cross_site_request"
        try:
            admitted = self._door(env) is True
        except Exception:  # noqa: BLE001 - an adapter that cannot answer has not admitted the device
            admitted = False
        if not admitted:
            return "device_not_authenticated"
        return None

    def __call__(self, env: dict, start_response) -> list[bytes]:
        # The same compiled theme on every answer of this request, including a refusal,
        # so the style hash matches the sheet. A theme that cannot be read is no theme.
        theme_css = self._theme_css()
        # The request line first, before anything compares it: a method or path that is not a str
        # exactly could raise in, or lie to, a comparison (DeepSeek rounds 4 and 5).
        method, path = env.get("REQUEST_METHOD"), env.get("PATH_INFO", "")
        if type(method) is not str or type(path) is not str:
            return self._send(start_response, "400 Bad Request",
                              self._page("Bad request", "<p>Bad request: request line</p>", theme_css), theme_css)
        refusal = self._refusal(env, method)
        if refusal is not None:
            status = "401 Unauthorized" if refusal == "device_not_authenticated" else "403 Forbidden"
            return self._send(start_response, status,
                              self._page("Refused", "<p>Refused: %s</p>" % escape(refusal), theme_css), theme_css)
        routes = {("GET", "/"): self._home, ("GET", "/read"): self._read, ("GET", "/panel"): self._panel,
                  ("POST", "/action"): self._action, ("POST", "/stop"): self._stop,
                  ("POST", "/appearance"): self._appearance}
        handler = routes.get((method, path))
        if handler is None:
            known = path in {p for _, p in routes}
            return self._send(start_response, "405 Method Not Allowed" if known else "404 Not Found",
                              self._page("Not available", "<p>Not available.</p>", theme_css), theme_css)
        try:
            title, body = handler(env)
            html = self._page(title, body, theme_css)
        except _BadRequest as exc:
            return self._send(start_response, "400 Bad Request",
                              self._page("Bad request", "<p>Bad request: %s</p>" % escape(str(exc)), theme_css),
                              theme_css)
        except Exception as exc:  # noqa: BLE001 - every answer this module gives carries the same headers
            # Only the exception's type is named, never its text. Nothing here says whether a request
            # that was sent took effect.
            return self._send(start_response, "500 Internal Server Error", self._page(
                "Not available", '<p role="status">Not available: the page could not finish this request '
                "(%s). It does not know whether anything that was sent took effect; check the run before "
                "sending it again.</p>" % escape(type(exc).__name__), theme_css), theme_css)
        return self._send(start_response, "200 OK", html, theme_css)

    def _theme_css(self) -> str:
        try:
            css = active_theme_css(self._theme_root)
        except Exception:  # noqa: BLE001 - a theme on disk must not take the page down
            return ""
        return css if type(css) is str else ""

    @staticmethod
    def _send(start_response, status: str, html: str, theme_css: str = "") -> list[bytes]:
        raw = html.encode("utf-8")
        start_response(status, [
            ("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(raw))),
            ("Cache-Control", "no-store"),
            ("Content-Security-Policy", _content_security_policy(theme_css)),
            ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "same-origin"),
            ("X-Frame-Options", "DENY")])
        return [raw]

    # -- pages --------------------------------------------------------------------------
    @staticmethod
    def _page(title: str, main: str, theme_css: str = "") -> str:
        # The core sheet has no attributes, so the unthemed page still has one <style>.
        # The compiled theme, when there is one, is a second sheet and a second hash.
        styles = "<style>" + _CSS + "</style>"
        if theme_css:
            styles += '<style id="theme">' + theme_css + "</style>"
        return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                "<title>" + escape(title) + " - Recursion operator</title>" + styles + "</head><body>"
                '<a class="skip" href="#main">Skip to main content</a>'
                "<header><h1>Recursion operator</h1><p>Every route is BETA unless all six bars pass with "
                "recorded evidence, and a route the control API tags beta stays BETA. No live demos. This page cannot place, approve or size orders.</p></header>"
                '<nav aria-label="Sections"><ul><li><a href="/">Overview</a></li>'
                '<li><a href="/#routes-h">Routes</a></li><li><a href="/#reads-h">Reads</a></li>'
                '<li><a href="/#actions-h">Interventions</a></li><li><a href="/#stop-h">Stop a run</a></li>'
                '</ul></nav><main id="main" data-theme-scope tabindex="-1">' + main + "</main>"
                '<aside id="core-approval" aria-label="Approval request, drawn by the core"></aside>'
                + _APPEARANCE
                + _STOP
                + "<footer><p>Results are shown as the control API returned them. A stop that is not "
                "confirmed shows as pending.</p></footer></body></html>")

    def _routes_table(self) -> str:
        try:
            records = self._client.routes()
        except ControlError as exc:
            # No inventory is not an empty inventory: report no route and no gap.
            return ('<section aria-labelledby="routes-h"><h2 id="routes-h">Routes</h2>'
                    '<p role="status">Route inventory unavailable: %s. Route coverage is unknown; '
                    "no route is reported present or missing until the control API answers.</p></section>"
                    % escape(exc.code))
        rows = [assess(r) for r in records] + coverage_gaps(records, self._required)
        body = "".join(
            "<tr><th scope=\"row\">%s</th><td>%s</td><td>%s</td><td>%s</td><td><span class=\"badge\">%s</span>"
            " %s</td><td>%s</td><td>%s</td></tr>" % (
                escape(r["id"]), escape(r["class"]), escape(r["subject"]), _sign_in_cell(r["sign_in"]),
                project(r, "gui"), escape(" ".join((["(beta tag)"] if r["beta_tag"] else [])
                                               + ([] if r["claim"] == "ok" else ["(" + r["claim"] + ")"]))),
                escape(", ".join(r["failed_bars"]) or "none"), escape(r["blocker"] or "-"))
            for r in rows)
        return ('<section aria-labelledby="routes-h"><h2 id="routes-h">Routes</h2><table><caption>'
                "Routes from the control API plus required routes it did not report. Bars: %s.</caption>"
                '<thead><tr><th scope="col">Route</th><th scope="col">Class</th><th scope="col">Covers</th>'
                '<th scope="col">Sign-in</th><th scope="col">Maturity</th><th scope="col">Failed bars</th>'
                '<th scope="col">Blocker</th></tr></thead><tbody>%s</tbody></table></section>'
                % (escape(", ".join(BARS)), body))

    def _home(self, env: dict) -> tuple[str, str]:
        reads = "".join('<li><a href="/read?command=%s">%s</a></li>' % (quote(c), escape(_LABELS[c]))
                        for c in READS)
        actions = "".join(
            '<form method="post" action="/action"><fieldset><legend>%s</legend>'
            '<input type="hidden" name="command" value="%s"><p>Scope: this operator session only.</p>'
            '<input type="checkbox" id="confirm-%d" name="confirm" value="explicit" required>'
            '<label for="confirm-%d">I confirm this request</label> <button type="submit">Send: %s</button>'
            "</fieldset></form>" % (escape(_LABELS[c]), escape(c), i, i, escape(_LABELS[c]))
            for i, c in enumerate(ACTIONS))
        # Stop is _STOP, drawn once after </main>, so a layout cannot cover it and the
        # core bytes stay the unthemed page's. The layout, when one loads, replaces only
        # the workspace inside <main data-theme-scope>.
        layout = load_user_layout(self._panels)
        if layout is not None:
            slots = "".join(self._layout_slot(slot, reads, actions) for slot in layout["slots"])
            return layout["title"], '<div class="layout cols-%d">%s</div>' % (layout["columns"], slots)
        panels = ('<section aria-labelledby="panels-h"><h2 id="panels-h">Plugin panels</h2><ul>'
                  + "".join('<li><a href="/panel?id=%s">%s</a></li>' % (quote(pid), escape(p["title"]))
                            for pid, p in self._panels.items()) + "</ul></section>")
        main = (panels + self._routes_table() + '<section aria-labelledby="reads-h"><h2 id="reads-h">Reads</h2><ul>'
                + reads + '</ul></section><section aria-labelledby="actions-h"><h2 id="actions-h">'
                "Interventions</h2>" + actions + "</section>")
        return "Overview", main

    def _layout_slot(self, slot, reads: str, actions: str) -> str:
        panel_id = slot["panel"]
        title, inner = self._view_body(panel_id, reads, actions)
        kind = "details" if slot["collapsed"] else "section"
        tag = ('<%s class="slot slot-%d-%d" data-column="%d" data-panel="%s" data-collapsed="%s">'
               % (kind, slot["column"], slot["span"], slot["column"], escape(panel_id, quote=True),
                  "true" if slot["collapsed"] else "false"))
        if slot["collapsed"]:
            return tag + "<summary>%s</summary>%s</details>" % (escape(title), inner)
        return tag + inner + "</section>"

    def _view_body(self, panel_id: str, reads: str, actions: str) -> tuple[str, str]:
        if panel_id == "routes":
            return "Routes", self._routes_table()
        if panel_id == "reads":
            return "Reads", ('<section aria-labelledby="reads-h"><h2 id="reads-h">Reads</h2><ul>'
                             + reads + "</ul></section>")
        if panel_id == "interventions":
            return "Interventions", ('<section aria-labelledby="actions-h"><h2 id="actions-h">'
                                     "Interventions</h2>" + actions + "</section>")
        return self._panels[panel_id]["title"], self._panel_body(self._panels[panel_id])

    @staticmethod
    def _query(env: dict) -> dict:
        try:
            form = parse_qs(env.get("QUERY_STRING", ""), strict_parsing=True)
        except ValueError:
            raise _BadRequest("query") from None
        return form

    def _form(self, env: dict, fields: set) -> dict:
        try:
            length = int(env.get("CONTENT_LENGTH") or "0")
        except ValueError:
            raise _BadRequest("length") from None
        if not 0 < length <= _MAX_BODY:
            raise _BadRequest("length")
        if (env.get("CONTENT_TYPE") or "").split(";")[0].strip() != "application/x-www-form-urlencoded":
            raise _BadRequest("content type")
        try:
            data = env["wsgi.input"].read(length)
            if len(data) != length:
                # The body ended before its Content-Length: never parse the part that arrived.
                # "run_id=run-1234567890" cut after "run-12" would name another run.
                raise ValueError("short body")
            raw = data.decode("utf-8")
            form = parse_qs(raw, strict_parsing=True)
        except (KeyError, UnicodeError, ValueError):
            raise _BadRequest("form") from None
        if set(form) != fields or any(len(v) != 1 for v in form.values()):
            raise _BadRequest("fields")
        return {k: v[0] for k, v in form.items()}

    def _posted(self, env: dict) -> dict:
        """One value per field, blank values kept. The appearance form uses blanks as default."""
        try:
            length = int(env.get("CONTENT_LENGTH") or "0")
        except ValueError:
            raise _BadRequest("length") from None
        if not 0 < length <= _MAX_BODY:
            raise _BadRequest("length")
        if (env.get("CONTENT_TYPE") or "").split(";")[0].strip() != "application/x-www-form-urlencoded":
            raise _BadRequest("content type")
        try:
            data = env["wsgi.input"].read(length)
            if len(data) != length:
                raise ValueError("short body")
            raw = data.decode("utf-8")
            form = parse_qs(raw, strict_parsing=True, keep_blank_values=True)
        except (KeyError, UnicodeError, ValueError):
            raise _BadRequest("form") from None
        if any(len(v) != 1 for v in form.values()):
            raise _BadRequest("fields")
        return {k: v[0] for k, v in form.items()}

    def _theme_dir(self) -> Path | None:
        """The directory whose ``selection.json`` the theme loader reads."""
        if self._theme_root is not None:
            try:
                root = Path(self._theme_root)
            except TypeError:
                return None
        else:
            root = gui_config_dir()
        if root is None or not root.is_absolute():
            return None
        return root

    def _loaded_theme_names(self) -> set[str]:
        """Theme names ``active_theme_css`` could apply. A duplicate name is not selectable."""
        root = self._theme_dir()
        if root is None or not _trusted_dir(root):
            return set()
        folder = root / "themes"
        if not _trusted_dir(folder):
            return set()
        try:
            names = [name for name in os.listdir(folder) if _FILENAME.fullmatch(name)]
        except OSError:
            return set()
        if len(names) > MAX_THEME_FILES:
            return set()
        found: dict[str, object] = {}
        blocked: set[str] = set()
        for filename in sorted(names):
            theme = _load_theme(folder / filename)
            if theme is None:
                continue
            if theme.name in found or theme.name in blocked:
                blocked.add(theme.name)
                found.pop(theme.name, None)
                continue
            found[theme.name] = theme
        return set(found)

    def _consider_layout(self, path: Path, found: dict[str, bytes], blocked: set[str], *,
                         same_bytes_ok: bool) -> None:
        try:
            parsed = read_layout(path, known_panels=self._panels)
        except (FileNotFoundError, LayoutRefused, OSError):
            return
        name = parsed["name"]
        if type(name) is not str or name in blocked:
            return
        raw = _canonical_layout(parsed)
        if len(raw) > _LAYOUT_MAX_BYTES:
            return
        if name in found:
            if same_bytes_ok and found[name] == raw:
                return
            blocked.add(name)
            found.pop(name, None)
            return
        found[name] = raw

    def _loaded_layouts(self) -> dict[str, bytes]:
        """Layout name to the bytes the layout loader will read from ``layout.json``.

        Candidates are ``layouts/*.json`` beside that file, plus the active file when it names
        the same bytes as one candidate. Two different documents with one name are not selectable.
        """
        found: dict[str, bytes] = {}
        blocked: set[str] = set()
        folder = user_layout_path().parent / "layouts"
        names: list[str] = []
        try:
            if folder.is_absolute() and _trusted_dir(folder):
                names = [name for name in os.listdir(folder) if _FILENAME.fullmatch(name)]
        except OSError:
            names = []
        if len(names) > _LAYOUT_CATALOG_CAP:
            names = []
        for filename in sorted(names):
            self._consider_layout(folder / filename, found, blocked, same_bytes_ok=False)
        self._consider_layout(user_layout_path(), found, blocked, same_bytes_ok=True)
        return found

    def _keep_appearance(self, theme_name: str | None, layout_bytes: bytes | None) -> None:
        """Write the theme selection and the layout file the loaders already read.

        ``theme_name`` None is the theme loader's default (``{"theme": null}``). ``layout_bytes``
        None removes ``layout.json`` when it is a regular file. A symlink is not followed and
        not replaced. A failure leaves the previous files.
        """
        root = self._theme_dir()
        if root is None or not _trusted_dir(root):
            raise _BadRequest("appearance directory")
        selection_path = root / "selection.json"
        if _snapshot(selection_path)[0] == "blocked" or not _dest_replaceable(selection_path):
            raise _BadRequest("appearance not kept")
        layout_path = user_layout_path()
        writing_layout = layout_bytes is not None
        removing_layout = False
        if writing_layout:
            home = layout_path.parent
            if (not layout_path.is_absolute() or not home.is_absolute() or not _trusted_dir(home)
                    or not _dest_replaceable(layout_path)):
                raise _BadRequest("appearance not kept")
        else:
            try:
                os.lstat(layout_path)
            except FileNotFoundError:
                removing_layout = False
            except OSError:
                raise _BadRequest("appearance not kept") from None
            else:
                if not _dest_replaceable(layout_path):
                    raise _BadRequest("appearance not kept")
                removing_layout = True
        selection_bytes = json.dumps({"theme": theme_name}, separators=(",", ":")).encode("utf-8")
        sel_snap = _snapshot(selection_path)
        lay_snap = _snapshot(layout_path) if writing_layout or removing_layout else None
        if sel_snap[0] == "blocked" or (lay_snap is not None and lay_snap[0] == "blocked"):
            raise _BadRequest("appearance not kept")
        try:
            _replace_owned(selection_path, selection_bytes)
            if writing_layout:
                _replace_owned(layout_path, layout_bytes)
            elif removing_layout:
                _remove_regular(layout_path)
        except OSError:
            try:
                _restore(selection_path, sel_snap)
                if lay_snap is not None and writing_layout:
                    _restore(layout_path, lay_snap)
            except OSError:
                pass
            raise _BadRequest("appearance not kept") from None

    def _appearance(self, env: dict) -> tuple[str, str]:
        """Choose a loaded theme and layout, or reset both. Local files only, no control call."""
        form = self._posted(env)
        if set(form) == {"reset"} and form["reset"] == "default":
            self._keep_appearance(None, None)
            return "Appearance", '<p role="status">Appearance reset.</p>'
        if set(form) != {"theme", "layout"}:
            raise _BadRequest("fields")
        theme_name = form["theme"] if form["theme"] != "" else None
        layout_name = form["layout"] if form["layout"] != "" else None
        if theme_name is not None and theme_name not in self._loaded_theme_names():
            raise _BadRequest("unknown theme")
        layouts = self._loaded_layouts()
        if layout_name is not None and layout_name not in layouts:
            raise _BadRequest("unknown layout")
        self._keep_appearance(theme_name, None if layout_name is None else layouts[layout_name])
        return "Appearance", '<p role="status">Appearance kept.</p>'

    @staticmethod
    def _response_view(data: object) -> str:
        shown = withhold_venue_data(data)
        return "<pre>%s</pre>" % escape(json.dumps(shown, indent=2, sort_keys=True))

    def _o7_view(self, command: str, data: dict, shown: object = None) -> str:
        status = data.get("status_code") if type(data.get("status_code")) is str else "not reported"
        if shown is not None:
            # A venue panel prints the response's own text only where the venue filter kept it.
            # status_code is not one of the filter's response-level exceptions (ADV2-10), so an
            # unfiltered status is shown only as one of the fixed O7 status strings, like the
            # visibility block; any other text is withheld, never echoed.
            kept = shown.get("status_code") if isinstance(shown, dict) else None
            if type(kept) is str:
                status = kept
            elif status != "not reported" and status not in _O7_STATUS:
                status = "withheld by this panel's venue filter"
        result = data.get("result") if type(data.get("result")) is dict else {}
        warn = ""
        if result.get("order_submitted") is not False and command in ACTIONS:
            warn = '<p role="alert">Contract problem: the response does not state order_submitted=false.</p>'
        return ('<p role="status">Status reported by the control API: <strong>%s</strong></p>%s%s%s'
                % (escape(status), self._visibility(data), warn,
                   self._response_view(data if shown is None else shown)))

    @staticmethod
    def _visibility(data: dict) -> str:
        """O7's refusal, uncertainty, stale and reconciliation states, from the unfiltered response.

        Rendered on every O7 view, so a venue panel's filter never hides them. Only the four
        keys of O7's closed visibility object are read, and only their schema values are shown,
        as fixed text from _VISIBILITY. The response's own text is never passed through: any
        other value reads as not an O7 value, and any other key is counted, not named.
        """
        vis = data.get("visibility")
        if type(vis) is not dict:
            return '<p role="status">Visibility: not reported by the control API.</p>'
        items = []
        for key, allowed in _VISIBILITY:
            if key not in vis:
                shown = "not reported"
            else:
                value = vis[key]
                shown = value if type(value) is str and value in allowed else "reported, not an O7 value"
            items.append("<li>%s: <strong>%s</strong></li>" % (key, shown))
        extra = sum(1 for k in vis if k not in _VISIBILITY_KEYS)
        if extra:
            items.append("<li>Other fields: <strong>%d withheld, not in O7 visibility</strong></li>" % extra)
        return ('<section aria-label="Visibility"><p role="status">Visibility reported by the '
                "control API:</p><ul>%s</ul></section>" % "".join(items))

    def _read(self, env: dict) -> tuple[str, str]:
        form = self._query(env)
        if set(form) != {"command"} or len(form["command"]) != 1 or form["command"][0] not in READS:
            raise _BadRequest("command_not_in_operator_contract")
        command = form["command"][0]
        try:
            data = self._client.operator(command)
        except ControlError as exc:
            return _LABELS[command], '<p role="status">Unavailable: %s</p>' % escape(exc.code)
        return _LABELS[command], "<h2>%s</h2>%s" % (escape(_LABELS[command]), self._o7_view(command, data))

    def _panel(self, env: dict) -> tuple[str, str]:
        form = self._query(env)
        if set(form) != {"id"} or len(form["id"]) != 1 or form["id"][0] not in self._panels:
            raise _BadRequest("panel_not_registered")
        panel = self._panels[form["id"][0]]
        return panel["title"], self._panel_body(panel)

    def _panel_body(self, panel) -> str:
        body = "<h2>%s</h2><p>Read-only plugin panel.</p>" % escape(panel["title"])
        if panel["venue_use"] is not None:
            body += ('<p role="note">Declared venue use: <code>%s</code> (a label). '
                     % escape(panel["venue_use"]))
            if panel.get("venue") is not None:
                body += ('The control API\'s O7 v0 reads take no venue argument; returned data '
                         'is %s The GUI redacts only fields the control API marks with '
                         'a venue-data switch that is not on; O7 v0 reports no switch state for '
                         'venue-labelled rows, so their clearance is the control API\'s to '
                         'enforce.</p>'
                         % _venue_scope_sentence(panel["venue"]))
            else:
                body += ('The control API\'s O7 v0 reads take no venue argument, so the results '
                         'below are not scoped to this venue. The GUI redacts only fields the '
                         'control API marks with a venue-data switch that is not on.</p>')
        elif panel.get("venue") is not None:
            body += ('<p role="note">The control API\'s O7 v0 reads take no venue argument; '
                     'returned data is %s</p>' % _venue_scope_sentence(panel["venue"]))
        for command in panel["reads"]:
            body += "<section><h3>%s</h3>" % escape(_LABELS[command])
            try:
                data = self._client.operator(command)
                if panel.get("venue") is None:
                    body += self._o7_view(command, data)
                else:
                    shown, withheld = filter_venue_rows(data, panel["venue"])
                    if withheld:
                        body += _withheld_note(panel["venue"])
                    body += self._o7_view(command, data, shown)
            except ControlError as exc:
                body += '<p role="status">Unavailable: %s</p>' % escape(exc.code)
            body += "</section>"
        return body

    def _action(self, env: dict) -> tuple[str, str]:
        form = self._form(env, {"command", "confirm"})
        if form["command"] not in ACTIONS or form["confirm"] != "explicit":
            raise _BadRequest("command_not_in_operator_contract")
        try:
            data = self._client.operator(form["command"], confirmed=True)
        except ControlError as exc:
            if exc.code in _REFUSED_BEFORE_SENDING:
                return "Request not sent", '<p role="status">Request not sent: %s</p>' % escape(exc.code)
            if exc.refused is not None:
                # ADV2-9: the control API answered with a typed refusal, so nothing was executed.
                return "Not sent", ('<p role="status">Not sent: refused by the daemon (%s); '
                                    "run Stop is available</p>" % escape(exc.refused))
            # Sent, but no readable answer: the control API may have recorded it.
            return "Intervention outcome unknown", (
                '<p role="status">Intervention outcome: unknown. The request may have reached the '
                "control API (%s). Check the run and event log before retrying.</p>" % escape(exc.code))
        return _LABELS[form["command"]], "<h2>%s</h2>%s" % (escape(_LABELS[form["command"]]),
                                                             self._o7_view(form["command"], data))

    def _stop(self, env: dict) -> tuple[str, str]:
        form = self._form(env, {"run_id", "confirm"})
        if form["confirm"] != "explicit":
            raise _BadRequest("confirmation_required")
        try:
            data = self._client.stop_run(form["run_id"])
        except ControlError as exc:
            return "Stop not confirmed", ('<p role="status">Stop state: unknown. The control API did not '
                                          "answer (%s). Check the run before assuming it stopped.</p>"
                                          % escape(exc.code))
        state = data.get("state") if type(data.get("state")) is str else None
        line = ("Stop state reported by the control API: <strong>%s</strong>" % escape(state) if state
                else "Stop state: not reported by the control API (unknown)")
        return "Stop", '<h2>Stop</h2><p role="status">%s</p>%s' % (line, self._response_view(data))


class _BadRequest(Exception):
    pass
