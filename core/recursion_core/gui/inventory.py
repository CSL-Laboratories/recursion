"""Route inventory, beta rubric and maturity projections (F25, grill Q1 and Q37, O50).

The GUI does not invent routes. It shows the route records the control API returns,
re-checks each record's maturity with the rubric below, and lists every route class
Nov 11 needs that the control API did not report, as a gap with its blocker.

Rubric: a route is ``mature`` only when every bar (``BARS``) passed *and* names its
evidence. A failing, missing or evidence-less bar makes it ``beta``. A record that
claims ``mature`` while the rubric says ``beta`` is shown as beta with the claim marked
rejected; a record without a ``maturity`` field is shown as beta and flagged. A
malformed record (not an object, a missing or non-string class, or a non-string subject field) is
shown as a beta row flagged ``malformed_route_record`` and covers no required route.
There is no score and no majority rule.

O50 ("Beta until measured"): a route the control API or its launch record tags beta
(``maturity`` exactly ``"beta"``, or ``beta`` set to ``true`` as the R2a launch grant does
when an engine's functional floor is unset) is shown as beta even when every bar passed.
A ``beta`` field that is neither ``true`` nor ``false`` is shown as beta and flagged. The
GUI only displays the tag; it never refuses a route for it.
"""

from __future__ import annotations

import re

__all__ = ["BARS", "REQUIRED", "assess", "coverage_gaps", "project"]

BARS = ("launch_check", "floor", "conformance", "truthful_stop", "venue_switch", "xf_verify")

# What the Nov 11 core inventory must cover (F25, PRODUCT.md 3.3, 7; Q42, 34b). Own-loop
# vendors are this slice's reading of the briefing, flagged for review. Venue uses belong
# to the Cockpit plugin, which passes its own classes in through ``required``.
REQUIRED = {
    "engine": ("claude_code", "codex", "opencode", "grok", "antigravity", "cursor", "gemini", "pi",
               "hermes"),
    "own_loop": ("anthropic", "openai", "google", "xai", "copilot", "nous"),
    "surface": ("cli", "desktop", "phone"),
    "mcp": ("2025-06-18", "2026-07-28"),
    "plugin_panels": ("plugin_panels",),
}
_O61_HELD = frozenset({"copilot", "nous"})
_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,96}")


def _subject(record: dict) -> str | None:
    """The thing a record covers inside its class, e.g. the engine id or the MCP version."""
    cls = record.get("class")
    if type(cls) is not str:
        return None
    key = {"engine": "engine", "own_loop": "vendor", "surface": "surface", "mcp": "mcp_version"}.get(cls)
    if key:
        return record.get(key) if type(record.get(key)) is str else None
    if cls == "venue_use":
        venue, use = record.get("venue"), record.get("use")
        return "%s:%s" % (venue, use) if type(venue) is str and type(use) is str else None
    if cls == "plugin_panels":
        return "plugin_panels"
    return None


_SUBJECT_FIELDS = {"engine": ("engine",), "own_loop": ("vendor",), "surface": ("surface",),
                   "mcp": ("mcp_version",), "venue_use": ("venue", "use")}


def _well_formed(record: object) -> bool:
    """An object with a string class whose subject fields, where present, are strings."""
    if type(record) is not dict or type(record.get("class")) is not str:
        return False
    return all(type(record[f]) is str for f in _SUBJECT_FIELDS.get(record["class"], ()) if f in record)


def assess(record: object) -> dict:
    """Return the record as the GUI shows it, with the rubric applied. Never upgrades."""
    if not _well_formed(record):
        rid = record.get("id") if type(record) is dict else None
        return {"id": rid if type(rid) is str and _ID.fullmatch(rid) else "invalid",
                "class": "invalid", "subject": "", "sign_in": "", "configured": False,
                "maturity": "beta", "claim": "malformed_route_record", "beta_tag": False,
                "failed_bars": list(BARS), "blocker": "malformed route record from the control API"}
    bars = record.get("bars") if type(record.get("bars")) is dict else {}
    failed = []
    for bar in BARS:
        entry = bars.get(bar)
        if not (type(entry) is dict and entry.get("pass") is True and type(entry.get("evidence")) is str
                and entry["evidence"].strip()):
            failed.append(bar)
    rubric = "beta" if failed else "mature"
    claimed = record.get("maturity")
    tag = record.get("beta", False)
    if claimed not in ("beta", "mature"):
        claim = "missing_maturity"
    elif type(tag) is not bool:
        claim = "beta_tag_invalid"
    elif claimed == "mature" and rubric == "beta":
        claim = "mature_claim_rejected"
    else:
        claim = "ok"
    held = record["class"] == "own_loop" and _subject(record) in _O61_HELD
    tagged = claimed == "beta" or tag is True or held
    route_id = record.get("id") if type(record.get("id")) is str and _ID.fullmatch(record["id"]) else "invalid"
    blocker = record.get("blocker") if type(record.get("blocker")) is str else ""
    if held:
        blocker = "O61: per-vendor permission verification held" + ("; " + blocker if blocker else "")
    if failed and not blocker:
        blocker = "not reported by control API"
    out = {
        "id": route_id, "class": str(record.get("class", "")), "subject": str(_subject(record) or ""),
        "sign_in": str(record.get("sign_in", "")), "configured": record.get("configured") is True,
        "maturity": "beta" if (rubric == "beta" or claim != "ok" or tagged) else "mature",
        "claim": claim, "beta_tag": tagged, "failed_bars": failed, "blocker": blocker,
    }
    return out


def coverage_gaps(records: list, required: dict | None = None) -> list[dict]:
    """Required route classes the control API did not report. Each gap is beta with a blocker."""
    seen: dict[str, set] = {}
    for record in records:
        if _well_formed(record):  # a malformed record never hides a required-route gap
            seen.setdefault(record.get("class"), set()).add(_subject(record))
    combined = {cls: list(subjects) for cls, subjects in REQUIRED.items()}
    for cls, subjects in (required or {}).items():
        combined[cls] = list(dict.fromkeys([*combined.get(cls, ()), *subjects]))
    gaps = []
    for cls, subjects in combined.items():
        for subject in subjects:
            if subject not in seen.get(cls, set()):
                gaps.append({"id": "gap:%s:%s" % (cls, subject), "class": cls, "subject": subject,
                             "sign_in": "", "configured": False, "maturity": "beta", "claim": "not_in_inventory",
                             "beta_tag": cls == "own_loop" and subject in _O61_HELD,
                             "failed_bars": list(BARS),
                             "blocker": "upstream: control API did not report this route"
                             + ("; O61: per-vendor permission verification held"
                                if cls == "own_loop" and subject in _O61_HELD else "")})
    return gaps


def project(row: dict, surface: str) -> object:
    """The same maturity on every surface: CLI line, MCP object, GUI badge text."""
    if surface == "cli":
        return "%s maturity=%s blocker=%s" % (row["id"], row["maturity"], row["blocker"] or "-")
    if surface == "mcp":
        return {"id": row["id"], "maturity": row["maturity"], "failed_bars": list(row["failed_bars"]),
                "blocker": row["blocker"]}
    if surface == "gui":
        return "BETA" if row["maturity"] == "beta" else "MATURE"
    raise ValueError("unknown surface")
