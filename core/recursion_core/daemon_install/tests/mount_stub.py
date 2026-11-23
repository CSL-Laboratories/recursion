"""A stand-in for the kernel's mount table, for every test module that runs install, upgrade or the setup command.

``install`` and ``upgrade`` read the type of the disk under the stores from ``install._MOUNTINFO``
(``/proc/self/mountinfo``) and refuse 9p, overlay and network disks. A test module that runs them imports
``setUpModule`` and ``tearDownModule`` from here: for the length of that module the reader is pointed at a
one-line table, written in a temporary folder the module makes, that puts ``/`` on ext4. The tests then run
the same where the machine's root is overlay (a container) as where it is a plain disk, and none of them
reads the machine's own table. The product reads no variable for the table: a test points the module's own
name at the stand-in, as ``test_store_filesystem`` does for each of its cases.
"""

from __future__ import annotations

import importlib
import os
import tempfile

INSTALL = importlib.import_module("recursion_core.daemon_install.install")
# One mount, / on ext4, in the kernel's mountinfo form.
LOCAL_ROOT = "20 1 0:20 / / rw,relatime shared:1 - ext4 src0 rw\n"

_held: list[tuple[tempfile.TemporaryDirectory, str]] = []


def write_table(folder: str, text: str = LOCAL_ROOT) -> str:
    """Write ``text`` as a mount table named ``mountinfo`` in ``folder``; return its path."""

    path = os.path.join(folder, "mountinfo")
    with open(path, "w", encoding="utf-8") as out:
        out.write(text)
    return path


def setUpModule():
    tmp = tempfile.TemporaryDirectory(prefix="mount-table-")
    try:
        path = write_table(tmp.name)
    except BaseException:
        tmp.cleanup()
        raise
    _held.append((tmp, INSTALL._MOUNTINFO))
    INSTALL._MOUNTINFO = path


def tearDownModule():
    tmp, before = _held.pop()
    INSTALL._MOUNTINFO = before
    tmp.cleanup()
