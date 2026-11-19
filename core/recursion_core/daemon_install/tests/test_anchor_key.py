"""``recursiond install`` makes the install's own authority key and binds the ledger to it.

The owner answered that the authority ledger is anchored by the install's own key: made on the user's
machine at install, its private half owner-only under ``~/.config/recursion/authority/``, never printed,
logged or answered, and no copy kept anywhere. Run again with key and ledger agreeing, install writes
nothing; a key, a ledger and ``install.json``'s ``authority_anchor`` that disagree are refused, typed, and
nothing is written. Each test runs with ``XDG_STATE_HOME`` unset and set to an absolute temporary folder.
Linux-only: on Windows install and upgrade refuse ``platform_unsupported`` before they read a path
(test_unit_surrogate.py).
"""

from __future__ import annotations

import importlib
import io
import json
import logging
import os
import stat
import tempfile
import unittest
from unittest import mock

import recursion_core
from recursion_core.control.commands import setup
from recursion_core.daemon_install import Roots, daemon_exec_args, install as install_function, owned_paths
from recursion_core.daemon_install.tests.test_daemon_install import POSIX_PATHS
from recursion_core.plugin_security.signing import backend_available, key_id

# The module, not the package attribute ``install``, which is the function.
install_module = importlib.import_module("recursion_core.daemon_install.install")
PY = "/opt/recursion/venv/bin/python3.14t"
PACKAGE = recursion_core.__file__
NEEDS_BACKEND = "needs the cryptography package to make this install's authority key (csl-recursion[plugins])"

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:  # the optional plugins extra: the key tests need it, the no-backend test does not
    Ed25519PrivateKey = serialization = None

needs_crypto = unittest.skipUnless(backend_available() and Ed25519PrivateKey is not None,
                                   "cryptography is not importable in this interpreter")


def _probe(interpreter):
    return {"implementation": "cpython", "version": [3, 14], "free_threaded_build": True, "package": PACKAGE}


def _other_public_key() -> str:
    key = Ed25519PrivateKey.generate()
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


@unittest.skipUnless(POSIX_PATHS, "the unit's interpreter path is not absolute on this host")
@unittest.skipUnless(os.name == "posix", "the installer is Linux-only; on Windows install and upgrade refuse "
                     "platform_unsupported, tested in test_unit_surrogate.py")
class AnchorKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="anchor-key-")
        self.addCleanup(self._tmp.cleanup)
        self._count = 0

    def profiles(self):
        """One fresh home per state-folder choice: unset, and an absolute temporary folder."""
        for chosen in (False, True):
            self._count += 1
            root = os.path.join(self._tmp.name, f"p{self._count}")
            os.mkdir(root, 0o700)
            home = os.path.join(root, "home")
            os.mkdir(home, 0o700)
            environ = {"XDG_STATE_HOME": os.path.join(root, "chosen-state")} if chosen else {}
            yield root, home, environ

    def run_action(self, home, environ, action="install"):
        out, err = io.StringIO(), io.StringIO()
        code = setup.run(action, out=out, err=err, roots=Roots(home=home), interpreter=PY, package=PACKAGE,
                         probe=_probe, environ=environ)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        return code, json.loads(lines[0]), err.getvalue()

    def paths(self, home, environ):
        return owned_paths(Roots(home=home, state_home=environ.get("XDG_STATE_HOME")))

    @staticmethod
    def snapshot(root):
        """Every entry under ``root``: its type and mode, and for a file or a link its bytes and mtime, without
        following a link. A folder's mtime is left out: reading a ledger (WAL) makes and removes its side
        files, which moves its folder's mtime and writes nothing."""
        found = {}
        for folder, dirs, files in os.walk(root):
            for name in dirs + files:
                path = os.path.join(folder, name)
                info = os.lstat(path)
                data = None
                if stat.S_ISREG(info.st_mode):
                    with open(path, "rb") as handle:
                        data = handle.read()
                elif stat.S_ISLNK(info.st_mode):
                    data = os.readlink(path).encode()
                mtime = None if stat.S_ISDIR(info.st_mode) else info.st_mtime_ns
                found[os.path.relpath(path, root)] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), data, mtime)
        return found

    @staticmethod
    def public_of(key_path):
        with open(key_path, "rb") as handle:
            key = serialization.load_pem_private_key(handle.read(), password=None)
        return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()

    @staticmethod
    def ledger_anchor(ledger):
        from recursion_core.plugin_security import stores
        db = stores.open_store(ledger, stores.LEDGER_MAGIC)
        try:
            return db.execute("SELECT v FROM meta WHERE k='anchor'").fetchone()[0]
        finally:
            db.close()

    def write_install_json(self, home, anchor):
        folder = os.path.join(home, ".config", "recursion", "plugins")
        os.makedirs(folder, 0o700, exist_ok=True)
        with open(os.path.join(folder, "install.json"), "w", encoding="utf-8") as handle:
            json.dump({"authority_anchor": anchor}, handle)

    @needs_crypto
    def test_a_fresh_install_makes_the_key_and_the_ledger_and_the_daemon_composes(self):
        from recursion_core.daemon.control_adapter import _installed_profile_store_refusal
        from recursion_core.plugin_security import Refused
        from recursion_core.plugin_security.authority import Authority
        for root, home, environ in self.profiles():
            with self.subTest(environ=environ):
                paths = self.paths(home, environ)
                code, answer, err = self.run_action(home, environ)
                self.assertEqual((code, answer["code"]), (0, "ok"), answer)
                self.assertEqual(answer["ledger"], {"kind": "ledger", "path": paths.ledger, "created": True,
                                                    "detail": ""})
                key = os.path.join(home, ".config", "recursion", "authority", "anchor.key")
                self.assertEqual(paths.anchor_key, key)
                self.assertEqual(stat.S_IMODE(os.lstat(key).st_mode), 0o600)
                self.assertTrue(stat.S_ISREG(os.lstat(key).st_mode))
                self.assertEqual(stat.S_IMODE(os.lstat(os.path.dirname(key)).st_mode), 0o700)
                with open(key, "rb") as handle:
                    self.assertTrue(handle.read().startswith(b"-----BEGIN PRIVATE KEY-----\n"))  # PKCS#8 PEM
                public = self.public_of(key)
                self.assertRegex(public, r"\A[0-9a-f]{64}\Z")
                self.assertEqual(self.ledger_anchor(paths.ledger), key_id(public))
                self.assertEqual(err, install_module.ANCHOR_MADE.format(path=key) + "\n")
                self.assertEqual(err.count("\n"), 1)
                # The installed profile now starts: its store refusal is gone, and an Authority composes.
                self.assertIsNone(_installed_profile_store_refusal(paths.state, environ={"HOME": home}))
                authority = Authority(public, paths.ledger, lambda: 0)
                authority.close()
                with self.assertRaises(Refused) as caught:
                    Authority(_other_public_key(), paths.ledger, lambda: 0)
                self.assertEqual(caught.exception.code, "ledger_anchor_mismatch")

    @needs_crypto
    def test_a_second_install_and_an_upgrade_write_nothing(self):
        for root, home, environ in self.profiles():
            with self.subTest(environ=environ):
                self.run_action(home, environ)
                before = self.snapshot(root)
                for action in ("install", "upgrade"):
                    code, answer, err = self.run_action(home, environ, action)
                    self.assertEqual((code, answer["changed"], answer["ledger"]["created"], err),
                                     (0, False, False, ""), answer)
                    self.assertEqual(self.snapshot(root), before)

    @needs_crypto
    def test_each_disagreement_is_refused_and_writes_nothing(self):
        from recursion_core.plugin_security import stores
        cases = ("key without ledger", "ledger without key", "ledger bound to another key",
                 "anchor without key", "anchor naming another key")
        for root, home, environ in self.profiles():
            for case in cases:
                with self.subTest(environ=environ, case=case):
                    case_home = os.path.join(root, case.replace(" ", "-"))
                    os.mkdir(case_home, 0o700)
                    case_environ = dict(environ)
                    if environ:
                        case_environ["XDG_STATE_HOME"] = os.path.join(root, case.replace(" ", "-") + "-state")
                    paths = self.paths(case_home, case_environ)
                    if case != "anchor without key":
                        self.run_action(case_home, case_environ)
                    if case == "key without ledger":
                        os.unlink(paths.ledger)
                        expected = "no ledger"
                    elif case == "ledger without key":
                        os.unlink(paths.anchor_key)
                        expected = "no key is at"
                    elif case == "ledger bound to another key":
                        os.unlink(paths.ledger)
                        stores.create_ledger(paths.ledger, _other_public_key())
                        expected = "bound to another key"
                    elif case == "anchor without key":
                        self.write_install_json(case_home, _other_public_key())
                        expected = "no key is at"
                    else:
                        self.write_install_json(case_home, _other_public_key())
                        expected = "not the public half"
                    before = self.snapshot(root)
                    for action in ("install", "upgrade"):
                        if action == "upgrade" and case == "anchor without key":
                            continue  # no unit yet: upgrade refuses unit_missing first
                        code, answer, err = self.run_action(case_home, case_environ, action)
                        self.assertEqual((code, answer["code"]), (1, "authority_anchor_disagrees"), answer)
                        self.assertIn(expected, answer["detail"])
                        self.assertEqual(err, "")
                        self.assertEqual(self.snapshot(root), before)

    @needs_crypto
    def test_a_key_or_an_anchor_on_csls_list_is_refused(self):
        for root, home, environ in self.profiles():
            with self.subTest(environ=environ):
                self.run_action(home, environ)
                paths = self.paths(home, environ)
                listed = frozenset({key_id(self.public_of(paths.anchor_key))})
                before = self.snapshot(root)
                with mock.patch.object(install_module, "CSL_KEY_IDS", listed):
                    code, answer, _ = self.run_action(home, environ)
                self.assertEqual((code, answer["code"]), (1, "anchor_is_csl_key"))
                self.assertEqual(self.snapshot(root), before)
                fresh = os.path.join(root, "fresh")
                os.mkdir(fresh, 0o700)
                other = _other_public_key()
                self.write_install_json(fresh, other)
                before = self.snapshot(root)
                with mock.patch.object(install_module, "CSL_KEY_IDS", frozenset({key_id(other)})):
                    code, answer, _ = self.run_action(fresh, {})
                self.assertEqual((code, answer["code"]), (1, "anchor_is_csl_key"))
                self.assertEqual(self.snapshot(root), before)

    @needs_crypto
    def test_a_link_or_an_open_folder_at_the_key_path_is_refused(self):
        for root, home, environ in self.profiles():
            with self.subTest(environ=environ, where="a link at the key"):
                paths = self.paths(home, environ)
                target = os.path.join(root, "target")
                with open(target, "wb") as handle:
                    handle.write(b"not a key\n")
                os.chmod(target, 0o600)
                os.makedirs(paths.authority_dir, 0o700)
                os.symlink(target, paths.anchor_key)
                before = self.snapshot(root)
                code, answer, _ = self.run_action(home, environ)
                self.assertEqual((code, answer["code"]), (1, "not_regular"))
                self.assertEqual(self.snapshot(root), before)  # the link's target untouched, no store made
                os.unlink(paths.anchor_key)
            with self.subTest(environ=environ, where="a link at the folder"):
                os.rmdir(paths.authority_dir)
                elsewhere = os.path.join(root, "elsewhere")
                os.mkdir(elsewhere, 0o700)
                os.symlink(elsewhere, paths.authority_dir)
                before = self.snapshot(root)
                code, answer, _ = self.run_action(home, environ)
                self.assertEqual((code, answer["code"]), (1, "symlink_refused"))
                self.assertEqual(self.snapshot(root), before)
                self.assertEqual(os.listdir(elsewhere), [])
                os.unlink(paths.authority_dir)
            for mode in (0o770, 0o750, 0o500):
                with self.subTest(environ=environ, where=f"the folder {oct(mode)}"):
                    os.mkdir(paths.authority_dir, 0o700)
                    os.chmod(paths.authority_dir, mode)
                    before = self.snapshot(root)
                    code, answer, _ = self.run_action(home, environ)
                    self.assertEqual((code, answer["code"]), (1, "folder_untrusted"))
                    self.assertEqual(self.snapshot(root), before)
                    os.chmod(paths.authority_dir, 0o700)
                    os.rmdir(paths.authority_dir)

    @needs_crypto
    def test_a_key_file_open_to_others_or_not_a_key_is_refused_without_its_bytes(self):
        for root, home, environ in self.profiles():
            paths = self.paths(home, environ)
            self.run_action(home, environ)
            with self.subTest(environ=environ, where="mode 0644"):
                os.chmod(paths.anchor_key, 0o644)
                before = self.snapshot(root)
                code, answer, _ = self.run_action(home, environ)
                self.assertEqual((code, answer["code"]), (1, "anchor_key_untrusted"))
                self.assertEqual(self.snapshot(root), before)
                self.assertNotIn("BEGIN", answer["detail"])
                os.chmod(paths.anchor_key, 0o600)
            with self.subTest(environ=environ, where="not a key"):
                os.unlink(paths.anchor_key)
                with open(paths.anchor_key, "wb") as handle:
                    handle.write(b"secret-looking-garbage-0123\n")
                os.chmod(paths.anchor_key, 0o600)
                before = self.snapshot(root)
                code, answer, _ = self.run_action(home, environ)
                self.assertEqual((code, answer["code"]), (1, "anchor_key_invalid"))
                self.assertNotIn("garbage", answer["detail"])
                self.assertEqual(self.snapshot(root), before)

    @needs_crypto
    def test_no_output_log_or_answer_holds_the_private_key(self):
        records = []

        class Keep(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))
        handler = Keep(logging.DEBUG)
        logger = logging.getLogger()
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, old_level)
        self.addCleanup(logger.removeHandler, handler)
        for root, home, environ in self.profiles():
            with self.subTest(environ=environ):
                paths = self.paths(home, environ)
                shown = []
                for action in ("install", "install", "upgrade"):
                    _, answer, err = self.run_action(home, environ, action)
                    shown += [json.dumps(answer), err]
                os.chmod(paths.anchor_key, 0o640)
                _, answer, err = self.run_action(home, environ)  # a refusal names the path, never the bytes
                shown += [json.dumps(answer), err]
                os.chmod(paths.anchor_key, 0o600)
                with open(paths.anchor_key, "rb") as handle:
                    pem = handle.read()
                key = serialization.load_pem_private_key(pem, password=None)
                raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                        serialization.NoEncryption()).hex()
                body = [line for line in pem.decode().splitlines() if line and not line.startswith("-----")]
                text = "\n".join(shown + records)
                self.assertNotIn("PRIVATE KEY", text)
                self.assertNotIn(raw, text)
                self.assertNotIn(raw.upper(), text)
                for line in body:
                    self.assertNotIn(line, text)

    def test_without_cryptography_no_key_and_no_ledger_are_made(self):
        for root, home, environ in self.profiles():
            with self.subTest(environ=environ):
                paths = self.paths(home, environ)
                with mock.patch.object(install_module, "_ed25519", return_value=None):
                    code, answer, err = self.run_action(home, environ)
                self.assertEqual((code, answer["code"]), (0, "ok"))
                self.assertEqual(answer["ledger"], {"kind": "ledger", "path": paths.ledger, "created": False,
                                                    "detail": NEEDS_BACKEND})
                self.assertEqual(err, "")
                self.assertFalse(os.path.lexists(paths.ledger))
                self.assertFalse(os.path.lexists(paths.authority_dir))

    @needs_crypto
    def test_a_key_made_is_removed_again_when_the_ledger_cannot_be_made(self):
        for root, home, environ in self.profiles():
            with self.subTest(environ=environ):
                paths = self.paths(home, environ)

                def failing(paths_, anchor):
                    if anchor:
                        raise install_module.InstallRefused("store_not_created", "the disk is full")
                    return (paths_.ledger, False, install_module.NEEDS_ANCHOR)
                with mock.patch.object(install_module, "ensure_ledger", side_effect=failing):
                    code, answer, err = self.run_action(home, environ)
                self.assertEqual((code, answer["code"]), (1, "store_not_created"))
                self.assertFalse(os.path.lexists(paths.anchor_key))
                self.assertEqual(err, "")
                code, answer, _ = self.run_action(home, environ)  # run again: a whole key and ledger
                self.assertEqual((code, answer["ledger"]["created"]), (0, True))

    def test_the_installer_function_without_make_anchor_makes_no_key(self):
        # A caller that passes neither a key nor make_anchor keeps the old step (test_ledger_step.py).
        for root, home, environ in self.profiles():
            with self.subTest(environ=environ):
                roots = Roots(home=home, state_home=environ.get("XDG_STATE_HOME"))
                plan = install_function(roots, daemon_exec_args(PY))
                self.assertEqual((plan.ledger[1], plan.ledger[2], plan.anchor), (False, install_module.NEEDS_ANCHOR,
                                                                                  None))
                self.assertFalse(os.path.lexists(owned_paths(roots).authority_dir))
                with self.assertRaises(install_module.InstallRefused) as caught:
                    install_function(roots, daemon_exec_args(PY), anchor_public_key="11" * 32, make_anchor=True)
                self.assertEqual(caught.exception.kind, "anchor_public_key")


if __name__ == "__main__":
    unittest.main()
