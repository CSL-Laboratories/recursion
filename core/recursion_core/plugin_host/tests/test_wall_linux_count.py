"""Where the kernel gives no count of a process's descriptors, the probe cannot tell whether it found every one, and
cannot tell is never proven: a descriptor left above a lowered hard limit is not passed over as if none were held.

Walls are applied in CHILD processes only, each in a temporary folder of the test's own; the kernel's missing count
is stood in for by a stat of /proc/self/fd that fails, in the child alone.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from recursion_core.plugin_host import wall_linux
from recursion_core.plugin_host.tests.test_wall_linux import _why_not_here
from recursion_core.plugin_host.tests.test_wall_linux_family import run_child

LOWERED_HARD_LIMIT = 64  # test-side: the hard limit the child lowers itself to after the wall, not a product figure

COUNT_CHILD = r'''
import json, os, resource, sys
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
w.apply_wall(spec["paths"], keep_descriptors=(1, 2))
high = None
if spec["mode"] == "high":  # a pipe made after the wall, both ends moved above the limit lowered next
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    read_end, write_end = os.pipe()
    high = soft - 1
    os.dup2(write_end, high)
    os.dup2(read_end, high - 1)
    os.close(read_end)
    os.close(write_end)
    resource.setrlimit(resource.RLIMIT_NOFILE, (spec["lowered"], spec["lowered"]))
real = os.stat
def no_count(path, *args, **kwargs):  # the kernel gives no count of the descriptors (Linux before 6.2)
    if path == "/proc/self/fd":
        raise OSError("no count")
    return real(path, *args, **kwargs)
w.os.stat = no_count
record = w.probe_wall(spec["canary"], spec["daemon"], write_folder=spec["folder"])
w.os.stat = real
print(json.dumps({"high": high, "writes": high is not None and os.write(high, b"x") == 1, "record": record,
                  "words": w.why_not_proven(record)}))
'''


class CannotCountIsCannotTellTests(unittest.TestCase):

    def test_no_count_from_the_kernel_is_no_list(self):
        real = os.stat

        def no_count(path, *args, **kwargs):
            if path == "/proc/self/fd":
                raise OSError("no count")
            return real(path, *args, **kwargs)

        with mock.patch.object(wall_linux.os, "stat", no_count):
            self.assertIsNone(wall_linux._descriptors_now())
            self.assertIsNone(wall_linux._descriptors_open_for_write())
        self.assertIsInstance(wall_linux._descriptors_now(), list, "with the kernel's count the scan answers a list")


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class ADescriptorAboveALoweredLimitIsNotPassedOverTests(unittest.TestCase):

    def test_a_descriptor_above_a_lowered_limit_with_no_count_is_never_proven(self):
        result, _ = run_child(COUNT_CHILD, "high", lowered=LOWERED_HARD_LIMIT)
        record = result["record"]
        self.assertGreater(result["high"], LOWERED_HARD_LIMIT)
        self.assertIs(result["writes"], True, "the descriptor above the limit still writes after the probe")
        self.assertIs(record["proven"], False, "a descriptor the scan cannot see is not covered by proven")
        self.assertIsNone(record["descriptors_counted_by"])
        self.assertIsNone(record["descriptors_unaccounted"])
        self.assertIsNone(record["descriptors_open_for_write"])
        self.assertIn(wall_linux.WORDS["descriptors_uncounted"], result["words"])

    def test_no_count_with_nothing_held_is_not_proven_either(self):
        result, _ = run_child(COUNT_CHILD, "none")
        self.assertIs(result["record"]["proven"], False, "cannot tell is never proven")
        self.assertIsNone(result["record"]["descriptors_counted_by"])
        self.assertIn(wall_linux.WORDS["descriptors_uncounted"], result["words"])


if __name__ == "__main__":
    unittest.main()
