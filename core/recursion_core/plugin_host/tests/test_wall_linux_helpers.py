"""A process started before the wall cannot act for the walled one unseen: a shared map read-only at the wall, a child
whose main thread ended alone, and the process's name and memory, each read by another process.

Walls are applied in CHILD processes only, each in a temporary folder of the test's own; every grandchild a test
starts is ended by the test's own child before it exits.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest

from recursion_core.plugin_host import wall_linux
from recursion_core.plugin_host.tests.test_wall_linux import CORE, _interpreter, _why_not_here

CHILD_TIMEOUT_S = 120  # a bound on one child process of this test; test-side only, not a product figure

CHILD = r'''
import ctypes, json, mmap, os, sys, threading, time
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
libc.mprotect.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
mode, out = spec["mode"], {}

def apply():
    try:
        w.apply_wall(spec["paths"], keep_descriptors=(1, 2))
        return "applied"
    except w.WallRefused as exc:
        return exc.code

def prctl(option, arg):
    ctypes.set_errno(0)
    r = libc.prctl(ctypes.c_int(option), ctypes.c_ulong(arg), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))
    return ctypes.get_errno() if r < 0 else 0

if mode == "ro_map":   # a shared page, writable, made read-only before the wall
    page = mmap.mmap(-1, mmap.PAGESIZE, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
    address = ctypes.addressof(ctypes.c_char.from_buffer(page))
    out["mprotect_ro"] = libc.mprotect(address, mmap.PAGESIZE, mmap.PROT_READ)
    out["apply"] = apply()
    out["maps"] = (w._STATE.get("seen_at_wall") or {}).get("maps")
elif mode == "zleader":  # a child whose main thread ends alone while a second thread runs on
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(reader)
        def late():
            time.sleep(3)
            os._exit(0)
        threading.Thread(target=late).start()
        os.write(writer, b"x")
        libc.syscall(60 if w._arch() == "x86_64" else 93, 0)  # exit of this thread only
    os.close(writer)
    os.read(reader, 1)
    os.close(reader)
    for _ in range(200):
        with open(f"/proc/{pid}/stat", "rb") as handle:
            if handle.read().rpartition(b")")[2].split()[0] == b"Z":
                break
        time.sleep(0.01)
    out["leader_state"] = "Z"
    out["apply"] = apply()
    os.waitpid(pid, 0)
elif mode == "prctl":  # after the wall: the name, a tracer, the dumpable state
    out["apply"] = apply()
    name = ctypes.create_string_buffer(b"walled-name", 16)
    out["set_name"] = prctl(15, ctypes.addressof(name))
    out["set_ptracer_any"] = prctl(0x59616D61, 0xFFFFFFFFFFFFFFFF)
    out["set_dumpable"] = prctl(4, 1)
    ctypes.set_errno(0)
    out["dumpable"] = libc.prctl(ctypes.c_int(3), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))
print(json.dumps(out))
'''


def run_child(mode: str) -> dict:
    with tempfile.TemporaryDirectory() as top:
        spec = {"core": str(CORE), "mode": mode, "paths": sorted({sys.base_prefix, sys.prefix})}
        child = subprocess.run(_interpreter() + ["-c", CHILD, json.dumps(spec)], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=CHILD_TIMEOUT_S, cwd=top, env={"HOME": top, "TMPDIR": top})
    lines = child.stdout.strip().splitlines()
    if child.returncode != 0 or not lines:
        raise AssertionError(f"the child ({mode}) ended {child.returncode}: {child.stderr[-1500:]}")
    return json.loads(lines[-1])


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class NothingStartedBeforeTheWallActsUnseenTests(unittest.TestCase):

    def test_a_shared_map_read_only_at_the_wall_is_named(self):
        result = run_child("ro_map")
        self.assertEqual((result["mprotect_ro"], result["apply"]), (0, "applied"))
        self.assertTrue(result["maps"], "a shared map that can be made writable after the wall is named")

    def test_a_child_whose_main_thread_ended_alone_is_alive(self):
        result = run_child("zleader")
        self.assertEqual(result["apply"], "wall_children_running", "a child is alive while any of its tasks is")

    def test_after_the_wall_the_name_a_tracer_and_the_dumpable_state_are_refused(self):
        result = run_child("prctl")
        self.assertEqual(result["apply"], "applied")
        self.assertEqual((result["set_name"], result["set_ptracer_any"], result["set_dumpable"]), (1, 1, 1), result)
        self.assertEqual(result["dumpable"], 0, "the wall leaves the process not dumpable")

    def test_the_allow_list_names_prctl_by_option_only(self):
        rows = {row["call"]: row for row in wall_linux.load_allow_list()["rows"]}
        self.assertEqual(rows["prctl"]["check"], "prctl_option")
        self.assertFalse(set(rows["prctl"]["options"]) & set(wall_linux.DENIED_PRCTL_OPTIONS.values()))
        from recursion_core.plugin_host.tests.test_wall_linux_proof import _run_program
        for arch in ("x86_64", "aarch64"):
            program, number = wall_linux._filter(arch, 3), wall_linux._NR_MORE[arch]["prctl"]
            with self.subTest(arch=arch):
                for option in wall_linux.DENIED_PRCTL_OPTIONS.values():
                    self.assertEqual(_run_program(program, number, args=(option, 0, 0), arch=arch), "eperm", option)
                self.assertEqual(_run_program(program, number, args=(16, 0, 0), arch=arch), "allow")
                self.assertEqual(_run_program(program, number, args=(0x53564D41, 0, 0), arch=arch), "eperm")


if __name__ == "__main__":
    unittest.main()
