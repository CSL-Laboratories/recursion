"""The setup registrant: ``doctor`` through ``hooks.compose()``.

Today, with no handler, that call answers ``unsupported`` / ``not_implemented``.
The report's shape is the one in ``h15-doctor.json``. ``register`` opens nothing.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from recursion_core.control.hooks import Hooks, Principal, compose
from recursion_core.setup.tests.test_doctor import FIXTURE_PATH, _OPENED, _validate, _watch_opens

_SAMPLE_KEYS = set(json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["answer"]["report"]["checks"][0])
_REPORT_KEYS = set(json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["answer"]["report"])


def _owner():
    return Principal("owner", "local")


def _dispatch(hooks, params):
    return hooks.methods.dispatch("doctor", params, _owner(), lambda *_args, **_kwargs: None)


class DoctorMethodTests(unittest.TestCase):
    def test_compose_answers_ok_with_a_report_of_the_fixture_shape(self):
        runtime = tempfile.TemporaryDirectory()
        self.addCleanup(runtime.cleanup)
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime.name}):
            answer = _dispatch(compose(), {})
        # Today this is unsupported / not_implemented: no handler is registered.
        self.assertEqual((answer.get("code"), answer.get("detail")), ("ok", None), answer)
        self.assertEqual(set(answer), {"code", "report"})
        report = answer["report"]
        self.assertEqual(set(report), _REPORT_KEYS)
        _validate(report)
        self.assertTrue(report["checks"])
        for check in report["checks"]:
            self.assertEqual(set(check), _SAMPLE_KEYS)
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime.name}):
            again = _dispatch(compose(), {})
        self.assertEqual(again.get("code"), "ok")
        self.assertIsNot(again["report"], report)
        _validate(again["report"])

    def test_a_stray_param_is_refused_and_an_interruption_propagates(self):
        hooks = compose()
        refused = _dispatch(hooks, {"extra": 1})
        self.assertEqual(refused, {"code": "request_invalid", "detail": "params"})
        self.assertEqual(_dispatch(hooks, []), {"code": "request_invalid", "detail": "params"})
        import recursion_core.setup.doctor as doctor
        with mock.patch.object(doctor, "run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _dispatch(hooks, {})
        with mock.patch.object(doctor, "run", side_effect=SystemExit(3)):
            with self.assertRaises(SystemExit):
                _dispatch(hooks, {})
        with mock.patch.object(doctor, "run", side_effect=RuntimeError("probe")):
            answer = _dispatch(hooks, {})
        self.assertEqual(answer["code"], "unavailable")
        self.assertIn("RuntimeError", answer["detail"])

    def test_register_adds_only_the_doctor_handler_and_opens_nothing(self):
        import recursion_core.setup.hooks as setup_hooks
        import recursion_core.setup.doctor as doctor
        _watch_opens()
        start = len(_OPENED)
        hooks = Hooks()
        with mock.patch.object(doctor, "run") as run:
            setup_hooks.register(hooks)
            self.assertEqual(_OPENED[start:], [])
            run.assert_not_called()
        self.assertEqual(hooks.methods.registered(), ("doctor",))
        hooks.freeze()
        # A second register on a new table is its own handler, and still opens nothing.
        other = Hooks()
        start = len(_OPENED)
        setup_hooks.register(other)
        self.assertEqual(_OPENED[start:], [])
        self.assertEqual(other.methods.registered(), ("doctor",))


if __name__ == "__main__":
    unittest.main()
