"""A kept place is always named, and what cannot be moved aside refuses with the way out (slice 7).

(K1) The line that names a kept place is printed the moment the entries are moved, before any later step
can fail: a command that fails after the move still names every place it kept. ``list`` names every kept
place that holds entries, so a stop between the move and the line hides nothing. No empty kept folder is
left behind.
(K2) An extra entry that cannot be moved aside (a folder without write permission) does not end the command
with ``filesystem_refused``: what was moved is put back, nothing stays moved, and the command refuses
``plugin_directory_untrusted`` with the way out naming the plugin and the entry. Following it, the next
command finishes.
(K3) Every refusal caused by a link on a path the installer walks prints the way out; the code is unchanged.
(K4) An extra manifest.sig in an unsigned plugin's folder refuses with a text that says what it is; a
damaged record does not say the live install file was edited.
(K5) The border between extra and changed: an own file changed beside an extra refuses and nothing is
moved; an own file replaced by a link to an identical copy refuses with the link's way out; an own file
with only its mode changed is proven, and its extras are kept.
"""
from __future__ import annotations

import errno
import io
import json
import os
import unittest

from recursion_core.plugin_security.tests.test_plugin_install_only_proven import (
    _Case,
    _extras_kept,
    _move_out,
    _whole_kept,
    entries,
)
from recursion_core.plugin_security.tests.test_plugin_install_publish import _Stop

ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def _listed_kept(plugin_id, kept):
    return f"The entries kept aside from the plugin {plugin_id} are at {kept}."


def _unmovable(folder, entry, plugin_id):
    return (f"The folder {folder} holds {folder / entry}, which the plugin {plugin_id} was not installed with "
            f"and which cannot be moved aside ({os.strerror(errno.EACCES)}). Move it out of {folder} yourself "
            f"(a folder must allow writing to be moved); then run the command again.")


def _link(path):
    return (f"{path} is a link, and this command does not follow links. Move the link out of {path.parent} "
            f"and put the file or folder it stands for in its place; then run the command again.")


def _signature_extra(folder, plugin_id):
    return (f"The folder {folder} holds a manifest.sig the plugin {plugin_id} was not installed with: it was "
            f"installed unsigned, and this command does not take a signature put in its folder. Move "
            f"{folder / 'manifest.sig'} out of the folder; then run the command again.")


class _KeptCase(_Case):
    def _with_extras(self, extra, plugin_id="fixture.unsigned"):
        folder = self.live() / plugin_id
        for name, data in extra.items():
            (folder / name).write_bytes(data)
            os.chmod(folder / name, 0o600)
        return folder

    def _kept_places(self):
        return [path for path in sorted(self.gens().iterdir()) if path.name.startswith(".kept-")]

    def _argvs(self):
        return {
            "install": self.argv("install", self.roots["beta"], "fixture.beta"),
            "update": self.argv("install", self.roots["new"]),
            "remove": self.argv("remove"),
        }

    def _patched(self, module, name, replacement):
        real = getattr(module, name)
        setattr(module, name, replacement(real))
        self.addCleanup(setattr, module, name, real)


class KeptLineAtTheMoveTests(_KeptCase):
    """(K1) The kept line comes at the move; a failure or a stop after it never hides the kept entries."""

    def test_a_failure_at_the_record_write_after_the_move_still_names_the_kept_place(self):
        from recursion_core.plugin_security import install as install_mod
        from recursion_core.plugin_security._entrypoints import main

        for command in ("install", "remove"):
            with self.subTest(command=command):
                self.use(f"record-full-{command}")
                self.install_from(self.roots["a"])
                folder = self._with_extras({"settings.json": b"{}\n"})
                err, seen = io.StringIO(), {}

                def full(real, err=err, seen=seen):
                    def write(directory, name, data):
                        if name == ".publishing":
                            seen["err"] = err.getvalue()
                            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
                        return real(directory, name, data)
                    return write

                real = install_mod._atomic_replace
                install_mod._atomic_replace = full(real)
                try:
                    status = main(self._argvs()[command], io.StringIO(), err)
                finally:
                    install_mod._atomic_replace = real
                kept = self._kept_places()[0] / "fixture.unsigned"
                line = _extras_kept(folder, "fixture.unsigned", kept)
                # Printed at the move, before the record write that failed.
                self.assertEqual(f"{line}\n", seen["err"])
                self.assertEqual((1, f"{line}\nrefused: filesystem_refused\nThe system refused a file read or "
                                     f"write: [Errno {errno.ENOSPC}] {os.strerror(errno.ENOSPC)}\n"),
                                 (status, err.getvalue()))
                self.assertEqual({".": ("dir",), "./settings.json": ("file", b"{}\n")}, entries(kept))
                self.assertEqual("1\n", self.current())
                status, said, out = self.main(self.argv("list"))
                self.assertEqual((0, f"{_listed_kept('fixture.unsigned', kept)}\n"), (status, said))
                self.assertEqual([_listed_kept("fixture.unsigned", kept)], json.loads(out)["kept"])

    def test_a_library_caller_gets_the_kept_line_on_the_failure(self):
        from recursion_core.plugin_security import install as install_mod

        self.install_from(self.roots["a"])
        folder = self._with_extras({"settings.json": b"{}\n"})

        def full(real):
            def write(directory, name, data):
                if name == ".publishing":
                    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
                return real(directory, name, data)
            return write

        self._patched(install_mod, "_atomic_replace", full)
        with self.assertRaises(OSError) as caught:
            self.remove("fixture.unsigned")
        kept = self._kept_places()[0] / "fixture.unsigned"
        self.assertEqual([_extras_kept(folder, "fixture.unsigned", kept)], caught.exception.kept)

    def test_a_stop_between_the_move_and_the_line_is_named_by_the_next_list(self):
        from recursion_core.plugin_security import install as install_mod

        self.install_from(self.roots["a"])
        self._with_extras({"settings.json": b"{}\n"})

        def stop(real):
            def keep(config, number, extras, *_rest, **_kwargs):
                real(config, number, extras)  # the entries are moved and nothing is said: then the stop
                raise _Stop()
            return keep

        real = install_mod._keep_extras
        install_mod._keep_extras = stop(real)
        try:
            with self.assertRaises(_Stop):
                self.main(self._argvs()["install"])
        finally:
            install_mod._keep_extras = real
        kept = self._kept_places()[0] / "fixture.unsigned"
        status, said, out = self.main(self.argv("list"))
        self.assertEqual((0, f"{_listed_kept('fixture.unsigned', kept)}\n"), (status, said))
        self.assertEqual(["fixture.unsigned"], [row["plugin_id"] for row in json.loads(out)["plugins"]])
        self.assertEqual((0, ""), self.main(self._argvs()["install"])[:2])
        self.assertEqual(["fixture.beta", "fixture.unsigned"], self.listed_ids())
        self.assertEqual([_listed_kept("fixture.unsigned", kept)], self.listed()["kept"])
        self.assertEqual({".": ("dir",), "./settings.json": ("file", b"{}\n")}, entries(kept))

    def test_list_names_only_kept_places_that_hold_entries(self):
        self.install_from(self.roots["a"])
        self.install_from(self.roots["beta"], "fixture.beta")
        status, said, out = self.main(self.argv("list"))
        self.assertEqual((0, ""), (status, said))
        self.assertNotIn("kept", json.loads(out))
        beta = self._with_extras({"b.txt": b"mine\n"}, "fixture.beta")
        status, said, _out = self.main(self.argv("remove", plugin_id="fixture.beta"))
        kept = self._kept_places()[0] / "fixture.beta"
        self.assertEqual((0, f"{_extras_kept(beta, 'fixture.beta', kept)}\n"), (status, said))
        # Not kept places: a file, an empty folder and a link under kept names; nothing is followed.
        (self.gens() / ".kept-9").write_bytes(b"x\n")
        (self.gens() / ".kept-8").mkdir(mode=0o700)
        (self.gens() / ".kept-8" / "fixture.zzz").mkdir(mode=0o700)
        outside = self.tmp / "outside"
        (outside / "fixture.yyy").mkdir(parents=True)
        (outside / "fixture.yyy" / "notes.txt").write_bytes(b"mine\n")
        (self.gens() / ".kept-7").symlink_to(outside, target_is_directory=True)
        status, said, out = self.main(self.argv("list"))
        self.assertEqual((0, f"{_listed_kept('fixture.beta', kept)}\n"), (status, said))
        self.assertEqual([_listed_kept("fixture.beta", kept)], json.loads(out)["kept"])
        self.assertEqual(b"mine\n", (kept / "b.txt").read_bytes())


@unittest.skipIf(ROOT, "root moves a folder it cannot write to")
class UnmovableExtraTests(_KeptCase):
    """(K2) An extra that cannot be moved aside refuses with the way out; nothing stays moved."""

    def _read_only_folder(self, folder):
        locked = folder / "ro"
        locked.mkdir(mode=0o700)
        (locked / "inside.txt").write_bytes(b"mine\n")
        os.chmod(locked / "inside.txt", 0o600)
        os.chmod(locked, 0o500)
        self.addCleanup(lambda: os.path.isdir(locked) and os.chmod(locked, 0o700))
        return locked

    def test_an_extra_folder_that_cannot_be_moved_refuses_with_the_way_out_for_install_update_and_remove(self):
        listed = {"install": ["fixture.beta", "fixture.unsigned"], "update": ["fixture.unsigned"], "remove": []}
        for command in ("install", "update", "remove"):
            with self.subTest(command=command):
                self.use(f"read-only-{command}")
                self.install_from(self.roots["a"])
                folder = self._with_extras({"a.txt": b"mine\n"})
                locked = self._read_only_folder(folder)
                held, pointer, listing = entries(folder), self.current(), self.generations()
                self.assertEqual((1, f"refused: plugin_directory_untrusted\n"
                                     f"{_unmovable(folder, 'ro', 'fixture.unsigned')}\n", ""),
                                 self.main(self._argvs()[command]))
                self.assertEqual(held, entries(folder))
                self.assertEqual([], self._kept_places())
                self.assertEqual((pointer, listing), (self.current(), self.generations()))
                # The way out: the folder given write permission and moved out; the command finishes.
                os.chmod(locked, 0o700)
                os.rename(locked, self.tmp / f"ro-{command}")
                status, said, _out = self.main(self._argvs()[command])
                kept = self._kept_places()[0] / "fixture.unsigned"
                self.assertEqual((0, f"{_extras_kept(folder, 'fixture.unsigned', kept)}\n"), (status, said))
                self.assertEqual({".": ("dir",), "./a.txt": ("file", b"mine\n")}, entries(kept))
                self.assertEqual(listed[command], self.listed_ids())

    def test_an_unmovable_extra_of_another_plugin_puts_back_what_was_already_moved(self):
        self.install_from(self.roots["a"])
        self.install_from(self.roots["beta"], "fixture.beta")
        beta = self._with_extras({"b.txt": b"mine\n"}, "fixture.beta")
        folder = self.live() / "fixture.unsigned"
        locked = self._read_only_folder(folder)
        held = entries(self.live())
        self.assertEqual((1, f"refused: plugin_directory_untrusted\n{_unmovable(folder, 'ro', 'fixture.unsigned')}\n",
                          ""), self.main(self.argv("remove", plugin_id="fixture.beta")))
        self.assertEqual(held, entries(self.live()))
        self.assertEqual([], self._kept_places())
        self.assertEqual("2\n", self.current())
        os.chmod(locked, 0o700)
        os.rename(locked, self.tmp / "ro")
        status, said, _out = self.main(self.argv("remove", plugin_id="fixture.beta"))
        kept = self._kept_places()[0] / "fixture.beta"
        self.assertEqual((0, f"{_extras_kept(beta, 'fixture.beta', kept)}\n"), (status, said))
        self.assertEqual(["fixture.unsigned"], self.listed_ids())


class LinkWayOutEverywhereTests(_KeptCase):
    """(K3) A link anywhere the installer walks refuses with its usual code and the way out."""

    def test_a_link_at_each_path_the_installer_walks_refuses_with_the_way_out(self):
        cases = (
            ("config", lambda: self.config, "plugin_directory_untrusted"),
            ("lock file", lambda: self.config / "plugin-install.lock", "plugin_directory_untrusted"),
            ("plugin-generations", lambda: self.gens(), "plugin_directory_untrusted"),
            ("current", lambda: self.gens() / "current", "plugin_directory_untrusted"),
            ("generation", lambda: self.gens() / "1", "generation_unreadable"),
            ("plugins", lambda: self.live(), "plugin_directory_untrusted"),
            ("install.json", lambda: self.live() / "install.json", "plugin_directory_untrusted"),
            ("plugin.py", lambda: self.live() / "fixture.unsigned" / "plugin.py", "plugin_file_untrusted"),
        )
        for name, where, code in cases:
            with self.subTest(link=name):
                self.use("link-" + name.replace(" ", "-").replace(".", "-"))
                self.install_from(self.roots["a"])
                path = where()
                target = self.link_in_place(path)
                for command in ("list", "install", "remove"):
                    argv = self.argv(command, self.roots["a"])
                    if name == "plugin.py" and command == "remove":
                        # A remove completes: the plugin's folder, link and all, is kept aside whole.
                        folder, held = path.parent, entries(path.parent)
                        status, said, _out = self.main(argv)
                        kept = self._kept_places()[0] / "fixture.unsigned"
                        self.assertEqual((0, f"{_whole_kept(folder, 'fixture.unsigned', kept)}\n"), (status, said))
                        self.assertEqual(held, entries(kept))
                        self.assertEqual([], self.listed_ids())
                        break
                    self.assertEqual((1, f"refused: {code}\n{_link(path)}\n", ""), self.main(argv), command)
                else:
                    # The way out: the link moved out and what it stands for put in its place.
                    os.unlink(path)
                    os.rename(target, path)
                    self.assertEqual(["fixture.unsigned"], self.listed_ids())

    def test_a_manifest_sig_link_and_a_revocation_store_link_refuse_with_the_way_out(self):
        self.install_from(self.roots["a"])
        signature = self.live() / "fixture.unsigned" / "manifest.sig"
        target = self.tmp / "a-signature"
        target.write_bytes(b"sig\n")
        signature.symlink_to(target)
        for command in ("list", "install"):
            self.assertEqual((1, f"refused: plugin_directory_untrusted\n{_link(signature)}\n", ""),
                             self.main(self.argv(command, self.roots["a"])), command)
        # A remove completes: the link is an entry the plugin was not installed with, kept aside, never followed.
        status, said, _out = self.main(self.argv("remove", self.roots["a"]))
        kept = self._kept_places()[0] / "fixture.unsigned"
        self.assertEqual((0, f"{_extras_kept(signature.parent, 'fixture.unsigned', kept)}\n"), (status, said))
        self.assertEqual(("link", str(target)), entries(kept)["./manifest.sig"])
        self.assertEqual(b"sig\n", target.read_bytes())
        store = self.state / "plugin-security" / "revoked.sqlite3"
        store.parent.mkdir(parents=True, exist_ok=True)
        store.symlink_to(target)
        self.assertEqual((1, f"refused: revocation_store_invalid\n{_link(store)}\n", ""),
                         self.main(self.argv("install", self.roots["beta"], "fixture.beta")))
        os.unlink(store)
        self.assertEqual(0, self.main(self.argv("install", self.roots["beta"], "fixture.beta"))[0])


    def test_a_link_inside_a_folder_no_install_wrote_still_says_move_the_folder_out(self):
        self.install_from(self.roots["a"])
        folder = self.live() / "fixture.zzz"
        folder.mkdir(mode=0o700)
        copy = self.tmp / "manifest-copy"
        copy.write_bytes((self.live() / "fixture.unsigned" / "manifest.json").read_bytes())
        (folder / "manifest.json").symlink_to(copy)
        self.assertEqual((1, f"refused: plugin_directory_untrusted\n{_move_out(folder)}\n", ""),
                         self.main(self.argv("list")))


class TextTests(_KeptCase):
    """(K4) A refusal says what it found."""

    def test_an_extra_manifest_sig_in_an_unsigned_plugins_folder_says_what_it_is(self):
        self.install_from(self.roots["a"])
        folder = self._with_extras({"manifest.sig": b"sig\n"})
        held = entries(folder)
        for command in ("list", "install", "remove"):
            self.assertEqual((1, f"refused: plugin_directory_untrusted\n"
                                 f"{_signature_extra(folder, 'fixture.unsigned')}\n", ""),
                             self.main(self.argv(command, self.roots["beta"], "fixture.beta")), command)
        self.assertEqual(held, entries(folder))
        self.assertEqual([], self._kept_places())
        os.rename(folder / "manifest.sig", self.tmp / "moved-sig")
        self.install_from(self.roots["beta"], "fixture.beta")
        self.assertEqual(["fixture.beta", "fixture.unsigned"], self.listed_ids())

    def test_a_damaged_record_does_not_say_the_install_file_was_changed(self):
        forms = {
            b"2\n": lambda gens: f"it names no generation it replaced, though {gens / '1'} is on disk",
            b"2\n9\n": lambda gens: f"the generation {gens / '9'} it names is not on disk",
            b"2\n2\n": lambda gens: f"it names {gens / '2'} twice",
        }
        for index, (form, why) in enumerate(forms.items()):
            with self.subTest(form=form):
                self.use(f"damaged-{index}")
                self.stopped_before_the_swap()
                record = self.gens() / ".publishing"
                record.write_bytes(form)
                path = self.live() / "install.json"
                copy = self.gens() / "2" / "install.json"
                sentence = (f"The file {path} is not the copy of the generation the record {record} names, and "
                            f"that record is damaged: {why(self.gens())}. This command cannot tell whether anyone "
                            f"edited it. Save it elsewhere and put {copy} in its place; then run the command again.")
                for command in ("list", "install", "remove"):
                    status, said, out = self.main(self.argv(command, self.roots["beta"], "fixture.beta"))
                    self.assertEqual((1, f"refused: plugin_directory_untrusted\n{sentence}\n", ""),
                                     (status, said, out), command)
                    self.assertNotIn("was changed", said)
                os.rename(path, self.tmp / f"saved-{index}")
                path.write_bytes(copy.read_bytes())
                os.chmod(path, 0o600)
                self.assertEqual(["fixture.beta", "fixture.unsigned"], self.listed_ids())
                self.assertFalse(os.path.lexists(record))


    def test_a_record_naming_a_missing_generation_refuses_generation_missing_and_says_nothing_changed(self):
        self.stopped_before_the_swap()
        data = self.read_live()
        data["reserved"]["hosts"].append("mine.example")
        self.write_live(data)
        mine = (self.live() / "install.json").read_bytes()
        os.rename(self.gens() / "2", self.tmp / "generation-2")
        for command in ("list", "remove"):
            self.assertEqual((1, "refused: generation_missing\n", ""), self.main(self.argv(command)), command)
        self.assertEqual(mine, (self.live() / "install.json").read_bytes())


class BorderTests(_KeptCase):
    """(K5) Where an extra entry ends and a changed folder begins."""

    def test_an_own_file_changed_beside_an_extra_refuses_and_nothing_is_moved(self):
        for command in ("install", "update", "remove"):
            with self.subTest(command=command):
                self.use(f"changed-{command}")
                self.install_from(self.roots["a"])
                folder = self._with_extras({"settings.json": b"{}\n", "plugin.py": b"CHANGED = True\n"})
                held = entries(folder)
                if command == "remove":
                    # A remove completes: the whole folder, changed file and extra, is kept aside.
                    status, said, _out = self.main(self._argvs()[command])
                    kept = self._kept_places()[0] / "fixture.unsigned"
                    self.assertEqual((0, f"{_whole_kept(folder, 'fixture.unsigned', kept)}\n"), (status, said))
                    self.assertEqual(held, entries(kept))
                    self.assertEqual("2\n", self.current())
                    continue
                self.assertEqual((1, f"refused: plugin_directory_untrusted\n{_move_out(folder)}\n", ""),
                                 self.main(self._argvs()[command]))
                self.assertEqual(held, entries(folder))
                self.assertEqual([], self._kept_places())
                self.assertEqual("1\n", self.current())

    def test_an_own_file_replaced_by_a_link_to_an_identical_copy_refuses_with_the_links_way_out(self):
        self.install_from(self.roots["a"])
        folder = self._with_extras({"settings.json": b"{}\n"})
        self.link_in_place(folder / "plugin.py")
        held = entries(folder)
        for command, argv in self._argvs().items():
            if command == "remove":
                continue
            self.assertEqual((1, f"refused: plugin_file_untrusted\n{_link(folder / 'plugin.py')}\n", ""),
                             self.main(argv), command)
        self.assertEqual(held, entries(folder))
        self.assertEqual([], self._kept_places())
        # A remove completes: the whole folder, link and extra, is kept aside; the link is never followed.
        status, said, _out = self.main(self._argvs()["remove"])
        kept = self._kept_places()[0] / "fixture.unsigned"
        self.assertEqual((0, f"{_whole_kept(folder, 'fixture.unsigned', kept)}\n"), (status, said))
        self.assertEqual(held, entries(kept))

    def test_an_own_file_with_only_its_mode_changed_is_proven_and_its_extras_are_kept(self):
        self.install_from(self.roots["a"])
        folder = self._with_extras({"settings.json": b"{}\n"})
        os.chmod(folder / "plugin.py", 0o400)
        status, said, _out = self.main(self._argvs()["install"])
        kept = self._kept_places()[0] / "fixture.unsigned"
        self.assertEqual((0, f"{_extras_kept(folder, 'fixture.unsigned', kept)}\n"), (status, said))
        self.assertEqual({".": ("dir",), "./settings.json": ("file", b"{}\n")}, entries(kept))
        self.assertEqual(entries(self.gens() / "2"), entries(self.live()))


if __name__ == "__main__":
    unittest.main()
