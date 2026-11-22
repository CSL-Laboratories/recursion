"""Exercise the host with a daemon peer over ordinary subprocess pipes."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import venv
from pathlib import Path

from recursion_core.control_transport.framing import HEADER
from recursion_core.plugin_security import CoreHealth, PluginLoader, ReservedEffects, TrustStore
from recursion_core.plugin_security import host_channel as hc
from recursion_core.plugin_security.limits import ARTIFACT
from recursion_core.plugin_security.tests.support import FakeGilProbe, manifest_bytes

CORE = Path(__file__).resolve().parents[3]
PLUGIN = "demo.plugin"
HELLO = {"type": "hello", "channel": hc.CHANNEL}
BYE = {"type": "bye", "reason": "done"}
SOURCE = (
    b"# coding: latin-1\n"
    b"def operator(command: dict):\n"
    b"    return {'module': __name__, 'evaluated': operator.__annotations__['command'] is dict,\n"
    b"            'command': command, 'text': 'caf\xe9'}\n"
)


def b64(value):
    return base64.b64encode(value).decode("ascii")


def load(source=SOURCE, *, extras=None, manifest_field=None):
    manifest = manifest_bytes("01" * 32, plugin_id=PLUGIN, source=source)
    if manifest_field is not None:
        manifest = json.dumps(dict(json.loads(manifest), gil_check=manifest_field)).encode()
    files = {ARTIFACT: source}
    files.update(extras or {})
    return {"type": "load", "plugin_id": PLUGIN, "manifest_b64": b64(manifest),
            "files": {path: b64(data) for path, data in files.items()}}


def call(id_, name="operator", /, **args):
    return {"type": "call", "id": id_, "name": name, "args": args}


def core(id_, data):
    return {"type": "core", "id": id_, "bytes_b64": b64(data)}


def result(id_, value):
    return {"type": "result", "id": id_, "value": value}


def frames(output):
    while output:
        (length,) = HEADER.unpack_from(output)
        end = HEADER.size + length
        if end > len(output):
            raise AssertionError("truncated frame from host")
        yield hc.check_message(hc.decode(output[HEADER.size:end], sender="host"), receiver="daemon")
        output = output[end:]


class HostProgramTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(scratch.cleanup)
        cls.root = Path(scratch.name)
        env = cls.root / "python"
        venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(env)
        cls.python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        cls.site = Path(subprocess.check_output(
            [str(cls.python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
        (cls.site / "host_test.pth").write_text(
            "import sys; sys.dont_write_bytecode = True; import _recursion_host_test_startup\n",
            encoding="ascii",
        )

    def exchange(self, *messages, states=(False,), real=False, extra=None):
        # Isolated Python runs venv .pth files but ignores PYTHONPATH. This shim
        # passes a probe to main and reports its returned status on stderr.
        env = {"PYTHONPATH": str(CORE), "__PYVENV_LAUNCHER__": str(self.python)}
        options = {}
        bootstrap = ""
        if os.name == "nt":
            import msvcrt

            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
            # Bypass the Windows venv redirector to keep this a single child.
            options["executable"] = sys._base_executable
        if extra is not None:
            env["CSL_HOST_TEST_MARKER"] = "inherited"
            if os.name == "nt":
                handle = msvcrt.get_osfhandle(extra)
                startup = subprocess.STARTUPINFO(lpAttributeList={"handle_list": [handle]})
                options["startupinfo"] = startup
                # Win32 inherits handles; register this one in the child CRT.
                bootstrap = f"import msvcrt\nextra = msvcrt.open_osfhandle({handle}, os.O_RDONLY)\n"
            else:
                options["pass_fds"] = (extra,)
                bootstrap = f"extra = {extra}\n"
            bootstrap += (
                "assert os.environ['CSL_HOST_TEST_MARKER'] == 'inherited'\n"
                "info = os.fstat(extra)\n"
                "extra_identity = [info.st_dev, info.st_ino]\n"
            )
        else:
            bootstrap = "extra_identity = None\n"
        startup_code = (
            f"import json, os, sys\nsys.path.insert(0, {str(CORE)!r})\n"
            "from recursion_core import plugin_host\n"
            "from recursion_core.plugin_security.tests.support import FakeGilProbe\n"
            + bootstrap
            + textwrap.dedent(f"""
                main = plugin_host.main
                probe = FakeGilProbe({states!r})

                def run():
                    if {real!r}:
                        sys._is_gil_enabled = probe.gil_enabled
                        status = main()
                    else:
                        status = main(gil_probe=probe)
                    print(json.dumps([status, probe.calls, extra_identity]), file=sys.stderr)

                plugin_host.main = run
                """)
        )
        # site imports a fresh copy for each child, including when launch options change.
        (self.site / "_recursion_host_test_startup.py").write_text(startup_code, encoding="utf-8")
        stdin = b"".join(msg if isinstance(msg, bytes) else hc.encode(msg) for msg in messages)
        with tempfile.TemporaryDirectory(dir=self.root) as cwd:
            if extra is not None and os.name == "nt":
                os.set_handle_inheritable(handle, True)
            try:
                process = subprocess.Popen(
                    [sys.executable, "-X", "gil=0", "-I", "-m", "recursion_core.plugin_host"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=cwd, env=env, **options,
                )
            finally:
                if extra is not None and os.name == "nt":
                    os.set_handle_inheritable(handle, False)
            with process:
                try:
                    stdout, stderr = process.communicate(stdin, timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        replies = list(frames(stdout))
        self.assertTrue(replies, stderr.decode(errors="replace"))
        self.assertEqual(replies.pop(0), HELLO)
        status, reads, identity = json.loads(stderr.splitlines()[-1])
        return replies, status, reads, identity

    def assert_refusal(self, reply, id_, code, detail=None):
        self.assertEqual((reply["type"], reply["id"], reply["code"]), ("refusal", id_, code))
        if detail is not None:
            self.assertEqual(reply["detail"], detail)

    def test_t1_operator_matches_the_in_process_loader(self):
        probe = FakeGilProbe()
        loader = PluginLoader(TrustStore({}), {}, ReservedEffects(), CoreHealth(), probe)
        plugin = loader.load(manifest_bytes("01" * 32, source=SOURCE), None, SOURCE)
        command = {"command": "positions", "terms": [1, False, None]}
        replies, _, reads, _ = self.exchange(HELLO, load(), call(7, command=command), BYE)
        self.assertEqual(replies, [result(7, plugin.module.operator(command))])
        self.assertEqual(reads, 2)

    def test_t2_plugin_has_no_file_and_reads_package_bytes(self):
        package = b"\x00\xff{}\r\n"
        source = textwrap.dedent("""
            def operator(command):
                if command == 'present':
                    value = package.read('themes/x.json')
                    assert type(value) is bytes
                    return value.hex()
                try:
                    package.read('themes/missing.json')
                except FileNotFoundError as exc:
                    return str(exc)
                raise AssertionError('missing file was read')
            """).encode()
        replies, _, _, _ = self.exchange(
            HELLO, load(b"open(__file__)\n"), load(source, extras={"themes/x.json": package}),
            call(7, command="present"), call(8, command="missing"), BYE,
        )
        self.assert_refusal(replies[0], None, "call_failed", "NameError")
        self.assertEqual(replies[1:], [result(7, package.hex()), result(8, "themes/missing.json")])

    def test_t3_print_and_descriptor_one_cannot_write_channel_bytes(self):
        source = textwrap.dedent("""
            import os
            import sys
            print('during import', flush=True)
            def operator(command):
                print('during call', flush=True)
                print('original stdout', file=sys.__stdout__, flush=True)
                os.write(1, b'direct descriptor write')
                return command
            """).encode()
        replies, _, _, _ = self.exchange(HELLO, load(source), call(7, command="intact"), BYE)
        self.assertEqual(replies, [result(7, "intact")])

    def test_t4_post_call_gil_refuses_work_and_latches(self):
        daemon_probe = getattr(sys, "_is_gil_enabled", None)
        daemon_gil = daemon_probe() if daemon_probe is not None else None
        replies, status, reads, _ = self.exchange(
            HELLO, load(), call(7, command={}), call(8, command={}), load(), BYE,
            states=(False, True),
        )
        self.assertEqual(len(replies), 3)
        for reply, id_ in zip(replies, (7, 8, None)):
            self.assert_refusal(reply, id_, "host_gil_enabled")
        self.assertEqual(status, {"gil_check": "on", "gil": "enabled", "accepting_work": False})
        self.assertEqual(reads, 2)
        self.assertIs(getattr(sys, "_is_gil_enabled", None), daemon_probe)
        if daemon_probe is not None:
            self.assertEqual(daemon_probe(), daemon_gil)

    @unittest.skipUnless(hasattr(sys, "_is_gil_enabled"), "this CPython build has no sys._is_gil_enabled")
    def test_t4_real_probe_uses_the_child_sys_function(self):
        replies, status, reads, _ = self.exchange(
            dict(HELLO, gil_check="on"), load(), call(7, command={}), BYE, states=(False, True), real=True,
        )
        self.assertEqual(len(replies), 1)
        self.assert_refusal(replies[0], 7, "host_gil_enabled")
        self.assertEqual((status["gil"], reads), ("enabled", 2))

    def test_t4_probe_follows_failed_loads_and_failed_calls(self):
        failure = b"def operator(command):\n    raise SystemExit('private detail')\n"
        cases = (
            ([load(b"def :\n")], (True,), None),
            ([load(b"raise RuntimeError('private detail')\n")], (True,), None),
            ([load(failure), call(7, command={})], (False, True), 7),
        )
        for work, states, id_ in cases:
            with self.subTest(id=id_, source=work[0]["files"][ARTIFACT]):
                replies, status, reads, _ = self.exchange(HELLO, *work, BYE, states=states)
                self.assertEqual(len(replies), 1)
                self.assert_refusal(replies[0], id_, "host_gil_enabled")
                self.assertFalse(status["accepting_work"])
                self.assertEqual(reads, len(states))

    def test_t5_daemon_can_disable_enforcement_at_hello_only(self):
        replies, status, reads, _ = self.exchange(
            dict(HELLO, gil_check="off"), load(), call(7, command="first"),
            dict(HELLO, gil_check="on"), call(8, command="second"), BYE, states=(False, True),
        )
        self.assertEqual([r["type"] for r in replies], ["result", "refusal", "result"])
        self.assert_refusal(replies[1], None, "message_invalid")
        self.assertEqual((replies[0]["value"]["command"], replies[2]["value"]["command"]), ("first", "second"))
        self.assertEqual(status, {"gil_check": "off", "gil": "enabled", "accepting_work": True})
        self.assertEqual(reads, 3)

    def test_t6_only_daemon_hello_can_set_the_gate(self):
        with self.assertRaises(hc.ChannelRefusal) as caught:
            hc.check_message(dict(HELLO, gil_check="off"), receiver="daemon")
        self.assertEqual(caught.exception.code, "message_invalid")
        _, baseline, _, _ = self.exchange(HELLO, load(), call(7, command={}), BYE, states=(False, True))
        for spoofed_package in (load(manifest_field="off"), load(extras={"gil_check": b"off"})):
            with self.subTest(package=spoofed_package):
                replies, status, reads, _ = self.exchange(
                    dict(HELLO, gil_check="wrong"), HELLO, dict(load(), gil_check="off"),
                    dict(call(3, command={}), gil_check="off"), dict(core(4, b""), gil_check="off"),
                    spoofed_package, call(7, command={}), BYE,
                    states=(False, True),
                )
                self.assertEqual(len(replies), 5)
                for reply, id_ in zip(replies[:4], (None, None, 3, 4)):
                    self.assert_refusal(reply, id_, "message_invalid")
                self.assert_refusal(replies[4], 7, "host_gil_enabled")
                self.assertEqual(status, baseline)
                self.assertEqual((status["gil_check"], reads), ("on", 2))

    def test_t7_inherited_descriptor_and_environment_are_removed(self):
        source = textwrap.dedent("""
            import os
            import sys

            def registry():
                if os.name == 'nt':
                    candidates = range(8192)  # UCRT descriptor table
                elif sys.platform.startswith('linux'):
                    candidates = map(int, os.listdir('/proc/self/fd'))
                else:
                    candidates = range(os.sysconf('SC_OPEN_MAX'))
                descriptors = {}
                for fd in candidates:
                    try:
                        info = os.fstat(fd)
                    except OSError:
                        continue
                    descriptors[str(fd)] = [info.st_dev, info.st_ino]
                return {'fds': descriptors, 'env': dict(os.environ), 'cwd_empty': not os.listdir('.')}
            """).encode()
        with tempfile.TemporaryFile() as extra:
            replies, _, _, identity = self.exchange(HELLO, load(source), call(7, "registry"), BYE,
                                                    extra=extra.fileno())
        self.assertEqual(len(replies), 1)
        observed = replies[0]["value"]
        fds = observed["fds"]
        self.assertEqual(len(fds), 5)
        self.assertTrue({"0", "1", "2"} <= fds.keys())
        self.assertNotIn(identity, fds.values())  # The same fd number may now belong to a channel.
        expected_env = {"SYSTEMROOT": os.environ["SYSTEMROOT"]} if os.name == "nt" else {}
        self.assertEqual(observed["env"], expected_env)
        self.assertTrue(observed["cwd_empty"])

    def test_t8_bye_and_eof_end_cleanly(self):
        for messages in ((), (HELLO,), (HELLO, BYE, call(7, command="after bye"))):
            with self.subTest(messages=messages):
                replies, _, reads, _ = self.exchange(*messages)
                self.assertEqual(replies, [])
                self.assertEqual(reads, 0)

    def test_a_truncated_frame_is_refused_before_eof(self):
        for truncated in (b"\x00", HEADER.pack(2) + b"{"):
            with self.subTest(truncated=truncated):
                replies, _, _, _ = self.exchange(HELLO, truncated)
                self.assertEqual(len(replies), 1)
                self.assert_refusal(replies[0], None, "message_invalid", "frame_truncated")

    def test_core_bytes_and_ids_round_trip_during_bind_and_call(self):
        source = textwrap.dedent("""
            def bind_core(client):
                global core_call, bound
                core_call = client
                bound = client(b'\\xff\\x00 bind\\n')

            def operator(command):
                return [bound.hex(), core_call(b'\\xfe call\\r\\n').hex()]

            def registry():
                return {}
            """).encode()
        first, second = b"\x00\xfe bind reply", b"\xff call reply\n"
        replies, _, reads, _ = self.exchange(
            HELLO, load(source), call(7, command={}), core(19, b"wrong id"), core(0, first),
            call(8, "registry"), core(1, second), BYE,
        )
        self.assertEqual(len(replies), 5)
        self.assertEqual(replies[0], core(0, b"\xff\x00 bind\n"))
        self.assert_refusal(replies[1], 19, "message_invalid")
        self.assertEqual(replies[2:], [core(1, b"\xfe call\r\n"), result(7, [first.hex(), second.hex()]),
                                       result(8, {})])
        self.assertEqual(reads, 3)

    def test_eof_and_bye_while_waiting_for_core_end_without_a_result(self):
        source = b"def bind_core(client):\n    client(b'pending')\n"
        for ending in ((), (BYE,)):
            with self.subTest(ending=ending):
                replies, _, reads, _ = self.exchange(HELLO, load(source), *ending)
                self.assertEqual(replies, [core(0, b"pending")])
                self.assertEqual(reads, 1)

    def test_channel_refusals_and_bad_returns_leave_the_host_up(self):
        source = textwrap.dedent("""
            def registry():
                return object()

            def operator(command):
                raise SystemExit('private')

            def surfaces():
                return ['theme']

            def surface(kind, name):
                return {'pair': [kind, name]}
            """).encode()
        malformed = HEADER.pack(1) + b"\xff"
        length = hc.frame_limit("daemon") + 1
        oversized = HEADER.pack(length) + b" " * length
        replies, _, _, _ = self.exchange(
            HELLO, malformed, oversized, load(source), call(7, "unknown"), call(8, "net.request"),
            call(9), call(10, "registry"), call(11, command={}), call(12, "surfaces"),
            call(13, "surface", name="night", kind="theme"), BYE,
        )
        self.assertEqual(len(replies), 9)
        for reply, id_, code, detail in zip(
            replies[:7],
            (None, None, 7, 8, 9, 10, 11),
            ("message_invalid", "message_invalid", "call_unknown", "call_reserved",
             "call_args_invalid", "call_failed", "call_failed"),
            ("frame_not_json", "frame_too_large", None, None, None, "TypeError", "SystemExit"),
        ):
            self.assert_refusal(reply, id_, code, detail)
        self.assertEqual(replies[7:], [result(12, ["theme"]), result(13, {"pair": ["theme", "night"]})])

    def test_t4_return_values_must_be_json_values(self):
        source = textwrap.dedent("""
            def registry():
                return (1, 2)

            def operator(command):
                return {1: 'integer key'}

            def surfaces():
                return ['ok']
            """).encode()
        replies, _, _, _ = self.exchange(HELLO, load(source), call(7, "registry"), call(8, command={}),
                                         call(9, "surfaces"), BYE)
        self.assert_refusal(replies[0], 7, "call_failed", "TypeError")
        self.assert_refusal(replies[1], 8, "call_failed", "TypeError")
        self.assertEqual(replies[2:], [result(9, ["ok"])])

    def test_schema_names_the_optional_hello_field(self):
        schema = json.loads((CORE.parent / "schemas/plugin/v1/host.v1.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["x-message-types"]["hello"]["optional_from"], {"daemon": ["gil_check"]})
        self.assertEqual(hc.OPTIONAL_MESSAGE_FIELDS, {("hello", "daemon"): frozenset({"gil_check"})})
        self.assertEqual(schema["$defs"]["hello"]["properties"]["gil_check"]["enum"], ["on", "off"])
