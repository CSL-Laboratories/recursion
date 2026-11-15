"""The third reads of #376 at 5079080c (Codex GPT-6 Astra and GPT-6.1 Sol, Nov 14, 2026), as lane tests.

Astra's change 2: the PROVISIONAL ``resume(engine_session_id, session, workspace, env)`` had three single
removals that left every lane test green: dropping the workspace, dropping the environment, and handing the
subclass the assembled record without the checked copy. Astra's ``test_review3_resume.py`` caught all three;
this is that test in this lane's fixtures, run for ``start`` as well and over each workspace form. Sol's
change 2 (the engine-table guard) is in ``test_engine_check.py``. Stand-ins only: no engine, process,
provider or network.
"""

from __future__ import annotations

import copy
import unittest

from recursion_core.routes.tests.test_driver import ALL, StandIn, session
from recursion_core.routes.tests.test_driver_review2 import worktree

DIRECT = {"path": "/srv/work", "mode": "direct", "base_ref": None, "worktree_path": None}


class MutatingStandIn(StandIn):
    """Records exactly what ``_start`` receives, then changes every part of it, nested parts included."""

    def __init__(self) -> None:
        super().__init__(ALL)
        self.seen: list[tuple] = []

    def _start(self, handle, request, env):
        self.seen.append((copy.deepcopy(request), dict(env)))
        request["run_id"] = "changed"
        request["route"]["connection"]["id"] = "changed"
        if request["workspace"] is not None:
            request["workspace"]["path"] = "/changed"
        env["SYNTHETIC"] = "changed"


class StartRecordForwardingTest(unittest.TestCase):
    """Astra change 2 (round 3), for resume and for start."""

    def test_the_whole_record_and_environment_reach_the_engine_driver_as_a_copy(self):
        for call, esid in (("start", None), ("resume", "conversation")):
            for workspace in (worktree(), dict(DIRECT), None):
                with self.subTest(call=call, workspace=workspace):
                    sess, env = session(run_id="run-2"), {"SYNTHETIC": "value"}
                    before = copy.deepcopy((sess, workspace, env))
                    driver = MutatingStandIn()
                    if call == "start":
                        handle = driver.start(sess, workspace, env)
                    else:
                        handle = driver.resume(esid, sess, workspace, env)
                    self.assertEqual(driver.seen, [({**before[0], "workspace": before[1], "engine_session_id": esid},
                                                    before[2])])
                    self.assertEqual((sess, workspace, env), before)  # the hook's changes reach no caller input
                    self.assertEqual(handle.as_record(), {"run_id": "run-2", "engine_session_id": esid})


if __name__ == "__main__":
    unittest.main()
