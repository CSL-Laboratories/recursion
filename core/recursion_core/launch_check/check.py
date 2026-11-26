"""One launch-time pass over every engine in the floor table (CSL-732, candidate).

``launch_check(path_string)`` detects each engine on an explicit PATH string and tries
to grant the detected candidate. Each engine gets exactly one record: ``granted`` with
the grant's fields, ``refused`` with a typed code, or ``absent``. Grants are returned
separately so a caller can never take a grant from a refused record.
"""

from __future__ import annotations

from recursion_core.launch_check.detect import detect
from recursion_core.launch_check.floors import FLOOR_TABLE_VERSION, FLOORS
from recursion_core.launch_check.grant import Grant, grant
from recursion_core.launch_check.probe import PROBE_TIMEOUT_S, Probe
from recursion_core.launch_check.refusals import REFUSAL_CODES_VERSION, LaunchRefused


def launch_check(
    path_string: str, *, timeout: float = PROBE_TIMEOUT_S, probe: Probe | None = None
) -> tuple[dict[str, Grant], list[dict[str, object]]]:
    grants: dict[str, Grant] = {}
    records: list[dict[str, object]] = []
    for engine in FLOORS:
        found = detect(engine, path_string)
        hits = [h.path for h in found.hits]
        if not found.hits:
            records.append({"outcome": "absent", "engine": engine, "floor_table": FLOOR_TABLE_VERSION,
                            "refusal_codes": REFUSAL_CODES_VERSION})
            continue
        if found.candidate is None:
            rec = LaunchRefused("wrapper_refused", engine, found.note).record()
        else:
            try:
                g = grant(engine, found.candidate, timeout=timeout, probe=probe)
            except LaunchRefused as r:
                rec = r.record()
            else:
                grants[engine] = g
                rec = g.record()
        rec["hits"] = hits
        rec["detection"] = found.note
        records.append(rec)
    return grants, records
