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
            if isinstance(text, bytes):
                (leaf / name).write_bytes(text)
            else:
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

    # The answer round of the read on 2978019b: no file content makes read_leaf raise, and a
    # counter the kernel cannot print (more than 20 digits, above 2**64 - 1, negative, not text)
    # reads as unknown.
    def assert_memory_peak_unknown(self, content):
        from recursion_core.measures import read_leaf
        got = by_name(read_leaf(self.leaf({'memory.peak': content})))['memory.peak']
        self.assertEqual(got['certainty'], 'unknown')
        self.assertIsNone(got['value'])

    def test_a_five_thousand_digit_counter_reads_unknown(self):
        self.assert_memory_peak_unknown('9' * 5000 + '\n')
        from recursion_core.measures import read_leaf
        got = by_name(read_leaf(self.leaf({'cpu.stat': 'usage_usec ' + '9' * 5000 + '\n',
                                           'io.stat': '8:0 rbytes=' + '9' * 5000 + ' wbytes=1\n'})))
        self.assertIsNone(got['cpu.usage_usec']['value'])
        self.assertIsNone(got['io.rbytes']['value'])

    def test_two_to_the_sixty_four_reads_unknown(self):
        self.assert_memory_peak_unknown(f'{2 ** 64}\n')
        from recursion_core.measures import read_leaf
        got = by_name(read_leaf(self.leaf({'memory.peak': f'{2 ** 64 - 1}\n'})))['memory.peak']
        self.assertEqual(got['value'], 2 ** 64 - 1)
        self.assertEqual(got['certainty'], 'known')

    def test_a_negative_counter_reads_unknown(self):
        self.assert_memory_peak_unknown('-1\n')

    def test_bytes_that_do_not_decode_read_unknown(self):
        self.assert_memory_peak_unknown(b'\xff\xfe\x00\x01')

    def test_a_readable_empty_io_stat_reads_known_zero(self):
        # The kernel prints an io.stat line only for a device that did io, so a readable empty
        # file means no device counted io for this leaf: a known 0. A missing file stays unknown.
        from recursion_core.measures import read_leaf
        got = by_name(read_leaf(self.leaf({'io.stat': ''})))
        for name in ('io.rbytes', 'io.wbytes'):
            self.assertEqual(got[name]['value'], 0, name)
            self.assertEqual(got[name]['certainty'], 'known', name)

    def test_an_io_stat_line_without_a_device_makes_the_whole_file_unknown(self):
        from recursion_core.measures import read_leaf
        got = by_name(read_leaf(self.leaf({'io.stat': '8:0 rbytes=1 wbytes=2\nrbytes=5 wbytes=6\n'})))
        for name in ('io.rbytes', 'io.wbytes'):
            self.assertEqual(got[name]['certainty'], 'unknown', name)
            self.assertIsNone(got[name]['value'], name)

    # The third commit (the read of bf71c0f0): a known figure never passes 2**64 - 1, and io.stat
    # input a real kernel never writes reads as unknown rather than as a wrong known figure.
    def io(self, text):
        from recursion_core.measures import read_leaf
        return by_name(read_leaf(self.leaf({'io.stat': text})))

    def assert_io_unknown(self, text, names=('io.rbytes', 'io.wbytes')):
        got = self.io(text)
        for name in names:
            self.assertEqual(got[name]['certainty'], 'unknown', name)
            self.assertIsNone(got[name]['value'], name)

    def test_an_io_stat_sum_past_two_to_the_sixty_four_reads_unknown(self):
        top = 2 ** 64 - 1
        self.assert_io_unknown(f'8:0 rbytes={top} wbytes=1\n259:0 rbytes=1 wbytes=1\n', ('io.rbytes',))
        got = self.io(f'8:0 rbytes={top - 1} wbytes=1\n259:0 rbytes=1 wbytes=1\n')
        self.assertEqual(got['io.rbytes']['value'], top)
        self.assertEqual(got['io.wbytes']['value'], 2)

    def test_an_io_stat_token_without_equals_makes_the_whole_file_unknown(self):
        self.assert_io_unknown('8:0 rbytes=1 wbytes=2 dios\n')

    def test_a_key_repeated_inside_one_io_stat_line_makes_the_file_unknown(self):
        self.assert_io_unknown('8:0 rbytes=1 rbytes=5 wbytes=2\n')

    def test_two_io_stat_lines_for_one_device_make_the_file_unknown(self):
        self.assert_io_unknown('8:0 rbytes=1 wbytes=2\n8:0 rbytes=3 wbytes=4\n')


class PayloadRefusalTests(unittest.TestCase):
    MEASURE = {'class': 'memory', 'name': 'memory.peak', 'value': 1, 'unit': 'bytes',
               'source': 'daemon', 'certainty': 'known'}

    def refused(self, **change):
        from recursion_core.measures import MeasureRefused, build_measure_payload
        with self.assertRaises(MeasureRefused):
            build_measure_payload('run-1', '2026-11-17T20:30:00Z', [dict(self.MEASURE, **change)])

    def test_a_known_nan_is_refused(self):
        self.refused(value=float('nan'))

    def test_a_known_infinity_is_refused(self):
        self.refused(value=float('inf'))

    def test_a_known_negative_number_is_refused(self):
        self.refused(value=-1)

    def test_a_known_bool_is_refused(self):
        self.refused(value=True)

    def test_a_certainty_outside_known_and_unknown_is_refused(self):
        self.refused(certainty='maybe')

    def test_a_known_without_a_number_is_refused(self):
        self.refused(value=None)
        self.refused(value='1')

    def test_an_unknown_with_any_value_but_null_is_refused(self):
        self.refused(certainty='unknown', value=5)
        self.refused(certainty='unknown', value='unknown')

    def test_a_known_number_above_two_to_the_sixty_four_minus_one_is_refused(self):
        self.refused(value=2 ** 64)
        self.refused(value=10 ** 5000)
        self.refused(value=float(2 ** 65))

    def test_a_missing_field_is_refused(self):
        from recursion_core.measures import MeasureRefused, build_measure_payload
        for field in self.MEASURE:
            measure = {k: v for k, v in self.MEASURE.items() if k != field}
            with self.assertRaises(MeasureRefused, msg=field):
                build_measure_payload('run-1', '2026-11-17T20:30:00Z', [measure])

    def test_an_extra_field_is_refused(self):
        self.refused(note='extra')

    def test_empty_strings_are_refused(self):
        from recursion_core.measures import MeasureRefused, build_measure_payload
        for field in ('class', 'name', 'unit'):
            self.refused(**{field: ''})
        for run_id, at in (('', '2026-11-17T20:30:00Z'), ('run-1', '')):
            with self.assertRaises(MeasureRefused):
                build_measure_payload(run_id, at, [dict(self.MEASURE)])

    def test_a_known_zero_and_a_finite_float_are_accepted(self):
        from recursion_core.measures import build_measure_payload
        for value in (0, 0.0, 2.5, 2 ** 64 - 1):
            payload = build_measure_payload('run-1', '2026-11-17T20:30:00Z',
                                            [dict(self.MEASURE, value=value)])
            self.assertEqual(payload['measures'][0]['value'], value)


if __name__ == '__main__':
    unittest.main()
