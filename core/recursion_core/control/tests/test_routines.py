"""The routine record a person writes, in the core's store (control/routines.py, routine.record.v1.json)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from recursion_core.control import routines as r
from recursion_core.plugin_security import host_channel as hc

REPO = Path(__file__).resolve().parents[4]
SCHEMA = json.loads((REPO / "schemas/control/v1/routine.record.v1.json").read_text(encoding="utf-8"))
COMMON = json.loads((REPO / "schemas/control/v1/common.v1.json").read_text(encoding="utf-8"))
OWNER = {"kind": "owner", "id": "owner"}
DEVICE = {"kind": "device", "id": "page"}
THREADS = 8          # the race below starts this many threads, no more
JOIN_S = 30          # and waits this long for each


def a_record(**changes):
    record = {"record_id": "morning-digest", "plugin": "demo.schedule", "tenant": "default",
              "engine": "claude_code", "prompt": "Summarise what changed overnight.", "lease_seconds": 3600}
    record.update(changes)
    return record


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "routines.sqlite3")
        self.now = [1795464000.0]
        self.store = self.open()

    def open(self):
        return r.RoutineStore(self.path, wall=lambda: self.now[0])

    def assertRefused(self, answer, code):
        self.assertEqual(answer["code"], code, answer)
        self.assertEqual(answer["text"], r.TEXT[code])


class TheRecordTest(StoreCase):
    def test_the_person_writes_a_record_and_the_core_stamps_it(self):
        answer = self.store.admit(OWNER, a_record())
        self.assertEqual(answer["code"], "ok")
        stored = answer["routine"]
        self.assertEqual((stored["kind"], stored["revision"], stored["written_by"]), ("routine.v1", 1, OWNER))
        self.assertEqual(stored["written_at"], "2026-11-23T20:00:00.000Z")
        self.assertEqual(self.store.get("morning-digest"), stored)
        self.assertEqual(self.store.list(), {"code": "ok", "routines": [stored]})

    def test_a_paired_device_changes_it_and_the_revision_rises(self):
        self.store.admit(OWNER, a_record())
        answer = self.store.change(DEVICE, "morning-digest", {"prompt": "Only the failures."}, revision=1)
        self.assertEqual(answer["code"], "ok")
        self.assertEqual((answer["routine"]["revision"], answer["routine"]["prompt"]), (2, "Only the failures."))
        self.assertEqual(answer["routine"]["written_by"], DEVICE)
        self.assertEqual(self.store.admit(OWNER, a_record())["routine"]["revision"], 3)

    def test_the_list_shows_which_plugin_may_ask_for_which_record(self):
        self.store.admit(OWNER, a_record())
        self.store.admit(OWNER, a_record(record_id="evening", plugin="other.schedule"))
        seen = {row["record_id"]: row["plugin"] for row in self.store.list()["routines"]}
        self.assertEqual(seen, {"evening": "other.schedule", "morning-digest": "demo.schedule"})

    def test_a_record_survives_a_restart_of_the_store(self):
        stored = self.store.admit(OWNER, a_record())["routine"]
        self.assertEqual(self.open().get("morning-digest"), stored)

    def test_removing_a_record_ends_it_at_once(self):
        self.store.admit(OWNER, a_record())
        self.assertEqual(self.store.remove(OWNER, "morning-digest"), {"code": "ok", "removed": "morning-digest"})
        self.assertIsNone(self.store.get("morning-digest"))
        self.assertIsNone(self.open().get("morning-digest"))
        self.assertRefused(self.store.remove(OWNER, "morning-digest"), "routine_unknown")


class OnlyThePersonWritesTest(StoreCase):
    OTHER_KINDS = [kind for kind in COMMON["$defs"]["principal"]["properties"]["kind"]["enum"]
                   if kind not in ("owner", "device")] + ["colour"]

    def test_a_plugin_can_neither_create_change_nor_remove_a_record(self):
        plugin = {"kind": "plugin", "id": "demo.schedule"}
        self.assertRefused(self.store.admit(plugin, a_record()), "writer_not_allowed")
        self.assertEqual(self.store.list()["routines"], [])
        stored = self.store.admit(OWNER, a_record())["routine"]
        self.assertRefused(self.store.change(plugin, "morning-digest", {"prompt": "More."}, revision=1),
                           "writer_not_allowed")
        self.assertRefused(self.store.admit(plugin, a_record(prompt="More.")), "writer_not_allowed")
        self.assertRefused(self.store.remove(plugin, "morning-digest"), "writer_not_allowed")
        self.assertEqual(self.store.get("morning-digest"), stored)

    def test_every_principal_kind_but_the_persons_is_refused_and_nothing_is_stored(self):
        for kind in self.OTHER_KINDS:
            with self.subTest(kind=kind):
                self.assertRefused(self.store.admit({"kind": kind, "id": "x"}, a_record()), "writer_not_allowed")
        for writer in (None, "owner", {"kind": "owner"}, {"kind": "owner", "id": "a b"},
                       {"kind": "owner", "id": "owner", "colour": "blue"}):
            with self.subTest(writer=writer):
                self.assertRefused(self.store.admit(writer, a_record()), "writer_not_allowed")
        self.assertEqual(self.store.list()["routines"], [])

    def test_the_writer_kinds_are_the_published_ones(self):
        writer = SCHEMA["$defs"]["writer"]["properties"]["kind"]["enum"]
        self.assertEqual(tuple(writer), r.WRITER_KINDS)
        principal = COMMON["$defs"]["principal"]["properties"]["kind"]["enum"]
        self.assertLessEqual(set(writer), set(principal))


class TheFormTest(StoreCase):
    def test_an_unknown_field_is_refused_and_nothing_is_stored(self):
        self.assertRefused(self.store.admit(OWNER, a_record(colour="blue")), "routine_invalid")
        self.assertEqual(self.store.admit(OWNER, a_record(colour="blue"))["detail"], "colour")
        self.store.admit(OWNER, a_record())
        answer = self.store.change(OWNER, "morning-digest", {"colour": "blue"}, revision=1)
        self.assertRefused(answer, "routine_invalid")
        self.assertEqual(self.store.get("morning-digest")["revision"], 1)

    def test_each_field_is_held_to_its_form(self):
        cases = {"record_id": ["a/b", "", 7], "plugin": ["host", "Demo", 7], "tenant": ["", 7],
                 "engine": ["", None], "prompt": ["", "\ud800", 7]}
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    answer = self.store.admit(OWNER, a_record(**{field: value}))
                    self.assertRefused(answer, "routine_invalid")
                    self.assertEqual(answer["detail"], field)
        record = a_record()
        del record["prompt"]
        self.assertEqual(self.store.admit(OWNER, record)["detail"], "prompt")
        self.assertEqual(self.store.list()["routines"], [])

    def test_a_change_names_the_revision_it_read(self):
        self.store.admit(OWNER, a_record())
        self.store.change(OWNER, "morning-digest", {"prompt": "Two."}, revision=1)
        self.assertRefused(self.store.change(OWNER, "morning-digest", {"prompt": "Three."}, revision=1), "conflict")
        self.assertEqual(self.store.get("morning-digest")["prompt"], "Two.")
        self.assertRefused(self.store.change(OWNER, "evening", {"prompt": "Three."}, revision=1), "routine_unknown")

    def test_of_many_changes_on_one_revision_exactly_one_is_taken(self):
        self.store.admit(OWNER, a_record())
        answers, start = [], threading.Barrier(THREADS)

        def change(n):
            start.wait(JOIN_S)
            answers.append(self.store.change(OWNER, "morning-digest", {"prompt": f"From {n}."}, revision=1)["code"])

        threads = [threading.Thread(target=change, args=(n,)) for n in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(JOIN_S)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(answers), ["conflict"] * (THREADS - 1) + ["ok"])
        self.assertEqual(self.store.get("morning-digest")["revision"], 2)

    def test_the_stored_record_has_exactly_the_published_fields(self):
        stored = self.store.admit(OWNER, a_record())["routine"]
        self.assertEqual(set(stored), set(SCHEMA["properties"]))
        self.assertEqual(set(SCHEMA["required"]), set(SCHEMA["properties"]))
        self.assertEqual(SCHEMA["properties"]["kind"]["const"], r.RECORD_KIND)
        self.assertEqual(hc.RUN_REQUEST_RECORD_KINDS, (r.RECORD_KIND,))

    def test_every_refusal_carries_its_sentence_from_the_one_table(self):
        for code, text in r.TEXT.items():
            with self.subTest(code=code):
                self.assertTrue(text.endswith(".") and len(text.split(". ")) >= 2, text)


if __name__ == "__main__":
    unittest.main()
