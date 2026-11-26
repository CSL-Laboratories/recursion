"""The wall's record is the plugin host's own report on itself: what that means today, held by tests.

The host applies its wall to itself and then probes it from inside the same process; the record (wall.v1) and its
word 'proven' are decided there. A process that is not walled as shipped can earn the same record: these tests build
such a process, a PROBE-SHAPED DECOY, and keep the truth about it in view.

The decoy is probe-shaped by construction, not by a hand list: a child under the shipped wall runs probe_wall
unchanged while every system call the probe makes through the module's own call helper is noted; the parent reads
the shipped filter program (wall_linux._filter) and finds, among those calls, each one the filter answers by its
last row (the answer to every call the allow list does not name), by running the program on the call's own
arguments. A second child then puts up the wall with that last row set to let every call through and with each of
those calls refused by its name from the module's own tables, and runs probe_wall unchanged. However the probe grows,
the decoy refuses what the probe tries, as the shipped filter answers it, and lets through everything else. (The
smaller means, the last row let through and one named call put back by hand, ages as soon as the probe tries one
call more; it is not used here.) A call the probe makes by another way than the module's call helper and that only
the last row refuses would leave the decoy short of proven: the pin below then goes red and says so.

Three tests:
- THE TRUTH TODAY, a pin: both records are proven over the shipped list and equal in every field, those the schema's
  'proven' description names among them (the record holds no pid, time or path, so no field is left out); a call that the probe does not try and that no table of the module and no row of the
  allow list names (setuid to the caller's own user, made by the standard library) goes through under the decoy and
  is refused under the shipped wall.
- THE KNOWN OPEN GAP, marked unittest.expectedFailure: the record tells the decoy from the shipped wall. It does not
  today. A check of the child's status flags from outside (NoNewPrivs, Seccomp, the count of filters) does not end
  this: the decoy shows the same flags. It ends only when the daemon applies the policy itself before the host
  program runs, so that the host can only add to it. Taking the mark off while the gap is open makes the run red; an
  unexpected success with the mark left on makes the run red too.
- THE WORDS: the first sentence a person reads beside the word proven says that the test is the host's own and that
  nothing outside the host has checked it, and the record's schema says so too.

Walls are applied in CHILD processes only, each in a temporary folder of the test's own with a stand-in daemon of the
test's own (run_child of test_wall_linux_family), each child bounded by that helper's timeout.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from recursion_core.plugin_host import wall_linux
from recursion_core.plugin_host.tests.test_wall_linux import SCHEMA, _why_not_here
from recursion_core.plugin_host.tests.test_wall_linux_family import run_child
from recursion_core.plugin_host.tests.test_wall_linux_reach import _run_program

REPORT_CHILD = r'''
import json, os, sys
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
arch = w._arch()
if spec["mode"] == "decoy":
    names = {number: name for name, number in dict(w._NR[arch], **w._NR_MORE[arch]).items()}
    w._RET_UNLISTED = w._RET_ALLOW
    w.DENIED_CALLS = w.DENIED_CALLS + tuple(names[number] for number in spec["lean"] if names[number] not in w.DENIED_CALLS)
tried, real_call = [], w._call
def noting(libc, number, *args):
    tried.append([number] + [int(value) & 0xFFFFFFFFFFFFFFFF for value in args[:6]])
    return real_call(libc, number, *args)
w.apply_wall(spec["paths"], keep_descriptors=(1, 2))
w._call = noting
record = w.probe_wall(spec["canary"], spec["daemon"], write_folder=spec["folder"])
w._call = real_call
try:
    os.setuid(os.geteuid())
    outside = "let_through"
except PermissionError:
    outside = "refused"
except OSError as exc:
    outside = "error %d" % exc.errno
print(json.dumps({"record": record, "tried": tried, "outside": outside, "arch": arch}))
'''

OUTSIDE_CALL = "setuid"  # the call the child makes through os.setuid; checked below to be in no table of the module


def _decided_fields() -> list:
    """Every field of wall.v1 that the description of 'proven' names, and 'proven' itself: read from the schema."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    said = schema["properties"]["proven"]["description"]
    return sorted({name for name in schema["properties"] if name in said} | {"proven"})


def _leaning_on_the_last_row(tried, arch: str, abi: int) -> list:
    """The call numbers among ``tried`` that the shipped filter refuses only by its last row: refused by the shipped
    program, let through by the same program with that row let through, on the call's own arguments."""
    shipped = wall_linux._filter(arch, abi)
    with mock.patch.object(wall_linux, "_RET_UNLISTED", wall_linux._RET_ALLOW):
        opened = wall_linux._filter(arch, abi)
    leaning = set()
    for number, *args in tried:
        args = (list(args) + [0] * 6)[:6]
        if (_run_program(shipped, number, args=args, arch=arch) == "eperm"
                and _run_program(opened, number, args=args, arch=arch) == "allow"):
            leaning.add(number)
    return sorted(leaning)


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class TheRecordIsTheHostsOwnReportTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # the children run here, so that a child that fails is an error of the class, never an expected failure
        cls.real, _ = run_child(REPORT_CHILD, "real", lean=[])
        cls.arch = cls.real["arch"]
        cls.lean = _leaning_on_the_last_row(cls.real["tried"], cls.arch, cls.real["record"]["landlock_abi"])
        cls.decoy, _ = run_child(REPORT_CHILD, "decoy", lean=cls.lean)
        cls.decided = _decided_fields()

    def test_today_a_probe_shaped_decoy_earns_the_record_of_the_shipped_wall(self):
        """A PIN of today's truth (green): the decoy's record equals the shipped wall's in every decided field, while
        a call neither the probe nor the tables name goes through under the decoy only."""
        real, decoy = self.real["record"], self.decoy["record"]
        for name, record in (("shipped wall", real), ("decoy", decoy)):
            with self.subTest(child=name):
                self.assertIs(record["proven"], True, wall_linux.why_not_proven(record))
                self.assertEqual(record["proven_over"], "shipped_list")
        self.assertGreater(len(self.decided), 10, "the description of 'proven' names the fields it decides")
        self.assertLessEqual(set(self.decided), set(real), "every field 'proven' names is in the record compared")
        # every field is compared: the record holds no pid, time or path of its own, so none is left out
        self.assertEqual(decoy, real, "if this differs, the probe makes a call the decoy does not refuse: see the module text")
        named = set(wall_linux._NR[self.arch]) | set(wall_linux._NR_MORE[self.arch])
        listed = {row["call"] for row in wall_linux.load_allow_list()["rows"]}
        self.assertNotIn(OUTSIDE_CALL, named, "the outside call is named by no table of the module")
        self.assertNotIn(OUTSIDE_CALL, listed, "the outside call is named by no row of the allow list")
        self.assertEqual(self.real["outside"], "refused", "the shipped wall refuses a call the list does not name")
        self.assertEqual(self.decoy["outside"], "let_through", "the decoy lets it through and its record still says proven")

    @unittest.expectedFailure
    def test_the_record_tells_a_probe_shaped_decoy_from_the_shipped_wall(self):
        """OPEN TODAY, so marked expectedFailure. The record is the host's own report: a process that is not walled as
        shipped earns it, so the record cannot tell the decoy from the shipped wall. A check of the child's status
        flags from outside does not end this: the decoy shows NoNewPrivs 1, Seccomp 2 and one filter, as the shipped
        wall does. It ends only when the daemon applies the policy itself before the host program runs; then this
        test passes, and the mark comes off in the same change."""
        real, decoy = self.real["record"], self.decoy["record"]
        tells = decoy.get("proven") is not True or decoy != real
        self.assertTrue(tells, "the decoy's record says proven and equals the shipped wall's record")


class TheWordsSayWhoseTestItIsTests(unittest.TestCase):

    def proven_lines(self) -> list:
        shared = sorted(operation for row in wall_linux.load_allow_list()["rows"] if row["call"] == "futex"
                        for operation in row.get("operations", ()) if not operation & 128)
        return wall_linux.what_proven_does_not_cover({"proven": True, "shared_waits_allowed": shared,
                                                      "limits_of_proven": wall_linux._limits_of_proven(shared)})

    def test_the_first_words_beside_proven_say_the_test_is_the_hosts_own(self):
        self.assertTrue(self.proven_lines()[0].startswith(wall_linux.WORDS["reported_by_host"]))

    def test_the_words_say_nothing_outside_the_host_has_checked_it(self):
        words = wall_linux.WORDS["reported_by_host"]
        self.assertIn("own test", words)
        self.assertIn("has not checked", words)
        self.assertIn("could make the same report", words)

    def test_the_words_say_what_the_person_can_rely_on(self):
        self.assertIn("You can rely on it as far as you trust the plugin host program", wall_linux.WORDS["reported_by_host"])

    def test_the_intro_does_not_say_the_test_shows_it_to_the_person(self):
        intro = wall_linux.WORDS["limits_intro"]
        self.assertNotIn("shut in as the test shows", intro)
        self.assertIn("The host's test shows that a plugin cannot write your files", intro)

    def test_the_schema_says_the_record_is_the_hosts_and_the_daemon_does_not_check_it(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        for where, text in (("record", schema["description"]), ("proven", schema["properties"]["proven"]["description"])):
            with self.subTest(where=where):
                self.assertIn("the daemon does not check it", text)
                self.assertIn("weaker filter", text)


if __name__ == "__main__":
    unittest.main()
