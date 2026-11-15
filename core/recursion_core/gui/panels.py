"""Plain plugin declarations, validated before any read can be sent.

Frozen interface, ``gui.panels/1`` (lane PAGE, slice 1, Nov 14 2026). The plugin host
(``daemon/plugins.py``) imports ``panel_registry``; the page imports ``filter_venue_rows`` and
``PAPER_BOOK``. ``gui/tests/test_panels_interface.py`` pins what follows, and changing any of it
is an interface change for every importer, not an edit:

* the names in ``__all__`` and the two signatures ``panel_registry(panels)`` and
  ``filter_venue_rows(value, venue)``;
* the accepted keys: ``REQUIRED_PANEL_KEYS`` and the optional ``venue``, ``venue_use`` and
  ``withhold_venue_data`` (together ``PANEL_KEYS``);
* the refusals: ``panel_registry`` raises ``ValueError`` whose text is one of ``REFUSALS``,
  checked in that order, so a declaration with several defects is refused with the first;
* the answer: a read-only mapping from panel id, in declaration order, to a read-only mapping of
  exactly ``title``, ``reads`` (a tuple, duplicates dropped, order kept), ``venue`` and
  ``venue_use``. ``withhold_venue_data`` is accepted and never kept: the page always filters.

Plain data means the built-in types themselves: a declaration is any mapping, its id, title,
venue, venue_use and every read are ``str`` exactly, ``reads`` is a ``list`` or ``tuple``
exactly, and ``withhold_venue_data`` is ``bool`` exactly. A subclass is refused, because the
page later compares, hashes and sends these values. ``reads`` is copied once and the copy is
what is checked and kept.
"""
from collections.abc import Mapping
import re
from types import MappingProxyType

from .client import READS

__all__ = ["PANEL_KEYS", "PAPER_BOOK", "REFUSALS", "REQUIRED_PANEL_KEYS", "filter_venue_rows",
           "panel_registry"]

REQUIRED_PANEL_KEYS = frozenset({"id", "title", "reads"})
PANEL_KEYS = REQUIRED_PANEL_KEYS | frozenset({"venue", "venue_use", "withhold_venue_data"})
# In the order panel_registry checks them.
REFUSALS = (
    "panel needs id, title and reads",
    "panel only declares reads",
    "invalid panel id",
    "duplicate panel id or empty title",
    "panel reads must be O7 reads",
    "venue must be a venue id",
    "venue_use must be venue:use",
    "withhold_venue_data must be a boolean",
)
(_NEEDS_KEYS, _ONLY_READS, _BAD_ID, _DUPLICATE_OR_TITLE, _BAD_READS, _BAD_VENUE, _BAD_VENUE_USE,
 _BAD_WITHHOLD) = REFUSALS


def panel_registry(panels):
    registered = {}
    for panel in panels:
        if not isinstance(panel, Mapping) or not REQUIRED_PANEL_KEYS <= panel.keys():
            raise ValueError(_NEEDS_KEYS)
        if set(panel) - PANEL_KEYS:
            raise ValueError(_ONLY_READS)
        pid, title, reads = panel["id"], panel["title"], panel["reads"]
        if type(pid) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", pid):
            raise ValueError(_BAD_ID)
        if pid in registered or type(title) is not str or not title.strip():
            raise ValueError(_DUPLICATE_OR_TITLE)
        # One copy, taken before any check: what is checked is what is kept.
        reads = tuple(reads) if type(reads) in (list, tuple) else ()
        if not reads or any(type(r) is not str or r not in READS for r in reads):
            raise ValueError(_BAD_READS)
        venue = panel.get("venue")
        if venue is not None and (type(venue) is not str or not re.fullmatch(r"[a-z0-9_]{1,48}", venue)):
            raise ValueError(_BAD_VENUE)
        venue_use = panel.get("venue_use")
        if venue_use is not None and (type(venue_use) is not str
                                      or not re.fullmatch(r"[a-z0-9_]{1,48}:[a-z0-9_]{1,48}", venue_use)):
            raise ValueError(_BAD_VENUE_USE)
        if type(panel.get("withhold_venue_data", True)) is not bool:
            raise ValueError(_BAD_WITHHOLD)
        # Always use the shared venue-data filter, even if the plugin asks otherwise.
        # O7 reads stay unscoped; venue_use is a label; declared venue filters data after the read.
        # paper and stock are one book in that filter (PAPER_BOOK); the panel still declares one id.
        registered[pid] = MappingProxyType({"title": title, "reads": tuple(dict.fromkeys(reads)),
                                            "venue": venue, "venue_use": venue_use})
    return MappingProxyType(registered)


_DROP = object()
_RESPONSE_LEVEL = ("errors", "provenance")
# The scalars json.loads gives (bool is an int; a float may be NaN or infinite). Anything else is withheld.
_JSON_SCALARS = (str, int, float, type(None))
# One paper book. O7 v0 labels positions ``stock``; the paper gateway labels orders ``paper``.
# A panel declaring either id keeps both row labels and both key prefixes. Any other venue
# matches one id exactly. Display only.
PAPER_BOOK = frozenset({"paper", "stock"})


def _owned_labels(venue):
    """Row labels a declared panel venue keeps. ``paper`` and ``stock`` own each other."""
    if isinstance(venue, str) and venue in PAPER_BOOK:
        return PAPER_BOOK
    if isinstance(venue, str):
        return frozenset({venue})
    return frozenset()


def _paper_book_key(key, venue):
    """True for ``paper_*`` and ``stock_*`` keys on a paper-book panel, and nothing else."""
    if not (isinstance(venue, str) and venue in PAPER_BOOK and isinstance(key, str)):
        return False
    return key.startswith("paper_") or key.startswith("stock_")


def filter_venue_rows(value, venue):
    """Keep read data for a declared panel venue, without mutating the response.

    ``value`` is one control-API response, a mapping; anything else is withheld whole.
    Returns ``(kept, withheld)``: ``kept`` is a new dict; ``withheld`` is true when the
    filter dropped a dict labelled with another venue, a value outside a dict labelled with
    an owned venue, or a value JSON cannot carry. An unlabelled container that is empty, or
    left empty, is left out too, and on its own does not set ``withheld`` (what was dropped
    from it does). A panel whose venue is ``paper`` or ``stock`` keeps both
    labels, and ``paper_*`` / ``stock_*`` keys, as one book. Any other venue keeps only
    that id. Fails closed: a dict labelled with a venue the panel does not own, and any
    value outside a dict labelled with an owned venue (unlabelled nested dicts, totals,
    scalars), is dropped and ``withheld`` is true. A matching dict is kept, and its nested
    data inherits that venue unless it declares a different one. A paper-book key is kept
    with what is under it, scalar or object, as part of the book; a dict under it labelled
    with another venue still stays out. Unlabelled containers stay only as the path to
    kept data. Exception: top-level ``errors`` and ``provenance`` of the response show
    whatever in them is unlabelled or labelled with an owned venue; a dict in them
    labelled with another venue, at any depth, is withheld. Nested keys with those names
    get no exception. This is a display filter, not order authorization or an API scope.

    Containers (C7 finding 4): any mapping is read as a dict and any list or tuple as a
    list, and they come back as a dict and a list, so a tuple of rows or a mapping proxy is
    filtered like the JSON it stands for. Any other value that is not a str, an int (bool
    included), a float or None (a set, a deque, any other object) is withheld wherever it is,
    even inside a kept row. A float is kept whatever its value, NaN and the infinities
    included, as ``json.loads`` gives them; this filter is not a strict-JSON sanitiser.
    """
    if not isinstance(value, Mapping):
        return {}, True
    withheld = []
    passed = {}
    if "venue" not in value:
        owned = _owned_labels(venue)
        for key in _RESPONSE_LEVEL:
            if key in value:
                shown = _without_other_venues(value[key], owned, withheld)
                if shown is not _DROP:
                    passed[key] = shown
        value = {key: item for key, item in value.items() if key not in _RESPONSE_LEVEL}
    kept = _venue_only(value, venue, False, withheld)
    return {**({} if kept is _DROP else kept), **passed}, bool(withheld)


def _without_other_venues(value, owned, withheld):
    """Response-level errors and provenance: drop only dicts labelled with a venue not owned."""
    if isinstance(value, Mapping):
        if "venue" in value and not (isinstance(value["venue"], str) and value["venue"] in owned):
            withheld.append(value)
            return _DROP
        shown = ((key, _without_other_venues(item, owned, withheld)) for key, item in value.items())
        return {key: item for key, item in shown if item is not _DROP}
    if isinstance(value, (list, tuple)):
        shown = (_without_other_venues(item, owned, withheld) for item in value)
        return [item for item in shown if item is not _DROP]
    if isinstance(value, _JSON_SCALARS):
        return value
    withheld.append(value)
    return _DROP


def _venue_only(value, venue, scoped, withheld):
    owned = _owned_labels(venue)
    if isinstance(value, Mapping):
        if "venue" in value:
            label = value["venue"]
            if not (isinstance(label, str) and label in owned):
                withheld.append(value)
                return _DROP
            scoped = True
        out = {}
        for key, item in value.items():
            # What sits under a paper_* / stock_* key is the book's own, like a labelled row.
            child = _venue_only(item, venue, scoped or _paper_book_key(key, venue), withheld)
            if child is not _DROP:
                out[key] = child
    elif isinstance(value, (list, tuple)):
        out = [item for item in (_venue_only(v, venue, scoped, withheld) for v in value) if item is not _DROP]
    elif scoped and isinstance(value, _JSON_SCALARS):
        return value
    else:
        withheld.append(value)
        return _DROP
    return out if out or scoped else _DROP
