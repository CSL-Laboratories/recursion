"""A wait or wake that is not private reaches no process outside the wall: futex is let through in its private forms
only, each named on the allow list with its reason.

Walls are applied in CHILD processes only, each in a temporary folder of the test's own; the helper a child starts
before its wall is a grandchild that waits a few seconds at most on a page of a file of the install, then ends.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from recursion_core.plugin_host import wall_linux
from recursion_core.plugin_host.tests.test_wall_linux import _why_not_here
from recursion_core.plugin_host.tests.test_wall_linux_family import PLANTED_TEXT, run_child
from recursion_core.plugin_host.tests.test_wall_linux_proof import _run_program

HELPER_WAIT_S = 3  # how long the test's helper waits to be woken; test-side only, not a product figure
SHARED = {"FUTEX_WAIT": 0, "FUTEX_WAKE": 1, "FUTEX_WAKE_OP": 5, "FUTEX_WAIT_BITSET": 9, "FUTEX_WAKE_BITSET": 10,
          "FUTEX_LOCK_PI": 6, "FUTEX_WAIT|FUTEX_CLOCK_REALTIME": 256}

FUTEX_CHILD = r'''
import ctypes, json, mmap, os, sys, time
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long)
FUTEX = {"x86_64": 202, "aarch64": 98}[w._arch()]  # the kernel's numbers

def page_of_the_file():  # a page of a file of the install, mapped shared and read only
    fd = os.open(os.__file__, os.O_RDONLY)
    try:
        return libc.mmap(None, mmap.PAGESIZE, mmap.PROT_READ, mmap.MAP_SHARED, fd, 0)
    finally:
        os.close(fd)

ready_r, ready_w = os.pipe()
first = os.fork()
if first == 0:
    if os.fork() == 0:  # the helper: started before the wall, and no longer a child of the walled process
        os.close(ready_r)
        address = page_of_the_file()
        value = ctypes.c_int.from_address(address).value
        limit = (ctypes.c_long * 2)(spec["wait"], 0)
        os.write(ready_w, b"w")
        ctypes.set_errno(0)
        woken = libc.syscall(ctypes.c_long(FUTEX), ctypes.c_void_p(address), ctypes.c_long(0), ctypes.c_long(value),
                             ctypes.byref(limit), None, ctypes.c_long(0)) == 0
        if woken:
            with open(spec["planted"], "r+b") as handle:  # the test's own file, outside the wall
                handle.write(b"WOKEN!!")
        os._exit(0)
    os._exit(0)
os.waitpid(first, 0)
os.close(ready_w)
os.read(ready_r, 1)
os.close(ready_r)
time.sleep(0.3)
w.apply_wall(spec["paths"], keep_descriptors=(1, 2))
record = w.probe_wall(spec["canary"], spec["daemon"], write_folder=spec["folder"])
address = page_of_the_file()  # mapped after the wall: no list read at the wall can see it
ctypes.set_errno(0)
result = libc.syscall(ctypes.c_long(FUTEX), ctypes.c_void_p(address), ctypes.c_long(1), ctypes.c_long(1), None, None,
                      ctypes.c_long(0))
print(json.dumps({"wake": [result, ctypes.get_errno() if result < 0 else 0], "record": record,
                  "words": w.why_not_proven(record)}))
'''


class FutexOnlyPrivateTests(unittest.TestCase):

    def test_the_program_lets_futex_through_in_its_private_forms_only(self):
        rows = {row["call"]: row for row in wall_linux.load_allow_list()["rows"]}
        self.assertEqual(rows["futex"]["check"], "futex_private")
        operations = rows["futex"]["operations"]
        self.assertTrue(operations)
        self.assertEqual([operation for operation in operations if not operation & 128], [9 | 256],
                         "every listed form is private but the one wait the C library's thread join makes")
        for arch in ("x86_64", "aarch64"):
            program, number = wall_linux._filter(arch, 3), dict(wall_linux._NR[arch], **wall_linux._NR_MORE[arch])["futex"]
            with self.subTest(arch=arch):
                for operation in operations:
                    self.assertEqual(_run_program(program, number, args=(0, operation, 0), arch=arch), "allow", operation)
                for name, operation in SHARED.items():
                    self.assertEqual(_run_program(program, number, args=(0, operation, 0), arch=arch), "eperm", name)
                self.assertEqual(_run_program(program, number, args=(0, 128 | 2, 0), arch=arch), "eperm", "not listed")

    def test_a_list_that_allows_a_form_that_is_not_private_is_refused(self):
        with open(wall_linux.ALLOW_LIST_FILE, encoding="utf-8") as handle:
            shipped = json.load(handle)
        shared = dict(shipped, futex_operations=shipped["futex_operations"] + [
            {"operation": 1, "name": "FUTEX_WAKE", "reason": "planted by the test: a shared wake"}])
        with tempfile.TemporaryDirectory() as top:
            path = os.path.join(top, "allow.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(shared, handle)
            with self.assertRaises(wall_linux.WallRefused) as caught:
                wall_linux.load_allow_list(path)
        self.assertEqual(caught.exception.code, "wall_allow_list_refused")
        self.assertIn("is not a private form", caught.exception.detail)


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class AHelperStartedBeforeTheWallIsNotWokenTests(unittest.TestCase):

    def test_a_wake_on_a_file_page_after_the_wall_reaches_no_helper(self):
        result, head = run_child(FUTEX_CHILD, "futex", wait=HELPER_WAIT_S)
        self.assertEqual(result["wake"], [-1, 1], "a wake that is not private is answered EPERM by the filter")
        self.assertEqual(head, PLANTED_TEXT[:7], "the helper waiting on the same file page was not woken")
        self.assertIs(result["record"]["kernel_surface"], True, "the probe's own wake that is not private was refused")
        self.assertIs(result["record"]["proven"], True, result["words"])


if __name__ == "__main__":
    unittest.main()
