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
        registered[pid] = MappingProxyType({"title": title, "reads": tuple(dict.fromkeys(reads)),
                                            "venue": venue, "venue_use": venue_use})
    return MappingProxyType(registered)


_DROP = object()
_RESPONSE_LEVEL = ("errors", "provenance")


def filter_venue_rows(value, venue):
    """Keep only read data labelled with a declared panel venue, without mutating the response.

    Returns ``(kept, withheld)``. Fails closed: a dict labelled with another venue, and any
    value outside a dict labelled ``venue`` (unlabelled nested dicts, totals, scalars), is
    dropped and ``withheld`` is true. A matching dict is kept, and its nested data inherits
    the venue unless it declares a different one. Unlabelled containers stay only as the
    path to kept data. Exception: top-level ``errors`` and ``provenance`` of the response
    pass through unfiltered unless they are a dict with a ``venue`` label; nested keys with
    those names get no exception. This is a display filter, not order authorization or an
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
    if isinstance(value, dict):
        if "venue" in value:
            if value["venue"] != venue:
                withheld.append(value)
                return _DROP
            scoped = True
        pairs = ((key, _venue_only(item, venue, scoped, withheld)) for key, item in value.items())
        out = {key: item for key, item in pairs if item is not _DROP}
    elif isinstance(value, list):
        out = [item for item in (_venue_only(v, venue, scoped, withheld) for v in value) if item is not _DROP]
    elif scoped:
        return value
    else:
        withheld.append(value)
        return _DROP
    return out if out or scoped else _DROP
