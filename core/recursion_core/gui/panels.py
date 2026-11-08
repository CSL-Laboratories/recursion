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
        if set(panel) - {"id", "title", "reads", "venue_use", "withhold_venue_data"}:
            raise ValueError("panel only declares reads")
        pid, title, reads = panel["id"], panel["title"], panel["reads"]
        if not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", pid):
            raise ValueError("invalid panel id")
        if pid in registered or not isinstance(title, str) or not title.strip():
            raise ValueError("duplicate panel id or empty title")
        if not isinstance(reads, (list, tuple)) or not reads or any(r not in READS for r in reads):
            raise ValueError("panel reads must be O7 reads")
        venue_use = panel.get("venue_use")
        if venue_use is not None and (not isinstance(venue_use, str)
                                      or not re.fullmatch(r"[a-z0-9_]{1,48}:[a-z0-9_]{1,48}", venue_use)):
            raise ValueError("venue_use must be venue:use")
        if type(panel.get("withhold_venue_data", True)) is not bool:
            raise ValueError("withhold_venue_data must be a boolean")
        # Always use the shared venue-data filter, even if the plugin asks otherwise.
        # venue_use is kept as a label only: O7 v0 reads take no venue argument, so the
        # page states that its reads are unscoped instead of implying a venue filter.
        registered[pid] = MappingProxyType({"title": title, "reads": tuple(dict.fromkeys(reads)),
                                            "venue_use": venue_use})
    return MappingProxyType(registered)
