"""Plain plugin declarations, validated before any read can be sent."""
from collections.abc import Mapping
import re
from types import MappingProxyType

from .client import READS


def panel_registry(panels):
    registered = {}
    for panel in panels:
        if not isinstance(panel, Mapping) or not {"id", "title", "reads"} <= panel.keys():
            raise ValueError("panel needs id, title and reads")
        if set(panel) - {"id", "title", "reads", "venue", "venue_use", "withhold_venue_data"}:
            raise ValueError("panel only declares reads")
        pid, title, reads = panel["id"], panel["title"], panel["reads"]
        if not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", pid):
            raise ValueError("invalid panel id")
        if pid in registered or not isinstance(title, str) or not title.strip():
            raise ValueError("duplicate panel id or empty title")
        if not isinstance(reads, (list, tuple)) or not reads or any(r not in READS for r in reads):
            raise ValueError("panel reads must be O7 reads")
        venue = panel.get("venue")
        if venue is not None and (not isinstance(venue, str) or not re.fullmatch(r"[a-z0-9_]{1,48}", venue)):
            raise ValueError("venue must be a venue id")
        venue_use = panel.get("venue_use")
        if venue_use is not None and (not isinstance(venue_use, str)
                                      or not re.fullmatch(r"[a-z0-9_]{1,48}:[a-z0-9_]{1,48}", venue_use)):
            raise ValueError("venue_use must be venue:use")
        if type(panel.get("withhold_venue_data", True)) is not bool:
            raise ValueError("withhold_venue_data must be a boolean")
        # Always use the shared venue-data filter, even if the plugin asks otherwise.
        # O7 reads stay unscoped; venue_use is a label; declared venue filters data after the read.
        # paper and stock are one book in that filter (PAPER_BOOK); the panel still declares one id.
        registered[pid] = MappingProxyType({"title": title, "reads": tuple(dict.fromkeys(reads)),
                                            "venue": venue, "venue_use": venue_use})
    return MappingProxyType(registered)


_DROP = object()
_RESPONSE_LEVEL = ("errors", "provenance")
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

    Returns ``(kept, withheld)``. A panel whose venue is ``paper`` or ``stock`` keeps both
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
    """
    withheld = []
    passed = {}
    if isinstance(value, dict) and "venue" not in value:
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
    if isinstance(value, dict):
        if "venue" in value and not (isinstance(value["venue"], str) and value["venue"] in owned):
            withheld.append(value)
            return _DROP
        shown = ((key, _without_other_venues(item, owned, withheld)) for key, item in value.items())
        return {key: item for key, item in shown if item is not _DROP}
    if isinstance(value, list):
        shown = (_without_other_venues(item, owned, withheld) for item in value)
        return [item for item in shown if item is not _DROP]
    return value


def _venue_only(value, venue, scoped, withheld):
    owned = _owned_labels(venue)
    if isinstance(value, dict):
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
    elif isinstance(value, list):
        out = [item for item in (_venue_only(v, venue, scoped, withheld) for v in value) if item is not _DROP]
    elif scoped:
        return value
    else:
        withheld.append(value)
        return _DROP
    return out if out or scoped else _DROP
