"""What a relay key may reach (lane REMOTE): rights in the core's one vocabulary and the relay's method table.

Pure: no socket, no file but the published method table this tree carries.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from recursion_core.control import credentials
from recursion_core.control.methods import METHODS
from recursion_core.remote import pairing, rights
from recursion_core.remote.pairing import PairingRefused, device_scopes
from recursion_core.remote.rights import NEEDS_ALL, RightsRefused, key_rights, needs

PUBLISHED_TABLE = Path(__file__).resolve().parents[4] / "schemas" / "control" / "v1" / "methods.table.v1.json"
READ_METHODS = ("run.list", "run.status", "events.read", "session.list", "approval.list", "doctor")
REFUSED_TO_A_DEVICE = {"config.set": NEEDS_ALL, "plugins.install": NEEDS_ALL, "daemon.shutdown": NEEDS_ALL,
                       "credentials.read": NEEDS_ALL, "run.start": "run", "session.start": "run",
                       "session.stop": "run", "approval.answer": "approve", "workspace.publish": "manage"}


def control(method) -> dict:
    return {"op": "control", "method": method, "params": {}}


class VocabularyTests(unittest.TestCase):
    def test_the_relays_rights_are_a_subset_of_the_cores(self) -> None:
        self.assertEqual(rights.RIGHTS, credentials.SCOPES)
        self.assertLessEqual(set(rights.METHOD_RIGHTS.values()), set(credentials.SCOPES))
        for word in pairing.DEVICE_SCOPES + rights.FULL_RIGHTS:
            with self.subTest(word=word):
                self.assertTrue(word in credentials.SCOPES or word in METHODS, word)

    def test_the_relays_table_is_the_published_table_row_for_row(self) -> None:
        with open(PUBLISHED_TABLE, encoding="utf-8") as source:
            table = json.load(source)
        self.assertEqual(rights.TABLE_VERSION, table["table_version"])
        self.assertEqual(list(rights.METHOD_RIGHTS.items()), [(row["name"], row["scope"]) for row in table["methods"]])
        self.assertEqual(dict(rights.METHOD_RIGHTS), {name: row.scope for name, row in METHODS.items()})

    def test_the_default_of_a_device_is_watch_and_stop_and_the_word_stop_is_gone(self) -> None:
        self.assertEqual(pairing.DEVICE_SCOPES, ("read", "run.stop"))
        for words in (["stop"], ["read", "stop"]):
            with self.subTest(words=words):
                with self.assertRaises(PairingRefused) as caught:
                    device_scopes(words)
                self.assertEqual(caught.exception.code, "scope_invalid")
                with self.assertRaises(RightsRefused):
                    key_rights(words)


class KeyRightsTests(unittest.TestCase):
    def test_rights_come_back_in_one_order(self) -> None:
        self.assertEqual(key_rights(["run.stop", "manage", "read"]), ("read", "manage", "run.stop"))
        self.assertEqual(key_rights(["run.list", "run.start"]), ("run.start", "run.list"))

    def test_an_empty_repeated_unknown_or_single_string_list_is_refused(self) -> None:
        for words in ([], ["read", "read"], ["Read"], ["admin"], "read", None, [7], [["read"]], ["config.set"]):
            with self.subTest(words=words), self.assertRaises(RightsRefused):
                key_rights(words)  # type: ignore[arg-type]


class DeviceRightsTests(unittest.TestCase):
    def test_a_device_may_hold_read_its_single_read_methods_and_run_stop(self) -> None:
        self.assertEqual(device_scopes(["run.stop", "read"]), ("read", "run.stop"))
        self.assertEqual(device_scopes(["run.list"]), ("run.list",))
        self.assertEqual(device_scopes(["run.stop"]), ("run.stop",))

    def test_run_approve_and_manage_as_single_methods_are_refused_without_a_passkey(self) -> None:
        for method in ("run.start", "session.stop", "session.interrupt", "approval.answer", "workspace.publish"):
            with self.subTest(method=method):
                with self.assertRaises(PairingRefused) as caught:
                    device_scopes(["read", method])
                self.assertEqual(caught.exception.code, "scope_refused")

    def test_a_method_the_table_does_not_have_is_unknown(self) -> None:
        for method in ("config.set", "daemon.shutdown", "run.stop2"):
            with self.subTest(method=method):
                with self.assertRaises(PairingRefused) as caught:
                    device_scopes([method])
                self.assertEqual(caught.exception.code, "scope_invalid")


class NeedsTests(unittest.TestCase):
    def test_the_default_device_reaches_the_read_methods_ping_health_and_stop(self) -> None:
        for frame in [control(method) for method in READ_METHODS] + [control("run.stop"), {"op": "ping"},
                                                                     {"op": "health"}]:
            with self.subTest(frame=frame):
                self.assertIsNone(needs(pairing.DEVICE_SCOPES, frame))

    def test_the_default_device_is_refused_everything_else_with_the_right_it_needs(self) -> None:
        for method, right in REFUSED_TO_A_DEVICE.items():
            with self.subTest(method=method):
                self.assertEqual(needs(pairing.DEVICE_SCOPES, control(method)), (method, right))

    def test_a_frame_that_names_no_known_method_needs_all_four(self) -> None:
        cases = ({"op": "x"}, {"method": "run.list"}, {"op": "control", "method": 7}, {"op": "control"},
                 {"op": "relay_hello"}, {"op": 7})
        for frame in cases:
            with self.subTest(frame=frame):
                missing = needs(("read", "run", "approve"), frame)
                self.assertIsNotNone(missing)
                self.assertEqual(missing[1], NEEDS_ALL)

    def test_all_four_rights_reach_everything_a_name_the_table_lacks_included(self) -> None:
        for frame in (control("config.set"), {"op": "x"}, control("workspace.publish"), control(7)):
            with self.subTest(frame=frame):
                self.assertIsNone(needs(rights.FULL_RIGHTS, frame))

    def test_a_single_method_reaches_that_method_only(self) -> None:
        self.assertIsNone(needs(("run.list",), control("run.list")))
        self.assertEqual(needs(("run.list",), control("run.status")), ("run.status", "read"))
        self.assertEqual(needs(("run.list",), {"op": "ping"}), ("ping", "read"))

    def test_a_whole_right_reaches_every_method_of_its_row(self) -> None:
        for method, right in rights.METHOD_RIGHTS.items():
            with self.subTest(method=method):
                self.assertIsNone(needs((right,), control(method)))
                others = tuple(word for word in rights.RIGHTS if word != right)
                self.assertEqual(needs(others, control(method)), (method, right))

    def test_a_method_name_that_is_a_str_subclass_names_no_method(self) -> None:
        class Sly(str):
            def __eq__(self, other):
                return True

            __hash__ = str.__hash__

        self.assertEqual(needs(pairing.DEVICE_SCOPES, control(Sly("run.list")))[1], NEEDS_ALL)


if __name__ == "__main__":
    unittest.main()
