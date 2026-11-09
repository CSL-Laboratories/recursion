"""Minimal deterministic repro: a probe that ended before a trip clears that trip (recursion #328).

No threads needed; this is the interleaving the stress driver hits thousands of times:

1. connection ``conn-a`` is tripped by a typed limit; the host starts a probe;
2. the probe succeeds and ends;
3. another run on ``conn-a`` reports a new typed limit (``plan_limit``);
4. the probe's result is handed back: ``reset_after_probe("conn-a", True)``.

Step 4 returns True and ``conn-a`` is no longer tripped, so a bound run on it passes
``check`` although the newest evidence is a vendor limit. ``reset_after_probe`` takes no probe
identity, lease or trip generation, so it cannot tell a probe older than the trip from a newer
one. Prints one JSON line; ``erased`` true means the race reproduced.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    tree = sys.argv[sys.argv.index("--tree") + 1]
    sys.path.insert(0, str(Path(tree) / "core"))
    from recursion_core.plugin_security.breaker import ConnectionBreaker
    from recursion_core.plugin_security.errors import Refused

    breaker = ConnectionBreaker()
    breaker.bind_run("run-1", "conn-a")
    breaker.report("conn-a", "rate_limit")          # 1. tripped; a probe starts
    probe_ok = True                                  # 2. the probe succeeded and ended
    breaker.report("conn-a", "plan_limit")           # 3. a newer limit lands
    cleared = breaker.reset_after_probe("conn-a", probe_ok)  # 4. the older probe reports
    try:
        breaker.check("run-1", "conn-a")
        check = "admitted"
    except Refused as exc:
        check = exc.code
    print(json.dumps({"reset_returned": cleared, "tripped_after": breaker.tripped("conn-a"),
                      "check_after": check, "erased": cleared and not breaker.tripped("conn-a")},
                     sort_keys=True))


if __name__ == "__main__":
    main()
