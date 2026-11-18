"""The installer deletes nothing it cannot prove is its own, and every refusal prints the way out.

(A) The swap of a publish, and the finishing of a stopped publish, remove a live entry only once it is
proven byte for byte equal to its copy in one of the two generations the record names: a folder, a regular
file, a link, a FIFO, any kind of entry under a published name. An entry that is not proven is never
removed: the command refuses ``plugin_directory_untrusted`` with the way out, and nothing is moved.
(B) The same for a publish that runs to its end: a live plugin folder that holds anything its generation
does not hold (a settings file, a data folder) refuses with the way out; nothing is deleted.
(C) A replaced entry is moved into a place of the publish's own under ``plugin-generations``, proven again
there and only then removed. What was put in its place between the check and the swap stays there: the
command says where in one line and exits non-zero.
(D) A file the installer reads that is not a regular file refuses without an open that can block.
(E) A broken, cut or empty ``.publishing`` record refuses with the way out printed.
"""
from __future__ import annotations

import os
import stat
import threading
import unittest

from recursion_core.plugin_security.tests import test_plugin_install_live_truth as live_truth
from recursion_core.plugin_security.tests.test_plugin_install_live_truth import MOVE_OUT, LiveCase


def _move_out(path):
    return f"The folder {path} is not a plugin this command installed. {MOVE_OUT}"


def _kept_at(path, kept):
    return (f"{path} was put in place while this command ran and was not deleted: "
            f"it is kept at {kept}. Move it back or delete it yourself.")


def _record_way_out(path):
    return (f"The file {path} is not a publish record this command wrote. "
            f"Move it out of {path.parent}; then run the command again.")


def entries(path):
    """Every entry at and under ``path`` by its relative path: its kind and, for a file, its bytes."""
    found = {}

    def one(item, name):
        info = os.lstat(item)
        if stat.S_ISLNK(info.st_mode):
            found[name] = ("link", os.readlink(item))
        elif stat.S_ISDIR(info.st_mode):
            found[name] = ("dir",)
            for child in sorted(os.listdir(item)):
                one(item / child, f"{name}/{child}")
        elif stat.S_ISREG(info.st_mode):
            found[name] = ("file", item.read_bytes())
        else:
            found[name] = ("other", stat.S_IFMT(info.st_mode))

    if os.path.lexists(path):
        one(path, ".")
    return found


class _Case(LiveCase):
    _commands = live_truth.FolderNotWrittenTests._commands

    def setUp(self):
        super().setUp()
        self.roots = self.registries()

    def refused_keeping(self, sentence, path, code="plugin_directory_untrusted", commands=None):
        """Every command refuses ``code`` with ``sentence``; ``path`` and the generations stay as they were."""
        held, pointer, listing = entries(path), self.current(), self.generations()
        for name, (command, argv) in self._commands().items():
            if commands is not None and name not in commands:
                continue
            with self.subTest(command=name):
                refusal = self.refused(command)
                self.assertEqual((code, sentence), (refusal.code, refusal.detail))
                self.assertEqual((1, f"refused: {code}\n{sentence}\n", ""), self.main(argv))
                self.assertEqual(held, entries(path))
                self.assertEqual(pointer, self.current())
                self.assertEqual(listing, self.generations())

    def stopped_with_live_whole(self):
        """The install of fixture.beta stops just before .publishing is cleared: current 2, live = generation 2."""
        from recursion_core.plugin_security import install as install_mod

        self.install_from(self.roots["a"])
        self._fail_once(install_mod, "_clear_marker")
        self.assertEqual("2\n", self.current())
        self.assertEqual(entries(self.gens() / "2"), entries(self.live()))

    def stopped_before_the_swap(self):
        """The install of fixture.beta stops before the live tree changes: current 2, live = generation 1."""
        from recursion_core.plugin_security import install as install_mod

        self.install_from(self.roots["a"])
        self._fail_once(install_mod, "_swap_into")
        self.assertEqual("2\n", self.current())
        self.assertEqual(entries(self.gens() / "1"), entries(self.live()))

    def _fail_once(self, module, name):
        real = getattr(module, name)

        def stop(*_args, **_kwargs):
            raise OSError("stopped")

        setattr(module, name, stop)
        try:
            with self.assertRaises(OSError):
                self.install_from(self.roots["beta"], "fixture.beta")
        finally:
            setattr(module, name, real)
        self.assertTrue(os.path.lexists(self.gens() / ".publishing"))

    def finished_after_moving_out(self, path, plugins=("fixture.beta", "fixture.unsigned")):
        os.rename(path, self.tmp / f"moved-out-{self.id().rsplit('.', 1)[-1]}-{len(os.listdir(self.tmp))}")
        self.assertEqual(list(plugins), self.listed_ids())
        self.assertFalse(os.path.lexists(self.gens() / ".publishing"))
        self.assertEqual(entries(self.gens() / self.current().strip()), entries(self.live()))

    def _within(self, command, seconds=10):
        done = {}

        def run():
            try:
                done["result"] = command()
            except BaseException as exc:  # noqa: BLE001 - carried to the test thread
                done["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(seconds)
        if thread.is_alive():
            for fifo in self.fifos:
                try:
                    os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
                except OSError:
                    pass
            thread.join(seconds)
            self.fail(f"the command hung for {seconds} s on a FIFO")
        return done

    def place(self, kind, path):
        """Put an entry of ``kind`` at ``path`` by hand."""
        if kind == "file":
            path.write_bytes(b"mine\n")
        elif kind == "fifo":
            os.mkfifo(path, 0o600)
        elif kind == "file link":
            target = self.tmp / f"target-{len(os.listdir(self.tmp))}"
            target.write_bytes(b"mine\n")
            path.symlink_to(target)
        return path


class EveryKindUnderAPublishedNameTests(_Case):
    """(A, the read's REQUIRED 1) After a stopped publish an entry of any kind under a published name is kept."""

    def test_a_file_a_fifo_or_a_file_link_under_a_published_name_after_a_stopped_publish_is_kept(self):
        for stop in ("before the swap", "with the live tree whole"):
            for kind in ("file", "fifo", "file link"):
                with self.subTest(stop=stop, kind=kind):
                    self.use(f"kind-{stop[:6]}-{kind.replace(' ', '-')}")
                    if stop == "before the swap":
                        self.stopped_before_the_swap()
                    else:
                        self.stopped_with_live_whole()
                        os.rename(self.live() / "fixture.beta", self.tmp / f"gen2-copy-{stop[:6]}-{kind.replace(' ', '-')}")
                    path = self.place(kind, self.live() / "fixture.beta")
                    self.refused_keeping(_move_out(path), path)
                    self.assertTrue(os.path.lexists(self.gens() / ".publishing"))
                    self.finished_after_moving_out(path)

    def test_a_file_under_a_name_no_generation_writes_is_left_and_the_publish_finishes(self):
        self.stopped_before_the_swap()
        path = self.place("file", self.live() / "notes.txt")
        self.assertEqual(["fixture.beta", "fixture.unsigned"], self.listed_ids())
        self.assertEqual(("file", b"mine\n"), entries(path)["."])
        self.assertFalse(os.path.lexists(self.gens() / ".publishing"))

    def test_a_file_under_the_name_an_install_publishes_is_kept_and_no_generation_is_written(self):
        # The read's SIX-2: no stop. The install of fixture.beta would publish a folder under that name.
        self.install_from(self.roots["a"])
        path = self.place("file", self.live() / "fixture.beta")
        self.refused_keeping(_move_out(path), path, commands=("install",))
        self.assertEqual(["fixture.unsigned"], self.listed_ids())
        os.rename(path, self.tmp / "my-file")
        self.install_from(self.roots["beta"], "fixture.beta")
        self.assertEqual(["fixture.beta", "fixture.unsigned"], self.listed_ids())
        self.assertEqual(b"mine\n", (self.tmp / "my-file").read_bytes())


class ExtraInAPluginFolderTests(_Case):
    """(B, the owner's point) A publish that runs to its end never deletes what a user keeps in a plugin folder."""

    EXTRAS = {
        "settings": {"settings.json": b"{}\n"},
        "data folder": {"data/cache.db": b"rows\n"},
        "empty folder": {"empty/": None},
    }

    def test_a_plugin_folder_holding_more_than_its_generation_refuses_install_and_remove_with_the_way_out(self):
        for kind, extra in self.EXTRAS.items():
            with self.subTest(kind=kind):
                self.use(f"extra-{kind.replace(' ', '-')}")
                self.install_from(self.roots["a"])
                folder = self.live() / "fixture.unsigned"
                for name, data in extra.items():
                    if data is None:
                        (folder / name).mkdir(mode=0o700)
                        continue
                    (folder / name).parent.mkdir(mode=0o700, exist_ok=True)
                    (folder / name).write_bytes(data)
                    os.chmod(folder / name, 0o600)
                # install of another plugin, remove of this one and of another: each would replace the folder.
                self.refused_keeping(_move_out(folder), folder, commands=("install", "remove"))
                self.assertEqual(["fixture.unsigned"], self.listed_ids())
                os.rename(folder, self.tmp / f"mine-{kind.replace(' ', '-')}")
                self.install_from(self.roots["beta"], "fixture.beta")
                self.assertEqual(["fixture.beta"], self.listed_ids())

    def test_a_folder_with_more_than_its_generation_placed_before_the_first_install_is_kept(self):
        self.write_live({})
        folder = self.live() / "fixture.unsigned"
        folder.mkdir(mode=0o700)
        source = self.roots["a"] / "registry" / "fixture.unsigned" / "1.0.0"
        for item in ("manifest.json", "plugin.py"):
            (folder / item).write_bytes((source / item).read_bytes())
            os.chmod(folder / item, 0o600)
        (folder / "settings.json").write_bytes(b"{}\n")
        held = entries(folder)
        refusal = self.refused(lambda: self.install_from(self.roots["a"]))
        self.assertEqual(("plugin_directory_untrusted", _move_out(folder)), (refusal.code, refusal.detail))
        self.assertEqual(held, entries(folder))
        self.assertIsNone(self.current())


class MovedAsideAndProvenAgainTests(_Case):
    """(C) An entry put in place between the check and the swap stays in the publish's own place."""

    def _put_before_swap(self, put):
        from recursion_core.plugin_security import install as install_mod

        real = install_mod._swap_into

        def swap(plugins, stage, old):
            put()
            return real(plugins, stage, old)

        install_mod._swap_into = swap
        self.addCleanup(setattr, install_mod, "_swap_into", real)

    def _kept_refusal(self, command, live_path, mine):
        refusal = self.refused(command)
        kept = [path for path in sorted(self.gens().iterdir()) if path.name.startswith(".kept-")]
        self.assertEqual(1, len(kept))
        kept_path = kept[0] / live_path.name
        self.assertEqual(("plugin_directory_untrusted", _kept_at(live_path, kept_path)),
                         (refusal.code, refusal.detail))
        self.assertNotIn("\n", refusal.detail)
        self.assertEqual(mine, entries(kept_path))
        return kept_path

    def test_a_folder_put_in_place_during_an_install_stays_in_the_publishes_own_place(self):
        self.install_from(self.roots["a"])
        hand = self.live() / "hand.made"

        def put():
            hand.mkdir(mode=0o700)
            (hand / "notes.txt").write_bytes(b"mine\n")

        self._put_before_swap(put)
        self._kept_refusal(lambda: self.install_from(self.roots["beta"], "fixture.beta"), hand,
                           {".": ("dir",), "./notes.txt": ("file", b"mine\n")})
        # The publish itself ran to its end: the live tree is generation 2 whole and the record is gone.
        self.assertEqual("2\n", self.current())
        self.assertEqual(entries(self.gens() / "2"), entries(self.live()))
        self.assertFalse(os.path.lexists(self.gens() / ".publishing"))

    def test_a_failure_after_the_swap_keeps_a_folder_put_in_place_and_says_where(self):
        from recursion_core.plugin_security import install as install_mod

        self.install_from(self.roots["a"])
        hand = self.live() / "hand.made"
        real = install_mod._swap_into

        def swap(plugins, stage, old):
            hand.mkdir(mode=0o700)
            (hand / "notes.txt").write_bytes(b"mine\n")
            real(plugins, stage, old)
            raise OSError("stopped")

        install_mod._swap_into = swap
        try:
            self._kept_refusal(lambda: self.install_from(self.roots["beta"], "fixture.beta"), hand,
                               {".": ("dir",), "./notes.txt": ("file", b"mine\n")})
        finally:
            install_mod._swap_into = real
        self.assertEqual(["fixture.beta", "fixture.unsigned"], self.listed_ids())
        self.assertFalse(os.path.lexists(self.gens() / ".publishing"))

    def test_a_stop_at_each_step_after_a_folder_is_put_in_place_never_loses_it(self):
        from recursion_core.plugin_security import install as install_mod
        from recursion_core.plugin_security.install import InstallRefused

        mine = {".": ("dir",), "./notes.txt": ("file", b"mine\n")}
        real = install_mod._swap_into
        for at in range(400):
            self.use(f"steps-{at}")
            self.install_from(self.roots["a"])
            hand = self.live() / "hand.made"
            placed = []

            def swap(plugins, stage, old, hand=hand, placed=placed):
                hand.mkdir(mode=0o700)
                (hand / "notes.txt").write_bytes(b"mine\n")
                placed.append(True)
                return real(plugins, stage, old)

            install_mod._swap_into = swap
            try:
                came = self.stopped(at, self.gens(), lambda: self.install_from(self.roots["beta"], "fixture.beta"))
            except InstallRefused as exc:  # no stop came: the install ran to its end and kept the folder
                self.assertEqual("plugin_directory_untrusted", exc.code)
                came = False
            finally:
                install_mod._swap_into = real
            with self.subTest(at=at):
                try:
                    self.listed()
                except Exception as exc:  # noqa: BLE001 - a refusal is allowed; a loss is not
                    self.assertEqual("plugin_directory_untrusted", getattr(exc, "code", None), repr(exc))
                places = [hand] + [path / "hand.made" for path in self.gens().iterdir()
                                   if path.name.startswith(".kept-")]
                self.assertEqual(1 if placed else 0, sum(entries(path) == mine for path in places))
            if not came:
                break
        else:
            self.fail("the install did not finish within 400 steps")

    def test_a_proven_folder_replaced_during_a_remove_is_kept(self):
        self.install_from(self.roots["a"])
        self.install_from(self.roots["beta"], "fixture.beta")
        folder = self.live() / "fixture.beta"

        def put():
            os.rename(folder, self.tmp / "gen2-copy")
            folder.mkdir(mode=0o700)
            (folder / "settings.json").write_bytes(b"{}\n")

        self._put_before_swap(put)
        self._kept_refusal(lambda: self.remove("fixture.beta"), folder,
                           {".": ("dir",), "./settings.json": ("file", b"{}\n")})
        self.assertEqual(entries(self.gens() / "3"), entries(self.live()))

    def test_the_command_line_says_where_in_one_line_and_exits_non_zero_when_it_finishes_a_stopped_publish(self):
        self.stopped_before_the_swap()
        hand = self.live() / "hand.made"

        def put():
            if not os.path.lexists(hand):
                hand.mkdir(mode=0o700)
                (hand / "notes.txt").write_bytes(b"mine\n")

        self._put_before_swap(put)
        status, err, out = self.main(self.argv("list"))
        kept = [path for path in sorted(self.gens().iterdir()) if path.name.startswith(".kept-")]
        self.assertEqual(1, len(kept))
        self.assertEqual((1, f"refused: plugin_directory_untrusted\n{_kept_at(hand, kept[0] / 'hand.made')}\n", ""),
                         (status, err, out))
        self.assertEqual(b"mine\n", (kept[0] / "hand.made" / "notes.txt").read_bytes())
        self.assertEqual(entries(self.gens() / "2"), entries(self.live()))
        self.assertEqual(["fixture.beta", "fixture.unsigned"], self.listed_ids())


class NoBlockingOpenTests(_Case):
    """(D) A manifest that is a FIFO refuses at once; the command does not hang holding the lock."""

    def _fifo_folder(self, name):
        folder = self.live() / name
        folder.mkdir(mode=0o700)
        fifo = folder / "manifest.json"
        os.mkfifo(fifo, 0o600)
        self.fifos = getattr(self, "fifos", []) + [fifo]
        return folder

    def test_a_fifo_manifest_in_a_folder_whose_name_is_not_an_id_refuses_with_the_way_out(self):
        self.install_from(self.roots["a"])
        folder = self._fifo_folder("x")
        done = self._within(self.listed)
        self.assertEqual(("plugin_directory_untrusted", _move_out(folder)),
                         (done["error"].code, done["error"].detail))

    def test_a_fifo_manifest_in_a_folder_named_with_an_id_refuses(self):
        for first in (True, False):
            with self.subTest(first=first):
                self.use(f"fifo-{int(first)}")
                if first:
                    self.write_live({})
                else:
                    self.install_from(self.roots["a"])
                folder = self._fifo_folder("fixture.zzz")
                done = self._within(self.listed)
                self.assertIn("error", done)
                if first:
                    self.assertEqual("plugin_file_untrusted", done["error"].code)
                else:
                    self.assertEqual(("plugin_directory_untrusted", _move_out(folder)),
                                     (done["error"].code, done["error"].detail))


class BrokenRecordTests(_Case):
    """(E) A broken, cut or empty .publishing refuses with the way out; once moved out, the next command runs."""

    FORMS = (b"", b"2\n1", b"2", b"2\nx\n", b"2\n0\n", b"2\n1\n1\n")

    def test_a_broken_record_refuses_in_every_command_with_the_way_out(self):
        for index, form in enumerate(self.FORMS):
            with self.subTest(form=form):
                self.use(f"record-{index}")
                self.stopped_before_the_swap()
                record = self.gens() / ".publishing"
                record.write_bytes(form)
                live = entries(self.live())
                self.refused_keeping(_record_way_out(record), record, code="generation_unreadable")
                self.refused_keeping(_record_way_out(record), record, code="generation_unreadable")
                self.assertEqual(live, entries(self.live()))
                os.rename(record, self.tmp / f"moved-record-{index}")
                self.assertEqual(["fixture.unsigned"], self.listed_ids())

    def test_a_record_that_is_a_fifo_refuses_with_the_way_out_and_does_not_hang(self):
        self.stopped_before_the_swap()
        record = self.gens() / ".publishing"
        os.unlink(record)
        os.mkfifo(record, 0o600)
        self.fifos = [record]
        done = self._within(self.listed)
        self.assertEqual(("plugin_file_untrusted", _record_way_out(record)),
                         (done["error"].code, done["error"].detail))


if __name__ == "__main__":
    unittest.main()
