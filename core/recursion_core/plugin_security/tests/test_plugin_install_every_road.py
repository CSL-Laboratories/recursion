"""Every road by which a release is chosen for install passes one decision, and a withdrawn release is said.

``install._installable`` decides, on what ``_select`` returns, whether the chosen release may be installed
and with what words: by an id alone, by a named version, from a registry folder (the command line and the
API alike), from a registry at a commit, with ``--git``, through ``install_plugin`` called directly, and
before a release can be held. Its one rule (``_WITHDRAWN_RULE``) says what a withdrawn release does on each
way it can be chosen. A withdrawn release that goes on is said to be withdrawn, with the registry's reason
and the newest release that is not, before the install goes on; one refused names the same and the one step.
The last tests read the source and fail when a new road reaches the install around the decision.
Every registry is made up by the test inside its own temporary folder.
"""
from __future__ import annotations

import ast
import io
import json
import os
from pathlib import Path
import shlex
import unittest

from recursion_core.plugin_security.tests.support import Key, needs_crypto
from recursion_core.plugin_security.tests.test_plugin_cli_install import (
    LINUX, LINUX_ONLY, PLUGIN_SOURCE, _entry, _manifest)
from recursion_core.plugin_security.tests.test_plugin_install_from_registry import RegistryCase
from recursion_core.plugin_security.tests.test_plugin_install_quarantine import NEXT_SOURCE, _asking, _QuarantineCase

_PLUGIN = "fixture.unsigned"
_REASON = "A made-up reason to withdraw."
_WORDS = f"WITHDRAWN: the release 0.9.0 of {_PLUGIN} was withdrawn by the keepers of the registry "
_PACKAGE = Path(__file__).resolve().parent.parent


def _over(status, reason):
    return {"status": status, "status_reason": reason}


@unittest.skipUnless(LINUX, LINUX_ONLY)
class EveryRoadTests(RegistryCase):
    def two(self, first=("0.9.0", "withdrawn", _REASON), second=("1.0.0", "active", None)):
        """A registry folder with two releases of the plugin, in this order."""
        from recursion_core.plugin_security.registry import build_index, index_bytes

        entry = self.write_registry(_PLUGIN, version=first[0], release_over=_over(*first[1:]))
        manifest = _manifest(PLUGIN_SOURCE, _PLUGIN, developer_key="11" * 32, version=second[0])
        entry["releases"].append(_entry(_PLUGIN, second[0], manifest, PLUGIN_SOURCE,
                                        **_over(*second[1:]))["releases"][0])
        folder = self.registry / "registry" / _PLUGIN / second[0]
        folder.mkdir(parents=True, mode=0o700)
        (folder / "manifest.json").write_bytes(manifest)
        (folder / "plugin.py").write_bytes(PLUGIN_SOURCE)
        (self.registry / "registry" / _PLUGIN / "entry.json").write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")
        (self.registry / "registry" / "index.json").write_bytes(index_bytes(build_index([entry])))
        return str(self.registry)

    def direct(self, **kwargs):
        from recursion_core.plugin_security.install import install_plugin

        return install_plugin(self.config, self.registry, _PLUGIN, state=self.state, **kwargs)

    def said_withdrawn(self, text, registry):
        self.assertIn(_WORDS + registry, text)
        self.assertIn(f"who give this reason: {_REASON}", text)
        self.assertNotIn("..", text.replace(registry, ""))
        self.assertIn(f"The newest release of {_PLUGIN} there that is not withdrawn is 1.0.0; to install it run: "
                      f"recursion plugin install {_PLUGIN} --registry {registry} --version 1.0.0", text)

    # (a) the command line, a registry folder, a withdrawn release named by its version
    def test_a_cli_folder_named_withdrawn_is_said_before_the_install_and_in_its_status_line(self):
        registry = self.two()
        status, out, err = self.run_cli(_PLUGIN, "--registry", registry, "--version", "0.9.0")
        self.assertEqual(status, 0, err)
        self.said_withdrawn(err.splitlines()[0], registry)
        self.assertIn("It is installed because you named it by its version.", err)
        self.assertIn(f"This release is withdrawn: {_REASON}\n", err)
        result = json.loads(out)
        self.assertEqual(("withdrawn", "named_version"), (result["status"], result["withdrawn_by"]))

    # (b) install_plugin called directly: said before anything is written, and in the result
    def test_b_direct_call_named_withdrawn_is_said_before_anything_is_written(self):
        self.two()
        seen = []
        result = self.direct(version="0.9.0", report_kept=lambda line: seen.append(
            (line, (self.config / "plugins").exists(), (self.config / "plugin-generations").exists())))
        self.assertEqual(1, len(seen), seen)
        self.said_withdrawn(seen[0][0], str(self.registry))
        self.assertEqual((False, False), seen[0][1:])
        self.assertEqual(("withdrawn", "named_version", seen[0][0]),
                         (result["status"], result["withdrawn_by"], result["withdrawn"]))

    # (c) a registry at a commit, a withdrawn release named by its version: refused, with the newer one named
    def test_c_pinned_named_withdrawn_is_refused_with_the_newest_and_the_step(self):
        self.two()
        bare, commit = self.commit_registry()
        registry = f"{bare}@{commit}"
        err = self.assert_refused("release_withdrawn", _PLUGIN, "--registry", registry, "--version", "0.9.0",
                                  words=("Nothing was installed.", "--version 0.9.0 --allow-withdrawn 0.9.0"))
        self.said_withdrawn(err, registry)

    # (d) the API's registry folder road: refused with the words, as it was before the one decision
    def test_d_install_from_registry_folder_refuses_it_with_the_words(self):
        from recursion_core.plugin_security.install import InstallRefused
        from recursion_core.plugin_security.registry_source import install_from_registry

        registry = self.two()
        before = self.listing()
        seen = []
        with self.assertRaises(InstallRefused) as caught:
            install_from_registry(self.config, _PLUGIN, registry=registry, version="0.9.0", state=self.state,
                                  report_kept=seen.append)
        self.assertEqual("release_withdrawn", caught.exception.code)
        self.said_withdrawn(caught.exception.detail, registry)
        self.assertIn("Nothing was installed.", caught.exception.detail)
        self.assertEqual(([], before), (seen, self.listing()))

    # (e) an id alone never takes a withdrawn release
    def test_e_id_alone_takes_the_newest_active_and_nothing_withdrawn(self):
        registry = self.two(first=("1.0.0", "active", None), second=("1.1.0", "withdrawn", _REASON))
        status, out, err = self.run_cli(_PLUGIN, "--registry", registry)
        self.assertEqual(status, 0, err)
        result = json.loads(out)
        self.assertEqual(("1.0.0", "active"), (result["version"], result["status"]))
        self.assertNotIn("WITHDRAWN", err)
        self.assertNotIn("withdrawn_by", result)

    # (f) --git: the decision comes before any git read
    def test_f_git_road_decides_before_any_git_read(self):
        self.two()
        bare, commit = self.commit_registry()
        missing = str(self.tmp / "no-such.git")
        self.assert_refused("release_withdrawn", _PLUGIN, "--registry", f"{bare}@{commit}", "--git", missing,
                            "--version", "0.9.0", words=(_WORDS,))
        status, _out, err = self.run_cli(_PLUGIN, "--registry", str(self.registry), "--git", missing,
                                         "--version", "0.9.0")
        self.assertEqual(status, 1, err)
        self.assertTrue(err.startswith(_WORDS), err)
        self.assertNotIn("refused: release_withdrawn", err)

    # (h) the person's one step, and a step that names another release
    def test_h_the_allow_step_installs_and_says_how(self):
        registry = self.two()
        status, out, err = self.run_cli(_PLUGIN, "--registry", registry, "--version", "0.9.0",
                                        "--allow-withdrawn", "0.9.0")
        self.assertEqual(status, 0, err)
        self.assertIn("It is installed because you allowed it for this one install with --allow-withdrawn.", err)
        self.assertEqual("allow_withdrawn", json.loads(out)["withdrawn_by"])

    def test_h_a_step_naming_another_release_is_refused_on_the_folder_road_too(self):
        registry = self.two()
        err = self.assert_refused("allow_withdrawn_unmatched", _PLUGIN, "--registry", registry, "--version", "0.9.0",
                                  "--allow-withdrawn", "1.0.0", words=("does not match this install",))
        self.assertIn("refused: release_withdrawn\n", err)
        self.assert_refused("allow_withdrawn_unmatched", _PLUGIN, "--registry", registry, "--version", "1.0.0",
                            "--allow-withdrawn", "1.0.0", words=("which is active",))
        self.assert_refused("allow_withdrawn_unmatched", _PLUGIN, "--registry", registry,
                            "--allow-withdrawn", "0.9.0", words=("the release chosen here is 1.0.0",))

    # (i) a deprecated release from a folder: its status line is printed
    def test_i_cli_folder_deprecated_prints_its_status_line(self):
        self.write_registry(_PLUGIN, release_over=_over("deprecated", "A made-up reason."))
        status, _out, err = self.run_cli(_PLUGIN, "--registry", str(self.registry), "--version", "1.0.0")
        self.assertEqual(status, 0, err)
        self.assertIn("This release is deprecated: A made-up reason.\n", err)

    # (j) a folder that fails its check: each file and rule is printed, as for a registry at a commit
    def test_j_cli_folder_failing_its_check_prints_the_findings(self):
        self.write_registry(_PLUGIN, index="empty")
        before = self.listing()
        status, out, err = self.run_cli(_PLUGIN, "--registry", str(self.registry))
        self.assertEqual(status, 1, (out, err))
        self.assertIn(f"The registry {self.registry} did not pass its check", err)
        self.assertIn("  registry/index.json: ", err)
        self.assertEqual(self.listing(), before)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class HeldRoadTests(_QuarantineCase):
    # (g) a withdrawn release that would be held passes the same decision and is said
    @needs_crypto
    def test_g_a_withdrawn_release_that_asks_for_more_is_said_before_it_is_held(self):
        signer = Key()
        self.signed_install(signer)
        self.release("more", key=signer, signature="real", source=NEXT_SOURCE,
                     manifest=_asking(signer.public), version="1.1.0", release_over=_over("withdrawn", _REASON))
        seen = []
        result = self.install(version="1.1.0", report_kept=seen.append)
        self.assertEqual("quarantined", result.get("result"), result)
        self.assertTrue(seen and seen[0].startswith(f"WITHDRAWN: the release 1.1.0 of {result['plugin_id']}"), seen)
        self.assertEqual(("withdrawn", "named_version"), (result["status"], result["withdrawn_by"]))


def _enclosing(tree):
    """Each node of ``tree`` with the name of the function that holds it (None at module level)."""
    out = []

    def walk(node, name):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else name
            out.append((child, name))
            walk(child, inner)

    walk(tree, None)
    return out


def _called(node, name):
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name


_INSTALL_KEYWORDS = {"version", "state", "wait", "git_repository", "report_kept", "confirm", "write_grant",
                     "confirm_grant", "allow_withdrawn", "registry_name", "registry_kind"}
_INSIDE = ("_select", "_installable", "_install_locked", "_hold", "_WITHDRAWN_RULE")


def road_faults(sources: dict) -> list:
    """Every way ``sources`` (module path to text) reach the install around ``_installable``; [] when none."""
    faults = []
    install = ast.parse(sources["install.py"])
    nodes = _enclosing(install)
    parents = {id(child): parent for parent in ast.walk(install) for child in ast.iter_child_nodes(parent)}
    for node, where in nodes:
        if _called(node, "_select"):
            parent = parents.get(id(node))
            if not (_called(parent, "_installable") and parent.args and parent.args[0] is node):
                faults.append(f"install.py:{node.lineno} {where}: _select's result does not go into _installable")
        if _called(node, "_install_locked") and where != "install_plugin":
            faults.append(f"install.py:{node.lineno} {where}: _install_locked called outside install_plugin")
        if _called(node, "_hold") and where != "_install_locked":
            faults.append(f"install.py:{node.lineno} {where}: _hold called outside _install_locked")
    main = next(node for node in install.body if isinstance(node, ast.FunctionDef) and node.name == "install_plugin")
    keywords = {arg.arg for arg in main.args.kwonlyargs}
    if keywords != _INSTALL_KEYWORDS:
        faults.append(f"install_plugin keywords changed: {sorted(keywords ^ _INSTALL_KEYWORDS)}")
    decided = [node for node in ast.walk(main) if isinstance(node, ast.Assign) and _called(node.value, "_installable")]
    chosen = [node for node in ast.walk(main) if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
              and "chosen" in {name.id for target in getattr(node, "targets", [getattr(node, "target", None)])
                               for name in ast.walk(target) if isinstance(name, ast.Name)}]
    locked = [node for node in ast.walk(main) if _called(node, "_install_locked")]
    if len(decided) != 1 or chosen != decided:
        faults.append("install_plugin: chosen is not set once, by _installable")
    for call in locked:
        if not decided or call.lineno < decided[0].lineno or not (
                len(call.args) > 1 and isinstance(call.args[1], ast.Name) and call.args[1].id == "chosen"):
            faults.append(f"install.py:{call.lineno}: _install_locked does not take the decided release")
    for path, text in sources.items():
        if path == "install.py":
            continue
        for node, where in _enclosing(ast.parse(text)):
            name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            if name in _INSIDE:
                faults.append(f"{path}:{node.lineno}: {name} is used outside install.py")
            if name == "install_plugin" and isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and not (
                    path.endswith("registry_source.py") and where == "_checked_install"):
                faults.append(f"{path}:{node.lineno} {where}: install_plugin reached outside _checked_install")
    return faults


def _sources() -> dict:
    found = {}
    core = _PACKAGE.parent
    for path in sorted(core.rglob("*.py")):
        if "tests" in path.parts or "fixtures" in path.parts:
            continue
        rel = path.relative_to(_PACKAGE).as_posix() if _PACKAGE in path.parents else path.relative_to(core).as_posix()
        found[rel] = path.read_text(encoding="utf-8")
    return found


class StructureTests(unittest.TestCase):
    def test_every_road_passes_the_one_decision(self):
        sources = _sources()
        self.assertIn("install.py", sources)
        self.assertEqual([], road_faults(sources))

    def test_a_second_road_around_the_decision_is_caught(self):
        sources = _sources()
        cuts = {
            "a second caller": sources["install.py"] + (
                "\n\ndef _second_road(report):\n"
                "    return _install_locked(None, _select(report, 'x', None), 'x', None, b'', False)\n"),
            "a skip keyword": sources["install.py"].replace(
                "registry_kind: str | None = None) -> dict:", "registry_kind: str | None = None, "
                "skip_decision: bool = False) -> dict:", 1),
        }
        for label, text in cuts.items():
            with self.subTest(label):
                self.assertNotEqual(text, sources["install.py"])
                self.assertNotEqual([], road_faults({**sources, "install.py": text}))
        other = dict(sources)
        other["plugin_security/_entrypoints.py"] = other.get("plugin_security/_entrypoints.py", "") + (
            "\n\ndef _around(config, root):\n    return install_plugin(config, root, 'x')\n")
        self.assertNotEqual([], road_faults(other))



# The table of every road and every way of naming a release. For each row: what the base of this rule did
# (the commit before the one decision; each value written as a run of this same table against that commit
# printed it) and what this head does. "installed V, no word" is an install that carries no status of a
# withdrawn release; "status in result" is one whose result (the command's stdout) carries the status and
# that says nothing on stderr or to report_kept; "status line" is the line printed on stderr after an install
# from a registry at a commit; "WITHDRAWN" is the line said before the install goes on.
_FIXTURES = {
    "two": ("0.9.0", "withdrawn", _REASON), "newest": ("1.1.0", "withdrawn", _REASON),
    "lone": ("0.9.0", "withdrawn", _REASON),
}
_NAMINGS = {
    "id alone": ("two", None, None),
    "named withdrawn": ("two", "0.9.0", None),
    "named active": ("two", "1.0.0", None),
    "named withdrawn, allowed": ("two", "0.9.0", "0.9.0"),
    "named withdrawn, another allowed": ("two", "0.9.0", "1.0.0"),
    "id alone, a withdrawn allowed": ("two", None, "0.9.0"),
    "id alone, the newest withdrawn": ("newest", None, None),
    "named the newest, withdrawn": ("newest", "1.1.0", None),
    "id alone, none active": ("lone", None, None),
    "named withdrawn, none active": ("lone", "0.9.0", None),
}
_FORMS = {
    # road, registry form, namings tried (every naming, or the two that reach a registry at all)
    "command, registry at a commit": ("command", "commit", tuple(_NAMINGS)),
    "command, registry folder": ("command", "folder", tuple(_NAMINGS)),
    "command, a file": ("command", "file", ("id alone", "named withdrawn")),
    "command, a folder with no registry": ("command", "empty", ("id alone", "named withdrawn")),
    "install_from_registry, registry at a commit": ("install_from_registry", "commit", tuple(_NAMINGS)),
    "install_from_registry, registry folder": ("install_from_registry", "folder", tuple(_NAMINGS)),
    "install_from_registry, a file": ("install_from_registry", "file", ("id alone", "named withdrawn")),
    "install_from_registry, a folder with no registry": ("install_from_registry", "empty", ("id alone", "named withdrawn")),
    "install_plugin, plain folder": ("install_plugin", "folder", tuple(_NAMINGS)),
    "install_plugin, a file": ("install_plugin", "file", ("id alone", "named withdrawn")),
    "install_plugin, a folder with no registry": ("install_plugin", "empty", ("id alone", "named withdrawn")),
}
_ROWS = [(form, naming) for form, (_road, _where, namings) in _FORMS.items() for naming in namings]

# (form, naming): (the base, this head)
ROAD_TABLE = {
    ('command, registry at a commit', 'id alone'):
        ('installed 1.0.0, no word', 'installed 1.0.0, no word'),
    ('command, registry at a commit', 'named withdrawn'):
        ('refused release_withdrawn', 'refused release_withdrawn'),
    ('command, registry at a commit', 'named active'):
        ('installed 1.0.0, no word', 'installed 1.0.0, no word'),
    ('command, registry at a commit', 'named withdrawn, allowed'):
        ('installed 0.9.0, status line', 'installed 0.9.0, WITHDRAWN'),
    ('command, registry at a commit', 'named withdrawn, another allowed'):
        ('refused release_withdrawn', 'refused release_withdrawn allow_withdrawn_unmatched'),
    ('command, registry at a commit', 'id alone, a withdrawn allowed'):
        ('installed 1.0.0, no word', 'refused allow_withdrawn_unmatched'),
    ('command, registry at a commit', 'id alone, the newest withdrawn'):
        ('installed 1.0.0, no word', 'installed 1.0.0, no word'),
    ('command, registry at a commit', 'named the newest, withdrawn'):
        ('refused release_withdrawn', 'refused release_withdrawn'),
    ('command, registry at a commit', 'id alone, none active'):
        ('refused release_not_active', 'refused release_not_active'),
    ('command, registry at a commit', 'named withdrawn, none active'):
        ('refused release_withdrawn', 'refused release_withdrawn'),
    ('command, registry folder', 'id alone'):
        ('installed 1.0.0, no word', 'installed 1.0.0, no word'),
    ('command, registry folder', 'named withdrawn'):
        ('installed 0.9.0, status in result', 'installed 0.9.0, WITHDRAWN'),
    ('command, registry folder', 'named active'):
        ('installed 1.0.0, no word', 'installed 1.0.0, no word'),
    ('command, registry folder', 'named withdrawn, allowed'):
        ('installed 0.9.0, status in result', 'installed 0.9.0, WITHDRAWN'),
    ('command, registry folder', 'named withdrawn, another allowed'):
        ('installed 0.9.0, status in result', 'refused release_withdrawn allow_withdrawn_unmatched'),
    ('command, registry folder', 'id alone, a withdrawn allowed'):
        ('installed 1.0.0, no word', 'refused allow_withdrawn_unmatched'),
    ('command, registry folder', 'id alone, the newest withdrawn'):
        ('installed 1.0.0, no word', 'installed 1.0.0, no word'),
    ('command, registry folder', 'named the newest, withdrawn'):
        ('installed 1.1.0, status in result', 'installed 1.1.0, WITHDRAWN'),
    ('command, registry folder', 'id alone, none active'):
        ('refused release_not_active', 'refused release_not_active'),
    ('command, registry folder', 'named withdrawn, none active'):
        ('installed 0.9.0, status in result', 'installed 0.9.0, WITHDRAWN'),
    ('command, a file', 'id alone'):
        ('refused root_unavailable', 'refused root_unavailable'),
    ('command, a file', 'named withdrawn'):
        ('refused root_unavailable', 'refused root_unavailable'),
    ('command, a folder with no registry', 'id alone'):
        ('refused registry_missing', 'refused registry_missing'),
    ('command, a folder with no registry', 'named withdrawn'):
        ('refused registry_missing', 'refused registry_missing'),
    ('install_from_registry, registry at a commit', 'id alone'):
        ('installed 1.0.0, no word', 'installed 1.0.0, no word'),
    ('install_from_registry, registry at a commit', 'named withdrawn'):
        ('refused release_withdrawn', 'refused release_withdrawn'),
    ('install_from_registry, registry at a commit', 'named active'):
        ('installed 1.0.0, no word', 'installed 1.0.0, no word'),
    ('install_from_registry, registry at a commit', 'named withdrawn, allowed'):
        ('installed 0.9.0, status in result', 'installed 0.9.0, WITHDRAWN'),
    ('install_from_registry, registry at a commit', 'named withdrawn, another allowed'):
        ('refused release_withdrawn', 'refused release_withdrawn allow_withdrawn_unmatched'),
    ('install_from_registry, registry at a commit', 'id alone, a withdrawn allowed'):
        ('installed 1.0.0, no word', 'refused allow_withdrawn_unmatched'),
    ('install_from_registry, registry at a commit', 'id alone, the newest withdrawn'):
        ('installed 1.0.0, no word', 'installed 1.0.0, no word'),
    ('install_from_registry, registry at a commit', 'named the newest, withdrawn'):
        ('refused release_withdrawn', 'refused release_withdrawn'),
    ('install_from_registry, registry at a commit', 'id alone, none active'):
        ('refused release_not_active', 'refused release_not_active'),
    ('install_from_registry, registry at a commit', 'named withdrawn, none active'):
        ('refused release_withdrawn', 'refused release_withdrawn'),
    ('install_from_registry, registry folder', 'id alone'):
        ('installed 1.0.0, no word', 'installed 1.0.0, no word'),
    ('install_from_registry, registry folder', 'named withdrawn'):
        ('refused release_withdrawn', 'refused release_withdrawn'),
    ('install_from_registry, registry folder', 'named active'):
        ('installed 1.0.0, no word', 'installed 1.0.0, no word'),
    ('install_from_registry, registry folder', 'named withdrawn, allowed'):
        ('installed 0.9.0, status in result', 'installed 0.9.0, WITHDRAWN'),
    ('install_from_registry, registry folder', 'named withdrawn, another allowed'):
        ('refused release_withdrawn', 'refused release_withdrawn allow_withdrawn_unmatched'),
    ('install_from_registry, registry folder', 'id alone, a withdrawn allowed'):
        ('installed 1.0.0, no word', 'refused allow_withdrawn_unmatched'),
    ('install_from_registry, registry folder', 'id alone, the newest withdrawn'):
        ('installed 1.0.0, no word', 'installed 1.0.0, no word'),
    ('install_from_registry, registry folder', 'named the newest, withdrawn'):
        ('refused release_withdrawn', 'refused release_withdrawn'),
    ('install_from_registry, registry folder', 'id alone, none active'):
        ('refused release_not_active', 'refused release_not_active'),
    ('install_from_registry, registry folder', 'named withdrawn, none active'):
        ('refused release_withdrawn', 'refused release_withdrawn'),
    ('install_from_registry, a file', 'id alone'):
        ('refused root_unavailable', 'refused root_unavailable'),
    ('install_from_registry, a file', 'named withdrawn'):
        ('refused root_unavailable', 'refused root_unavailable'),
    ('install_from_registry, a folder with no registry', 'id alone'):
        ('refused registry_missing', 'refused registry_missing'),
    ('install_from_registry, a folder with no registry', 'named withdrawn'):
        ('refused registry_missing', 'refused registry_missing'),
    ('install_plugin, plain folder', 'id alone'):
        ('installed 1.0.0, no word', 'installed 1.0.0, no word'),
    ('install_plugin, plain folder', 'named withdrawn'):
        ('installed 0.9.0, status in result', 'installed 0.9.0, WITHDRAWN'),
    ('install_plugin, plain folder', 'named active'):
        ('installed 1.0.0, no word', 'installed 1.0.0, no word'),
    ('install_plugin, plain folder', 'named withdrawn, allowed'):
        ('installed 0.9.0, status in result', 'installed 0.9.0, WITHDRAWN'),
    ('install_plugin, plain folder', 'named withdrawn, another allowed'):
        ('installed 0.9.0, status in result', 'refused release_withdrawn allow_withdrawn_unmatched'),
    ('install_plugin, plain folder', 'id alone, a withdrawn allowed'):
        ('installed 1.0.0, no word', 'refused allow_withdrawn_unmatched'),
    ('install_plugin, plain folder', 'id alone, the newest withdrawn'):
        ('installed 1.0.0, no word', 'installed 1.0.0, no word'),
    ('install_plugin, plain folder', 'named the newest, withdrawn'):
        ('installed 1.1.0, status in result', 'installed 1.1.0, WITHDRAWN'),
    ('install_plugin, plain folder', 'id alone, none active'):
        ('refused release_not_active', 'refused release_not_active'),
    ('install_plugin, plain folder', 'named withdrawn, none active'):
        ('installed 0.9.0, status in result', 'installed 0.9.0, WITHDRAWN'),
    ('install_plugin, a file', 'id alone'):
        ('refused root_unavailable', 'refused root_unavailable'),
    ('install_plugin, a file', 'named withdrawn'):
        ('refused root_unavailable', 'refused root_unavailable'),
    ('install_plugin, a folder with no registry', 'id alone'):
        ('refused registry_missing', 'refused registry_missing'),
    ('install_plugin, a folder with no registry', 'named withdrawn'):
        ('refused registry_missing', 'refused registry_missing'),
}


@unittest.skipUnless(LINUX, LINUX_ONLY)
class RoadTableCase(RegistryCase):
    """One row of ``ROAD_TABLE``, run in its own profile and its own registry."""

    two = EveryRoadTests.two

    def observe(self, form, naming):
        """What the row does on the code under test, in the words of ``ROAD_TABLE``."""
        from recursion_core.plugin_security.install import InstallRefused, install_plugin
        from recursion_core.plugin_security.registry_source import install_from_registry

        road, where, _namings = _FORMS[form]
        fixture, version, allow = _NAMINGS[naming]
        if fixture == "two":
            self.two()
        elif fixture == "newest":
            self.two(first=("1.0.0", "active", None), second=("1.1.0", "withdrawn", _REASON))
        else:
            self.write_registry(_PLUGIN, version="0.9.0", release_over=_over("withdrawn", _REASON))
        if where == "commit":
            bare, commit = self.commit_registry()
            registry = f"{bare}@{commit}"
        elif where == "folder":
            registry = str(self.registry)
        elif where == "file":
            registry = str(self.tmp / "a-file")
            Path(registry).write_text("not a registry\n")
        else:
            registry = str(self.tmp / "no-registry")
            os.mkdir(registry, 0o700)
        before = self.listing()
        if road == "command":
            argv = [_PLUGIN, "--registry", registry] + (["--version", version] if version else []) + (
                ["--allow-withdrawn", allow] if allow else [])
            status, out, err = self.run_cli(*argv)
            if status == 0:
                result = json.loads(out)
                word = ("WITHDRAWN" if "WITHDRAWN:" in err else
                        "status line" if "This release is withdrawn" in err else
                        "status in result" if result.get("status") == "withdrawn" else "no word")
                return f"installed {result['version']}, {word}"
            self.assertEqual(self.listing(), before, err)
            codes = [line[len("refused: "):] for line in err.splitlines() if line.startswith("refused: ")]
            return f"refused {' '.join(codes) or status}"
        seen = []
        options = dict(version=version, state=self.state, report_kept=seen.append)
        if allow:
            options["allow_withdrawn"] = allow
        try:
            if road == "install_from_registry":
                result = install_from_registry(self.config, _PLUGIN, registry=registry, **options)
            else:
                result = install_plugin(self.config, registry, _PLUGIN, **options)
        except InstallRefused as exc:
            self.assertEqual(self.listing(), before)
            return "refused " + " ".join(getattr(exc, "codes", None) or [exc.code])
        word = ("WITHDRAWN" if any(str(line).startswith("WITHDRAWN:") for line in seen) else
                "status in result" if result.get("status") == "withdrawn" else "no word")
        return f"installed {result['version']}, {word}"


def _row_test(form, naming):
    def test(self):
        self.assertEqual(ROAD_TABLE[(form, naming)][1], self.observe(form, naming))
    return test


for _number, (_form, _naming) in enumerate(_ROWS, 1):
    setattr(RoadTableCase, f"test_row_{_number:02d}", _row_test(_form, _naming))


class RoadTableRuleTests(unittest.TestCase):
    def test_every_road_and_naming_has_its_row(self):
        self.assertEqual(sorted(_ROWS), sorted(ROAD_TABLE))

    def test_no_row_goes_from_refused_to_installed(self):
        moved = [row for row, (base, head) in ROAD_TABLE.items()
                 if base.startswith("refused") and head.startswith("installed")]
        self.assertEqual([], moved)

    def test_every_withdrawn_release_installed_here_is_said_before_the_install(self):
        withdrawn = {version for version, status, _reason in _FIXTURES.values() if status == "withdrawn"}
        silent = [row for row, (_base, head) in ROAD_TABLE.items() if head.startswith("installed")
                  and head.split()[1].rstrip(",") in withdrawn and not head.endswith("WITHDRAWN")]
        self.assertEqual([], silent)


_COMMAND = "recursion plugin install "


def assert_stops(case, text):
    """No stop after white space, no doubled stop, and nothing after a printed command on its line."""
    case.assertNotRegex(text, r"\s\.", text)
    case.assertNotRegex(text, r"\.\s*\.", text)
    commands = 0
    for line in text.splitlines():
        at = line.find(_COMMAND)
        if at < 0:
            continue
        commands += 1
        command = line[at:]
        case.assertEqual(command, command.rstrip(), line)
        case.assertFalse(command.endswith((".", ",", ";", ":")), line)
        words = shlex.split(command)
        case.assertEqual(command, " ".join(shlex.quote(word) for word in words), line)
        # After the id, only options with their values: a word or a stop after the last value is caught.
        options = words[4:]
        case.assertEqual(0, len(options) % 2, line)
        case.assertTrue(all(word.startswith("--") for word in options[0::2]), line)
        case.assertFalse(any(word.startswith("--") for word in options[1::2]), line)
    return commands


@unittest.skipUnless(LINUX, LINUX_ONLY)
class WordsTests(RegistryCase):
    two = EveryRoadTests.two

    # The refusal's words, with and without an active release, and the words of an install that goes on.
    def test_the_refusal_with_no_active_release_has_one_stop_and_each_command_ends_its_line(self):
        self.write_registry(_PLUGIN, version="0.9.0", release_over=_over("withdrawn", _REASON))
        bare, commit = self.commit_registry()
        err = self.assert_refused("release_withdrawn", _PLUGIN, "--registry", f"{bare}@{commit}",
                                  "--version", "0.9.0", words=("none can be taken in its place.",))
        self.assertEqual(1, assert_stops(self, err))

    def test_the_refusal_with_an_active_release_has_one_stop_and_each_command_ends_its_line(self):
        self.two()
        bare, commit = self.commit_registry()
        err = self.assert_refused("release_withdrawn", _PLUGIN, "--registry", f"{bare}@{commit}",
                                  "--version", "0.9.0")
        self.assertEqual(2, assert_stops(self, err))

    def test_the_refusal_of_install_from_registry_on_a_folder_has_the_same_stops(self):
        from recursion_core.plugin_security.install import InstallRefused
        from recursion_core.plugin_security.registry_source import install_from_registry

        registry = self.two()
        with self.assertRaises(InstallRefused) as caught:
            install_from_registry(self.config, _PLUGIN, registry=registry, version="0.9.0", state=self.state)
        self.assertEqual("release_withdrawn", caught.exception.code)
        self.assertEqual(2, assert_stops(self, str(caught.exception.detail)))

    def test_a_step_for_another_release_has_the_same_stops(self):
        registry = self.two()
        err = self.assert_refused("allow_withdrawn_unmatched", _PLUGIN, "--registry", registry, "--version", "0.9.0",
                                  "--allow-withdrawn", "1.0.0")
        self.assertEqual(2, assert_stops(self, err))

    def test_the_words_of_an_install_that_goes_on_have_the_same_stops(self):
        registry = self.two()
        status, _out, err = self.run_cli(_PLUGIN, "--registry", registry, "--version", "0.9.0")
        self.assertEqual(status, 0, err)
        self.assertEqual(1, assert_stops(self, err))

    def test_the_guard_catches_each_stray_stop(self):
        command = f"{_COMMAND}{_PLUGIN} --registry /r --version 1.0.0"
        for label, text in {
            "a stop after white space": f"None can be taken in its place. . To install it run: {command}",
            "a stop set apart after a command": f"To install it run: {command} . To install 0.9.0 anyway",
            "a doubled stop": "who give this reason: A made-up reason to withdraw..",
            "a stop glued to a command": f"To install it run: {command}.",
            "words after a command": f"To install it run: {command} and then",
        }.items():
            with self.subTest(label):
                with self.assertRaises(AssertionError):
                    assert_stops(self, text)

    # The newest release that is not withdrawn, when the withdrawn one named is itself the newest of all.
    def test_the_newest_not_withdrawn_is_named_when_the_withdrawn_one_is_the_newest_of_all(self):
        newer = ("1.1.0", "withdrawn", _REASON)
        registry = self.two(first=("1.0.0", "active", None), second=newer)
        status, _out, err = self.run_cli(_PLUGIN, "--registry", registry, "--version", "1.1.0")
        self.assertEqual(status, 0, err)
        self.assertIn(f"The newest release of {_PLUGIN} there that is not withdrawn is 1.0.0; to install it run: "
                      f"{_COMMAND}{_PLUGIN} --registry {registry} --version 1.0.0", err)
        self.assertNotIn("that is not withdrawn is 1.1.0", err)
        bare, commit = self.commit_registry()
        err = self.assert_refused("release_withdrawn", _PLUGIN, "--registry", f"{bare}@{commit}", "--version", "1.1.0")
        self.assertIn("there that is not withdrawn is 1.0.0; to install it run: ", err)
        self.assertNotIn("that is not withdrawn is 1.1.0", err)


if __name__ == "__main__":
    unittest.main()
