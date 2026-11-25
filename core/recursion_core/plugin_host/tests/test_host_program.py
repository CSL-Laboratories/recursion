"""Exercise the host with a daemon peer over ordinary subprocess pipes."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
import venv
from pathlib import Path

from recursion_core.control_transport.framing import HEADER
from recursion_core.plugin_security import CoreHealth, PluginLoader, ReservedEffects, TrustStore
from recursion_core.plugin_security import host_channel as hc
from recursion_core.plugin_security.limits import ARTIFACT
from recursion_core.plugin_security.tests.support import FakeGilProbe, manifest_bytes

CORE = Path(__file__).resolve().parents[3]
PLUGIN = "demo.plugin"
SECRET_NAME = "CSL_HOST_TEST_SECRET"
SECRET = "stand-in-secret-value"
MARKER = "CSL_PLUGIN_HOST_CLEAN"
CANARY_NAME = "CSL_HOST_TEST_CANARY"
CANARY = "canary-value-7d3f19"
RESTARTED = "--restarted-once"
# The bytes of a stand-in file already in the host's working folder before it starts.
KEPT = b"a person's file, stand-in\n"
# The named exit statuses of a host that refuses to start (sysexits EX_OSERR and EX_CONFIG).
EXIT_RESTART_FAILED = 71
EXIT_ENVIRONMENT_NOT_CLEAN = 78
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


def load(source=SOURCE, *, extras=None, manifest_field=None, plugin_id=PLUGIN):
    manifest = manifest_bytes("01" * 32, plugin_id=plugin_id, source=source)
    if manifest_field is not None:
        manifest = json.dumps(dict(json.loads(manifest), gil_check=manifest_field)).encode()
    files = {ARTIFACT: source}
    files.update(extras or {})
    return {"type": "load", "plugin_id": plugin_id, "manifest_b64": b64(manifest),
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


ENV_SOURCE = textwrap.dedent("""
    import glob
    import os

    def registry():
        def read(path):
            with open(path, 'rb') as record:
                return record.read().decode('latin-1')
        return {'environ': dict(os.environ), 'cmdline': read('/proc/self/cmdline'),
                'proc': read('/proc/self/environ'),
                'tasks': [read(path) for path in glob.glob('/proc/self/task/*/environ')]}
    """).encode()

FDS_SOURCE = textwrap.dedent("""
    import os

    def registry():
        found = {}
        for entry in os.listdir('/proc/self/fd'):
            try:
                info = os.fstat(int(entry))
            except OSError:
                continue
            found[entry] = [info.st_dev, info.st_ino]
        with open('/proc/self/environ', 'rb') as record:
            return {'fds': found, 'proc': record.read().decode('latin-1')}
    """).encode()


PREFIX_SOURCE = textwrap.dedent("""
    import os
    import sys

    def registry():
        proc = None
        if os.path.exists('/proc/self/environ'):
            with open('/proc/self/environ', 'rb') as record:
                proc = record.read().decode('latin-1')
        return {'prefix': sys.prefix, 'argv': sys.orig_argv, 'executable': sys.executable, 'proc': proc}
    """).encode()


def require_proc(*paths):
    missing = [path for path in paths if not os.path.exists(path)]
    return unittest.skipIf(bool(missing), f"requires {', '.join(missing)}")


def venv_python(env: Path) -> Path:
    windows = env / "Scripts/python.exe"
    return windows if windows.is_file() else env / "bin/python"


class HostTestHelperTests(unittest.TestCase):
    def test_require_proc_skips_only_when_a_path_is_missing(self):
        def sample():
            return "ran"

        with unittest.mock.patch.object(os.path, "exists", return_value=False):
            skipped = require_proc("/proc/self/environ", "/proc/self/fd")(sample)
        self.assertTrue(getattr(skipped, "__unittest_skip__", False))
        self.assertIn("/proc/self/environ", getattr(skipped, "__unittest_skip_why__", ""))
        self.assertIn("/proc/self/fd", getattr(skipped, "__unittest_skip_why__", ""))
        with self.assertRaises(unittest.SkipTest) as caught:
            skipped()
        self.assertTrue(str(caught.exception))

        with unittest.mock.patch.object(os.path, "exists", return_value=True):
            kept = require_proc("/proc/self/environ", "/proc/self/fd")(sample)
        self.assertFalse(getattr(kept, "__unittest_skip__", False))
        self.assertEqual(kept(), "ran")

    def test_venv_python_uses_environment_layout_under_nt_name(self):
        with tempfile.TemporaryDirectory() as scratch:
            posix_env = Path(scratch, "posix")
            (posix_env / "bin").mkdir(parents=True)
            posix_python = posix_env / "bin/python"
            posix_python.write_text("", encoding="ascii")
            with unittest.mock.patch.object(os, "name", "nt"):
                chosen = venv_python(posix_env)
            self.assertEqual(chosen, posix_python)
            self.assertTrue(chosen.is_file())

            windows_env = Path(scratch, "windows")
            (windows_env / "Scripts").mkdir(parents=True)
            windows_python = windows_env / "Scripts/python.exe"
            windows_python.write_text("", encoding="ascii")
            with unittest.mock.patch.object(os, "name", "posix"):
                chosen_windows = venv_python(windows_env)
            self.assertEqual(chosen_windows, windows_python)
            self.assertTrue(chosen_windows.is_file())


    @staticmethod
    def proc_skips(present):
        """Load a fresh copy of this module while only the /proc files in `present` exist.

        Returns {test name: skip reason} for the tests of HostProgramTests that come out skipped
        for a missing /proc file, and the names of the tests that load the /proc readers.
        """
        real_exists = os.path.exists

        def exists(path):
            text = os.fspath(path)
            return text in present if text.startswith("/proc") else real_exists(path)

        spec = importlib.util.spec_from_file_location("host_program_proc_probe", __file__)
        probe = importlib.util.module_from_spec(spec)
        with unittest.mock.patch.object(os.path, "exists", exists):
            spec.loader.exec_module(probe)
        skipped, readers = {}, set()
        for name in dir(probe.HostProgramTests):
            member = getattr(probe.HostProgramTests, name)
            if not name.startswith("test_"):
                continue
            inner = member
            while hasattr(inner, "__wrapped__"):
                inner = inner.__wrapped__
            if {"ENV_SOURCE", "FDS_SOURCE"} & set(inner.__code__.co_names):
                readers.add(name)
            reason = getattr(member, "__unittest_skip_why__", "")
            if getattr(member, "__unittest_skip__", False) and reason.startswith("requires "):
                skipped[name] = reason
        return skipped, readers

    @unittest.skipIf(os.name == "nt", "the /proc paths are POSIX only")
    def test_every_proc_reading_test_is_skipped_without_proc_and_no_other(self):
        skipped, readers = self.proc_skips(present=set())
        self.assertEqual(len(readers), 4, readers)
        self.assertEqual(set(skipped), readers)
        for name, reason in skipped.items():
            self.assertIn("/proc/self/environ", reason, name)
        everything, _ = self.proc_skips(present={"/proc/self/environ", "/proc/self/cmdline", "/proc/self/fd"})
        self.assertEqual(everything, {})

    @unittest.skipIf(os.name == "nt", "the /proc paths are POSIX only")
    def test_a_test_is_skipped_when_only_one_of_its_proc_paths_is_missing(self):
        skipped, readers = self.proc_skips(present={"/proc/self/environ", "/proc/self/cmdline"})
        self.assertEqual(len(skipped), 1, skipped)
        self.assertEqual(set(skipped), {n for n in readers if n.endswith("channels_and_nothing_more")})
        self.assertEqual(list(skipped.values()), ["requires /proc/self/fd"])
        only_fd, _ = self.proc_skips(present={"/proc/self/fd", "/proc/self/environ"})
        self.assertEqual(len(only_fd), 3, only_fd)
        for reason in only_fd.values():
            self.assertEqual(reason, "requires /proc/self/cmdline")


@unittest.skipUnless(sys.platform.startswith("linux"), "the canary is made only where the wall can be (Linux)")
class CanarySetupTests(unittest.TestCase):
    """The host's setup before its wall removes only what it made: a name already there stays as it was."""

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(scratch.name)
        self.cwd = Path(scratch.name)
        from recursion_core import plugin_host

        self.host = plugin_host
        self.folder = self.cwd / (plugin_host.CANARY_PREFIX + str(os.getpid()) + plugin_host.WRITE_FOLDER_SUFFIX)
        self.folder.mkdir()
        (self.folder / "kept.txt").write_bytes(KEPT)

    def assert_left_as_it_was(self):
        self.assertEqual(os.listdir(self.cwd), [self.folder.name], "the canary is gone, the folder is still there")
        self.assertEqual(os.listdir(self.folder), ["kept.txt"])
        self.assertEqual((self.folder / "kept.txt").read_bytes(), KEPT)

    def test_c1_a_folder_already_there_by_the_write_folders_name_stays_whole(self):
        self.assertEqual(self.host._canary(), ("", "", None))
        self.assert_left_as_it_was()

    def test_c2_when_the_canary_cannot_be_written_a_folder_already_there_stays_whole(self):
        with unittest.mock.patch.object(self.host.os, "write", side_effect=OSError(28, "no space left")):
            self.assertEqual(self.host._canary(), ("", "", None))
        self.assert_left_as_it_was()

    def test_c3_a_write_folder_this_call_made_is_removed_when_a_later_step_fails(self):
        (self.folder / "kept.txt").unlink()
        self.folder.rmdir()
        with unittest.mock.patch.object(self.host.wall_linux, "prepare_write_folder",
                                        side_effect=ValueError("stand-in")):
            self.assertEqual(self.host._canary(), ("", "", None))
        self.assertEqual(os.listdir(self.cwd), [], "the canary and the folder this call made are both gone")


class HostProgramTests(unittest.TestCase):
    counter = __import__("itertools").count()

    @classmethod
    def setUpClass(cls):
        scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(scratch.cleanup)
        cls.root = Path(scratch.name)
        env = cls.root / "python"
        venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(env)
        cls.python = venv_python(env)
        cls.site = Path(subprocess.check_output(
            [str(cls.python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
        (cls.site / "host_test.pth").write_text(
            "import sys; sys.dont_write_bytecode = True; import _recursion_host_test_startup\n",
            encoding="ascii",
        )

    def exchange(self, *messages, states=(False,), real=False, extra=None, secret=False, environ=None,
                 standin=None, code=0, wall="standin", files=None, bare=False):
        # Isolated Python runs venv .pth files but ignores PYTHONPATH. This shim passes a probe to
        # main and writes its returned status to a file in the host's working folder, because the
        # host points its own stderr at the null device. The host may start itself again once with a
        # clean environment: the venv's own interpreter is the command, so the shim loads both times.
        env = {"PYTHONPATH": str(CORE)} if environ is None else {}
        env.update(environ or {})
        starts = self.root / ("starts-%d" % next(self.counter))
        command = [str(self.python), "-X", "gil=0", "-I", "-m", "recursion_core.plugin_host"]
        if bare:
            # The interpreter is typed by its bare name and found through the PATH the caller gives.
            command[0] = self.python.name
        if secret:
            env[SECRET_NAME] = SECRET
        options = {}
        bootstrap = ""
        if os.name == "nt":
            import msvcrt

            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
            env["__PYVENV_LAUNCHER__"] = str(self.python)
            command[0] = sys.executable
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
                "info = os.fstat(extra)\n"
                "extra_identity = [info.st_dev, info.st_ino]\n"
            )
        else:
            bootstrap = "extra_identity = None\n"
        # standin "dirty": the first restart hands the new image one more variable, so no restart ever
        # comes out clean. standin "fail": the interpreter path the restart uses is a file that is not there.
        # standin "norecord": the kernel's record of the environment cannot be read (os.environ stands in).
        # wall: see WALL_SHIMS below. The host's probe takes a canary file it writes in the working folder
        # below and removes before any plugin byte runs; canaries_left says what of it is still there.
        startup_code = (
            f"import json, os, sys\nsys.path.insert(0, {str(CORE)!r})\n"
            f"open({str(starts)!r}, 'a').write('x')\n"
            + ("_real = os.execve\nos.execve = lambda p, a, e: _real(p, a, dict(e, CHECK_STANDIN_DIRTY='x'))\n"
               if "dirty" in (standin or "") else "")
            + ("sys.executable = '/nonexistent/python'\n" if standin == "fail" else "")
            + ("import builtins\n_open = builtins.open\n"
               "def _no_record(name, *a, **k):\n"
               "    if name == '/proc/self/environ':\n        raise OSError('no record')\n"
               "    return _open(name, *a, **k)\n"
               "builtins.open = _no_record\n" if "norecord" in (standin or "") else "")
            +
            "from recursion_core import plugin_host\n"
            "from recursion_core.plugin_security.tests.support import FakeGilProbe\n"
            + WALL_SHIMS[wall]
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
                    try:
                        with open("host-status.json", "w") as out:
                            out.write(json.dumps([status, probe.calls, extra_identity]))
                    except PermissionError:
                        pass  # a real wall refuses every new write; the caller reads no status then

                plugin_host.main = run
                """)
        )
        # site imports a fresh copy for each child, including when launch options change.
        (self.site / "_recursion_host_test_startup.py").write_text(startup_code, encoding="utf-8")
        stdin = b"".join(msg if isinstance(msg, bytes) else hc.encode(msg) for msg in messages)
        with tempfile.TemporaryDirectory(dir=self.root) as cwd:
            for name, text in (files or {}).items():
                Path(cwd, name).parent.mkdir(parents=True, exist_ok=True)
                Path(cwd, name).write_text(text, encoding="utf-8")
            if extra is not None and os.name == "nt":
                os.set_handle_inheritable(handle, True)
            try:
                process = subprocess.Popen(
                    command,
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
            report = Path(cwd, "host-status.json")
            report = report.read_text() if report.exists() else None
            self.canaries_left = sorted(
                name for name in os.listdir(cwd) if name.startswith(".csl-plugin-host-canary"))
            # What each folder among them holds: a file's bytes, or None for anything else.
            self.left_inside = {
                name: {child: Path(cwd, name, child).read_bytes() if Path(cwd, name, child).is_file() else None
                       for child in sorted(os.listdir(Path(cwd, name)))}
                for name in self.canaries_left if Path(cwd, name).is_dir() and not Path(cwd, name).is_symlink()}
        self.stderr = stderr
        self.starts = len(starts.read_text()) if starts.exists() else 0
        self.assertEqual(process.returncode, code, stderr.decode(errors="replace"))
        replies = list(frames(stdout))
        self.assertTrue(replies, stderr.decode(errors="replace"))
        self.hello = replies.pop(0)
        self.assertEqual({key: value for key, value in self.hello.items() if key != "wall"}, HELLO)
        if code != 0 or wall in ("real", "noreadfile", "nowrite", "notruncate", "keepcanary",
                                  "folderthere"):
            return replies, None, None, None
        self.assertIsNotNone(report, "the host wrote no status")
        status, reads, identity = json.loads(report)
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
            import stat
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
                standard = {str(fd): [stat.S_ISCHR(os.fstat(fd).st_mode), getattr(os.fstat(fd), 'st_rdev', 0)] for fd in (0, 1, 2)}
                null = [True, os.stat(os.devnull).st_rdev] if os.name != 'nt' else None
                return {'fds': descriptors, 'env': dict(os.environ), 'cwd_empty': not os.listdir('.'),
                        'standard': standard, 'null': null}
            """).encode()
        with tempfile.TemporaryFile() as extra:
            replies, _, _, identity = self.exchange(HELLO, load(source), call(7, "registry"), BYE,
                                                    extra=extra.fileno())
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["type"], "result", replies[0])
        observed = replies[0]["value"]
        fds = observed["fds"]
        self.assertEqual(len(fds), 5)
        self.assertTrue({"0", "1", "2"} <= fds.keys())
        self.assertNotIn(identity, fds.values())  # The same fd number may now belong to a channel.
        # Descriptors 0, 1 and 2 are the null device: nothing of the channel or the starter is there.
        for fd, (is_device, rdev) in observed["standard"].items():
            self.assertTrue(is_device, fd)
            if observed["null"] is not None:
                self.assertEqual([is_device, rdev], observed["null"], fd)
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
        self.assertEqual(schema["x-message-types"]["hello"]["optional_from"],
                         {"daemon": ["gil_check"], "host": ["wall"]})
        self.assertEqual(hc.OPTIONAL_MESSAGE_FIELDS, {("hello", "daemon"): frozenset({"gil_check"}),
                                                      ("hello", "host"): frozenset({"wall"})})
        self.assertEqual(schema["$defs"]["hello"]["properties"]["gil_check"]["enum"], ["on", "off"])

    def test_a1_plugin_reading_standard_input_cannot_reach_the_channel(self):
        source = textwrap.dedent("""
            import os
            import stat
            import sys

            def operator(command):
                if command != 'drain':
                    return command
                data = b''
                while True:
                    chunk = os.read(0, 65536)
                    if not chunk:
                        break
                    data += chunk
                return [len(data), len(sys.stdin.buffer.read()), stat.S_ISCHR(os.fstat(0).st_mode)]
            """).encode()
        big = "x" * 200000  # far more than the host reads ahead, so later frames wait in the pipe
        replies, _, _, _ = self.exchange(
            HELLO, load(source), call(7, command="drain"), call(8, command=big), call(9, command="after"), BYE,
        )
        self.assertEqual(replies, [result(7, [0, 0, True]), result(8, big), result(9, "after")])

    def test_a2_plugin_output_reaches_neither_the_channel_nor_the_starter(self):
        source = textwrap.dedent("""
            import os
            import sys

            def operator(command):
                sys.stdout.write('stdout-marker'); sys.stdout.flush()
                sys.stderr.write('stderr-marker'); sys.stderr.flush()
                os.write(1, b'fd1-marker')
                os.write(2, b'fd2-marker')
                print('dunder-marker', file=sys.__stderr__, flush=True)
                return command
            """).encode()
        replies, _, _, _ = self.exchange(HELLO, load(source), call(7, command="a"), call(8, command="b"), BYE)
        self.assertEqual(replies, [result(7, "a"), result(8, "b")])
        self.assertNotIn(b"marker", self.stderr)

    def test_a3_starter_variables_are_not_readable_by_the_plugin(self):
        source = textwrap.dedent("""
            import os

            def operator(command):
                proc = None
                if os.path.exists('/proc/self/environ'):
                    with open('/proc/self/environ', 'rb') as record:
                        proc = record.read().decode('latin-1')
                return {'environ': dict(os.environ), 'proc': proc}
            """).encode()
        replies, _, _, _ = self.exchange(HELLO, load(source), call(7, command={}), BYE, secret=True)
        value = replies[0]["value"]
        expected_env = {"SYSTEMROOT": os.environ["SYSTEMROOT"]} if os.name == "nt" else {}
        self.assertEqual(value["environ"], expected_env)
        if value["proc"] is not None:
            self.assertNotIn(SECRET, value["proc"])
            self.assertNotIn(SECRET_NAME, value["proc"])

    def test_a4_nothing_runs_before_hello(self):
        replies, _, reads, _ = self.exchange(load(), call(7, command={}), HELLO, load(), call(8, command="after"), BYE)
        self.assert_refusal(replies[0], None, "message_invalid", "expected hello")
        self.assert_refusal(replies[1], 7, "message_invalid", "expected hello")
        self.assertEqual(replies[2:], [result(8, replies[2]["value"])])
        self.assertEqual(replies[2]["value"]["command"], "after")
        self.assertEqual(reads, 2)  # only the load and call after hello were worked

    def test_a4_a_host_takes_one_plugin_id_and_one_load(self):
        failing = b"raise RuntimeError('private')\n"
        replies, _, _, _ = self.exchange(
            HELLO, load(failing), load(plugin_id="other.plugin"), load(), load(), call(7, command="ok"), BYE,
        )
        self.assert_refusal(replies[0], None, "call_failed", "RuntimeError")  # a failed load may be retried
        self.assert_refusal(replies[1], None, "message_invalid")  # another plugin id
        self.assert_refusal(replies[2], None, "message_invalid")  # a second load after one succeeded
        self.assertEqual(len(replies), 4)
        self.assertEqual(replies[3]["value"]["module"], "csl_plugin_demo_plugin")

    def test_a4_core_requests_are_refused_off_the_plugin_thread(self):
        source = textwrap.dedent("""
            import threading

            def bind_core(client):
                global core_call
                core_call = client

            def operator(command):
                seen = []
                def other():
                    try:
                        core_call(b'x')
                    except Exception as exc:
                        seen.append(type(exc).__name__)
                    else:
                        seen.append('sent')
                thread = threading.Thread(target=other)
                thread.start()
                thread.join()
                return seen
            """).encode()
        replies, _, _, _ = self.exchange(HELLO, load(source), call(7, command={}), call(8, command={}), BYE)
        self.assertEqual(replies, [result(7, ["RuntimeError"]), result(8, ["RuntimeError"])])

    def assert_no_canary(self, seen):
        for place in ("environ", "cmdline", "proc"):
            self.assertNotIn(CANARY, str(seen[place]), place)
            self.assertNotIn(CANARY_NAME, str(seen[place]), place)
        for task in seen["tasks"]:
            self.assertNotIn(CANARY, task)
            self.assertNotIn(CANARY_NAME, task)

    @unittest.skipIf(os.name == "nt", "the restart is POSIX only")
    @require_proc("/proc/self/environ", "/proc/self/cmdline")
    def test_n1_a_canary_is_not_readable_however_the_starter_sets_the_marker(self):
        # The host goes on only when the environment block holds nothing but the marker; it never
        # takes the marker alone as the answer. Each form below gets one restart.
        forms = {"marker absent": {}, "marker 1": {MARKER: "1"}, "marker 0": {MARKER: "0"},
                 "marker yes": {MARKER: "yes"}, "marker 2": {MARKER: "2"}, "marker empty": {MARKER: ""}}
        for label, marker in forms.items():
            with self.subTest(form=label):
                replies, _, _, _ = self.exchange(
                    HELLO, load(ENV_SOURCE), call(7, "registry"), BYE,
                    environ=dict(marker, **{CANARY_NAME: CANARY, "PATH": "/stand/in", "HOME": "/stand/in"}))
                self.assertEqual(len(replies), 1, replies)
                self.assertEqual(replies[0]["type"], "result", replies[0])
                self.assert_no_canary(replies[0]["value"])
                self.assertEqual(replies[0]["value"]["proc"], MARKER + "=1\0")
                self.assertEqual(self.starts, 2)

    @unittest.skipIf(os.name == "nt", "the restart is POSIX only")
    @require_proc("/proc/self/environ", "/proc/self/cmdline")
    def test_n1_an_environment_that_holds_only_the_marker_or_nothing_is_not_restarted(self):
        for label, environ in (("marker only", {MARKER: "1"}), ("nothing", {})):
            with self.subTest(form=label):
                replies, _, _, _ = self.exchange(
                    HELLO, load(ENV_SOURCE), call(7, "registry"), BYE, environ=environ)
                self.assertEqual(self.starts, 1)
                self.assertEqual(replies[0]["value"]["environ"], {})
                self.assertIn(replies[0]["value"]["proc"], ("", MARKER + "=1\0"))

    @unittest.skipIf(os.name == "nt", "the restart is POSIX only")
    @require_proc("/proc/self/fd", "/proc/self/environ")
    def test_n1_descriptors_after_the_restart_are_the_channels_and_nothing_more(self):
        with tempfile.TemporaryFile() as extra:
            replies, _, _, identity = self.exchange(
                HELLO, load(FDS_SOURCE), call(7, "registry"), BYE, extra=extra.fileno(),
                environ={MARKER: "1", CANARY_NAME: CANARY})
        value = replies[0]["value"]
        self.assertEqual(len(value["fds"]), 5)  # 0, 1, 2 and the channel's two descriptors
        self.assertNotIn(identity, value["fds"].values())
        self.assertNotIn(CANARY, value["proc"])
        self.assertEqual(self.starts, 2)

    @unittest.skipIf(os.name == "nt", "the restart is POSIX only")
    def test_n1_an_environment_that_never_comes_out_clean_ends_in_one_typed_refusal(self):
        ran = self.root / "plugin-ran"
        source = ("open(%r, 'w').write('x')\ndef registry():\n    return {}\n" % str(ran)).encode()
        replies, _, _, _ = self.exchange(
            HELLO, load(source), call(7, "registry"), BYE,
            environ={MARKER: "1", CANARY_NAME: CANARY}, standin="dirty", code=EXIT_ENVIRONMENT_NOT_CLEAN)
        self.assertEqual(len(replies), 1)
        self.assert_refusal(replies[0], None, "host_start_refused", "environment_not_clean")
        self.assertEqual(self.starts, 2)  # one restart, then the refusal: bounded by the mechanism
        self.assertFalse(ran.exists(), "plugin code ran in a host that was not clean")
        self.assertEqual(self.stderr, b"")

    @unittest.skipIf(os.name == "nt", "the restart is POSIX only")
    def test_n2_a_restart_that_cannot_be_made_is_a_typed_refusal_and_a_named_exit(self):
        ran = self.root / "plugin-ran-2"
        source = ("open(%r, 'w').write('x')\ndef registry():\n    return {}\n" % str(ran)).encode()
        replies, _, _, _ = self.exchange(
            HELLO, load(source), call(7, "registry"), BYE,
            environ={CANARY_NAME: CANARY}, standin="fail", code=EXIT_RESTART_FAILED)
        self.assertEqual(len(replies), 1)
        self.assert_refusal(replies[0], None, "host_start_refused", "restart_failed")
        self.assertEqual(self.starts, 1)
        self.assertFalse(ran.exists())
        self.assertEqual(self.stderr, b"")  # no traceback on the starter's stream
        from recursion_core import plugin_host
        self.assertEqual((plugin_host.EXIT_RESTART_FAILED, plugin_host.EXIT_ENVIRONMENT_NOT_CLEAN),
                         (EXIT_RESTART_FAILED, EXIT_ENVIRONMENT_NOT_CLEAN))
        self.assertEqual(plugin_host.START_REFUSED, "host_start_refused")

    @unittest.skipIf(os.name == "nt", "the restart is POSIX only")
    def test_n1_a_host_started_by_bare_name_restarts_into_the_same_environment(self):
        # The starter finds the interpreter through a PATH that names only the environment's own bin
        # folder. The restart gives the new image no PATH, so the new image can keep the environment's
        # prefix only when its first word is the interpreter's own path.
        path = {"PATH": str(self.python.parent)}
        replies, _, _, _ = self.exchange(HELLO, BYE, environ=path, bare=True)
        self.assertEqual(replies, [])
        self.assertEqual(self.starts, 2)
        expected = os.path.realpath(self.python.parent.parent)
        for label, bare in (("bare name", True), ("absolute path", False)):
            with self.subTest(started_by=label):
                replies, _, _, _ = self.exchange(
                    HELLO, load(PREFIX_SOURCE), call(7, "registry"), BYE, environ=path, bare=bare)
                self.assertEqual(self.starts, 2)
                self.assertEqual(len(replies), 1, replies)
                self.assertEqual(replies[0]["type"], "result", replies[0])
                value = replies[0]["value"]
                self.assertEqual(os.path.realpath(value["prefix"]), expected)
                self.assertEqual(value["executable"], str(self.python))
                self.assertEqual(value["argv"], [str(self.python), "-X", "gil=0", "-I", "-m",
                                                 "recursion_core.plugin_host", RESTARTED])
                if value["proc"] is not None:
                    self.assertEqual(value["proc"], MARKER + "=1\0")

    @unittest.skipIf(os.name == "nt", "the restart is POSIX only")
    @require_proc("/proc/self/environ", "/proc/self/cmdline")
    def test_n1_a_marker_with_another_value_is_not_clean_even_alone(self):
        for value in ("0", "yes", "2", ""):
            with self.subTest(marker=value):
                replies, _, _, _ = self.exchange(
                    HELLO, load(ENV_SOURCE), call(7, "registry"), BYE, environ={MARKER: value})
                self.assertEqual(self.starts, 2)
                self.assertEqual(replies[0]["value"]["proc"], MARKER + "=1\0")

    @unittest.skipIf(os.name == "nt", "the restart is POSIX only")
    def test_n1_without_the_kernel_record_the_host_restarts_once_and_then_reads_os_environ(self):
        plugin = b"import os\ndef registry():\n    return dict(os.environ)\n"
        # The host cannot see what the block holds, so it always restarts once and then accepts only
        # os.environ with the marker alone (the locale name the interpreter may add is set aside).
        forms = ({MARKER: "1"}, {MARKER: "1", CANARY_NAME: CANARY}, {"LC_CTYPE": "stand-in-locale"})
        for environ in forms:
            with self.subTest(environ=environ):
                replies, _, _, _ = self.exchange(
                    HELLO, load(plugin), call(7, "registry"), BYE, environ=environ, standin="norecord")
                self.assertEqual(self.starts, 2)
                self.assertEqual(replies[0]["value"], {})
        # An image that is not clean after its one restart is refused, record or no record.
        replies, _, _, _ = self.exchange(
            HELLO, load(plugin), call(7, "registry"), BYE, environ={CANARY_NAME: CANARY},
            standin="dirty+norecord", code=EXIT_ENVIRONMENT_NOT_CLEAN)
        self.assert_refusal(replies[0], None, "host_start_refused", "environment_not_clean")
        self.assertEqual(self.starts, 2)

    # THE WALL. The host walls itself in before it reads any message, proves the wall from inside, sends
    # the record in its hello, and answers every load with plugin_wall_unproven when the record is not
    # proven. The stand-in home holds a canary; the plugin below tries to read it and to make sockets. The
    # host's own probe reads a canary file of its own in its working folder.

    def stand_in_home(self):
        home = self.root / ("home-%d" % next(self.counter))
        home.mkdir()
        canary = home / "canary.txt"
        canary.write_text(CANARY, encoding="ascii")
        return home, canary

    def test_w1_a_walled_host_keeps_its_plugin_from_a_canary_and_a_socket_and_still_answers(self):
        why = _why_no_wall()
        if why:
            self.skipTest(why)
        home, canary = self.stand_in_home()
        replies, _, _, _ = self.exchange(HELLO, load(WALL_SOURCE), call(7, command={"canary": str(canary)}), BYE,
                                         wall="real")
        self.assertEqual(replies, [result(7, {"canary": "PermissionError", "inet": "PermissionError",
                                              "unix": "PermissionError"})])
        record = self.hello.get("wall")
        self.assertIsInstance(record, dict, self.hello)
        self.assertEqual({name: record[name] for name in RECORD_CHECKS}, dict.fromkeys(RECORD_CHECKS, True))
        self.assertIs(record["proven"], True)
        self.assertEqual(self.canaries_left, [])
        # The same walled host still answers an ordinary read command, as the in-process loader does.
        loader = PluginLoader(TrustStore({}), {}, ReservedEffects(), CoreHealth(), FakeGilProbe())
        plugin = loader.load(manifest_bytes("01" * 32, source=SOURCE), None, SOURCE)
        command = {"command": "positions", "terms": [1, False, None]}
        replies, _, _, _ = self.exchange(HELLO, load(), call(7, command=command), BYE, wall="real")
        self.assertIs(self.hello["wall"]["proven"], True)
        self.assertEqual(replies, [result(7, plugin.module.operator(command))])

    @unittest.skipUnless(sys.platform.startswith("linux"), "the stand-in kernel answer is Linux's")
    def test_w2_with_no_landlock_the_record_is_not_proven_and_no_plugin_byte_runs(self):
        home, _ = self.stand_in_home()
        ran = home / "plugin-ran"
        source = SOURCE + b"open(%r, 'w').close()\n" % str(ran).encode()
        replies, _, _, _ = self.exchange(HELLO, load(source), call(7, command={"command": "positions"}), load(source),
                                         BYE, wall="nolandlock")
        self.assertEqual(len(replies), 3, replies)
        for reply in (replies[0], replies[2]):
            self.assert_refusal(reply, None, "plugin_wall_unproven")
            for missing in ("landlock_unavailable", "reads_outside", "proven"):
                self.assertIn(missing, reply["detail"])
        self.assert_refusal(replies[1], 7, "call_not_offered")
        self.assertFalse(ran.exists(), "plugin code ran")
        record = self.hello.get("wall")
        self.assertIsInstance(record, dict, self.hello)
        self.assertIs(record["proven"], False)
        self.assertEqual(record["landlock_abi"], 0)

    @unittest.skipUnless(sys.platform.startswith("linux"), "the stand-in kernel answer is Linux's")
    def test_w3_no_message_manifest_field_file_or_setting_skips_the_wall(self):
        home, _ = self.stand_in_home()
        ran = home / "plugin-ran"
        source = SOURCE + b"open(%r, 'w').close()\n" % str(ran).encode()
        asking = load(source)
        manifest = json.loads(base64.b64decode(asking["manifest_b64"]))
        manifest.update(wall="none", sandbox="none", runtime="python-inprocess/1")
        asking["manifest_b64"] = b64(json.dumps(manifest).encode())
        config = home / "config"
        for place in (config / "settings.toml", config / "recursion" / "settings.toml",
                      home / ".config" / "recursion" / "settings.toml"):
            place.parent.mkdir(parents=True, exist_ok=True)
            place.write_text('[plugin_host]\nwall = "none"\n', encoding="utf-8")
        files = {name: 'wall = "none"\n' for name in
                 ("settings.toml", "recursion.toml", ".recursion/settings.toml", "plugin.toml", "wall.toml")}
        environ = {"PYTHONPATH": str(CORE), "HOME": str(home), "XDG_CONFIG_HOME": str(config),
                   "CSL_PLUGIN_WALL": "none", "CSL_PLUGIN_HOST_WALL": "off"}
        claimed = dict(STANDIN_RECORD)
        replies, _, _, _ = self.exchange(
            dict(HELLO, wall=claimed), dict(HELLO, gil_check="off"), dict(claimed, type="wall"), asking,
            call(7, command={"command": "positions"}), load(source), BYE,
            wall="nolandlock", environ=environ, files=files)
        self.assertEqual([(reply["type"], reply["code"]) for reply in replies],
                         [("refusal", "message_invalid"), ("refusal", "message_unknown"),
                          ("refusal", "plugin_wall_unproven"), ("refusal", "call_not_offered"),
                          ("refusal", "plugin_wall_unproven")], replies)
        self.assertFalse(ran.exists(), "plugin code ran")
        self.assertIs(self.hello["wall"]["proven"], False)

    def test_w4_without_the_two_calls_the_host_sends_no_record_and_refuses_every_load(self):
        from recursion_core import plugin_host

        with unittest.mock.patch.object(sys, "platform", "darwin"):
            record, refused = plugin_host._wall()  # the wall's first step refuses before any change
        self.assertIsNone(record)
        self.assertEqual(refused, "wall_system_unsupported")
        sent = []

        class Channel:
            inbox = [HELLO, load(), call(7, command={"command": "positions"}), BYE]

            def receive(self):
                return self.inbox.pop(0) if self.inbox else None

            def send(self, message):
                sent.append(hc.check_message(message, receiver="daemon"))

        plugin_host._Program(Channel(), FakeGilProbe(), (record, refused)).serve()
        self.assertEqual(sent[0], HELLO)
        self.assert_refusal(sent[1], None, "plugin_wall_unproven",
                            "the wall around this host is not proven, so it loads nothing and runs no plugin "
                            "code; missing: wall_system_unsupported, no_record")
        self.assert_refusal(sent[2], 7, "call_not_offered")
        self.assertEqual(len(sent), 3, sent)

    def test_w5_a_wall_that_leaves_file_reads_open_is_not_proven_and_no_plugin_byte_runs(self):
        why = _why_no_wall()
        if why:
            self.skipTest(why)
        _, canary = self.stand_in_home()
        replies, _, _, _ = self.exchange(HELLO, load(WALL_SOURCE), call(7, command={"canary": str(canary)}),
                                         load(WALL_SOURCE), BYE, wall="noreadfile")
        self.assertEqual([(reply["type"], reply.get("code")) for reply in replies],
                         [("refusal", "plugin_wall_unproven"), ("refusal", "call_not_offered"),
                          ("refusal", "plugin_wall_unproven")], replies)
        for reply in (replies[0], replies[2]):
            self.assert_refusal(reply, None, "plugin_wall_unproven")
            self.assertIn("home_canary", reply["detail"])
            self.assertTrue(reply["detail"].endswith(", proven"), reply["detail"])
        record = self.hello.get("wall")
        self.assertIsInstance(record, dict, self.hello)
        self.assertIs(record["home_canary"], False)
        self.assertIs(record["proven"], False)
        self.assertEqual(self.canaries_left, [])

    def test_w6_a_wall_that_leaves_writes_open_is_not_proven_and_no_plugin_byte_runs(self):
        why = _why_no_wall()
        if why:
            self.skipTest(why)
        from recursion_core.plugin_host import wall_linux
        for wall in ("nowrite", "notruncate"):
            with self.subTest(wall=wall), tempfile.TemporaryDirectory(dir=self.root) as outside:
                victim = Path(outside, "yours.txt")
                victim.write_text("a person's text, stand-in", encoding="ascii")
                source = WRITE_SOURCE.replace(b"VICTIM", repr(str(victim)).encode())
                replies, _, _, _ = self.exchange(HELLO, load(source), call(7, command={}), load(source), BYE,
                                                 wall=wall)
                self.assertEqual([(reply["type"], reply.get("code")) for reply in replies],
                                 [("refusal", "plugin_wall_unproven"), ("refusal", "call_not_offered"),
                                  ("refusal", "plugin_wall_unproven")], replies)
                self.assertEqual(victim.read_text(encoding="ascii"), "a person's text, stand-in",
                                 "no plugin byte ran, so the file outside is whole")
                record = self.hello["wall"]
                self.assertIs(record["proven"], False)
                held = ("refused_by_landlock", "refused_by_filter")
                self.assertEqual([kind for kind in WRITE_KINDS if record["writes"][kind] not in held],
                                 ["truncate"] if wall == "notruncate" else list(WRITE_KINDS))
                for reply in (replies[0], replies[2]):
                    detail = reply["detail"]
                    self.assertLessEqual(len(detail), 1000)
                    self.assertTrue(detail.startswith(wall_linux.WORDS["not_loaded"]), detail)
                    self.assertIn(wall_linux.WORDS["let_through"].format(what=wall_linux.WORDS["truncate"]), detail)
                    self.assertIn("writes.truncate", detail)
                    self.assertTrue(detail.endswith(", proven"), detail)
                self.assertEqual(self.canaries_left, [], "the canary and the write folder are gone")

    def test_w7_a_canary_left_behind_refuses_every_load(self):
        why = _why_no_wall()
        if why:
            self.skipTest(why)
        from recursion_core.plugin_host import wall_linux
        replies, _, _, _ = self.exchange(HELLO, load(WALL_SOURCE), BYE, wall="keepcanary")
        self.assertEqual([(reply["type"], reply.get("code")) for reply in replies],
                         [("refusal", "plugin_wall_unproven")], replies)
        self.assertIs(self.hello["wall"]["proven"], True, "the wall itself holds")
        self.assertIn("canary_left", replies[0]["detail"])
        self.assertIn(wall_linux.WORDS["canary_left"], replies[0]["detail"])
        self.assertEqual(len(self.canaries_left), 2, "the canary and the write folder, both named")

    def test_w8_a_folder_already_there_by_the_write_folders_name_stays_whole_and_every_load_is_refused(self):
        why = _why_no_wall()
        if why:
            self.skipTest(why)
        replies, _, _, _ = self.exchange(HELLO, load(WALL_SOURCE), BYE, wall="folderthere")
        self.assertEqual([(reply["type"], reply.get("code")) for reply in replies],
                         [("refusal", "plugin_wall_unproven")], replies)
        record = self.hello["wall"]
        self.assertIs(record["home_canary"], False)
        self.assertIs(record["proven"], False)
        self.assertEqual(len(self.canaries_left), 1, self.canaries_left)
        name = self.canaries_left[0]
        self.assertTrue(name.endswith(".d"), name)
        self.assertEqual(self.left_inside, {name: {"kept.txt": KEPT}}, "the folder and its file, as they were")


# The plugin of the write test: at load it cuts a file outside its folder to nothing (VICTIM is its path).
WRITE_SOURCE = textwrap.dedent("""
    import os
    try:
        os.truncate(VICTIM, 0)
    except OSError:
        pass

    def operator(command):
        return {}
    """).encode()


def _why_no_wall():
    if not sys.platform.startswith("linux"):
        return "the wall is Linux-only"
    from recursion_core.plugin_host.tests.test_wall_linux import _why_not_here

    return _why_not_here()


# The plugin of the wall tests: it tries to read the canary it is given and to make an AF_INET and an
# AF_UNIX socket, and says what happened to each.
WALL_SOURCE = textwrap.dedent("""
    import socket

    def operator(command):
        seen = {}
        try:
            with open(command['canary'], 'rb') as record:
                seen['canary'] = record.read().decode('ascii')
        except OSError as exc:
            seen['canary'] = type(exc).__name__
        for name, family in (('inet', socket.AF_INET), ('unix', socket.AF_UNIX)):
            try:
                socket.socket(family, socket.SOCK_STREAM).close()
                seen[name] = 'made'
            except OSError as exc:
                seen[name] = type(exc).__name__
        return seen
    """).encode()

RECORD_CHECKS = ("reads_outside", "home_canary", "program_start", "new_socket", "signal_other",
                 "signal_other_ways", "metadata_change", "kernel_surface")
# A proven record as the stand-in wall gives it (and as a daemon might claim one in its own hello).
WRITE_KINDS = ("open_for_write", "truncate", "create_file", "create_folder", "make_fifo", "make_symlink",
               "make_link", "make_char_device", "make_block_device", "remove_file", "remove_folder")
STANDIN_RECORD = dict(dict.fromkeys(RECORD_CHECKS, True), writes=dict.fromkeys(WRITE_KINDS, "refused_by_landlock"),
                      writes_not_tried={"make_socket_node": "stand-in", "device_ioctl": "stand-in",
                                        "move_between_folders": "stand-in"},
                      landlock_abi=6, landlock_scopes=["abstract_unix_socket", "signal"], arch="x86_64", proven=True)
# The wall each exchange gives its host, as lines run inside the host before main. "standin": no wall
# and a proven stand-in record, for the tests of the channel, the environment and the GIL rule, several of
# whose plugins read /proc, which a real wall refuses. "real": the host's own wall on this kernel.
# "nolandlock": the kernel answers that it has no Landlock, so apply_wall refuses before any change.
# "noreadfile": a wall whose Landlock step leaves file reads unhandled, so every file stays readable while
# folders, writes, sockets, programs and signals stay walled as before.
WALL_SHIMS = {
    "standin": ("from recursion_core.plugin_host import wall_linux as _wall_linux\n"
                "_wall_linux.apply_wall = lambda paths: None\n"
                f"_wall_linux.probe_wall = lambda canary, pid, **writes: {STANDIN_RECORD!r}\n"),
    "real": "",
    "nolandlock": ("from recursion_core.plugin_host import wall_linux as _wall_linux\n"
                   "_wall_linux._landlock_abi = lambda libc: 0\n"),
    "noreadfile": ("from recursion_core.plugin_host import wall_linux as _wall_linux\n"
                   "_wall_linux._FS_ABI1 &= ~_wall_linux._FS_READ_FILE\n"
                   "_wall_linux._FS_READ_FILE = 0\n"),
    # "nowrite": a wall whose Landlock step leaves every write right unhandled (reads stay walled).
    "nowrite": ("from recursion_core.plugin_host import wall_linux as _wall_linux\n"
                "_wall_linux._FS_ABI1 = (1 << 0) | (1 << 2) | (1 << 3)\n"
                "_wall_linux._FS_LATER = {5: 1 << 15}\n"),
    # "notruncate": a wall whose Landlock step leaves only truncation unhandled.
    "notruncate": ("from recursion_core.plugin_host import wall_linux as _wall_linux\n"
                   "_wall_linux._FS_LATER = {2: 1 << 13, 5: 1 << 15}\n"),
    # "keepcanary": the real wall, and a helper that leaves the canary and the write folder behind.
    "keepcanary": "plugin_host._remove_probe_files = lambda path, folder: None\n",
    # "folderthere": the real wall, and a folder already in the working folder by the name of the host's write
    # folder (made here, before main, for the host's own process id), holding a file.
    "folderthere": ("_there = plugin_host.CANARY_PREFIX + str(os.getpid()) + plugin_host.WRITE_FOLDER_SUFFIX\n"
                    "if not os.path.lexists(_there):\n"
                    "    os.mkdir(_there)\n"
                    f"    open(os.path.join(_there, 'kept.txt'), 'wb').write({KEPT!r})\n"),
}
