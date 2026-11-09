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
# matches one id exactly. Same book as Cockpit #318 at 70302970. Display only.
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
    data inherits that venue unless it declares a different one. A paper-book key is kept:
    a scalar shows, and a dict or list under it is still filtered, so a foreign row inside
    ``paper_*`` stays out. Unlabelled containers stay only as the path to kept data.
    Exception: top-level ``errors`` and ``provenance`` of the response pass through
    unfiltered unless they are a dict with a ``venue`` label; nested keys with those
    names get no exception. This is a display filter, not order authorization or an
    API scope.
    """
    withheld = []
    passed = {}
    if isinstance(value, dict) and "venue" not in value:
        passed = {key: value[key] for key in _RESPONSE_LEVEL
                  if key in value and not (isinstance(value[key], dict) and "venue" in value[key])}
        value = {key: item for key, item in value.items() if key not in passed}
    kept = _venue_only(value, venue, False, withheld)
    return {**({} if kept is _DROP else kept), **passed}, bool(withheld)


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
            if not scoped and _paper_book_key(key, venue):
                if isinstance(item, (dict, list)):
                    child = _venue_only(item, venue, False, withheld)
                    if child is not _DROP:
                        out[key] = child
                else:
                    out[key] = item
                continue
            child = _venue_only(item, venue, scoped, withheld)
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
