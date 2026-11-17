"""gui.theme/1 for the daemon page.

An operator theme is data. This module checks it and compiles it to custom
properties on ``[data-theme-scope]``. It does not fetch, run, or copy author CSS.
The page inserts that compiled sheet and nothing else from the file.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from types import MappingProxyType

__all__ = [
    "Theme",
    "ThemeError",
    "active_theme_css",
    "gui_config_dir",
    "no_duplicate_members",
    "parse_theme",
]

# Same five names as the desktop workspace, plus the two optional tokens.
_COLOURS = ("bg", "fg", "muted", "accent", "border")
_TOKEN_KEYS = frozenset(_COLOURS) | {"density", "radius"}
_DOCUMENT_KEYS = frozenset({"schema", "name", "title", "tokens"})
_DENSITY = frozenset({"compact", "normal", "roomy"})
_RADIUS = frozenset({0, 2, 4, 8})
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_COLOUR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.json$")
_DECL = re.compile(
    r"--theme-(?:bg|fg|muted|accent|border):#[0-9a-f]{6}"
    r"|--theme-density:(?:compact|normal|roomy)"
    r"|--theme-radius:(?:0|2|4|8)px"
)
_COMPILED = re.compile(r"^\[data-theme-scope\]\{(?:(?:%s);)+\}$" % _DECL.pattern)

# One operator file, and one directory of them. A larger file or a larger
# directory is not applied: the page stays on the core default.
MAX_THEME_BYTES = 8192
MAX_THEME_FILES = 64
_SELECTION_KEYS = frozenset({"theme"})


class ThemeError(Exception):
    """The whole theme document is refused. ``code`` names the first defect."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Theme:
    """A theme the core accepted. ``css`` is the only text the page may embed."""

    def __init__(self, name: str, title: str, css: str, tokens: dict) -> None:
        self.name = name
        self.title = title
        self.css = css
        self.tokens = MappingProxyType(tokens)


def no_duplicate_members(pairs: list) -> dict:
    """A JSON object whose members are all named once.

    ``json.loads`` otherwise keeps the last value of a repeated name.
    """
    out: dict = {}
    for name, value in pairs:
        if name in out:
            raise ValueError("duplicate member")
        out[name] = value
    return out


def parse_theme(data: object) -> Theme:
    """Accept one ``gui.theme/1`` document, or raise ``ThemeError`` for the whole thing."""
    if type(data) is not dict:
        raise ThemeError("theme_not_object")
    extra = set(data) - _DOCUMENT_KEYS
    if extra:
        raise ThemeError("theme_unknown_key")
    if not _DOCUMENT_KEYS <= set(data):
        raise ThemeError("theme_missing_key")
    if type(data["schema"]) is not str or data["schema"] != "gui.theme/1":
        raise ThemeError("theme_schema")
    name = data["name"]
    if type(name) is not str or _NAME.fullmatch(name) is None:
        raise ThemeError("theme_name")
    title = data["title"]
    if type(title) is not str or not 1 <= len(title) <= 100 or any(ord(ch) < 32 or ord(ch) == 127 for ch in title):
        raise ThemeError("theme_title")
    tokens = data["tokens"]
    if type(tokens) is not dict:
        raise ThemeError("theme_tokens")
    unknown = set(tokens) - _TOKEN_KEYS
    if unknown:
        raise ThemeError("theme_token_unknown")
    if not set(_COLOURS) <= set(tokens):
        raise ThemeError("theme_token_missing")
    colours = {}
    for key in _COLOURS:
        value = tokens[key]
        if type(value) is not str or _COLOUR.fullmatch(value) is None:
            raise ThemeError("theme_colour")
        colours[key] = value.lower()
    density = None
    if "density" in tokens:
        density = tokens["density"]
        if type(density) is not str or density not in _DENSITY:
            raise ThemeError("theme_density")
    radius = None
    if "radius" in tokens:
        radius = tokens["radius"]
        if type(radius) is not int or radius not in _RADIUS:
            raise ThemeError("theme_radius")
    if _contrast(colours["fg"], colours["bg"]) < 4.5 or _contrast(colours["muted"], colours["bg"]) < 4.5:
        raise ThemeError("theme_contrast")
    if _contrast(colours["accent"], colours["bg"]) < 3:
        raise ThemeError("theme_contrast")
    css = _compile(colours, density, radius)
    kept = dict(colours)
    if density is not None:
        kept["density"] = density
    if radius is not None:
        kept["radius"] = radius
    return Theme(name, title, css, kept)


def gui_config_dir(environ: dict | None = None) -> Path | None:
    """``$XDG_CONFIG_HOME/recursion/gui``, or ``$HOME/.config/recursion/gui``.

    The base must be an absolute path. A relative base, a non-string, or a
    missing home is no directory: the page does not guess one.
    """
    env = os.environ if environ is None else environ
    xdg = env.get("XDG_CONFIG_HOME")
    if type(xdg) is str and xdg != "":
        if not _absolute(xdg):
            return None
        base = xdg
    elif xdg is None or xdg == "":
        home = env.get("HOME")
        if type(home) is not str or not _absolute(home):
            return None
        base = home + "/.config"
    else:
        return None
    return Path(base) / "recursion" / "gui"


def active_theme_css(root: Path | None = None) -> str:
    """Compiled CSS for the selected theme, or ``""`` when nothing is applied.

    ``root`` is the operator's gui directory (``themes/*.json`` and
    ``selection.json``). ``None`` uses :func:`gui_config_dir`. A missing,
    untrusted, or oversized directory applies nothing. One bad file costs
    only itself. Two files that claim the same theme name are not selectable.
    There is no cache: each call reads the directory again.
    """
    if root is None:
        root = gui_config_dir()
        if root is None:
            return ""
    try:
        directory = Path(root)
    except TypeError:
        return ""
    themes = directory / "themes"
    if not _trusted_dir(directory) or not _trusted_dir(themes):
        return ""
    try:
        names = [name for name in os.listdir(themes) if _FILENAME.fullmatch(name)]
    except OSError:
        return ""
    if len(names) > MAX_THEME_FILES:
        return ""
    selected = _selected_name(directory / "selection.json")
    if selected is None:
        return ""
    found: dict[str, Theme] = {}
    blocked: set[str] = set()
    for filename in sorted(names):
        theme = _load_theme(themes / filename)
        if theme is None:
            continue
        if theme.name in found or theme.name in blocked:
            blocked.add(theme.name)
            found.pop(theme.name, None)
            continue
        found[theme.name] = theme
    theme = found.get(selected)
    if theme is None:
        return ""
    return theme.css


def _absolute(path: str) -> bool:
    return path.startswith("/") and os.path.isabs(path)


def _channel(value: int) -> float:
    # WCAG relative luminance, the same sRGB curve the desktop uses.
    channel = value / 255
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _luminance(hex_colour: str) -> float:
    number = int(hex_colour[1:], 16)
    red, green, blue = (number >> 16) & 255, (number >> 8) & 255, number & 255
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def _contrast(left: str, right: str) -> float:
    hi, lo = sorted((_luminance(left), _luminance(right)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _compile(colours: dict, density: str | None, radius: int | None) -> str:
    parts = ["--theme-%s:%s" % (name, colours[name]) for name in _COLOURS]
    if density is not None:
        parts.append("--theme-density:%s" % density)
    if radius is not None:
        parts.append("--theme-radius:%dpx" % radius)
    css = "[data-theme-scope]{%s;}" % ";".join(parts)
    if _COMPILED.fullmatch(css) is None:
        raise ThemeError("theme_compile")
    return css


def _process_uid() -> int | None:
    """The user id this process is acting as, or None when it cannot be known.

    ``os.geteuid`` is the POSIX-only call. The simulated-Windows child deletes
    it when it sets ``os.name`` to ``nt``, and a Windows host has no such call.
    The id is then the owner of this module, read with ``os.stat`` (not
    ``lstat`` or ``fstat``, so a stand-in of those two still disagrees with it).
    """
    if hasattr(os, "geteuid"):
        return os.geteuid()
    try:
        return os.stat(__file__).st_uid
    except OSError:
        return None


def _owned(info: os.stat_result, kind) -> bool:
    """A directory or regular file of this user, not writable by group or others."""
    uid = _process_uid()
    if uid is None:
        return False
    return kind(info.st_mode) and info.st_uid == uid and not info.st_mode & 0o022


def _trusted_dir(path: Path) -> bool:
    """A real directory owned by this user and not writable by group or others."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return False
    return _owned(info, stat.S_ISDIR)


def _read_owned(path: Path) -> bytes | None:
    """Bytes of an operator-owned regular file, or None when it cannot be used.

    A symlink is refused from ``lstat``. ``O_NOFOLLOW`` is POSIX-only: the
    simulated-Windows child deletes it, and ``os.open`` would then follow the
    link. Where the platform has the flag, the open still sets it. A FIFO is
    opened non-blocking and then refused because it is not a regular file.
    Group or other write permission, another owner's file, and a file over
    the size cap are not read as a theme.
    """
    try:
        linked = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(linked.st_mode):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not _owned(info, stat.S_ISREG):
            return None
        if info.st_size > MAX_THEME_BYTES:
            return None
        data = os.read(fd, MAX_THEME_BYTES + 1)
        if len(data) > MAX_THEME_BYTES:
            return None
        return data
    except OSError:
        return None
    finally:
        os.close(fd)


def _selected_name(path: Path) -> str | None:
    """The theme name in ``selection.json``, or None when nothing should apply.

    The file is exactly ``{"theme": "<name>"}`` or ``{"theme": null}``. Any
    other key (a layout name belongs to a later draft), a repeated member, or
    a name that is not a theme name applies nothing.
    """
    raw = _read_owned(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicate_members)
    except (UnicodeError, ValueError, RecursionError):
        return None
    if type(data) is not dict or set(data) != _SELECTION_KEYS:
        return None
    name = data["theme"]
    if name is None:
        return None
    if type(name) is not str or _NAME.fullmatch(name) is None:
        return None
    return name


def _load_theme(path: Path) -> Theme | None:
    raw = _read_owned(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicate_members)
        return parse_theme(data)
    except (OSError, UnicodeError, ValueError, RecursionError, ThemeError):
        return None
