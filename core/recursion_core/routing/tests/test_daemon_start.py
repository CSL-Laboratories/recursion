"""The first proof of lane ROUTING through the daemon (slice 3): "A hand pick overrides the config on one task"
(CONTRACT 1729). A daemon is composed as SPINE's seam tests compose one (``daemon/tests/test_seams.py``:
everything real but the service manager, with the stand-in engine), reading the operator's ``routes.json``
and ``routing.json`` from its config folder.

What cannot be shown yet: a start matching a rule. ``run.start`` carries no ``task_kind`` or ``tags`` until
SPINE's next schema revision, so a start gets its hand pick or the default; the rule in the file below never
matches a start and is there to show that.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from recursion_core.daemon.tests.test_composition import LINUX

if LINUX:
    from recursion_core.daemon.tests.test_seams import LIVE, Composed, hooks_of
    from recursion_core.routing import hooks as rh
else:  # pragma: no cover - the composed daemon is Linux only
    Composed = unittest.TestCase


@unittest.skipUnless(LINUX, "the composed daemon is Linux only")
class HandPickThroughTheDaemonTest(Composed):
    def setUp(self) -> None:
        super().setUp()
        folder = self.tmp / "config" / "recursion"
        folder.mkdir(parents=True, mode=0o700)
        entry = dict(self.routes["claude_code"]["route"], deadline_s=120, binary=str(self.engine))
        entry.pop("engine")
        self.write(folder / "routes.json", {"routes": {"claude_code": entry}})
        self.write(folder / "routing.json", {
            "version": 1,
            "logins": [{"id": "anthropic-sub", "vendor": "anthropic", "kind": "subscription",
                        "connection_id": "stand-in-login", "engines": ["claude_code"]}],
            "default": {"engine": "claude_code"},
            "rules": [{"id": "review", "when": {"task_kind": ["review"]},
                       "route": {"engine": "claude_code", "model": "rule-model"}}]})
        self.routes = None  # the daemon reads routes.json from its config folder, as the registrant does
        # The registrant reads the process's own environment, as compose_control does when it is given none.
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.environ["XDG_CONFIG_HOME"]})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def write(path, data) -> None:
        path.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(path, 0o600)

    def test_a_start_naming_no_engine_gets_the_default(self) -> None:
        # SPINE's harness (#370 at a3cb385c) gives every installed registrant a stand-in that registers nothing, and
        # each lane tests its own registration: routing's is passed in here, and test_hooks checks that
        # control/hooks.py's own compose() finds it.
        _, composed = self.boot(hooks_of(rh.register))
        answer = composed.control.call("run.start", {"tenant": "t1", "prompt": LIVE})
        self.assertEqual(answer["code"], "started", answer)
        launch = composed.control.registry.get(answer["run"]["run_id"])["launch"]
        self.assertEqual(launch["route_decision"]["reason"], "default")
        self.assertEqual(launch["route_entry"]["route"]["model"], "stand-in-model")
        self.assertEqual(launch["route_entry"]["binary"], str(self.engine))
        self.stop(composed.control, answer["run"]["run_id"])

    def test_a_hand_pick_overrides_the_config_and_the_adapter_gets_its_route(self) -> None:
        seen = []

        def register(made):
            rh.register(made)
            made.set("drive_owner", lambda ctx, request: seen.append(request))
        _, composed = self.boot(hooks_of(register))
        answer = composed.control.call("run.start", {"tenant": "t1", "prompt": LIVE,
                                                     "hand_pick": {"engine": "claude_code", "model": "picked-model"}})
        self.assertEqual(answer["code"], "started", answer)
        record = composed.control.registry.get(answer["run"]["run_id"])
        self.assertEqual((record["launch"]["route_decision"]["reason"], record["launch"]["route_decision"]["hand_pick"]),
                         ("hand_pick", {"engine": "claude_code", "model": "picked-model"}))
        self.assertEqual((seen[0]["route"]["model"], seen[0]["route"]["connection"]),
                         ("picked-model", {"kind": "subscription", "id": "stand-in-login"}))
        self.stop(composed.control, answer["run"]["run_id"])

    def test_a_hand_pick_of_an_undetected_engine_is_refused_and_nothing_launches(self) -> None:
        _, composed = self.boot(hooks_of(rh.register))
        answer = composed.control.call("run.start", {"tenant": "t1", "prompt": LIVE,
                                                     "hand_pick": {"engine": "codex", "model": None}})
        self.assertEqual(answer, {"code": "request_invalid", "detail": "hand_pick_engine_without_login:no_login"})
        self.assertEqual(composed.control.registry.list(), [])
        self.assertEqual(self.units.procs, {})

    def test_a_named_engine_with_a_hand_pick_of_another_is_refused_before_a_run_exists(self) -> None:
        _, composed = self.boot(hooks_of(rh.register))
        answer = composed.control.call("run.start", {"tenant": "t1", "prompt": LIVE, "engine": "claude_code",
                                                     "hand_pick": {"engine": "codex", "model": None}})
        self.assertEqual(answer, {"code": "request_invalid", "detail": "engine_and_hand_pick_differ"})
        self.assertEqual(composed.control.registry.list(), [])


if __name__ == "__main__":
    unittest.main()
