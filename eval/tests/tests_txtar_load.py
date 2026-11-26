"""load_txtar reads an archive in the form the repository stores: CRLF read as LF before the parse and the digest.

The repository keeps eval txtar files LF (.gitattributes). A working copy that holds CRLF must give the task
the same digest, and the same section contents, as the committed file, so a pin made from it holds on every
checkout. A lone CR is not a line ending of the stored form and is kept.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from eval import tasks

LF_ARCHIVE = (
    "-- task.json --\n"
    '{"id": "t-1", "test_command": ["python", "-m", "pytest"]}\n'
    "-- prompt.md --\n"
    "Say hello.\n"
    "-- start/a.py --\n"
    "print('a')\n"
    "-- hidden/test_x.py --\n"
    "assert True\n"
    "-- solution/a.py --\n"
    "print('hello')\n"
).encode("utf-8")


def load(folder: Path, name: str, data: bytes) -> tasks.Task:
    path = folder / name
    path.write_bytes(data)
    return tasks.load_txtar(path)


class LoadTxtarLineEndingTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.folder = Path(holder.name)

    def test_a_crlf_copy_and_its_lf_twin_give_the_same_digest_and_files(self):
        lf = load(self.folder, "lf.txtar", LF_ARCHIVE)
        crlf = load(self.folder, "crlf.txtar", LF_ARCHIVE.replace(b"\n", b"\r\n"))
        self.assertEqual(crlf.sha256, lf.sha256)
        self.assertEqual(crlf.start_files, lf.start_files)
        self.assertEqual(crlf.hidden, lf.hidden)
        self.assertEqual(crlf.solution, lf.solution)
        self.assertEqual(crlf.prompt, lf.prompt)
        self.assertNotIn(b"\r", crlf.hidden["test_x.py"])

    def test_the_lf_digest_is_the_sha256_of_the_raw_bytes(self):
        lf = load(self.folder, "lf.txtar", LF_ARCHIVE)
        self.assertEqual(lf.sha256, hashlib.sha256(LF_ARCHIVE).hexdigest())

    def test_a_bare_cr_inside_a_section_is_kept_and_changes_the_digest(self):
        plain = load(self.folder, "plain.txtar", LF_ARCHIVE)
        with_cr = load(self.folder, "cr.txtar", LF_ARCHIVE.replace(b"assert True\n", b"assert True\rpass\n"))
        self.assertIn(b"\r", with_cr.hidden["test_x.py"])
        self.assertNotEqual(with_cr.sha256, plain.sha256)

    def test_a_mixed_archive_gives_the_lf_twins_digest(self):
        lf = load(self.folder, "lf.txtar", LF_ARCHIVE)
        lines = LF_ARCHIVE.split(b"\n")
        mixed = b"".join(line + (b"\r\n" if number % 2 else b"\n") for number, line in enumerate(lines[:-1]))
        self.assertIn(b"\r\n", mixed)
        self.assertIn(b"\n", mixed.replace(b"\r\n", b""))
        twin = load(self.folder, "mixed.txtar", mixed)
        self.assertEqual(twin.sha256, lf.sha256)
        self.assertEqual(twin.hidden, lf.hidden)


if __name__ == "__main__":
    unittest.main()
