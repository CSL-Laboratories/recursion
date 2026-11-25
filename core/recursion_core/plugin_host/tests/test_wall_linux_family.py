"""What was obtained before the wall, or handed in after it, and whether the record still says proven.

Walls are applied in CHILD processes only, each in a temporary folder of the test's own; every file a child may
touch is one the test made for the purpose, every connection is a socket pair of the child's own, and the stand-in
daemon is a sleeping process of the test's own, ended when the test ends.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from recursion_core.plugin_host import wall_linux
from recursion_core.plugin_host.tests.test_wall_linux import CORE, _interpreter, _why_not_here
from recursion_core.plugin_host.tests.test_wall_linux_proof import _schema_errors

CHILD_TIMEOUT_S = 120  # a bound on one child process of this test; test-side only, not a product figure
PLANTED_TEXT = b"a file of the test's own, planted for the hand-in\n"

HANDIN_CHILD = r'''
import json, os, socket, sys
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
out = {}
mode = spec["mode"]
# a connection held from before the wall; a descriptor put in it before the wall is in flight, in no list of
# this process, until it is received after the wall, as one handed in by an unwalled peer would be
held, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
if mode == "file":
    fd = os.open(spec["planted"], os.O_RDWR)
    socket.send_fds(peer, [b"x"], [fd])
    os.close(fd)
elif mode == "pipe":
    read_end, write_end = os.pipe()
    socket.send_fds(peer, [b"x"], [read_end])
    os.close(read_end)
    os.close(write_end)
w.apply_wall(spec["paths"])
if mode != "none":
    _, fds, _, _ = socket.recv_fds(held, 1, 1)
    out["received"] = fds[0]
record = w.probe_wall(spec["canary"], spec["daemon"], write_folder=spec["folder"])
out.update(record=record, words=w.why_not_proven(record))
if mode == "file":
    os.lseek(out["received"], 0, os.SEEK_SET)
    os.write(out["received"], b"WRITTEN")  # through the handed-in descriptor, after the wall
print(json.dumps(out))
'''


def run_child(code: str, mode: str, **extra) -> tuple:
    """Run ``code`` walled in a child with a write folder, a canary and a stand-in daemon of the test's own; the
    child's last line of output (JSON) and the first bytes of the planted file afterwards."""
    with tempfile.TemporaryDirectory() as top:
        canary, folder, planted = os.path.join(top, "canary"), os.path.join(top, "w"), os.path.join(top, "planted")
        with open(canary, "w", encoding="utf-8") as handle:
            handle.write("the host's test file, stand-in")
        with open(planted, "wb") as handle:
            handle.write(PLANTED_TEXT)
        os.mkdir(folder)
        wall_linux.prepare_write_folder(folder)
        daemon = subprocess.Popen(_interpreter() + ["-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, cwd=top, env={"HOME": top, "TMPDIR": top})
        try:
            spec = dict(extra, core=str(CORE), mode=mode, canary=canary, daemon=daemon.pid, folder=folder,
                        planted=planted, top=top, paths=sorted({sys.base_prefix, sys.prefix}))
            child = subprocess.run(_interpreter() + ["-c", code, json.dumps(spec)], capture_output=True, text=True,
                                   stdin=subprocess.DEVNULL, timeout=CHILD_TIMEOUT_S, cwd=top,
                                   env={"HOME": top, "TMPDIR": top})
        finally:
            daemon.kill()
            daemon.wait()
        with open(planted, "rb") as handle:
            head = handle.read(7)
    lines = child.stdout.strip().splitlines()
    if child.returncode != 0 or not lines:
        raise AssertionError(f"the child ({mode}) ended {child.returncode}: {child.stderr[-1500:]}")
    return json.loads(lines[-1]), head


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class DescriptorHandedInAfterTheWallTests(unittest.TestCase):
    """A descriptor received between apply_wall and probe_wall is named, and the record is not proven."""

    def test_a_file_handed_in_after_the_wall_is_named_and_the_record_is_not_proven(self):
        result, head = run_child(HANDIN_CHILD, "file")
        record, number = result["record"], result["received"]
        self.assertIs(record["proven"], False, "a descriptor handed in after the wall is not covered by proven")
        self.assertIn({"descriptor": number, "kind": "regular_file"}, record["descriptors_unaccounted"] or [])
        self.assertIn({"descriptor": number, "kind": "regular_file"}, record["descriptors_open_for_write"] or [])
        self.assertIs(record["proven"], False)
        self.assertIn(wall_linux.WORDS["descriptor_after_wall"].format(kind=wall_linux.WORDS["regular_file"]),
                      result["words"])
        self.assertEqual(head, b"WRITTEN", "what the words say is true: a write through it still reaches the file")
        self.assertEqual(_schema_errors(record), [])

    def test_a_descriptor_handed_in_that_is_not_open_for_writing_is_named_all_the_same(self):
        result, _ = run_child(HANDIN_CHILD, "pipe")
        record = result["record"]
        self.assertIs(record["proven"], False, "a descriptor handed in after the wall is not covered by proven")
        self.assertEqual(record["descriptors_unaccounted"], [{"descriptor": result["received"], "kind": "pipe"}])
        self.assertEqual(record["descriptors_open_for_write"], [])
        self.assertIs(record["proven"], False)
        self.assertEqual(_schema_errors(record), [])

    def test_nothing_handed_in_is_proven(self):
        result, head = run_child(HANDIN_CHILD, "none")
        record = result["record"]
        self.assertEqual(record["descriptors_unaccounted"], [])
        self.assertIs(record["proven"], True, result["words"])
        self.assertEqual(head, PLANTED_TEXT[:7])
        self.assertEqual(_schema_errors(record), [])


@unittest.skipUnless(sys.platform.startswith("linux"), "the probe is Linux-only")
class NoWallInItsOwnProcessTests(unittest.TestCase):
    """A record made where apply_wall did not run in the process is never proven, even when every part of it holds
    (a wall from outside the process, say): its descriptors cannot be accounted against the wall's list."""

    def test_every_check_refused_without_a_wall_of_its_own_is_not_proven(self):
        refused = dict.fromkeys(wall_linux.WRITE_KINDS, "refused_by_landlock")
        with mock.patch.dict(wall_linux._STATE, {"seen_at_wall": None, "failed": None}), \
                mock.patch.object(wall_linux, "_refused", lambda check: True), \
                mock.patch.object(wall_linux, "_write_outcomes", lambda *a: dict(refused)), \
                mock.patch.object(wall_linux, "_landlock_abi", lambda libc: 3), \
                mock.patch.object(wall_linux, "_threads_now", lambda: 1), \
                mock.patch.object(wall_linux, "_descriptors_open_for_write", lambda: []), \
                mock.patch.object(wall_linux, "_descriptors_now", lambda: [], create=True), \
                mock.patch.object(wall_linux, "_maps_open_for_write", lambda: []):
            record = wall_linux.probe_wall("/nonexistent-canary", os.getppid())
        self.assertEqual(record["descriptors_seen_by"], "scan_by_number")
        self.assertIs(record["proven"], False)
        self.assertIn(wall_linux.WORDS.get("no_wall_here", "no_wall_here"), wall_linux.why_not_proven(record))
        self.assertEqual(_schema_errors(record), [])


if __name__ == "__main__":
    unittest.main()
