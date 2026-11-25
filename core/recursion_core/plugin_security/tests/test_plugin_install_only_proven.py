"""The installer deletes nothing it cannot prove is its own, and every refusal prints the way out.

(A) The swap of a publish, and the finishing of a stopped publish, remove a live entry only once it is
proven byte for byte equal to its copy in one of the two generations the record names: a folder, a regular
file, a link, a FIFO, any kind of entry under a published name. An entry that is not proven is never
removed: the command refuses ``plugin_directory_untrusted`` with the way out, and nothing is moved.
(B) The same for a publish that runs to its end, with one exception (slice 6): an installed plugin's folder
whose own files are each its generation's copy byte for byte, and which holds more (a settings file, a data
folder), does not refuse. The extra entries are moved into a kept place under ``plugin-generations``, the
command names the plugin and the place in one line, and install, update and remove complete. A folder with a
changed or missing file of its own, a folder no install wrote, and every case while a stopped publish is being
finished, refuse with the way out; nothing is deleted.
(C) A replaced entry is moved into a place of the publish's own under ``plugin-generations``, proven again
there and only then removed. What was put in its place between the check and the swap stays there: the
command says where in one line and exits non-zero.
(D) A file the installer reads that is not a regular file refuses without an open that can block.
(E) A broken, cut or empty ``.publishing`` record refuses with the way out printed; so does a record that is a
link, and a link under a plugin's name in the live tree with no stopped publish (slice 6).
(F, slice 6) After a publish that stopped, the live ``install.json`` is replaced only when it is absent or byte
for byte the copy of one of the two generations the record names. One changed by hand is never overwritten:
every command refuses with the way out and changes nothing.
"""
from __future__ import annotations

import json
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


def _install_file_way_out(path, copies):
    if len(copies) == 1:
        where = f"The copy it recorded is {copies[0]}. Save the changed file elsewhere and put that copy in its place"
    else:
        where = (f"The two copies it recorded are {copies[0]} and {copies[1]}. Save the changed file elsewhere "
                 f"and put either copy in its place")
    return f"The file {path} was changed after a command stopped part way. {where}; then run the command again."


def _extras_kept(folder, plugin_id, kept):
    return (f"The folder {folder} held entries the plugin {plugin_id} was not installed with: "
            f"they are kept at {kept}.")


def _whole_kept(folder, plugin_id, kept, kind="folder"):
    return (f"The {kind} {folder} of the plugin {plugin_id} is not the copy this command installed: "
            f"it is kept at {kept}.")


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
    """(B) A publish that runs to its end never deletes what a user keeps in a plugin folder: it keeps it aside."""

    EXTRAS = {
        "settings": {"settings.json": b"{}\n"},
        "data folder": {"data/cache.db": b"rows\n"},
        "empty folder": {"empty/": None},
    }

    def _with_extras(self, extra):
        folder = self.live() / "fixture.unsigned"
        for name, data in extra.items():
            if data is None:
                (folder / name).mkdir(mode=0o700)
                continue
            (folder / name).parent.mkdir(mode=0o700, exist_ok=True)
            (folder / name).write_bytes(data)
            os.chmod(folder / name, 0o600)
        return folder

    def _kept_places(self):
        return [path for path in sorted(self.gens().iterdir()) if path.name.startswith(".kept-")]

    def test_extra_entries_in_a_proven_plugin_folder_are_kept_aside_and_install_update_and_remove_complete(self):
        # Slice 6 (order lines 239-240): this replaces slice 5's refusal of install and remove.
        argvs = {
            "install": lambda: self.argv("install", self.roots["beta"], "fixture.beta"),
            "update": lambda: self.argv("install", self.roots["new"]),
            "remove": lambda: self.argv("remove"),
        }
        listed = {"install": ["fixture.beta", "fixture.unsigned"], "update": ["fixture.unsigned"], "remove": []}
        for kind, extra in self.EXTRAS.items():
            for command, argv in argvs.items():
                with self.subTest(kind=kind, command=command):
                    self.use(f"extra-{kind.replace(' ', '-')}-{command}")
                    self.install_from(self.roots["a"])
                    folder = self._with_extras(extra)
                    own = entries(self.gens() / "1" / "fixture.unsigned")
                    mine = {name: kept for name, kept in entries(folder).items() if name not in own}
                    status, err, out = self.main(argv())
                    places = self._kept_places()
                    self.assertEqual(1, len(places))
                    kept = places[0] / "fixture.unsigned"
                    line = _extras_kept(folder, "fixture.unsigned", kept)
                    self.assertEqual((0, f"{line}\n"), (status, err))
                    self.assertEqual([line], json.loads(out)["kept"])
                    self.assertEqual({".": ("dir",), **mine}, entries(kept))
                    self.assertEqual("2\n", self.current())
                    self.assertEqual(entries(self.gens() / "2"), entries(self.live()))
                    self.assertFalse(os.path.lexists(self.gens() / ".publishing"))
                    self.assertEqual(listed[command], self.listed_ids())

    def test_remove_returns_the_kept_place_and_a_command_with_nothing_kept_says_nothing(self):
        self.install_from(self.roots["a"])
        result = self.install_from(self.roots["beta"], "fixture.beta")
        self.assertNotIn("kept", result)
        folder = self._with_extras({"settings.json": b"{}\n"})
        result = self.remove("fixture.unsigned")
        kept = self._kept_places()[0] / "fixture.unsigned"
        self.assertEqual([_extras_kept(folder, "fixture.unsigned", kept)], result["kept"])
        self.assertEqual({".": ("dir",), "./settings.json": ("file", b"{}\n")}, entries(kept))
        self.assertEqual((0, ""), self.main(self.argv("remove", plugin_id="fixture.beta"))[:2])
        self.assertEqual([], self.listed_ids())

    def test_extra_entries_while_a_stopped_publish_is_finished_still_refuse_with_the_way_out(self):
        for stop in ("before the swap", "with the live tree whole"):
            with self.subTest(stop=stop):
                self.use(f"extra-stop-{stop[:6]}")
                if stop == "before the swap":
                    self.stopped_before_the_swap()
                else:
                    self.stopped_with_live_whole()
                folder = self._with_extras({"settings.json": b"{}\n"})
                self.refused_keeping(_move_out(folder), folder)
                self.assertEqual([], self._kept_places())
                self.assertTrue(os.path.lexists(self.gens() / ".publishing"))

    def test_a_command_that_keeps_extra_entries_and_meets_a_folder_put_in_place_names_both(self):
        from recursion_core.plugin_security import install as install_mod

        self.install_from(self.roots["a"])
        folder = self._with_extras({"settings.json": b"{}\n"})
        hand = self.live() / "hand.made"
        real = install_mod._swap_into

        def swap(plugins, stage, old):
            hand.mkdir(mode=0o700)
            (hand / "notes.txt").write_bytes(b"mine\n")
            return real(plugins, stage, old)

        install_mod._swap_into = swap
        self.addCleanup(setattr, install_mod, "_swap_into", real)
        refusal = self.refused(lambda: self.install_from(self.roots["beta"], "fixture.beta"))
        extras_place, hand_place = self._kept_places()
        line = _extras_kept(folder, "fixture.unsigned", extras_place / "fixture.unsigned")
        self.assertEqual(("plugin_directory_untrusted", f"{line} {_kept_at(hand, hand_place / 'hand.made')}"),
                         (refusal.code, refusal.detail))
        self.assertEqual(("file", b"{}\n"), entries(extras_place / "fixture.unsigned")["./settings.json"])
        self.assertEqual(("file", b"mine\n"), entries(hand_place / "hand.made")["./notes.txt"])
        self.assertEqual(entries(self.gens() / "2"), entries(self.live()))

    def test_an_own_file_changed_after_the_folder_was_read_refuses_and_nothing_is_moved(self):
        from recursion_core.plugin_security import install as install_mod

        self.install_from(self.roots["a"])
        folder = self._with_extras({"settings.json": b"{}\n"})
        real = install_mod._write_profile

        def write(directory, snapshot):
            real(directory, snapshot)
            if directory.name.startswith(".partial-"):
                (folder / "plugin.py").write_bytes(b"CHANGED = True\n")

        install_mod._write_profile = write
        self.addCleanup(setattr, install_mod, "_write_profile", real)
        # A remove completes: the whole folder, changed file and extra, is kept aside; nothing is deleted.
        result = self.remove("fixture.unsigned")
        kept = self._kept_places()[0] / "fixture.unsigned"
        held = entries(kept)
        self.assertEqual([_whole_kept(folder, "fixture.unsigned", kept)], result["kept"])
        self.assertEqual(("file", b"{}\n"), held["./settings.json"])
        self.assertEqual(("file", b"CHANGED = True\n"), held["./plugin.py"])
        self.assertFalse(os.path.lexists(folder))
        self.assertEqual("2\n", self.current())

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


class InstallFileProvenTests(_Case):
    """(F) After a stopped publish, a live install.json changed by hand is never overwritten."""

    def _stop(self, stop):
        if stop == "before the swap":
            self.stopped_before_the_swap()
        else:
            self.stopped_with_live_whole()

    def test_a_hand_edit_of_the_live_install_file_after_a_stopped_publish_refuses_and_is_kept(self):
        for stop in ("before the swap", "with the live tree whole"):
            with self.subTest(stop=stop):
                self.use(f"install-file-{stop[:6]}")
                self._stop(stop)
                path = self.live() / "install.json"
                data = self.read_live()
                data["reserved"]["hosts"].append("mine.example")
                self.write_live(data)
                mine = path.read_bytes()
                copies = [self.gens() / "2" / "install.json", self.gens() / "1" / "install.json"]
                self.refused_keeping(_install_file_way_out(path, copies), path)
                self.assertTrue(os.path.lexists(self.gens() / ".publishing"))
                # The way out: the changed file saved elsewhere, either recorded copy in its place.
                saved = self.tmp / f"saved-{stop[:6]}"
                os.rename(path, saved)
                path.write_bytes(copies[1 if stop == "before the swap" else 0].read_bytes())
                os.chmod(path, 0o600)
                self.assertEqual(["fixture.beta", "fixture.unsigned"], self.listed_ids())
                self.assertFalse(os.path.lexists(self.gens() / ".publishing"))
                self.assertEqual(entries(self.gens() / "2"), entries(self.live()))
                self.assertEqual(mine, saved.read_bytes())

    def test_with_one_recorded_copy_the_way_out_names_that_copy(self):
        from recursion_core.plugin_security import install as install_mod

        self._fail_first_install(install_mod)
        path = self.live() / "install.json"
        data = self.read_live()
        data["reserved"]["hosts"].append("mine.example")
        self.write_live(data)
        self.refused_keeping(_install_file_way_out(path, [self.gens() / "1" / "install.json"]), path)

    def _fail_first_install(self, module):
        real = module._clear_marker

        def stop(*_args, **_kwargs):
            raise OSError("stopped")

        module._clear_marker = stop
        try:
            with self.assertRaises(OSError):
                self.install_from(self.roots["a"])
        finally:
            module._clear_marker = real
        self.assertEqual("1\n", (self.gens() / ".publishing").read_text())

    def test_a_live_install_file_taken_out_after_a_stopped_publish_lets_the_publish_finish(self):
        self.stopped_before_the_swap()
        os.rename(self.live() / "install.json", self.tmp / "taken-out")
        self.assertEqual(["fixture.beta", "fixture.unsigned"], self.listed_ids())
        self.assertEqual(entries(self.gens() / "2"), entries(self.live()))


class LinkWayOutTests(_Case):
    """(E, slice 6) A link at the record, or under a plugin's name with no stopped publish, prints the way out."""

    def test_a_record_that_is_a_link_refuses_in_every_command_with_the_way_out(self):
        for index, target in enumerate(("a good record", "nothing")):
            with self.subTest(target=target):
                self.use(f"record-link-{index}")
                self.stopped_before_the_swap()
                record = self.gens() / ".publishing"
                copy = self.tmp / f"record-copy-{index}"
                if target == "a good record":
                    copy.write_bytes(record.read_bytes())
                os.unlink(record)
                record.symlink_to(copy)
                self.refused_keeping(_record_way_out(record), record)
                os.rename(record, self.tmp / f"moved-record-link-{index}")
                self.assertEqual(["fixture.unsigned"], self.listed_ids())

    def test_a_link_under_a_plugin_name_with_no_stopped_publish_refuses_with_the_way_out(self):
        for where in ("inside", "outside"):
            with self.subTest(where=where):
                self.use(f"live-link-{where}")
                self.install_from(self.roots["a"])
                target = (self.config if where == "inside" else self.tmp) / f"elsewhere-{where}"
                target.mkdir(mode=0o700)
                (target / "notes.txt").write_bytes(b"mine\n")
                link = self.live() / "fixture.beta"
                link.symlink_to(target, target_is_directory=True)
                self.refused_keeping(_move_out(link), link)
                os.rename(link, self.tmp / f"moved-link-{where}")
                self.install_from(self.roots["beta"], "fixture.beta")
                self.assertEqual(["fixture.beta", "fixture.unsigned"], self.listed_ids())
                self.assertEqual(b"mine\n", (target / "notes.txt").read_bytes())


if __name__ == "__main__":
    unittest.main()
