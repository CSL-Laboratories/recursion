"""After the last unpin a plugin stays unpinned, every printed way out runs as printed, and the pin texts say
what happens.

(P) Once ``install.json`` has held ``pins``, every later write keeps the key, as ``"pins": {}`` when no plugin
is pinned: pins are derived from grants only for a file that never held the key. After the last unpin, a grant
the person writes again does not pin the plugin again, the same as when another plugin is still pinned, and a
hand-written empty map stays on the next write.
(W) Every way out a refusal prints names the profile path as an absolute path, quoted for the shell: the
``chmod 700`` way out runs as printed for a path that begins with ``-`` and for a path with a space, and a
refusal met under ``--config X`` prints its unpin command with ``--config`` and the absolute X (without it only
for the default profile).
(T) The unpin question names the entries the commit will move aside and the kept place, or says none; the
question of a pin move says what happens to the grant; an unpin that first finishes a stopped publish says so,
and says nothing was written only when nothing was; the refusal after the pinned key left the trusted list
never tells the person to install a release signed by that key.
(L) A lock file the user cannot write refuses ``plugin_directory_untrusted`` as before, now with the lock file's
path and the way out.
"""
from __future__ import annotations

import os
import shlex
import stat
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.plugin_security.signing import key_id
from recursion_core.plugin_security.tests.support import PLUGIN_SOURCE, Key, needs_crypto
from recursion_core.plugin_security.tests.test_plugin_cli_install import LINUX, LINUX_ONLY
from recursion_core.plugin_security.tests.test_plugin_install_pinned import PLUGIN, _PinCase, _tree

BETA = "fixture.beta"


class _Stop(BaseException):
    """A stop of the process at one point: nothing after it runs and nothing is cleaned up."""


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


class _AnswerCase(_PinCase):
    def use(self, name: str) -> None:
        """A profile, a state folder and registries of their own, under the test's own folder."""
        self.base = self.tmp / name
        self.config = self.base / "config" / "recursion"
        self.state = self.base / "state"
        self.registry = self.base / "registry-root"

    def release(self, name, **kwargs):
        """Write a registry root of its own for one release, and use it for the next install."""
        self.registry = getattr(self, "base", self.tmp) / f"registry-{name}"
        return self.write_registry(**kwargs)

    def grant_again(self, signer, plugin_id=PLUGIN) -> None:
        """The person writes a signer-bound grant for ``plugin_id`` by hand."""
        from recursion_core.daemon.tests import plugin_fixture as fx

        data = self.live_json()
        data["grants"] = [item for item in data["grants"] if item["plugin_id"] != plugin_id] + [
            fx.grant_for(PLUGIN_SOURCE, key=signer, plugin_id=plugin_id)]
        self.write_live(data)

    def chdir(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        previous = os.getcwd()
        os.chdir(folder)
        self.addCleanup(os.chdir, previous)

    def run_sh(self, command: str) -> subprocess.CompletedProcess:
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(home), "TMPDIR": str(self.tmp)}
        return subprocess.run(["sh", "-c", command], capture_output=True, text=True, env=env, timeout=60)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class StaysUnpinnedTests(_AnswerCase):
    def after_the_last_unpin(self, second_pinned: bool):
        """Unpin with yes, write a signer-bound grant again, then install an unsigned release (the read's E0/E1)."""
        signer = Key()
        self.signed_install(signer)
        if second_pinned:
            self.release("beta", plugin_id=BETA, key=signer, signature="real")
            self.install(BETA)
        self.unpin(confirm=lambda _question: "yes")
        self.grant_again(signer)
        self.release("unsigned", source=b"VALUE = 7\n", version="1.1.0")
        result = self.install()
        return result["trust"], PLUGIN in self.live_json()["pins"]

    @needs_crypto
    def test_after_the_last_unpin_a_grant_written_again_does_not_pin_the_plugin_again(self):
        signer = Key()
        self.signed_install(signer)
        self.unpin(confirm=lambda _question: "yes")
        self.assertEqual({}, self.live_json()["pins"])
        current = (self.config / "plugin-generations" / "current").read_text(encoding="ascii").strip()
        generation = self.config / "plugin-generations" / current / "install.json"
        self.assertEqual(generation.read_bytes(), self.live().read_bytes())
        self.grant_again(signer)
        self.release("unsigned", source=b"VALUE = 7\n", version="1.1.0")
        result = self.install()
        self.assertEqual("unsigned", result["trust"])
        self.assertEqual({}, self.live_json()["pins"])

    @needs_crypto
    def test_the_answer_does_not_depend_on_another_pinned_plugin(self):
        answers = {}
        for second_pinned in (False, True):
            with self.subTest(second_pinned=second_pinned):
                self.use(f"second-{second_pinned}")
                answers[second_pinned] = self.after_the_last_unpin(second_pinned)
        self.assertEqual(("unsigned", False), answers[False])
        self.assertEqual(answers[True], answers[False])

    @needs_crypto
    def test_a_hand_written_empty_pins_map_stays_on_the_next_write(self):
        self.signed_install(Key())
        data = self.live_json()
        data["pins"] = {}
        self.write_live(data)
        self.release("unsigned", source=b"VALUE = 7\n", version="1.1.0")
        self.assertEqual("unsigned", self.install()["trust"])
        self.assertEqual({}, self.live_json()["pins"])


@unittest.skipUnless(LINUX, LINUX_ONLY)
class PrintedWayOutRunsTests(_AnswerCase):
    def assert_chmod_way_out_runs(self, argv, folder: Path):
        status, said, _out = self.main(argv)
        self.assertEqual(1, status, said)
        command = _between(said, "Make it owner-only with: ", "; then")
        self.assertEqual(["chmod", "700", str(folder.absolute())], shlex.split(command))
        # The path handed to the shell is inside the test's own temporary folder.
        self.assertTrue(Path(os.path.realpath(folder)).is_relative_to(os.path.realpath(self.tmp)), folder)
        ran = self.run_sh(command)
        self.assertEqual(0, ran.returncode, ran.stderr)
        self.assertEqual(0o700, stat.S_IMODE(os.lstat(folder).st_mode))
        self.assertEqual(0, self.main(argv)[0])

    def test_the_chmod_way_out_runs_as_printed_for_a_config_path_that_begins_with_a_dash(self):
        work = self.tmp / "work"
        self.chdir(work)
        folder = work / "-profile"
        folder.mkdir()
        os.chmod(folder, 0o775)
        self.assert_chmod_way_out_runs(["plugin", "list", "--config=-profile", "--state", str(self.state)], folder)

    def test_the_chmod_way_out_runs_as_printed_for_a_path_with_a_space(self):
        folder = self.tmp / "with space" / "recursion"
        folder.mkdir(parents=True)
        os.chmod(folder, 0o775)
        self.assert_chmod_way_out_runs(["plugin", "list", "--config", str(folder), "--state", str(self.state)],
                                       folder)

    @needs_crypto
    def test_a_refusal_under_another_profile_prints_an_unpin_that_runs_in_that_profile(self):
        xdg = self.tmp / "xdg"
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}):
            # The default profile, with a pinned plugin of its own.
            self.config, self.state = xdg / "recursion", self.tmp / "default-state"
            self.signed_install(Key())
            default = _tree(self.config)
            # The other profile, named by a relative path.
            work = self.tmp / "work"
            self.chdir(work)
            self.use("other")
            self.config = Path("other-profile")
            self.signed_install(Key())
            self.release("unsigned", source=b"VALUE = 7\n", version="1.1.0")
            argv = self.install_argv()
            status, said, _out = self.main(argv)
            self.assertEqual(1, status)
            printed = shlex.split(_between(said, "or run: ", " (it asks"))
            self.assertEqual(["recursion", "plugin", "unpin", PLUGIN, "--config", str(work / "other-profile"),
                              "--state", str(self.state.absolute())], printed)
            status, said, _out = self.main(printed[1:], "yes\n")
            self.assertEqual(0, status, said)
            self.assertEqual({}, self.live_json()["pins"])
            self.assertEqual(default, _tree(xdg / "recursion"))

    @needs_crypto
    def test_a_refusal_in_the_default_profile_prints_an_unpin_without_config(self):
        xdg = self.tmp / "xdg"
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}):
            self.config = xdg / "recursion"
            self.signed_install(Key())
            self.release("unsigned", source=b"VALUE = 7\n", version="1.1.0")
            argv = self.install_argv()
            at = argv.index("--config")
            del argv[at:at + 2]
            status, said, _out = self.main(argv)
            self.assertEqual(1, status)
            self.assertEqual(["recursion", "plugin", "unpin", PLUGIN, "--state", str(self.state.absolute())],
                             shlex.split(_between(said, "or run: ", " (it asks")))


@unittest.skipUnless(LINUX, LINUX_ONLY)
class TextsSayWhatHappensTests(_AnswerCase):
    def asked(self, answer):
        questions = []

        def confirm(question):
            questions.append(question)
            return answer

        return questions, confirm

    @needs_crypto
    def test_the_unpin_question_names_the_entries_it_moves_aside_and_where(self):
        self.signed_install(Key())
        self.release("beta", plugin_id=BETA)
        self.install(BETA)
        beta = self.config / "plugins" / BETA
        (beta / "settings.toml").write_bytes(b"x = 1\n")
        questions, confirm = self.asked("yes")
        result = self.unpin(confirm=confirm)
        place = self.config / "plugin-generations" / f".kept-{result['generation']}"
        self.assertEqual(1, len(questions))
        self.assertNotIn("\n", questions[0])
        for words in ("settings.toml", str(beta), str(place)):
            self.assertIn(words, questions[0])
        self.assertEqual(b"x = 1\n", (place / BETA / "settings.toml").read_bytes())

    @needs_crypto
    def test_the_unpin_question_says_when_nothing_is_moved_aside(self):
        self.signed_install(Key())
        questions, confirm = self.asked("no")
        self.not_changed(lambda: self.unpin(confirm=confirm))
        self.assertIn("nothing is moved aside", questions[0])

    @needs_crypto
    def test_the_pin_move_question_says_a_matching_grant_is_kept_and_names_it(self):
        from recursion_core.daemon.tests import plugin_fixture as fx

        first, second = Key(), Key()
        self.signed_install(first, trusted={"first": first.public, "second": second.public})
        self.release("second", key=second, signature="real", source=b"VALUE = 7\n", version="1.1.0")
        data = self.live_json()
        data["grants"] = [fx.grant_for(b"VALUE = 7\n", key=second, plugin_id=PLUGIN,
                                       permissions=("read", "panel", "propose"))]
        self.write_live(data)
        questions, confirm = self.asked("yes")
        self.install(confirm=confirm)
        self.assertNotIn("grant is dropped", questions[0])
        self.assertIn("its grant of read, panel, propose", questions[0])
        self.assertIn("is kept", questions[0])
        self.assertEqual([["read", "panel", "propose"]], [item["permissions"] for item in self.live_json()["grants"]])

    @needs_crypto
    def test_the_pin_move_question_says_the_grant_is_dropped_when_it_is(self):
        first, second = Key(), Key()
        self.signed_install(first, trusted={"first": first.public, "second": second.public})
        self.release("second", key=second, signature="real", source=b"VALUE = 7\n", version="1.1.0")
        questions, confirm = self.asked("yes")
        self.install(confirm=confirm)
        self.assertIn("its grant is dropped", questions[0])
        self.assertEqual([], self.live_json()["grants"])

    @needs_crypto
    def test_an_unpin_that_finishes_a_stopped_publish_says_so_and_not_that_nothing_was_written(self):
        from recursion_core.daemon.tests import plugin_fixture as fx
        from recursion_core.plugin_security import install

        signer = Key()
        fx.write_install(self.config, trusted={"developer": signer.public})
        self.release("signed", key=signer, signature="real")
        self.install()
        self.release("beta", plugin_id=BETA)
        real = install._write_pointer

        def stop_after(*args, **kwargs):
            real(*args, **kwargs)
            raise _Stop()

        with mock.patch.object(install, "_write_pointer", stop_after), self.assertRaises(_Stop):
            self.install(BETA)
        self.assertTrue((self.config / "plugin-generations" / ".publishing").exists())
        questions, confirm = self.asked("no")
        said = self.not_changed(lambda: self.unpin(confirm=confirm)).detail
        self.assertNotIn("nothing was written", said)
        for text in (said, questions[0]):
            self.assertIn("generation 2", text)
            self.assertIn(BETA, text)
        self.assertIn("the answer was not yes", said)
        self.assertTrue((self.config / "plugins" / BETA).is_dir())

    @needs_crypto
    def test_the_refusal_after_the_pinned_key_left_the_trusted_list_never_names_it_as_the_way_forward(self):
        signer = Key()
        self.signed_install(signer)
        data = self.live_json()
        data["trusted_keys"] = {}
        self.write_live(data)
        self.release("again", key=signer, signature="real")
        refusal = self.refused(self.install)
        self.assertEqual("update_unsigned_over_signed", refusal.code)
        signer_key = key_id(signer.public)
        self.assertNotIn(f"Install a release signed by {signer_key}", refusal.detail)
        self.assertIn(f"The key {signer_key} is not on this install's trusted list: put it on the trusted list again, "
                      f"or install a release signed by a key that is on it", refusal.detail)
        self.assertIn(f"recursion plugin unpin {PLUGIN}", refusal.detail)


@unittest.skipUnless(LINUX, LINUX_ONLY)
@unittest.skipIf(os.geteuid() == 0 if hasattr(os, "geteuid") else True, "root can write a 0400 file")
class LockWayOutTests(_AnswerCase):
    def test_a_lock_file_the_user_cannot_write_is_named_with_its_way_out(self):
        self.release("unsigned")
        self.install()
        lock = self.config / "plugin-install.lock"
        os.chmod(lock, 0o400)
        self.addCleanup(os.chmod, lock, 0o600)
        refusal = self.refused(self.listed)
        self.assertEqual(("plugin_directory_untrusted", True), (refusal.code, refusal.show_detail))
        self.assertIn(f"The lock file {lock} cannot be opened for writing", refusal.detail)
        self.assertIn(f"chmod 600 {shlex.quote(str(lock))}", refusal.detail)
        self.assertIn("remove it while no plugin command runs", refusal.detail)
        status, said, _out = self.main(["plugin", "list", "--config", str(self.config), "--state", str(self.state)])
        self.assertEqual((1, f"refused: plugin_directory_untrusted\n{refusal.detail}\n"), (status, said))
        command = _between(refusal.detail, "Make it writable by its owner with: ", ";")
        self.assertEqual(["chmod", "600", str(lock)], shlex.split(command))
        # The path handed to the shell is inside the test's own temporary folder.
        self.assertTrue(Path(os.path.realpath(lock)).is_relative_to(os.path.realpath(self.tmp)), lock)
        ran = self.run_sh(command)
        self.assertEqual(0, ran.returncode, ran.stderr)
        self.assertEqual(0, self.main(["plugin", "list", "--config", str(self.config), "--state",
                                       str(self.state)])[0])


if __name__ == "__main__":
    unittest.main()
