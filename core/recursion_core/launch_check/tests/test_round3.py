"""Lane LAUNCH-CHECK, #424 round 3: DeepSeek r3 MEDIUM F1 at 9d0cf4f2.

``authenticate`` reads each grant field before the guard that turns a bad field into
``grant_forged``. A required field that is absent — deleted on the object, or missing from
a grant restored across the process boundary — left as ``AttributeError`` at
``authenticate``, ``verify`` and ``launch_plan``. It is now ``grant_forged``, with the
engine name when that field is still text and ``?`` when it is not.

Run from the repository root:
``PYTHONPATH=core python3 -B -m unittest recursion_core.launch_check.tests.test_round3``.
"""

from __future__ import annotations

import pickle
import threading
import unittest
from unittest import mock

from recursion_core.launch_check import (
    FLOORS,
    REFUSAL_CODES,
    LaunchRefused,
    ProbeResult,
    authenticate,
    grant,
    launch_plan,
    verify,
)
from recursion_core.launch_check.grant import Grant
from recursion_core.launch_check.tests.fixtures import Factory

F: Factory


def setUpModule() -> None:
    global F
    F = Factory()


def tearDownModule() -> None:
    F.cleanup()


class IterRaise(list):
    """A list whose iteration raises, so encoding the field is not plain data."""

    def __iter__(self):
        raise RuntimeError("injected iteration failure")


class AbsentFieldTests(unittest.TestCase):
    def refused(self, fn, *args) -> LaunchRefused:
        with self.assertRaises(LaunchRefused) as cm:
            fn(*args)
        self.assertEqual("grant_forged", cm.exception.code, cm.exception)
        self.assertIn(cm.exception.code, REFUSAL_CODES)
        self.assertIn("a field is missing", cm.exception.detail)
        self.assertNotIn("MAC missing", cm.exception.detail)
        return cm.exception

    def claude_grant(self) -> Grant:
        return grant(
            "claude_code",
            F.make("claude", "2.1.318 (Claude Code)"),
            probe=lambda spec, realpath, timeout: ProbeResult("2.1.318 (Claude Code)", 0, 0),
        )

    def without(self, field: str) -> Grant:
        g = self.claude_grant()
        object.__delattr__(g, field)
        return g

    def restored_without(self, field: str) -> Grant:
        """A grant unpickled in this process whose record no longer holds ``field``."""
        g = self.claude_grant()
        state = dict(g.__dict__)
        del state[field]
        crafted = pickle.loads(pickle.dumps(g))
        crafted.__dict__.clear()
        crafted.__dict__.update(state)
        self.assertIs(type(crafted), Grant)
        self.assertNotIn(field, crafted.__dict__)
        return crafted

    def test_absent_required_field_is_grant_forged_at_every_entry(self):
        # A field that is present but not plain data stays on the other sentence, so the
        # absent-field refusal is not the one that already covered encoding.
        hostile = self.claude_grant()
        object.__setattr__(hostile, "path", IterRaise(["x"]))
        with self.assertRaises(LaunchRefused) as cm:
            authenticate(hostile)
        self.assertEqual("grant_forged", cm.exception.code)
        self.assertIn("not plain data", cm.exception.detail)

        for field, engine in (("path", "claude_code"), ("sha256", "claude_code"), ("engine", "?")):
            bad = self.without(field)
            for fn, args in (
                (authenticate, (bad,)),
                (verify, (bad,)),
                (launch_plan, (bad, ["claude"])),
            ):
                with self.subTest(field=field, fn=fn.__name__):
                    e = self.refused(fn, *args)
                    self.assertEqual(engine, e.engine)

        good = self.claude_grant()
        self.assertIs(FLOORS["claude_code"], authenticate(good))
        verify(good)

        # An interrupt during the read is not turned into a refusal.
        original = getattr

        def interrupt_on_sha256(obj, name, *args):
            if name == "sha256":
                raise KeyboardInterrupt("during field read")
            return original(obj, name, *args)

        with mock.patch("recursion_core.launch_check.grant.getattr", interrupt_on_sha256):
            with self.assertRaises(KeyboardInterrupt):
                authenticate(good)

    def test_grant_restored_without_a_field_is_grant_forged(self):
        for field, engine in (("path", "claude_code"), ("version_raw", "claude_code")):
            bad = self.restored_without(field)
            for fn, args in (
                (authenticate, (bad,)),
                (verify, (bad,)),
                (launch_plan, (bad, ["claude"], {})),
            ):
                with self.subTest(field=field, fn=fn.__name__):
                    e = self.refused(fn, *args)
                    self.assertEqual(engine, e.engine)

    def test_a_second_caller_is_not_affected_by_an_absent_field(self):
        good = self.claude_grant()
        bad = self.without("realpath")
        seen: list[BaseException | str] = []

        def other() -> None:
            try:
                authenticate(good)
                verify(good)
            except BaseException as exc:  # noqa: BLE001 - the test records whatever the other caller sees
                seen.append(exc)
            else:
                seen.append("ok")

        thread = threading.Thread(target=other)
        thread.start()
        e = self.refused(authenticate, bad)
        self.assertEqual("claude_code", e.engine)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["ok"], seen)


if __name__ == "__main__":
    unittest.main()
