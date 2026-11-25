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
HELD_STAND_IN = lambda: ({k: "not_tried" if k in wall_linux.HELD_NOT_TRIED else "closed_by_wall"  # noqa: E731
                          for k in wall_linux.HELD_KINDS}, dict(wall_linux.HELD_NOT_TRIED))

KEEP_CHILD = r'''
import fcntl, json, os, socket, sys
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
mode, out = spec["mode"], {}
has_keep = "keep_descriptors" in w.apply_wall.__code__.co_varnames
held, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)  # the channel, kept
keep = [1, 2, held.fileno(), peer.fileno()]
if mode == "owner_kept":
    read_end, write_end = os.pipe()
    fcntl.fcntl(read_end, fcntl.F_SETOWN, spec["daemon"])  # a receiver of its signals, set before the wall
    keep.append(read_end)
if mode == "k1":
    held_file = os.open(spec["planted"], os.O_RDONLY | os.O_CLOEXEC)  # held at the wall, closed after it
    keep += w._descriptors_now() if has_keep else []  # no gap below it, so the kernel gives its number again
    sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    socket.send_fds(peer, [b"x"], [sender.fileno()])  # in flight until it is received after the wall
    sender.close()
out["keep"] = sorted(set(keep) | {0, 1, 2})
try:
    if mode == "none" or not has_keep:
        w.apply_wall(spec["paths"])
    else:
        w.apply_wall(spec["paths"], keep_descriptors=keep)
    out["apply"] = "applied"
except w.WallRefused as exc:
    out["apply"], out["applied"] = exc.code, list(w._STATE["applied"])
    print(json.dumps(out))
    sys.exit(0)
if mode == "k1":
    os.close(held_file)
    _, fds, _, _ = socket.recv_fds(held, 1, 1)  # the kernel gives it the lowest free number: the closed one's
    out["closed"], out["received"] = held_file, fds[0]
record = w.probe_wall(spec["canary"], spec["daemon"], write_folder=spec["folder"])
out.update(record=record, words=w.why_not_proven(record))
print(json.dumps(out))
'''


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class TheWallClosesWhatIsNotKeptTests(unittest.TestCase):
    """apply_wall closes every descriptor the host does not keep, after it made one object of each held kind; the
    probe tries each after the wall, and a descriptor received at the number of one closed since is named."""

    def test_everything_not_kept_is_closed_and_each_held_kind_is_named_with_its_outcome(self):
        result, _ = run_child(KEEP_CHILD, "closed")
        record = result["record"]
        expected = dict.fromkeys(("ring", "datagram_socket", "stream_socket", "signal_owner"), "closed_by_wall")
        if record["held_objects"]["ring"] == "not_tried":  # a kernel with rings turned off: named with its reason
            expected["ring"] = "not_tried"
            self.assertTrue(record["held_objects_not_tried"]["ring"])
        expected.update(shared_map="not_tried", namespace="not_tried")
        self.assertEqual(record["held_objects"], expected, result)
        self.assertEqual(record["held_objects_not_tried"]["shared_map"], wall_linux.HELD_NOT_TRIED["shared_map"])
        self.assertEqual(record["descriptors_not_closed"], [])
        self.assertEqual([entry["descriptor"] for entry in record["descriptors_kept"]], result["keep"])
        self.assertEqual(record["descriptors_kept"][0], {"descriptor": 0, "kind": "character_device"},
                         "a standard number not kept points at /dev/null")
        self.assertIs(record["proven"], True, result["words"])
        self.assertEqual(_schema_errors(record), [])

    def test_with_no_keep_list_a_connected_socket_and_a_preset_signal_still_act_and_nothing_is_proven(self):
        result, _ = run_child(KEEP_CHILD, "none")
        record = result["record"]
        self.assertEqual((record["held_objects"]["stream_socket"], record["held_objects"]["signal_owner"]),
                         ("acted", "acted"), "what the words say is true: both still act when nothing is closed")
        self.assertEqual(record["held_objects"]["datagram_socket"], "refused_by_filter")
        self.assertIsNone(record["descriptors_kept"])
        self.assertIs(record["proven"], False)
        self.assertIn(wall_linux.WORDS["keep_not_named"], result["words"])
        self.assertIn(wall_linux.WORDS["held_acted"].format(what=wall_linux.WORDS["held_stream_socket"]),
                      result["words"])
        self.assertEqual(_schema_errors(record), [])

    def test_a_kept_descriptor_with_a_receiver_of_its_signals_is_refused_before_any_change(self):
        result, _ = run_child(KEEP_CHILD, "owner_kept")
        self.assertEqual((result["apply"], result.get("applied")), ("wall_keep_refused", []), result)

    def test_a_descriptor_received_at_the_number_of_one_closed_after_the_wall_is_named(self):
        result, _ = run_child(KEEP_CHILD, "k1")
        self.assertEqual(result["received"], result["closed"], "the case needs the same number")
        record = result["record"]
        self.assertIn({"descriptor": result["closed"], "kind": "socket"}, record["descriptors_unaccounted"] or [])
        self.assertIs(record["proven"], False)
        self.assertEqual(_schema_errors(record), [])

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
KEEP = (1, 2, held.fileno()) if "keep_descriptors" in w.apply_wall.__code__.co_varnames else None
w.apply_wall(spec["paths"], **({} if KEEP is None else {"keep_descriptors": KEEP}))
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


USE_CHILD = r'''
import ctypes, errno, fcntl, json, os, socket, sys
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
numbers = dict(w._NR[w._arch()], **w._NR_MORE[w._arch()])
def call(name, *args):
    ctypes.set_errno(0)
    result = libc.syscall(ctypes.c_long(numbers.get(name, {"io_uring_setup": 425, "io_uring_enter": 426,
                                                           "io_uring_register": 427}.get(name, -1))),
                          *(ctypes.c_long(a) for a in args))
    return 0 if result >= 0 else ctypes.get_errno()
def errno_of(act):
    try:
        act()
    except OSError as exc:
        return exc.errno
    return 0
out, mode = {}, spec["mode"]
# planted before the wall: objects of the child's own only (a ring, loopback and unix-path receivers of its own, a
# pipe and a folder whose signal owner is the test's stand-in process)
if mode == "ring":
    params = ctypes.create_string_buffer(120)
    ctypes.set_errno(0)
    ring = libc.syscall(ctypes.c_long(425), ctypes.c_long(1), ctypes.c_long(ctypes.addressof(params)))
    out["ring_made"] = ring >= 0
elif mode == "datagram":
    inet_receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    inet_receiver.bind(("127.0.0.1", 0))
    inet_receiver.setblocking(False)
    unix_path = os.path.join(spec["top"], "receiver")
    unix_receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    unix_receiver.bind(unix_path)
    unix_receiver.setblocking(False)
    inet_sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    unix_sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    connected, connected_peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)  # connected before the wall
elif mode == "owner":
    read_end, write_end = os.pipe()
    folder = os.open(spec["top"], os.O_RDONLY | os.O_DIRECTORY)
    for fd in (read_end, folder):
        fcntl.fcntl(fd, fcntl.F_SETOWN, spec["daemon"])
if "keep_descriptors" in w.apply_wall.__code__.co_varnames:
    w.apply_wall(spec["paths"], keep_descriptors=(1, 2))
else:
    w.apply_wall(spec["paths"])
if mode == "ring":
    out["enter"] = call("io_uring_enter", ring, 0, 0, 0, 0, 0)
    out["register"] = call("io_uring_register", ring, 0, 0, 0)  # IORING_REGISTER_BUFFERS of nothing
elif mode == "datagram":
    out["inet_sendto"] = errno_of(lambda: inet_sender.sendto(b"after the wall", inet_receiver.getsockname()))
    out["unix_sendto"] = errno_of(lambda: unix_sender.sendto(b"after the wall", unix_path))
    out["inet_sendmsg"] = errno_of(lambda: inet_sender.sendmsg([b"after"], [], 0, inet_receiver.getsockname()))
    out["connected_send"] = errno_of(lambda: connected.send(b"after the wall"))
    out["received"] = sum(errno_of(lambda: r.recv(64)) == 0 for r in (inet_receiver, unix_receiver))
elif mode == "owner":
    out["setsig"] = errno_of(lambda: fcntl.fcntl(read_end, 10, 10))
    out["async"] = errno_of(lambda: fcntl.fcntl(read_end, fcntl.F_SETFL, os.O_ASYNC))
    out["notify"] = errno_of(lambda: fcntl.fcntl(folder, 1026, 0x4 | 0x80000000))  # DN_CREATE | DN_MULTISHOT
    out["nonblocking"] = errno_of(lambda: os.set_blocking(read_end, False))  # the flags a process needs stay
record = w.probe_wall(spec["canary"], spec["daemon"], write_folder=spec["folder"])
out.update(record=record)
print(json.dumps(out))
'''


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class HeldObjectsCannotActTests(unittest.TestCase):
    """An object the host held from before the wall and did not keep: the wall closed it (EBADF), whether the filter
    would refuse its use or not (a socket connected before the wall), and the record is proven."""

    def test_a_ring_made_before_the_wall_cannot_submit_or_register_after_it(self):
        result, _ = run_child(USE_CHILD, "ring")
        if not result["ring_made"]:
            self.skipTest("this kernel makes no ring for this process (io_uring disabled)")
        self.assertEqual((result["enter"], result["register"]), (1, 1), result)  # the filter answers first
        self.assertIs(result["record"]["proven"], True)

    def test_a_datagram_socket_made_before_the_wall_cannot_send_to_an_address_after_it(self):
        result, _ = run_child(USE_CHILD, "datagram")
        self.assertTrue({result["inet_sendto"], result["unix_sendto"], result["inet_sendmsg"]} <= {1, 9}, result)
        self.assertEqual(result["connected_send"], 9, "a datagram socket connected before the wall is shut by it")
        self.assertEqual(result["received"], 0, "no datagram reached either receiver")
        self.assertIs(result["record"]["proven"], True)

    def test_a_signal_owner_set_before_the_wall_gets_no_signal_chosen_after_it(self):
        result, _ = run_child(USE_CHILD, "owner")
        self.assertTrue({result["setsig"], result["async"], result["notify"]} <= {1, 9}, result)  # EPERM or EBADF
        self.assertEqual(result["nonblocking"], 9, "the descriptor itself was shut by the wall")
        self.assertIs(result["record"]["proven"], True)


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
                mock.patch.object(wall_linux, "_descriptors_unaccounted", lambda: []), \
                mock.patch.object(wall_linux, "_kept_and_not_closed", lambda: ([], []), create=True), \
                mock.patch.object(wall_linux, "_held_objects", HELD_STAND_IN, create=True), \
                mock.patch.object(wall_linux, "_maps_open_for_write", lambda: []):
            record = wall_linux.probe_wall("/nonexistent-canary", os.getppid())
        self.assertEqual(record["descriptors_seen_by"], "scan_by_number")
        self.assertIs(record["proven"], False)
        self.assertIn(wall_linux.WORDS.get("no_wall_here", "no_wall_here"), wall_linux.why_not_proven(record))
        self.assertEqual(_schema_errors(record), [])


if __name__ == "__main__":
    unittest.main()
