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


if __name__ == '__main__':
    unittest.main()
