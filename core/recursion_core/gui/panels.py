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
        # O7 reads stay unscoped; venue_use is a label; declared venue filters rows after the read.
        registered[pid] = MappingProxyType({"title": title, "reads": tuple(dict.fromkeys(reads)),
                                            "venue": venue, "venue_use": venue_use})
    return MappingProxyType(registered)


def filter_venue_rows(value, venue, *, scoped=False):
    """Filter unscoped read rows for a declared panel venue, without mutating the response.

    Rows without venue attribution are withheld too. Nested data in a matching row
    inherits its venue, unless it declares a different one. This is a display filter,
    not order authorization or an API scope.
    """
    if isinstance(value, dict):
        if "venue" in value:
            if value["venue"] != venue:
                return {}
            scoped = True
        return {key: filter_venue_rows(item, venue, scoped=scoped) for key, item in value.items()}
    if isinstance(value, list):
        return [filter_venue_rows(item, venue, scoped=scoped) for item in value
                if (isinstance(item, dict) and item.get("venue", venue if scoped else None) == venue)
                or (scoped and not isinstance(item, dict))]
    return value
