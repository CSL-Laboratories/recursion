"""WSL probe contract. Red on master: the package is not there, so importing it fails.

Fake roots only, except the one live test marked wsl_live, which is skipped off WSL.
The kernel and binfmt strings are the ones read on this host (osrelease
``6.6.114.1-microsoft-standard-WSL2``, binfmt ``enabled`` / ``interpreter /init`` /
``flags: P``).
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path
import tempfile
import threading
import unittest

from recursion_core.wsl.probe import Record, read

# This host's kernel string, and the matching /proc/version line.
HOST_OSRELEASE = '6.6.114.1-microsoft-standard-WSL2\n'
HOST_VERSION = (
    'Linux version 6.6.114.1-microsoft-standard-WSL2 (root@507f3e43091d) '
    '(gcc (GCC) 13.2.0, GNU ld (GNU Binutils) 2.41) '
    '#1 SMP PREEMPT_DYNAMIC Mon Dec  1 20:46:23 UTC 2025\n'
)
HOST_BINFMT = 'enabled\ninterpreter /init\nflags: P\n'

# A WSL1 root names Microsoft and does not name the WSL2 kernel.
WSL1_OSRELEASE = '4.4.0-19041-Microsoft\n'
WSL1_VERSION = (
    'Linux version 4.4.0-19041-Microsoft (Microsoft@Microsoft.com) '
    '(gcc version 5.4.0 (GCC)) #1 SMP Mon Dec 16 00:00:00 PST 2019\n'
)

UBUNTU_OSRELEASE = '6.8.0-45-generic\n'
UBUNTU_VERSION = (
    'Linux version 6.8.0-45-generic (buildd@lcy02-amd64-001) '
    '(x86_64-linux-gnu-gcc-13 (Ubuntu 13.2.0-23ubuntu3) 13.2.0, GNU ld '
    '(GNU Binutils for Ubuntu) 2.42) '
    '#45-Ubuntu SMP PREEMPT_DYNAMIC Fri Nov  1 12:00:00 UTC 2024\n'
)

FIELDS = (
    'kind', 'systemd_pid1', 'interop', 'wslconf_interop', 'wslconf_systemd', 'automount_root',
)


def _host_is_wsl() -> bool:
    try:
        text = Path('/proc/sys/kernel/osrelease').read_text(encoding='utf-8')
    except OSError:
        return False
    return 'microsoft' in text.lower()


def _plant(root: Path, relative: str, text: str, mode: int = 0o644) -> Path:
    path = root.joinpath(*relative.split('/'))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    os.chmod(path, mode)
    return path


def _plant_unreadable(root: Path, relative: str) -> Path:
    """A node whose text cannot be read, including when the process is root."""
    path = _plant(root, relative, 'enabled\ninterpreter /init\nflags: P\n', 0)
    try:
        path.read_text(encoding='utf-8')
    except OSError:
        return path
    path.unlink()
    path.mkdir()
    return path


def _unlock(root: Path) -> None:
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        os.chmod(current, 0o755)
        for name in dirnames:
            try:
                os.chmod(os.path.join(current, name), 0o755)
            except OSError:
                pass
        for name in filenames:
            try:
                os.chmod(os.path.join(current, name), 0o644)
            except OSError:
                pass


class _Env(dict):
    """Mapping whose membership test fails on the second check."""

    def __init__(self, exc: BaseException):
        super().__init__()
        self._exc = exc
        self.checks = 0

    def __contains__(self, key: object) -> bool:
        self.checks += 1
        if self.checks >= 2:
            raise self._exc
        return False


class ProbeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        _unlock(self.root)
        self._tmp.cleanup()

    def fresh(self):
        """A new root. A second read of the same root is the process cache."""
        _unlock(self.root)
        self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def identity(self, osrelease: str, version: str, comm: str = 'systemd\n'):
        _plant(self.root, 'proc/sys/kernel/osrelease', osrelease)
        _plant(self.root, 'proc/version', version)
        _plant(self.root, 'proc/1/comm', comm)

    def wsl2(self, comm: str = 'systemd\n'):
        self.identity(HOST_OSRELEASE, HOST_VERSION, comm)
        return self.root

    def wsl1(self):
        self.identity(WSL1_OSRELEASE, WSL1_VERSION, 'init\n')
        return self.root

    def ubuntu(self):
        self.identity(UBUNTU_OSRELEASE, UBUNTU_VERSION, 'systemd\n')
        return self.root

    def read(self, environ: dict | None = None) -> Record:
        # An explicit mapping: the process environment is not part of a fake root.
        return read(self.root, {} if environ is None else environ)


class KindTests(ProbeCase):
    def test_record_fields_and_signature(self):
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', HOST_BINFMT)
        record = self.read()
        self.assertIsInstance(record, Record)
        self.assertEqual(tuple(Record.__dataclass_fields__), FIELDS)
        self.assertTrue(record.__dataclass_params__.frozen)
        signature = inspect.signature(read)
        self.assertEqual(signature.parameters['root'].default, Path('/'))
        self.assertIs(signature.parameters['environ'].default, os.environ)
        with self.assertRaises(AttributeError):
            record.kind = 'none'  # type: ignore[misc]

    def test_host_wsl2_strings_are_kind_wsl2(self):
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', HOST_BINFMT)
        record = self.read()
        self.assertEqual(record.kind, 'wsl2')
        self.assertTrue(record.systemd_pid1)
        self.assertEqual(record.interop, 'on')

    def test_wsl1_root_names_microsoft_and_not_wsl2(self):
        self.wsl1()
        record = self.read()
        self.assertEqual(record.kind, 'wsl1')
        self.assertFalse(record.systemd_pid1)
        self.assertEqual(record.interop, 'off')

    def test_plain_ubuntu_is_kind_none_and_interop_off(self):
        self.ubuntu()
        record = self.read()
        self.assertEqual(record.kind, 'none')
        self.assertTrue(record.systemd_pid1)
        self.assertEqual(record.interop, 'off')
        self.assertEqual(record.wslconf_interop, 'absent')
        self.assertEqual(record.wslconf_systemd, 'absent')
        self.assertEqual(record.automount_root, 'absent')

    def test_microsoft_standard_without_the_wsl2_suffix_is_wsl2(self):
        # Early WSL2 kernels use this name. Labelling it wsl1 would understate the kind.
        self.identity('5.15.153.1-microsoft-standard\n',
                      'Linux version 5.15.153.1-microsoft-standard\n')
        self.assertEqual(self.read().kind, 'wsl2')

    def test_wsl2_in_either_identity_file_is_wsl2(self):
        self.identity(UBUNTU_OSRELEASE, HOST_VERSION)
        self.assertEqual(self.read().kind, 'wsl2')
        self.fresh()
        self.identity(HOST_OSRELEASE, UBUNTU_VERSION)
        self.assertEqual(self.read().kind, 'wsl2')

    def test_microsoft_in_either_identity_file_is_wsl1(self):
        self.identity(UBUNTU_OSRELEASE, WSL1_VERSION)
        self.assertEqual(self.read().kind, 'wsl1')
        self.fresh()
        self.identity(WSL1_OSRELEASE, UBUNTU_VERSION)
        self.assertEqual(self.read().kind, 'wsl1')

    def test_missing_identity_file_does_not_invent_wsl(self):
        _plant(self.root, 'proc/version', UBUNTU_VERSION)
        _plant(self.root, 'proc/1/comm', 'systemd\n')
        self.assertEqual(self.read().kind, 'none')

    def test_unreadable_identity_is_not_recorded_as_native(self):
        self.ubuntu()
        _plant_unreadable(self.root, 'proc/sys/kernel/osrelease')
        _plant(self.root, 'proc/version', UBUNTU_VERSION)
        self.assertEqual(self.read().kind, 'wsl1')
        self.fresh()
        self.ubuntu()
        _plant_unreadable(self.root, 'proc/sys/kernel/osrelease')
        _plant_unreadable(self.root, 'proc/version')
        self.assertEqual(self.read().kind, 'wsl1')

    def test_unreadable_identity_does_not_hide_a_wsl2_sibling(self):
        self.ubuntu()
        _plant_unreadable(self.root, 'proc/sys/kernel/osrelease')
        _plant(self.root, 'proc/version', HOST_VERSION)
        self.assertEqual(self.read().kind, 'wsl2')


class InteropTests(ProbeCase):
    def test_host_binfmt_lines_are_on(self):
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', HOST_BINFMT)
        record = self.read()
        self.assertEqual(record.interop, 'on')
        self.assertEqual(record.kind, 'wsl2')

    def test_each_source_alone_is_on(self):
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', HOST_BINFMT)
        self.assertEqual(self.read().interop, 'on')

        self.fresh()
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', 'disabled\n')
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop-late', HOST_BINFMT)
        self.assertEqual(self.read().interop, 'on')

        self.fresh()
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', 'disabled\n')
        (self.root / 'run' / 'WSL').mkdir(parents=True)
        self.assertEqual(self.read().interop, 'on')

        self.fresh()
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', 'disabled\n')
        self.assertEqual(self.read({'WSL_INTEROP': ''}).interop, 'on')
        self.assertEqual(self.read({'WSL_INTEROP': '/run/WSL/1_interop'}).interop, 'on')

    def test_lowercase_run_directory_is_not_interop(self):
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', 'disabled\n')
        (self.root / 'run' / 'wsl').mkdir(parents=True)
        self.assertEqual(self.read().interop, 'off')

    def test_symlink_at_run_wsl_is_on(self):
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', 'disabled\n')
        target = self.root / 'run' / 'wsl'
        target.mkdir(parents=True)
        (self.root / 'run' / 'WSL').symlink_to(target, target_is_directory=True)
        self.assertEqual(self.read().interop, 'on')

    def test_unreadable_binfmt_is_unknown_on_ubuntu_and_on_wsl2(self):
        self.ubuntu()
        _plant_unreadable(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop')
        record = self.read()
        self.assertEqual(record.kind, 'none')
        self.assertEqual(record.interop, 'unknown')

        self.fresh()
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', 'disabled\n')
        _plant_unreadable(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop-late')
        self.assertEqual(self.read().interop, 'unknown')

    def test_unreadable_run_directory_is_unknown(self):
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', 'disabled\n')
        _plant(self.root, 'run', 'not-a-directory\n')
        self.assertEqual(self.read().interop, 'unknown')

    def test_a_positive_source_stays_on_when_another_source_cannot_be_read(self):
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', HOST_BINFMT)
        _plant_unreadable(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop-late')
        self.assertEqual(self.read().interop, 'on')
        self.assertEqual(self.read({'WSL_INTEROP': '/run/WSL/2_interop'}).interop, 'on')

    def test_disabled_binfmt_and_no_other_source_is_off(self):
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', 'disabled\ninterpreter /init\nflags: P\n')
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop-late', 'disabled\n')
        record = self.read()
        self.assertEqual(record.interop, 'off')
        self.assertEqual(record.kind, 'wsl2')

    def test_config_does_not_override_the_live_interop_fact(self):
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', HOST_BINFMT)
        _plant(self.root, 'etc/wsl.conf', '[interop]\nenabled = false\n')
        record = self.read()
        self.assertEqual(record.interop, 'on')
        self.assertEqual(record.wslconf_interop, 'false')

        self.fresh()
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', 'disabled\n')
        _plant(self.root, 'etc/wsl.conf', '[interop]\nenabled = true\n')
        record = self.read()
        self.assertEqual(record.interop, 'off')
        self.assertEqual(record.wslconf_interop, 'true')


class SystemdTests(ProbeCase):
    def test_comm_systemd_is_true_and_init_is_false(self):
        self.ubuntu()
        self.assertTrue(self.read().systemd_pid1)
        self.fresh()
        self.ubuntu()
        _plant(self.root, 'proc/1/comm', 'init\n')
        self.assertFalse(self.read().systemd_pid1)
        self.fresh()
        self.ubuntu()
        _plant(self.root, 'proc/1/comm', 'systemd\x00\n')
        self.assertTrue(self.read().systemd_pid1)

    def test_missing_or_unreadable_comm_is_not_false(self):
        self.ubuntu()
        (self.root / 'proc' / '1' / 'comm').unlink()
        self.assertIsNone(self.read().systemd_pid1)
        self.fresh()
        self.ubuntu()
        _plant_unreadable(self.root, 'proc/1/comm')
        self.assertIsNone(self.read().systemd_pid1)

    def test_a_longer_comm_name_is_not_systemd(self):
        self.ubuntu()
        _plant(self.root, 'proc/1/comm', 'systemd-journal\n')
        self.assertFalse(self.read().systemd_pid1)


class WslConfTests(ProbeCase):
    def test_parsed_boot_interop_and_automount_root(self):
        self.wsl2()
        _plant(self.root, 'etc/wsl.conf', (
            '# comment\n'
            '[Boot]\n'
            'Systemd = True\n'
            '\n'
            '[Interop]\n'
            'Enabled = False\n'
            '\n'
            '[automount]\n'
            'root = "/mnt/host" # drives\n'
            'enabled = true\n'
            '\n'
            '[user]\n'
            'default = operator\n'
        ))
        record = self.read()
        self.assertEqual(record.wslconf_systemd, 'true')
        self.assertEqual(record.wslconf_interop, 'false')
        self.assertEqual(record.automount_root, '/mnt/host')

    def test_absent_file_and_empty_file_are_absent(self):
        self.ubuntu()
        record = self.read()
        self.assertEqual(record.wslconf_interop, 'absent')
        self.assertEqual(record.wslconf_systemd, 'absent')
        self.assertEqual(record.automount_root, 'absent')
        self.fresh()
        self.ubuntu()
        _plant(self.root, 'etc/wsl.conf', '\n# nothing\n')
        record = self.read()
        self.assertEqual(record.wslconf_systemd, 'absent')
        self.assertEqual(record.automount_root, 'absent')

    def test_unreadable_file_is_unreadable_for_every_config_field(self):
        self.wsl2()
        _plant_unreadable(self.root, 'etc/wsl.conf')
        record = self.read()
        self.assertEqual(record.wslconf_interop, 'unreadable')
        self.assertEqual(record.wslconf_systemd, 'unreadable')
        self.assertEqual(record.automount_root, 'unreadable')

    def test_bad_boolean_and_disagreeing_duplicates_are_unreadable(self):
        self.wsl2()
        _plant(self.root, 'etc/wsl.conf', '[interop]\nenabled = yes\n[boot]\nsystemd = true\n')
        record = self.read()
        self.assertEqual(record.wslconf_interop, 'unreadable')
        self.assertEqual(record.wslconf_systemd, 'true')
        self.assertEqual(record.automount_root, 'absent')

        self.fresh()
        self.wsl2()
        _plant(self.root, 'etc/wsl.conf', '[interop]\nenabled = true\nenabled = false\n')
        self.assertEqual(self.read().wslconf_interop, 'unreadable')

        self.fresh()
        self.wsl2()
        _plant(self.root, 'etc/wsl.conf', '[interop]\nenabled = true\nenabled = TRUE\n')
        self.assertEqual(self.read().wslconf_interop, 'true')

    def test_automount_root_must_be_absolute_and_one_value(self):
        self.wsl2()
        _plant(self.root, 'etc/wsl.conf', '[automount]\nroot = mnt\n')
        self.assertEqual(self.read().automount_root, 'unreadable')
        self.fresh()
        self.wsl2()
        _plant(self.root, 'etc/wsl.conf', '[automount]\nroot = /mnt\nroot = /windows\n')
        self.assertEqual(self.read().automount_root, 'unreadable')
        self.fresh()
        self.wsl2()
        _plant(self.root, 'etc/wsl.conf', '[automount]\nenabled = false\n')
        self.assertEqual(self.read().automount_root, 'absent')
        self.fresh()
        self.wsl2()
        _plant(self.root, 'etc/wsl.conf', "[automount]\nroot = '/mnt/wsl'\n")
        self.assertEqual(self.read().automount_root, '/mnt/wsl')

    def test_host_style_boot_systemd_line(self):
        self.wsl2()
        _plant(self.root, 'etc/wsl.conf', '[boot]\nsystemd=true\n\n[user]\ndefault=operator\n')
        record = self.read()
        self.assertEqual(record.wslconf_systemd, 'true')
        self.assertEqual(record.wslconf_interop, 'absent')
        self.assertEqual(record.automount_root, 'absent')


class CacheTests(ProbeCase):
    def test_second_caller_gets_the_same_record(self):
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', HOST_BINFMT)
        first = self.read()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', 'disabled\n')
        second = self.read()
        self.assertIs(first, second)
        self.assertEqual(second.interop, 'on')

    def test_another_root_or_environ_is_a_different_record(self):
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', 'disabled\n')
        off = self.read()
        on = self.read({'WSL_INTEROP': '/run/WSL/9_interop'})
        self.assertIsNot(off, on)
        self.assertEqual(off.interop, 'off')
        self.assertEqual(on.interop, 'on')
        other_tmp = tempfile.TemporaryDirectory()
        try:
            other = Path(other_tmp.name)
            _plant(other, 'proc/sys/kernel/osrelease', UBUNTU_OSRELEASE)
            _plant(other, 'proc/version', UBUNTU_VERSION)
            _plant(other, 'proc/1/comm', 'systemd\n')
            native = read(other, {})
            self.assertEqual(native.kind, 'none')
            self.assertIsNot(native, off)
        finally:
            _unlock(Path(other_tmp.name))
            other_tmp.cleanup()

    def test_default_environ_is_the_process_environment(self):
        self.ubuntu()
        by_default = read(self.root)
        by_argument = read(self.root, os.environ)
        self.assertIs(by_default, by_argument)
        if 'WSL_INTEROP' in os.environ:
            self.assertEqual(by_default.interop, 'on')
        else:
            self.assertEqual(by_default.interop, 'off')

    def test_a_fake_root_does_not_replace_the_host_record(self):
        self.ubuntu()
        host = read()
        fake = self.read()
        self.assertIs(read(), host)
        self.assertIsNot(fake, host)

    def test_interrupt_does_not_store_a_record(self):
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', HOST_BINFMT)
        environ = _Env(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            read(self.root, environ)
        self.assertGreaterEqual(environ.checks, 2)
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', 'disabled\n')
        self.assertEqual(self.read().interop, 'off')

    def test_exception_does_not_store_a_record(self):
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', HOST_BINFMT)
        with self.assertRaises(RuntimeError):
            read(self.root, _Env(RuntimeError('boom')))
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', 'disabled\n')
        self.assertEqual(self.read().interop, 'off')

    def test_two_threads_receive_one_record(self):
        self.wsl2()
        _plant(self.root, 'proc/sys/fs/binfmt_misc/WSLInterop', HOST_BINFMT)
        barrier = threading.Barrier(2)
        found = []

        def caller():
            barrier.wait()
            found.append(self.read())

        threads = [threading.Thread(target=caller) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(found), 2)
        self.assertIs(found[0], found[1])
        self.assertEqual(found[0].interop, 'on')


@unittest.skipUnless(_host_is_wsl(), 'wsl_live: skipped off WSL')
class WslLiveTests(unittest.TestCase):
    """The builder's own host. Marked wsl_live. Skipped when osrelease does not name microsoft."""

    wsl_live = True

    def test_wsl_live_host_record(self):
        first = read()
        second = read()
        self.assertIs(first, second)
        self.assertIsInstance(first, Record)
        release = Path('/proc/sys/kernel/osrelease').read_text(encoding='utf-8')
        version = Path('/proc/version').read_text(encoding='utf-8')
        blob = (release + '\n' + version).lower()
        self.assertIn('microsoft', blob)
        if 'wsl2' in blob or 'microsoft-standard' in blob:
            self.assertEqual(first.kind, 'wsl2')
        else:
            self.assertEqual(first.kind, 'wsl1')
        comm = Path('/proc/1/comm').read_text(encoding='utf-8').replace('\x00', '').strip()
        self.assertEqual(first.systemd_pid1, comm == 'systemd')

        def enabled(path: Path):
            try:
                text = path.read_text(encoding='utf-8')
            except FileNotFoundError:
                return False
            except OSError:
                return None
            return any(line.strip() == 'enabled' for line in text.splitlines())

        sources = [
            enabled(Path('/proc/sys/fs/binfmt_misc/WSLInterop')),
            enabled(Path('/proc/sys/fs/binfmt_misc/WSLInterop-late')),
        ]
        try:
            Path('/run/WSL').lstat()
        except FileNotFoundError:
            sources.append(False)
        except OSError:
            sources.append(None)
        else:
            sources.append(True)
        sources.append('WSL_INTEROP' in os.environ)
        if any(source is True for source in sources):
            self.assertEqual(first.interop, 'on')
        elif any(source is None for source in sources):
            self.assertEqual(first.interop, 'unknown')
        else:
            self.assertEqual(first.interop, 'off')
        self.assertIn(first.interop, ('on', 'off', 'unknown'))
        self.assertIn(first.wslconf_interop, ('true', 'false', 'absent', 'unreadable'))
        self.assertIn(first.wslconf_systemd, ('true', 'false', 'absent', 'unreadable'))
        conf = Path('/etc/wsl.conf')
        try:
            text = conf.read_text(encoding='utf-8')
        except FileNotFoundError:
            self.assertEqual(first.wslconf_interop, 'absent')
            self.assertEqual(first.wslconf_systemd, 'absent')
            self.assertEqual(first.automount_root, 'absent')
        except OSError:
            self.assertEqual(first.wslconf_interop, 'unreadable')
            self.assertEqual(first.wslconf_systemd, 'unreadable')
            self.assertEqual(first.automount_root, 'unreadable')
        else:
            folded = text.replace(' ', '').lower()
            if 'systemd=true' in folded and 'systemd=false' not in folded:
                self.assertEqual(first.wslconf_systemd, 'true')
            if '[interop]' not in text.lower() and 'enabled=' not in folded:
                self.assertEqual(first.wslconf_interop, 'absent')
            if 'root=' not in folded:
                self.assertEqual(first.automount_root, 'absent')


if __name__ == '__main__':
    unittest.main()
