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

    # (d) the API's folder road: the same decision and words as the command line's
    def test_d_install_from_registry_folder_says_it(self):
        from recursion_core.plugin_security.registry_source import install_from_registry

        registry = self.two()
        seen = []
        result = install_from_registry(self.config, _PLUGIN, registry=registry, version="0.9.0", state=self.state,
                                       report_kept=seen.append)
        self.said_withdrawn(seen[0], registry)
        self.assertEqual("named_version", result["withdrawn_by"])

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


if __name__ == "__main__":
    unittest.main()
