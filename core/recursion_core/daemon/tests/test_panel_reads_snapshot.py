"""A panel's reads list is frozen with the startup snapshot.

``LoopbackGUI`` copies a panel shallowly today, so the caller's list stays
aliased. Changing that list after construction must not change the reads the
GUI validated, and must not make a later bind see a different declaration.
"""
import unittest

from recursion_core.daemon import gui


class PanelReadsSnapshotTests(unittest.TestCase):
    def test_panel_reads_must_be_frozen_with_the_startup_snapshot(self):
        reads = ['operator.positions.inspect']
        listener = gui.LoopbackGUI(plugins=({'panels': [{'id': 'stable', 'title': 'Stable', 'reads': reads}]},), port=0)
        reads[:] = ['stop']
        try:
            with listener:
                self.assertEqual(tuple(listener.plugins[0]['panels'][0]['reads']), ('operator.positions.inspect',))
        except ValueError as exc:
            self.fail('validated snapshot changed before bind: ' + str(exc))

    def test_a_string_subject_is_not_split_into_characters(self):
        text = "kalshi:model_fed_read"
        with self.assertRaises(ValueError):
            gui._snapshot({"required": {"venue_use": text}})
        subjects = (s for s in [text])
        self.assertEqual(gui._snapshot({"required": {"venue_use": subjects}})["required"]["venue_use"], (text,))


if __name__ == "__main__":
    unittest.main()
