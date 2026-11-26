"""What an install by registry shows and refuses: the label, the requested permissions, a withdrawn release.

A withdrawn release is refused by default, with its status and reason and the one step that installs
it anyway (--allow-withdrawn naming that same version); a deprecated release installs and says so.
The label is shown as a label and the permissions as requested, never as enforced. Every registry is
made up by the test in a local bare repository of its own.
"""
from __future__ import annotations

import unittest

from recursion_core.plugin_security.tests.test_plugin_cli_install import LINUX, LINUX_ONLY
from recursion_core.plugin_security.tests.test_plugin_install_from_registry import RegistryCase

_PLUGIN = "fixture.unsigned"
_REASON = "A made-up reason to withdraw."


@unittest.skipUnless(LINUX, LINUX_ONLY)
class ReleaseStatusTests(RegistryCase):
    def withdrawn(self):
        self.write_registry(_PLUGIN, version="0.9.0", release_over={"status": "withdrawn", "status_reason": _REASON})
        bare, commit = self.commit_registry()
        return f"{bare}@{commit}"

    def test_a_withdrawn_release_is_refused_by_default_with_its_reason_and_the_step(self):
        registry = self.withdrawn()
        self.assert_refused("release_withdrawn", _PLUGIN, "--registry", registry, "--version", "0.9.0",
                            words=(_REASON, "--allow-withdrawn 0.9.0"))

    def test_the_persons_explicit_step_installs_that_one_release(self):
        registry = self.withdrawn()
        status, _out, err = self.run_cli(_PLUGIN, "--registry", registry, "--version", "0.9.0",
                                         "--allow-withdrawn", "0.9.0")
        self.assertEqual(status, 0, err)
        self.assertIn(f"This release is withdrawn: {_REASON}\n", err)

    def test_the_step_for_another_version_does_not_install_it(self):
        registry = self.withdrawn()
        self.assert_refused("release_withdrawn", _PLUGIN, "--registry", registry, "--version", "0.9.0",
                            "--allow-withdrawn", "1.0.0")

    def test_a_deprecated_release_named_by_version_installs_and_says_so(self):
        # As before this draft, only an active release is taken when no version is named.
        self.write_registry(_PLUGIN, release_over={"status": "deprecated", "status_reason": "A made-up reason."})
        bare, commit = self.commit_registry()
        status, _out, err = self.run_cli(_PLUGIN, "--registry", f"{bare}@{commit}", "--version", "1.0.0")
        self.assertEqual(status, 0, err)
        self.assertIn("This release is deprecated: A made-up reason.\n", err)

    def test_the_label_and_the_requested_permissions_are_shown_as_such(self):
        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry()
        status, _out, err = self.run_cli(_PLUGIN, "--registry", f"{bare}@{commit}")
        self.assertEqual(status, 0, err)
        self.assertIn("Registry label: non_verified (a label, not a gate).\n", err)
        self.assertIn("Requested permissions (requested by the release, not enforced by this install): "
                      "read, panel.\n", err)


if __name__ == "__main__":
    unittest.main()
