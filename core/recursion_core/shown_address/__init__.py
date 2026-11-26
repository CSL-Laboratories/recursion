"""The one shown form of an address (CSL-238).

An address a person gives (an argument, a setting, an environment value) can carry a sign-in:
``http://user:password@host/``, or a token in its query or its path. Nothing the product shows (a
refusal's text, a doctor's record, a log line, a ``repr``) carries one. Every place that puts an address
into words calls ``shown_address`` and shows what it returns, never the address as it was given; a
structure test of the product tree holds every place to that. A caller that needs the address to work
with keeps the given text in a value of its own that it never shows.

The shown form is BUILT UP from a few parts, never cut down from what was given:

* the scheme, as written, only when it is one of the ``schemes`` the caller takes; with no ``schemes``
  given no scheme is shown and the words ``<scheme not shown>`` stand for it;
* the host, as written, when it is a name made of letters, digits, dots, dashes and underscores, or an
  IPv6 address in brackets; an IPv6 zone id (``%25eth0``) is left out and the words
  ``<zone id not shown>`` follow;
* the port, as the number it reads as.

Whatever stands after the host and the port is never shown as written. A path is the words
``<path not shown>``, whatever its text, its length or its number of parts, unless the caller asks for
``plain_path``: then a path whose every character is a letter, a digit, a slash, a dash, an underscore
or a dot is shown, path parameters (from the first ``;``) are the words ``<path parameters not shown>``
after the plain part before them, and a path of any other kind is ``<path not shown>``. Set words stand
for each other part that was there, never its text or its length: ``<sign-in hidden>`` for a user name
or a password, ``<query not shown>``, ``<fragment not shown>``.

The checks run on the text as it was given, before the URL library reads it (the library deletes tab,
CR and LF and strips control characters from the front and the scheme, so a check after it would not
see them). Only white space at the two ends is set aside first, as every caller does. Then:

* An address with white space or a character that cannot be printed anywhere inside it is never shown
  in any part: a pasted second line must not come back joined to the first.
* An address with an @ after its host (in the path, the query or the fragment) is not shown in any
  part: the host may be the front of a sign-in whose password holds a ``/``, a ``?`` or a ``#``
  (``http://admin:12?34@host`` parses with the host ``admin`` and the port ``12``).
* An address that does not parse, that has no scheme, a scheme the caller does not take, no host, a
  port that is not a number from 0 to 65535, or a host of another kind is never shown in any part:
  words say what was wrong and that the address is not shown.
* A value that is not a text is named by its type only.

No shown form holds an @, so a rule for free text that hides everything before an @ leaves every shown
form as it is. ``shown_address`` never raises.

What the rule does not hide, known: a secret used as a host name (``http://SECRET.example/``) is shown
wherever the host is, since the host says which address was meant and nothing tells such a secret from a
name; and where a caller asks for ``plain_path``, a secret written as a plain path
(``http://host/v1/SECRET``) is shown there.

``holds_sign_in`` says whether a user name or a password stands in an address's authority.

Standard library only, so every package of the product can call it.
"""

from __future__ import annotations

import ipaddress
import re
import string
from collections.abc import Iterable
from urllib.parse import urlsplit

__all__ = ["HIDDEN_SIGN_IN", "holds_sign_in", "shown_address"]

# The words put where a user name or password stood. A rule for free text that hides a sign-in uses the
# same words, so a person reads one phrase for one thing.
HIDDEN_SIGN_IN = "<sign-in hidden>"
_ZONE_NOT_SHOWN = "<zone id not shown>"
_SCHEME_NOT_SHOWN = "<scheme not shown>"
_PARAMETERS_NOT_SHOWN = "<path parameters not shown>"
_PATH_NOT_SHOWN = "<path not shown>"
_QUERY_NOT_SHOWN = "<query not shown>"
_FRAGMENT_NOT_SHOWN = "<fragment not shown>"
_UNREADABLE = "<an address that could not be read; the address is not shown>"

# The characters of the parts that may be shown. These are the address's grammar, not a figure a person
# chooses.
_PLAIN_PATH = frozenset(string.ascii_letters + string.digits + "/-_.")
_PLAIN_NAME = frozenset(string.ascii_letters + string.digits + "-_.")
_IPV6 = frozenset(string.hexdigits + ":.")
# Where an authority starts: ``scheme://`` or ``//`` at the front of the text.
_AUTHORITY_START = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]*:)?//")
# The URL library deletes these wherever they stand, so they alone make no user name.
_DELETED_BY_THE_LIBRARY = ("\t", "\r", "\n")


def _not_shown(what: str) -> str:
    return f"<an address with {what}; the address is not shown>"


def _one_of(schemes: tuple[str, ...]) -> str:
    if not schemes:
        return "a scheme taken here"
    if len(schemes) == 1:
        return schemes[0]
    return ", ".join(schemes[:-1]) + " or " + schemes[-1]


def _unparsed(exc: ValueError) -> str:
    # The error's own text is never shown: on some interpreters it carries the whole authority, sign-in
    # included. It is only read here to say which kind of form was refused.
    text = str(exc)
    if "NFKC" in text:
        return ("a character before the path that NFKC normalization turns into a slash, a question mark, a "
                "number sign, an at sign or a colon")
    if "IPv6" in text:
        return "a [ or ] that does not frame an IPv6 host"
    return "a form that does not parse as an address"


def _odd(text: str) -> bool:
    return any(not ch.isprintable() or ch.isspace() for ch in text)


def holds_sign_in(value: object) -> bool:
    """True when a user name or a password stands in the authority of ``value``: the text after
    ``scheme://`` or ``//`` at its front, up to the first ``/``, ``?`` or ``#``, holds an @, and what
    stands before its last @ is more than a colon. Tab, CR and LF are left out of that reading first,
    since the URL library deletes them. An @ in the path, the query or the fragment, an empty user
    (``http://@host``) and a text with no authority are not a sign-in by this rule. Never raises."""

    try:
        if not isinstance(value, str):
            return False
        text = value.strip()
        front = _AUTHORITY_START.match(text)
        if front is None:
            return False
        rest = text[front.end():]
        ends = [at for at in (rest.find(mark) for mark in "/?#") if at >= 0]
        userinfo, at, _ = rest[:min(ends, default=len(rest))].rpartition("@")
        if not at:
            return False
        for ch in _DELETED_BY_THE_LIBRARY:
            userinfo = userinfo.replace(ch, "")
        return userinfo not in ("", ":")
    except Exception:  # a text subclass that misbehaves is not read further
        return True


def shown_address(value: object, *, schemes: Iterable[str] | None = None, plain_path: bool = False) -> str:
    """The form of ``value`` that may be shown to a person or written to a log, by the rule of this module.

    ``schemes``, when given, are the schemes the caller takes (lower case): such a scheme is shown and an
    address with another scheme is described in words that do not name it. Without ``schemes`` no scheme
    is shown. ``plain_path`` shows a plain path; without it no path is shown. Never raises."""

    try:
        return _shown(value, None if schemes is None else tuple(schemes), plain_path is True)
    except Exception:  # never raises: an odd value is described, never shown
        return _UNREADABLE


def _host(hostport: str) -> tuple[str, list[str]] | None:
    """The host as it may be shown and the words that follow it, or None when it is of another kind."""

    if hostport.startswith("["):
        inside = hostport[1:hostport.find("]")]
        address, zone, _ = inside.partition("%")
        if not address or any(ch not in _IPV6 for ch in address):
            return None
        try:
            ipaddress.IPv6Address(address)
        except ValueError:
            return None
        return f"[{address}]", [_ZONE_NOT_SHOWN] if zone else []
    name = hostport.partition(":")[0]
    if not name or any(ch not in _PLAIN_NAME for ch in name):
        return None
    return name, []


def _path(path: str, plain_path: bool) -> tuple[str, list[str]]:
    if not path:
        return "", []
    if not plain_path:
        return "", [_PATH_NOT_SHOWN]
    plain, semicolon, _ = path.partition(";")
    if any(ch not in _PLAIN_PATH for ch in plain):
        return "", [_PATH_NOT_SHOWN]
    return plain, [_PARAMETERS_NOT_SHOWN] if semicolon else []


def _shown(value: object, schemes: tuple[str, ...] | None, plain_path: bool) -> str:
    if not isinstance(value, str):
        return f"<a value of type {type(value).__name__}, not an address text>"
    text = value.strip()
    if not text:
        return "<an empty address>"
    if _odd(text):  # on the text as given, before the URL library deletes or strips any of it
        return _not_shown("white space or a character that cannot be printed inside it")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        return _not_shown(_unparsed(exc))
    if text.startswith("//"):
        if schemes is not None:
            return _not_shown(f"no scheme, which must be {_one_of(schemes)}")
        front = "//"
    else:
        scheme = parts.scheme
        if not scheme or text[len(scheme):len(scheme) + 3] != "://":
            return _not_shown("no scheme such as http:// before its host")
        if schemes is not None and scheme not in schemes:
            return _not_shown(f"a scheme that is not {_one_of(schemes)}")  # its text is never shown
        front = (_SCHEME_NOT_SHOWN if schemes is None else text[:len(scheme)]) + "://"
    if "@" in parts.path or "@" in parts.query or "@" in parts.fragment:
        return _not_shown("an at sign after its host, which may end a sign-in")
    if not parts.hostname:
        return _not_shown("no host")
    try:
        port = parts.port
    except ValueError:
        return _not_shown("a port that is not a number from 0 to 65535")
    host = _host(parts.netloc.rpartition("@")[2])
    if host is None:
        return _not_shown("a host that is neither a name of letters, digits, dots, dashes and underscores nor "
                          "an IPv6 address in brackets")
    shown_host, notes = host
    if "@" in parts.netloc:
        notes.insert(0, HIDDEN_SIGN_IN)
    shown_path, path_notes = _path(parts.path, plain_path)
    notes += path_notes
    if "?" in text.split("#", 1)[0]:  # the scheme, the authority and the path never hold a ?
        notes.append(_QUERY_NOT_SHOWN)
    if "#" in text:
        notes.append(_FRAGMENT_NOT_SHOWN)
    shown_port = "" if port is None else f":{port}"
    return " ".join([front + shown_host + shown_port + shown_path, *notes])
