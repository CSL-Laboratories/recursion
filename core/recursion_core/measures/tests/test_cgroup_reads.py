"""The measures reader against a fake cgroup v2 leaf made in a temporary directory.

No real cgroup is needed: the reader takes a directory and reads plain files in it, so these
tests build that directory with tempfile and run the same on every platform. An unknown is
value None, never 0 (PRODUCT bar 7 and pillar P10).
"""
from pathlib import Path
import tempfile
import unittest

IO_STAT = ('8:0 rbytes=100 wbytes=200 rios=1 wios=2 dbytes=0 dios=0\n'
           '259:0 rbytes=1000 wbytes=2000 rios=10 wios=20 dbytes=0 dios=0\n')
CPU_STAT = ('usage_usec 1000\nuser_usec 600\nsystem_usec 400\n'
            'nr_periods 0\nnr_throttled 0\nthrottled_usec 0\n')


def by_name(measures):
    return {m['name']: m for m in measures}


class CgroupReadTests(unittest.TestCase):
    def leaf(self, files):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        leaf = Path(temp.name) / 'run.leaf'
        leaf.mkdir()
        for name, text in files.items():
            (leaf / name).write_text(text, encoding='utf-8')
        return leaf

    def test_memory_peak_reads_as_known_bytes(self):
        from recursion_core.measures import read_leaf
        got = by_name(read_leaf(self.leaf({'memory.peak': '67108864\n'})))['memory.peak']
        self.assertEqual(got['value'], 67108864)
        self.assertEqual(got['unit'], 'bytes')
        self.assertEqual(got['class'], 'memory')
        self.assertEqual(got['source'], 'daemon')
        self.assertEqual(got['certainty'], 'known')

    def test_missing_pids_peak_reads_unknown_not_zero(self):
        from recursion_core.measures import read_leaf
        got = by_name(read_leaf(self.leaf({'memory.peak': '4096\n'})))['pids.peak']
        self.assertEqual(got['certainty'], 'unknown')
        self.assertIsNone(got['value'])
        self.assertNotEqual(got['value'], 0)

    def test_cpu_stat_parses_and_io_stat_sums_two_devices(self):
        from recursion_core.measures import read_leaf
        got = by_name(read_leaf(self.leaf({'cpu.stat': CPU_STAT, 'io.stat': IO_STAT})))
        self.assertEqual(got['cpu.usage_usec']['value'], 1000)
        self.assertEqual(got['cpu.user_usec']['value'], 600)
        self.assertEqual(got['cpu.system_usec']['value'], 400)
        self.assertEqual(got['io.rbytes']['value'], 1100)
        self.assertEqual(got['io.wbytes']['value'], 2200)
        for name in ('cpu.usage_usec', 'cpu.user_usec', 'cpu.system_usec', 'io.rbytes', 'io.wbytes'):
            self.assertEqual(got[name]['certainty'], 'known', name)
        self.assertEqual(got['io.rbytes']['unit'], 'bytes')
        self.assertEqual(got['cpu.usage_usec']['unit'], 'usec')

    def test_garbage_memory_peak_reads_unknown(self):
        from recursion_core.measures import read_leaf
        got = by_name(read_leaf(self.leaf({'memory.peak': 'sixty-four\n'})))['memory.peak']
        self.assertEqual(got['certainty'], 'unknown')
        self.assertIsNone(got['value'])

    def test_payload_with_another_source_is_refused(self):
        from recursion_core.measures import MeasureRefused, build_measure_payload
        measure = {'class': 'memory', 'name': 'memory.peak', 'value': 1, 'unit': 'bytes',
                   'source': 'plugin', 'certainty': 'known'}
        with self.assertRaises(MeasureRefused):
            build_measure_payload('run-1', '2026-11-17T20:30:00Z', [measure])
        for source in ('daemon', 'engine', 'estimate'):
            payload = build_measure_payload('run-1', '2026-11-17T20:30:00Z',
                                            [dict(measure, source=source)])
            self.assertEqual(payload['measures'][0]['source'], source)
        self.assertEqual(payload['run_id'], 'run-1')
        self.assertEqual(payload['at'], '2026-11-17T20:30:00Z')

    def test_every_unknown_carries_value_none(self):
        from recursion_core.measures import MeasureRefused, build_measure_payload, read_leaf
        measures = read_leaf(self.leaf({'memory.peak': 'garbage', 'cpu.stat': 'usage_usec x\n',
                                        'io.stat': '8:0 rbytes wbytes\n'}))
        self.assertTrue(measures)
        for m in measures:
            self.assertEqual(m['certainty'], 'unknown', m['name'])
            self.assertIs(m['value'], None, m['name'])
        payload = build_measure_payload('run-1', '2026-11-17T20:30:00Z', measures)
        for m in payload['measures']:
            self.assertIs(m['value'], None, m['name'])
        zero_unknown = dict(measures[0], value=0)
        with self.assertRaises(MeasureRefused):
            build_measure_payload('run-1', '2026-11-17T20:30:00Z', [zero_unknown])


if __name__ == '__main__':
    unittest.main()
