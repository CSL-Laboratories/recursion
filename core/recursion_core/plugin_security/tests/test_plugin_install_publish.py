"""Commit and publish order of ``plugin install``, ``list`` and ``remove``, and the rules that make it a trust boundary.

A stop at any step of the commit leaves the next install and the next remove working: a generation that
``current`` does not name is no obstacle. Every check of the live ``plugins/`` path runs before a
generation is written, so a refused command leaves ``current`` and the generations as they were. At no
step does the live tree hold a plugin directory without the whole old or the whole new install.json, and
each written file and directory is synced before the rename that publishes it. The config directory is
checked before anything is changed. Each trust rule below has a test that goes red when the rule is cut.
A stop is simulated by an exception no handler of the code catches; after it nothing more is changed,
as after a real stop of the process.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import stat
import unittest

from recursion_core.plugin_security.tests.support import PLUGIN_SOURCE
from recursion_core.plugin_security.tests.test_plugin_cli_install import (
    LINUX, LINUX_ONLY, ProfileCase, _entry, _manifest, _sha,
)

SOURCE_BETA = b"VALUE = 2\n"
SOURCE_NEW = b"VALUE = 3\n"


class _Stop(BaseException):
    """A stop of the process. It is not an Exception, so no handler of the code under test catches it."""


class _Stopper:
    """Stop at the ``at``-th change of the file tree under ``watch``; after that, at every change under ``scope``.

    A change is os.mkdir, os.rename, os.replace, os.unlink, os.rmdir, or an os.open that may create.
    """

    NAMES = ("mkdir", "rename", "replace", "unlink", "rmdir", "open")

    def __init__(self, watch, scope, at):
        self.watch = os.path.abspath(os.fspath(watch))
        self.scope = os.path.abspath(os.fspath(scope))
        self.at = at
        self.count = 0
        self.stopped = False
        self.real = {}

    @staticmethod
    def _under(path, root):
        return path == root or path.startswith(root + os.sep)

    def _wrap(self, name):
        real = self.real[name]

        def call(*args, **kwargs):
            if kwargs.get("dir_fd") is not None or kwargs.get("src_dir_fd") is not None:
                return real(*args, **kwargs)
            if name == "open":
                flags = args[1] if len(args) > 1 else kwargs.get("flags", 0)
                if not flags & os.O_CREAT:
                    return real(*args, **kwargs)
            paths = [os.path.abspath(os.fspath(arg)) for arg in args[:2] if isinstance(arg, (str, os.PathLike))]
            if self.stopped:
                if any(self._under(path, self.scope) for path in paths):
                    raise _Stop()
                return real(*args, **kwargs)
            if any(self._under(path, self.watch) for path in paths):
                if self.count == self.at:
                    self.stopped = True
                    raise _Stop()
                self.count += 1
            return real(*args, **kwargs)

        return call

    def __enter__(self):
        for name in self.NAMES:
            self.real[name] = getattr(os, name)
            setattr(os, name, self._wrap(name))
        return self

    def __exit__(self, *_exc):
        for name, real in self.real.items():
            setattr(os, name, real)
        return False


@unittest.skipUnless(LINUX, LINUX_ONLY)
class PublishCase(ProfileCase):
    def use(self, name):
        self.config = self.tmp / name / "config" / "recursion"

    def registries(self):
        roots = {}
        for name, kwargs in (("a", {}), ("beta", {"plugin_id": "fixture.beta", "source": SOURCE_BETA}),
                             ("new", {"version": "1.1.0", "source": SOURCE_NEW})):
            self.registry = self.tmp / ("registry-" + name)
            self.write_registry(**kwargs)
            roots[name] = self.registry
        return roots

    def install_from(self, root, plugin_id="fixture.unsigned", **kwargs):
        self.registry = root
        return self.install(plugin_id, **kwargs)

    def stopped(self, at, watch, action):
        """Run ``action`` with a stop at the ``at``-th change under ``watch``. True when the stop came."""
        with _Stopper(watch, self.config, at):
            try:
                action()
            except _Stop:
                return True
        return False

    def generations(self):
        return sorted(os.listdir(self.config / "plugin-generations"))

    def gens(self):
        return self.config / "plugin-generations"

    def current(self):
        path = self.config / "plugin-generations" / "current"
        return path.read_text() if os.path.lexists(path) else None

    def listed_ids(self):
        return [row["plugin_id"] for row in self.listed()["plugins"]]

    @staticmethod
    def tree(directory):
        """Every file under ``directory`` by its relative path, with its bytes."""
        found = {}
        for dirpath, _dirnames, filenames in os.walk(directory):
            for name in filenames:
                path = Path(dirpath) / name
                found[path.relative_to(directory).as_posix()] = path.read_bytes()
        return found

    @staticmethod
    def part(found, name):
        return {key: value for key, value in found.items() if key.startswith(name + "/")}

    def refused(self, command):
        from recursion_core.plugin_security.install import InstallRefused

        with self.assertRaises(InstallRefused) as caught:
            command()
        return caught.exception

    def link_in_place(self, path):
        """Move ``path`` out of its tree and put a symbolic link to it where it was."""
        self.moved = getattr(self, "moved", 0) + 1
        away = self.tmp / "away" / str(self.moved)
        away.mkdir(parents=True)
        target = away / path.name
        os.rename(path, target)
        path.symlink_to(target, target_is_directory=target.is_dir())
        return target


class CommitStopTests(PublishCase):
    """(P3) A failure or a stop at any step of the commit leaves the next install and the next remove working."""

    def _pointer_fails_once(self, action):
        from recursion_core.plugin_security import install as install_mod

        real = install_mod._write_pointer

        def full_disk(*_args, **_kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        install_mod._write_pointer = full_disk
        try:
            with self.assertRaises(install_mod.InstallRefused) as caught:
                action()
            self.assertEqual("filesystem_refused", caught.exception.code)
        finally:
            install_mod._write_pointer = real

    def test_a_failure_between_the_generation_rename_and_the_pointer_write_blocks_no_later_install_or_remove(self):
        roots = self.registries()
        self.use("update")
        self.install_from(roots["a"])
        self._pointer_fails_once(lambda: self.install_from(roots["beta"], "fixture.beta"))
        # Generation 2 is on disk and current still names 1.
        self.assertEqual("1\n", self.current())
        self.assertIn("2", self.generations())
        result = self.install_from(roots["beta"], "fixture.beta")
        self.assertEqual(f"{result['generation']}\n", self.current())
        self.assertEqual(["fixture.beta", "fixture.unsigned"], self.listed_ids())
        removed = self.remove("fixture.unsigned")
        self.assertEqual(f"{removed['generation']}\n", self.current())
        self.assertEqual(["fixture.beta"], self.listed_ids())

    def test_a_failure_before_the_first_pointer_write_blocks_no_later_install_or_remove(self):
        roots = self.registries()
        self.use("first")
        self._pointer_fails_once(lambda: self.install_from(roots["a"]))
        self.assertIsNone(self.current())
        self.assertIn("1", self.generations())
        self.install_from(roots["a"])
        self.assertEqual(["fixture.unsigned"], self.listed_ids())
        self.remove("fixture.unsigned")
        self.assertEqual([], self.listed_ids())

    def test_a_stop_at_any_step_of_the_commit_leaves_install_and_remove_working(self):
        roots = self.registries()
        for first in (True, False):
            for at in range(400):
                self.use(f"stop-{int(first)}-{at}")
                if not first:
                    self.install_from(roots["a"])
                came = self.stopped(at, self.config / "plugin-generations",
                                    lambda: self.install_from(roots["beta"], "fixture.beta"))
                with self.subTest(first=first, at=at):
                    self.install_from(roots["beta"], "fixture.beta")
                    kept = [] if first else ["fixture.unsigned"]
                    self.assertEqual(sorted(["fixture.beta", *kept]), self.listed_ids())
                    self.remove("fixture.beta")
                    self.assertEqual(kept, self.listed_ids())
                if not came:
                    break
            else:
                self.fail("the commit did not finish within 400 steps")


class PublishOrderTests(PublishCase):
    """(P4) The live tree always holds a whole install.json beside its plugin directories; writes are synced."""

    def test_a_stop_at_each_publish_step_leaves_a_whole_install_file_beside_every_plugin_directory(self):
        from recursion_core.daemon import plugins as daemon_plugins

        roots = self.registries()
        for at in range(400):
            self.use(f"publish-{at}")
            self.install_from(roots["a"])
            self.install_from(roots["beta"], "fixture.beta")
            live = self.config / "plugins"
            generations = self.config / "plugin-generations"
            came = self.stopped(at, live, lambda: self.install_from(roots["new"]))
            old, new = self.tree(generations / "2"), self.tree(generations / "3")
            with self.subTest(at=at):
                seen = self.tree(live)
                directories = [name for name in os.listdir(live)
                               if (live / name).is_dir() and not (live / name).is_symlink()]
                if directories:
                    self.assertIn("install.json", seen, f"plugin directories {directories} with no install.json")
                    self.assertIn(seen["install.json"], (old["install.json"], new["install.json"]))
                    daemon_plugins.read_install(live)
                for name in directories:
                    self.assertIn(self.part(seen, name), (self.part(old, name), self.part(new, name)),
                                  f"{name} is not a whole copy")
                # The next list publishes the new generation whole.
                self.listed()
                self.assertEqual(new, self.tree(live))
            if not came:
                break
        else:
            self.fail("the publish did not finish within 400 steps")

    def test_each_written_file_and_directory_is_synced_before_the_rename_that_publishes_it(self):
        roots = self.registries()
        for first in (True, False):
            with self.subTest(first=first):
                self.use(f"sync-{int(first)}")
                if not first:
                    self.install_from(roots["a"])
                events = self._record(lambda: self.install_from(roots["new"]))
                live = os.path.realpath(self.config / "plugins")
                renames = [(index, event) for index, event in enumerate(events)
                           if event[0] == "rename" and not _Stopper._under(event[1], live)]
                self.assertTrue(renames)
                for index, (_kind, source, dest, inside) in renames:
                    before = {event[1] for event in events[:index] if event[0] == "fsync"}
                    after = {event[1] for event in events[index + 1:] if event[0] == "fsync"}
                    for path in inside:
                        self.assertIn(path, before, f"{path} not synced before its rename to {dest}")
                    self.assertIn(os.path.dirname(dest), after, f"the directory of {dest} not synced after the rename")
                # The generation's rename is synced before the pointer moves, and install.json's rename
                # before any plugin directory in plugins/ changes.
                generations = os.path.realpath(self.gens())
                made = next(index for index, event in renames if os.path.dirname(event[2]) == generations
                            and os.path.basename(event[2]).isdigit())
                pointer = next(index for index, event in renames if event[2] == os.path.join(generations, "current"))
                self.assertIn(("fsync", generations), events[made + 1:pointer],
                              "the generation's rename is not synced before the pointer moves")
                put = next(index for index, event in renames if event[2] == os.path.join(live, "install.json"))
                changed = next((index for index, event in enumerate(events) if index > put and event[0] == "rename"
                                and live in (os.path.dirname(event[1]), os.path.dirname(event[2]))), len(events))
                self.assertIn(("fsync", live), events[put + 1:changed],
                              "install.json's rename is not synced before a plugin directory changes")

    def _record(self, action):
        events = []
        real_fsync, real_rename = os.fsync, os.rename

        def fsync(fd):
            events.append(("fsync", os.readlink(f"/proc/self/fd/{fd}")))
            return real_fsync(fd)

        def rename(source, dest, *args, **kwargs):
            source_text = os.path.realpath(os.fspath(source))
            inside = [source_text]
            if os.path.isdir(source_text):
                for dirpath, dirnames, filenames in os.walk(source_text):
                    inside += [os.path.join(dirpath, name) for name in dirnames + filenames]
            events.append(("rename", source_text, os.path.realpath(os.fspath(dest)), inside))
            return real_rename(source, dest, *args, **kwargs)

        os.fsync, os.rename = fsync, rename
        try:
            action()
        finally:
            os.fsync, os.rename = real_fsync, real_rename
        return events


class RefusedPublishTests(PublishCase):
    """(P5) A refusal of the live plugins/ path comes before a generation is written; a retry adds none."""

    def _refused_twice_and_nothing_moved(self, roots):
        pointer, listing = self.current(), self.generations()
        for command in (lambda: self.install_from(roots["a"]), lambda: self.install_from(roots["a"]),
                        lambda: self.remove("fixture.unsigned"), self.listed):
            self.assertEqual("plugin_directory_untrusted", self.refused(command).code)
            self.assertEqual(pointer, self.current())
            self.assertEqual(listing, self.generations())

    def test_a_group_writable_live_plugins_directory_refuses_before_a_generation_is_written(self):
        roots = self.registries()
        self.install_from(roots["a"])
        os.chmod(self.config / "plugins", 0o770)
        self._refused_twice_and_nothing_moved(roots)
        self.assertEqual(0o770, stat.S_IMODE(os.lstat(self.config / "plugins").st_mode))

    def test_a_live_plugins_link_refuses_before_a_generation_is_written(self):
        roots = self.registries()
        self.install_from(roots["a"])
        target = self.link_in_place(self.config / "plugins")
        held = self.tree(target)
        self._refused_twice_and_nothing_moved(roots)
        self.assertEqual(held, self.tree(target))


class ConfigDirectoryTests(PublishCase):
    """(P8) The config directory is lstat'ed first; a refusal changes nothing, not even a link's target."""

    def _commands(self):
        roots = self.registries()
        return (self.listed, lambda: self.install_from(roots["a"]), lambda: self.remove("fixture.unsigned"))

    def test_a_config_path_that_links_to_a_directory_refuses_and_leaves_the_target_as_it_was(self):
        target = self.tmp / "target"
        target.mkdir()
        os.chmod(target, 0o755)
        self.config.parent.mkdir(parents=True)
        self.config.symlink_to(target, target_is_directory=True)
        for command in self._commands():
            self.assertEqual("plugin_directory_untrusted", self.refused(command).code)
            self.assertEqual(0o755, stat.S_IMODE(os.stat(target).st_mode))
            self.assertEqual([], os.listdir(target))

    def test_a_group_writable_config_directory_refuses_and_is_not_narrowed(self):
        self.config.mkdir(parents=True, mode=0o700)
        os.chmod(self.config, 0o770)
        for command in self._commands():
            self.assertEqual("plugin_directory_untrusted", self.refused(command).code)
            self.assertEqual(0o770, stat.S_IMODE(os.lstat(self.config).st_mode))
            self.assertEqual([], os.listdir(self.config))

    def test_a_config_path_that_is_a_file_refuses_typed(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_bytes(b"not a directory\n")
        for command in self._commands():
            self.assertEqual("plugin_directory_untrusted", self.refused(command).code)
            self.assertEqual(b"not a directory\n", self.config.read_bytes())

    def test_a_config_directory_this_call_made_is_owner_only(self):
        self.listed()
        self.assertEqual(0o700, stat.S_IMODE(os.lstat(self.config).st_mode))


class TrustRuleTests(PublishCase):
    """(P12) Each rule that makes the install a trust boundary goes red when it is cut."""

    def _nothing_written(self):
        self.assertFalse(os.path.lexists(self.config / "plugins"))
        self.assertFalse(os.path.lexists(self.config / "plugin-generations"))

    def test_a_link_at_each_level_of_the_registry_refuses_registry_symlink_and_writes_nothing(self):
        levels = {
            "registry": lambda root, release: root / "registry",
            "entry": lambda root, release: release.parent,
            "entry.json": lambda root, release: release.parent / "entry.json",
            "release": lambda root, release: release,
            "manifest.json": lambda root, release: release / "manifest.json",
            "plugin.py": lambda root, release: release / "plugin.py",
            "manifest.sig": lambda root, release: release / "manifest.sig",
        }
        for level, where in levels.items():
            with self.subTest(level=level):
                self.use("registry-" + level)
                self.registry = self.tmp / ("linked-" + level)
                self.write_registry(signature="bad" if level == "manifest.sig" else "omit")
                release = self.registry / "registry" / "fixture.unsigned" / "1.0.0"
                if level == "manifest.sig":
                    (release / "manifest.sig").write_text("00" * 64 + "\n")
                self.link_in_place(where(self.registry, release))
                self.assertIn("registry_symlink", self.codes_of(self.registry))
                refusal = self.refused(self.install)
                self.assertIn("registry_symlink", refusal.codes)
                self._nothing_written()

    def test_a_link_or_group_write_at_each_level_of_an_installed_profile_refuses_typed(self):
        roots = self.registries()
        cases = (
            ("plugin-generations link", lambda: self.link_in_place(self.gens()), "plugin_directory_untrusted"),
            ("plugin-generations group write", lambda: os.chmod(self.gens(), 0o770), "plugin_directory_untrusted"),
            ("current link", lambda: self.link_in_place(self.gens() / "current"), "plugin_directory_untrusted"),
            ("current group write", lambda: os.chmod(self.gens() / "current", 0o620), "plugin_file_untrusted"),
            ("plugins link", lambda: self.link_in_place(self.config / "plugins"), "plugin_directory_untrusted"),
            ("plugins group write", lambda: os.chmod(self.config / "plugins", 0o770), "plugin_directory_untrusted"),
            ("plugin directory link", lambda: self.link_in_place(self.gens() / "1" / "fixture.unsigned"),
             "plugin_directory_untrusted"),
            ("plugin directory group write", lambda: os.chmod(self.gens() / "1" / "fixture.unsigned", 0o770),
             "plugin_directory_untrusted"),
            ("install.json link", lambda: self.link_in_place(self.gens() / "1" / "install.json"),
             "plugin_directory_untrusted"),
            ("install.json group write", lambda: os.chmod(self.gens() / "1" / "install.json", 0o620),
             "plugin_file_untrusted"),
        )
        for name, change, code in cases:
            with self.subTest(level=name):
                self.use("profile-" + name.replace(" ", "-"))
                self.install_from(roots["a"])
                pointer, listing = self.current(), self.generations()
                change()
                for command in (self.listed, lambda: self.install_from(roots["a"]),
                                lambda: self.remove("fixture.unsigned")):
                    self.assertEqual(code, self.refused(command).code)
                    self.assertEqual(pointer, self.current())
                    self.assertEqual(listing, self.generations())

    def _write_live_profile(self):
        """A live plugins/ tree placed before any install: an empty install.json and one plugin directory."""
        release = self.registry / "registry" / "fixture.unsigned" / "1.0.0"
        live = self.config / "plugins"
        (live / "fixture.unsigned").mkdir(parents=True, mode=0o700)
        os.chmod(self.config, 0o700)
        os.chmod(self.config.parent, 0o700)
        os.chmod(live, 0o700)
        (live / "install.json").write_bytes(b"{}\n")
        os.chmod(live / "install.json", 0o600)
        for name in ("manifest.json", "plugin.py"):
            (live / "fixture.unsigned" / name).write_bytes((release / name).read_bytes())
            os.chmod(live / "fixture.unsigned" / name, 0o600)
        return live

    def test_a_link_or_group_write_in_a_live_tree_placed_before_the_first_install_refuses_typed(self):
        roots = self.registries()
        cases = (
            ("plugins link", lambda live: self.link_in_place(live), "plugin_directory_untrusted"),
            ("plugins group write", lambda live: os.chmod(live, 0o770), "plugin_directory_untrusted"),
            ("plugin directory link", lambda live: self.link_in_place(live / "fixture.unsigned"),
             "plugin_directory_untrusted"),
            ("plugin directory group write", lambda live: os.chmod(live / "fixture.unsigned", 0o770),
             "plugin_directory_untrusted"),
            ("install.json link", lambda live: self.link_in_place(live / "install.json"), "plugin_directory_untrusted"),
            ("install.json group write", lambda live: os.chmod(live / "install.json", 0o620), "plugin_file_untrusted"),
        )
        for name, change, code in cases:
            with self.subTest(level=name):
                self.use("live-" + name.replace(" ", "-"))
                self.registry = roots["a"]
                change(self._write_live_profile())
                for command in (self.listed, lambda: self.install_from(roots["a"]),
                                lambda: self.remove("fixture.unsigned")):
                    self.assertEqual(code, self.refused(command).code)
                    self.assertFalse(os.path.lexists(self.config / "plugin-generations"))

    def test_a_declared_signature_with_no_verifier_refuses_and_writes_nothing(self):
        from recursion_core.plugin_security import registry as registry_mod

        self.write_registry(signature="bad")
        release = self.registry / "registry" / "fixture.unsigned" / "1.0.0"
        (release / "manifest.sig").write_text("00" * 64 + "\n")
        real = registry_mod.backend_available
        # The missing backend is simulated as the daemon tests do: the check says no verifier is there.
        registry_mod.backend_available = lambda: False
        try:
            self.assertEqual([], self.codes_of(self.registry))
            self.assertEqual("signature_backend_unavailable", self.refused(self.install).code)
        finally:
            registry_mod.backend_available = real
        self._nothing_written()

    def _grant(self, **over):
        grant = {"plugin_id": "fixture.unsigned", "artifact_sha256": _sha(PLUGIN_SOURCE), "signer_key_id": None,
                 "permissions": ["read", "panel"]}
        grant.update(over)
        live = self.config / "plugins"
        live.mkdir(parents=True, mode=0o700)
        os.chmod(self.config, 0o700)
        os.chmod(self.config.parent, 0o700)
        (live / "install.json").write_bytes((json.dumps({"grants": [grant]}) + "\n").encode("utf-8"))
        os.chmod(live / "install.json", 0o600)

    def _grants_now(self):
        from recursion_core.daemon import plugins as daemon_plugins

        return daemon_plugins.read_install(self.config / "plugins")["grants"]

    def test_a_kept_grant_is_dropped_on_a_digest_change(self):
        self.write_registry()
        self._grant(artifact_sha256="ab" * 32)
        self.install()
        self.assertEqual({}, self._grants_now())

    def test_a_kept_grant_is_dropped_on_a_signer_change(self):
        self.write_registry()
        self._grant(signer_key_id="ed25519:" + "ab" * 16)
        self.install()
        self.assertEqual({}, self._grants_now())

    def test_a_grant_for_the_same_digest_and_signer_is_kept_and_remove_drops_it(self):
        self.write_registry()
        self._grant()
        self.install()
        self.assertEqual(["fixture.unsigned"], list(self._grants_now()))
        self.remove("fixture.unsigned")
        self.assertEqual({}, self._grants_now())
        written = json.loads((self.config / "plugins" / "install.json").read_text())
        self.assertEqual([], written["grants"])

    def _write_versions(self, versions):
        from recursion_core.plugin_security.registry import build_index, index_bytes

        entry = None
        releases = []
        for version, source in versions:
            manifest = _manifest(source, "fixture.unsigned", developer_key="11" * 32, version=version)
            entry = _entry("fixture.unsigned", version, manifest, source)
            releases.append(entry["releases"][0])
            folder = self.registry / "registry" / "fixture.unsigned" / version
            folder.mkdir(parents=True, mode=0o700)
            (folder / "manifest.json").write_bytes(manifest)
            (folder / "plugin.py").write_bytes(source)
        entry["releases"] = releases
        (self.registry / "registry" / "fixture.unsigned" / "entry.json").write_text(
            json.dumps(entry, indent=2, sort_keys=True) + "\n")
        (self.registry / "registry" / "index.json").write_bytes(index_bytes(build_index([entry])))

    def test_version_picks_that_release_and_an_unknown_version_refuses(self):
        self._write_versions([("1.0.0", b"VALUE = 10\n"), ("1.1.0", b"VALUE = 11\n"), ("1.2.0", b"VALUE = 12\n")])
        self.assertEqual([], self.codes_of(self.registry))
        self.assertEqual("release_not_in_registry", self.refused(lambda: self.install(version="9.9.9")).code)
        self._nothing_written()
        result = self.install(version="1.1.0")
        self.assertEqual("1.1.0", result["version"])
        self.assertEqual(b"VALUE = 11\n", (self.config / "plugins" / "fixture.unsigned" / "plugin.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
