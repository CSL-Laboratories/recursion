"""Minimal deterministic repro of CSL-804: a probe that ended before a trip clears that trip (#328).

No threads needed; this is the interleaving the stress driver hits under load:

1. connection ``conn-a`` is tripped by a typed limit; the host leases a probe
   (``begin_probe``, #328 ``afe38c1f`` and later);
2. the probe succeeds and ends;
3. another run on ``conn-a`` reports a new typed limit (``plan_limit``);
4. the probe's result is handed back: ``reset_after_probe("conn-a", lease, True)``.

At #328 ``0264628b`` there was no lease: ``reset_after_probe("conn-a", True)`` returned True
and erased the newer trip. At ``afe38c1f`` a new trip drops the lease, so step 4 must return
False and ``conn-a`` stays tripped. The script also tries a reset with no lease at all. Prints
one JSON line; ``erased`` true means the race reproduced. A tree without ``begin_probe``
exits 3 (the pre-lease API this repro no longer drives).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    tree = sys.argv[sys.argv.index("--tree") + 1]
    sys.path.insert(0, str(Path(tree) / "core"))
    from recursion_core.plugin_security import breaker as mod
    from recursion_core.plugin_security.errors import Refused

    if not hasattr(mod.ConnectionBreaker, "begin_probe"):
        print(json.dumps({"lease_api": False, "error": "tree predates the #328 lease API"}))
        sys.exit(3)
    with tempfile.TemporaryDirectory(prefix="repro328-") as tmp:
        path = os.path.join(tmp, "breaker.db")
        mod.create_breaker_store(path)
        breaker = mod.ConnectionBreaker(path, clock=lambda: 0.0)
        breaker.bind_run("run-1", "conn-a", "plug-1", "prov", "model-1")
        breaker.report("conn-a", "rate_limit")                         # 1. tripped
        lease = breaker.begin_probe("conn-a")                          #    probe leased
        probe_ok = True                                                # 2. probe succeeded
        breaker.report("conn-a", "plan_limit")                         # 3. a newer limit lands
        cleared = breaker.reset_after_probe("conn-a", lease, probe_ok)  # 4. the older probe reports
        unleased = breaker.reset_after_probe("conn-a", None, True)     #    and a reset with no lease
        try:
            breaker.check("run-1", "conn-a", "plug-1")
            check = "admitted"
        except Refused as exc:
            check = exc.code
        tripped = breaker.tripped("conn-a")
        breaker.close()
    print(json.dumps({"reset_returned": cleared, "unleased_reset_returned": unleased,
                      "tripped_after": tripped, "check_after": check,
                      "erased": (cleared or unleased) and not tripped,
                      "probe_leased": True, "lease_api": True}, sort_keys=True))


if __name__ == "__main__":
    main()
