"""Operator layout file ``gui.layout/1``. Data only: no theme, no script, no schema library.

The page reads one file, ``$XDG_CONFIG_HOME/recursion/gui/layout.json`` (or
``~/.config/recursion/gui/layout.json``). Columns are numbered from 1. A name of a core
region (``stop``, ``approval``, ``header``, ``footer``) refuses the whole file, even when a
plugin panel uses that id. ``routes``, ``reads`` and ``interventions`` always mean the core
views. Anything else must be a panel id the page already accepted.

The file is the operator's. A symlink, a file or parent directory someone else can write, or
a file over 64 KiB is refused. A missing or refused file is no layout: the page stays as it
is. This module does not import a schema package; the rules below are the check.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

__all__ = [
    "CORE_REGIONS", "CORE_VIEWS", "MAX_BYTES", "SCHEMA", "LayoutRefused", "load_user_layout",
    "parse_layout", "parse_layout_text", "read_layout", "user_layout_path",
]

log = logging.getLogger(__name__)

SCHEMA = "gui.layout/1"
CORE_VIEWS = ("routes", "reads", "interventions")
CORE_REGIONS = frozenset({"stop", "approval", "header", "footer"})
MAX_BYTES = 64 * 1024

_TOP = frozenset({"schema", "name", "title", "columns", "slots"})
_SLOT = frozenset({"panel", "column", "span", "order", "collapsed"})
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_PANEL = re.compile(r"[A-Za-z0-9_.-]{1,96}")


class LayoutRefused(ValueError):
    """The layout file is not applied. ``reason`` is a short code, never the file text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _known(known_panels) -> frozenset:
    # A mapping's keys are the panel ids (the page passes its panel registry).
    return frozenset(known_panels)


def parse_layout(data, *, known_panels) -> Mapping:
    """Return a read-only layout, or raise ``LayoutRefused``."""
    if type(data) is not dict:
        raise LayoutRefused("not_object")
    extra = set(data) - _TOP
    if extra:
        raise LayoutRefused("unknown_key")
    if not _TOP <= set(data):
        raise LayoutRefused("missing_key")
    if data["schema"] != SCHEMA:
        raise LayoutRefused("schema")
    name, title, columns, slots = data["name"], data["title"], data["columns"], data["slots"]
    if type(name) is not str or _NAME.fullmatch(name) is None:
        raise LayoutRefused("name")
    if type(title) is not str or not 1 <= len(title) <= 100 or not title.strip():
        raise LayoutRefused("title")
    if type(columns) is not int or not 1 <= columns <= 3:
        raise LayoutRefused("columns")
    if type(slots) is not list:
        raise LayoutRefused("slots")
    known = _known(known_panels)
    parsed = []
    seen = set()
    for index, slot in enumerate(slots):
        parsed.append((index, _slot(slot, columns, known, seen)))
    parsed.sort(key=lambda item: (item[1]["column"], item[1]["order"], item[0]))
    return MappingProxyType({
        "schema": SCHEMA, "name": name, "title": title, "columns": columns,
        "slots": tuple(item[1] for item in parsed),
    })


def _slot(slot, columns: int, known: frozenset, seen: set) -> Mapping:
    if type(slot) is not dict:
        raise LayoutRefused("slot")
    if set(slot) - _SLOT:
        raise LayoutRefused("unknown_key")
    if not _SLOT <= set(slot):
        raise LayoutRefused("slot")
    panel, column, span = slot["panel"], slot["column"], slot["span"]
    order, collapsed = slot["order"], slot["collapsed"]
    if type(panel) is not str or _PANEL.fullmatch(panel) is None or panel in CORE_REGIONS:
        raise LayoutRefused("panel")
    if panel not in CORE_VIEWS and panel not in known:
        raise LayoutRefused("panel")
    if panel in seen:
        raise LayoutRefused("panel")
    seen.add(panel)
    if type(column) is not int or not 1 <= column <= columns:
        raise LayoutRefused("column")
    if type(span) is not int or not 1 <= span <= 3 or column + span - 1 > columns:
        raise LayoutRefused("span")
    if type(order) is not int:
        raise LayoutRefused("order")
    if type(collapsed) is not bool:
        raise LayoutRefused("collapsed")
    return MappingProxyType({
        "panel": panel, "column": column, "span": span, "order": order, "collapsed": collapsed,
    })


def parse_layout_text(raw: bytes, *, known_panels) -> Mapping:
    """Parse layout bytes. Duplicate JSON keys are refused here; a dict cannot show them."""
    if type(raw) is not bytes:
        raise LayoutRefused("not_json")
    if len(raw) > MAX_BYTES:
        raise LayoutRefused("too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise LayoutRefused("not_json") from exc

    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise LayoutRefused("duplicate_key")
            obj[key] = value
        return obj

    try:
        data = json.loads(text, object_pairs_hook=pairs)
    except LayoutRefused:
        raise
    except (json.JSONDecodeError, ValueError, UnicodeError, RecursionError) as exc:
        raise LayoutRefused("not_json") from exc
    return parse_layout(data, known_panels=known_panels)


def user_layout_path(environ=None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("XDG_CONFIG_HOME") or ""
    if not base:
        home = env.get("HOME") or ""
        base = os.path.join(home, ".config") if home else ""
    if not base:
        return Path("/.no-operator-config") / "recursion" / "gui" / "layout.json"
    return Path(base) / "recursion" / "gui" / "layout.json"


def _dir_trust(path: Path) -> str:
    """``ok``, ``missing``, or ``untrusted``. Only this directory, not its parents.

    A world-writable ancestor such as ``/tmp`` is normal. The layout's own directory is what
    the operator can keep private.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "untrusted"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return "untrusted"
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        return "untrusted"
    return "ok"


def read_layout(path, *, known_panels) -> Mapping:
    """Read one layout file. ``FileNotFoundError`` when there is nothing to read."""
    path = Path(path)
    if not hasattr(os, "O_NOFOLLOW"):
        # Following a link is not allowed, and there is no flag to refuse one. Do not open it.
        try:
            os.lstat(path)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise LayoutRefused("untrusted") from exc
        raise LayoutRefused("untrusted")
    parent = _dir_trust(path.parent)
    if parent == "missing":
        raise FileNotFoundError(path)
    if parent != "ok":
        raise LayoutRefused("untrusted")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise LayoutRefused("untrusted") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022):
            raise LayoutRefused("untrusted")
        if info.st_size > MAX_BYTES:
            raise LayoutRefused("too_large")
        raw = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_BYTES:
        raise LayoutRefused("too_large")
    return parse_layout_text(raw, known_panels=known_panels)


def load_user_layout(known_panels, *, environ=None):
    """The operator's layout, or ``None`` when the file is absent or refused.

    Absence is silent. A refusal is logged as its reason code only.
    """
    path = user_layout_path(environ)
    try:
        return read_layout(path, known_panels=known_panels)
    except FileNotFoundError:
        return None
    except LayoutRefused as exc:
        log.warning("layout refused: %s", exc.reason)
        return None
    except OSError:
        log.warning("layout refused: unreadable")
        return None
