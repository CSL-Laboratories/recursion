"""A failed result write and a line-aligned stdout tail.

An existing result.json stays byte for byte when the result writer raises
after a partial temp. The new result stays in memory, and the temp this write
created does not remain. A stdout window that begins at the start of a line
keeps that line. A window that begins mid-line still drops that partial line.
"""
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from recursion_core.daemon import control_adapter as adapter

LINUX = sys.platform.startswith('linux')


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class AdapterP3Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='adapter-p3-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_dir(self):
        folder = self.root / 'runs' / 'run-one'
        folder.mkdir(parents=True, mode=0o700)
        return folder

    def test_failed_result_write_removes_partial_temp(self):
        folder = self.run_dir()
        old = b'{"status": "old"}\n'
        (folder / 'result.json').write_bytes(old)
        starter = adapter.RouteStarter(None, {}, None, self.root)

        def no_space(obj, handle, **kwargs):
            handle.write('{"status":')
            handle.flush()
            raise OSError(errno.ENOSPC, 'injected short write')

        with self.assertLogs(adapter.log, 'ERROR') as logs:
            with mock.patch('recursion_core.daemon.control_adapter.json.dump', no_space):
                starter._record_result('run-one', {'status': 'new'})
        self.assertEqual((folder / 'result.json').read_bytes(), old)
        self.assertEqual(starter.results['run-one'], {'status': 'new'})
        self.assertFalse((folder / 'result.json.tmp').exists())
        self.assertIn('OSError', '\n'.join(record.getMessage() for record in logs.records))
        self.assertFalse(any(str(self.root) in line for line in logs.output))
        self.assertTrue(all(not record.exc_info for record in logs.records))

    def test_failed_result_write_removes_symlink_temp(self):
        folder = self.run_dir()
        target = self.root / 'kept.txt'
        target.write_text('keep')
        starter = adapter.RouteStarter(None, {}, None, self.root)
        tmp = folder / 'result.json.tmp'

        def swap_to_symlink(obj, handle, **kwargs):
            os.unlink(tmp)
            os.symlink(target, tmp)
            raise OSError(errno.ENOSPC, 'injected short write')

        with self.assertLogs(adapter.log, 'ERROR'):
            with mock.patch('recursion_core.daemon.control_adapter.json.dump', swap_to_symlink):
                starter._record_result('run-one', {'status': 'new'})
        self.assertEqual(target.read_text(), 'keep')
        self.assertFalse(tmp.is_symlink())
        self.assertFalse((folder / 'result.json').exists())
        self.assertEqual(starter.results['run-one'], {'status': 'new'})

    def test_tail_preserves_line_aligned_window(self):
        folder = self.run_dir()
        (folder / 'stdout.jsonl').write_bytes(b'old\n1234567\n')
        control = object.__new__(adapter.DaemonControl)
        control._state = self.root
        with mock.patch.object(adapter, 'OUTPUT_TAIL_BYTES', 8):
            self.assertEqual(control._output('run-one', live=True)['lines'], ['1234567'])

    def test_tail_drops_a_partial_first_line(self):
        folder = self.run_dir()
        (folder / 'stdout.jsonl').write_bytes(b'abcdefg\nZZ\n')
        control = object.__new__(adapter.DaemonControl)
        control._state = self.root
        with mock.patch.object(adapter, 'OUTPUT_TAIL_BYTES', 8):
            self.assertEqual(control._output('run-one', live=True)['lines'], ['ZZ'])

    def test_a_non_empty_planted_directory_is_left_and_the_result_stays_in_memory(self):
        folder = self.run_dir()
        tmp = folder / 'result.json.tmp'
        tmp.mkdir()
        (tmp / 'planted').write_text('stay')
        starter = adapter.RouteStarter(None, {}, None, self.root)
        with self.assertLogs(adapter.log, 'ERROR'):
            starter._record_result('run-one', {'status': 'new'})
        self.assertEqual((tmp / 'planted').read_text(), 'stay')
        self.assertFalse((folder / 'result.json').exists())
        self.assertEqual(starter.results['run-one'], {'status': 'new'})

    def test_an_empty_planted_directory_is_removed_so_the_write_can_proceed(self):
        folder = self.run_dir()
        tmp = folder / 'result.json.tmp'
        tmp.mkdir()
        starter = adapter.RouteStarter(None, {}, None, self.root)
        starter._record_result('run-one', {'status': 'new'})
        self.assertFalse(tmp.exists())
        self.assertIn(b'new', (folder / 'result.json').read_bytes())

    def test_a_fifo_swapped_in_for_the_opened_temp_is_left(self):
        folder = self.run_dir()
        starter = adapter.RouteStarter(None, {}, None, self.root)
        tmp = folder / 'result.json.tmp'

        def swap_to_fifo(obj, handle, **kwargs):
            os.unlink(tmp)
            os.mkfifo(tmp)
            raise OSError(errno.ENOSPC, 'injected short write')

        with self.assertLogs(adapter.log, 'ERROR'):
            with mock.patch('recursion_core.daemon.control_adapter.json.dump', swap_to_fifo):
                starter._record_result('run-one', {'status': 'new'})
        self.assertTrue(tmp.is_fifo())
        self.assertEqual(starter.results['run-one'], {'status': 'new'})

    def test_a_file_planted_when_open_fails_is_left(self):
        folder = self.run_dir()
        starter = adapter.RouteStarter(None, {}, None, self.root)
        tmp = folder / 'result.json.tmp'
        real_open = os.open

        def open_plants(path, flags, mode=0o777, *, dir_fd=None):
            if Path(path).name == 'result.json.tmp':
                fd = real_open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                os.write(fd, b'planted')
                os.close(fd)
                raise OSError(errno.EIO, 'injected open failure')
            if dir_fd is None:
                return real_open(path, flags, mode)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with self.assertLogs(adapter.log, 'ERROR'):
            with mock.patch('recursion_core.daemon.control_adapter.os.open', open_plants):
                starter._record_result('run-one', {'status': 'new'})
        self.assertEqual(tmp.read_bytes(), b'planted')
        self.assertFalse((folder / 'result.json').exists())
        self.assertEqual(starter.results['run-one'], {'status': 'new'})

    def test_an_unlink_failure_keeps_the_result_in_memory(self):
        folder = self.run_dir()
        starter = adapter.RouteStarter(None, {}, None, self.root)
        tmp = folder / 'result.json.tmp'
        real_unlink = os.unlink

        def unlink_refuses(path, *args, **kwargs):
            if Path(path).name == 'result.json.tmp' and Path(path).exists():
                raise OSError(errno.EPERM, 'injected unlink refusal')
            return real_unlink(path, *args, **kwargs)

        def short_write(obj, handle, **kwargs):
            handle.write('{"status":')
            handle.flush()
            raise OSError(errno.ENOSPC, 'injected short write')

        with self.assertLogs(adapter.log, 'ERROR'):
            with mock.patch('recursion_core.daemon.control_adapter.os.unlink', unlink_refuses), \
                 mock.patch('recursion_core.daemon.control_adapter.json.dump', short_write):
                try:
                    starter._record_result('run-one', {'status': 'new'})
                except OSError:
                    self.fail('a removal failure took the place of the in-memory result')
        self.assertEqual(starter.results['run-one'], {'status': 'new'})
        self.assertTrue(tmp.is_file())


if __name__ == '__main__':
    unittest.main()
