"""CSL-734 B7 / F21: privileged spawn detection, measured backends, beta fallback.

Live probes on this host do not install helpers, edit sudoers or polkit, create a
system user, copy credentials, or launch Claude Code or Codex.
"""

from __future__ import annotations

import os
import stat
import unittest
from types import SimpleNamespace

from recursion_core.control.privileged_spawn import (
    CLAUDE_CODE_SIGNIN_SURVIVES_REBOOT,
    CODEX_SIGNIN_SURVIVES_REBOOT,
    DEFAULT_N,
    ENGINE_USER_CREATED_AT_INSTALL,
    HEADLESS_SECRET_SERVICE_LIVE,
    PRODUCT_MD_COMMIT,
    PRODUCT_MD_SHA256,
    Backend,
    HostFacts,
    ProcResult,
    byok_storage_when_secret_service_fails_closed,
    collect_host_facts,
    detect_backends,
    evaluate_host,
    percentile,
    run_same_uid_control_probes,
    select_path,
)


def _facts(**overrides) -> HostFacts:
    values = dict(
        uid=1000,
        username="box",
        sudo_n_true=False,
        subuid_lines=("box:100000:65536",),
        subgid_lines=("box:100000:65536",),
        newuidmap_path=None,
        newuidmap_is_setuid_root=False,
        user_ns_max=63991,
        unshare_user_works=True,
        unshare_host_uid=1000,
        systemd_is_pid1=False,
    )
    values.update(overrides)
    return HostFacts(**values)


def _run_fail(cmd, timeout: float) -> ProcResult:
    return ProcResult(1, "", f"denied:{' '.join(cmd)}")


class PinAndUnmeasuredTests(unittest.TestCase):
    def test_product_md_pin(self) -> None:
        self.assertEqual(PRODUCT_MD_COMMIT, "055cc7a5c793b7135e5d0565dc4198cf96230531")
        self.assertEqual(len(PRODUCT_MD_COMMIT), 40)
        int(PRODUCT_MD_COMMIT, 16)
        self.assertEqual(
            PRODUCT_MD_SHA256,
            "e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf",
        )
        self.assertEqual(len(PRODUCT_MD_SHA256), 64)
        int(PRODUCT_MD_SHA256, 16)

    def test_founder_box_items_are_explicitly_unmeasured(self) -> None:
        self.assertIsNone(ENGINE_USER_CREATED_AT_INSTALL)
        self.assertIsNone(CLAUDE_CODE_SIGNIN_SURVIVES_REBOOT)
        self.assertIsNone(CODEX_SIGNIN_SURVIVES_REBOOT)
        self.assertIsNone(HEADLESS_SECRET_SERVICE_LIVE)


class ByokStorageTests(unittest.TestCase):
    def test_model_provider_keys_may_use_an_owner_only_file(self) -> None:
        self.assertEqual(
            byok_storage_when_secret_service_fails_closed("model_provider"),
            "owner_only_file",
        )

    def test_broker_and_venue_keys_fail_closed(self) -> None:
        self.assertEqual(byok_storage_when_secret_service_fails_closed("broker"), "fail_closed")
        self.assertEqual(byok_storage_when_secret_service_fails_closed("venue"), "fail_closed")

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            byok_storage_when_secret_service_fails_closed("session_cookie")


class PercentileTests(unittest.TestCase):
    def test_nearest_rank_on_twenty_samples(self) -> None:
        samples = [float(i + 1) for i in range(20)]
        self.assertEqual(percentile(samples, 50), 10.0)
        self.assertEqual(percentile(samples, 95), 19.0)
        self.assertEqual(percentile(samples, 100), 20.0)

    def test_rejects_empty_or_bad_p(self) -> None:
        with self.assertRaises(ValueError):
            percentile([], 50)
        with self.assertRaises(ValueError):
            percentile([1.0], 0)


class SelectionTests(unittest.TestCase):
    def test_fallback_is_beta_when_no_other_uid_backend_exists(self) -> None:
        backends = (
            Backend("setuid_helper", False, False, "absent"),
            Backend("sudo", False, False, "sudo -n true failed"),
            Backend("systemd_run", False, False, "no systemd"),
            Backend("subuid_userns", False, False, "no newuidmap"),
        )
        selected = select_path(backends)
        self.assertEqual(selected.name, "managed_login_folder")
        self.assertTrue(selected.beta)
        self.assertIn("fallback ships as beta", selected.reason)
        self.assertIsNone(selected.backend)

    def test_unavailable_backend_that_does_not_change_host_uid_is_ignored(self) -> None:
        backends = (
            Backend("setuid_helper", True, False, "exists but protocol unknown"),
            Backend("sudo", False, False, "no"),
            Backend("systemd_run", False, False, "no"),
            Backend("subuid_userns", False, False, "no"),
        )
        selected = select_path(backends)
        self.assertEqual(selected.name, "managed_login_folder")
        self.assertTrue(selected.beta)

    def test_prefers_setuid_helper_then_sudo_then_systemd_then_subuid(self) -> None:
        sudo = Backend("sudo", True, True, "ok", target_user="nobody", target_uid=65534)
        systemd = Backend("systemd_run", True, True, "ok", target_user="nobody", target_uid=65534)
        subuid = Backend("subuid_userns", True, True, "ok", target_uid=100000)
        helper = Backend("setuid_helper", True, True, "ok", helper_path="/helper", target_uid=123)
        self.assertEqual(select_path((sudo, systemd, subuid)).name, "sudo")
        self.assertEqual(select_path((helper, sudo, systemd, subuid)).name, "setuid_helper")
        self.assertFalse(select_path((helper, sudo)).beta)


class DetectionInjectionTests(unittest.TestCase):
    def test_missing_helper_sudo_systemd_and_newuidmap_select_beta_fallback(self) -> None:
        backends = detect_backends(
            facts=_facts(),
            run_fn=_run_fail,
            helper_paths=("/tmp/csl734-no-such-helper",),
            env={},
        )
        by_name = {b.name: b for b in backends}
        self.assertFalse(by_name["setuid_helper"].available)
        self.assertIn("absent", by_name["setuid_helper"].reason)
        self.assertFalse(by_name["sudo"].available)
        self.assertFalse(by_name["systemd_run"].available)
        self.assertFalse(by_name["subuid_userns"].available)
        self.assertIn("newuidmap absent", by_name["subuid_userns"].reason)
        self.assertIn("box:100000:65536", by_name["subuid_userns"].reason)
        selected = select_path(backends)
        self.assertEqual(selected.name, "managed_login_folder")
        self.assertTrue(selected.beta)

    def test_user_owned_setuid_bit_is_not_treated_as_a_helper(self) -> None:
        def stat_fn(path: str) -> os.stat_result:
            mode = stat.S_IFREG | stat.S_ISUID | 0o755
            return SimpleNamespace(st_mode=mode, st_uid=1000)  # type: ignore[return-value]

        backends = detect_backends(
            facts=_facts(),
            run_fn=_run_fail,
            helper_paths=("/tmp/fake-helper",),
            stat_fn=stat_fn,
            env={},
        )
        helper = next(b for b in backends if b.name == "setuid_helper")
        self.assertFalse(helper.available)
        self.assertIsNone(helper.helper_path)
        self.assertIn("setuid_root=False", helper.reason)

    def test_setuid_root_helper_is_recorded_but_not_executed(self) -> None:
        def stat_fn(path: str) -> os.stat_result:
            mode = stat.S_IFREG | stat.S_ISUID | 0o755
            return SimpleNamespace(st_mode=mode, st_uid=0)  # type: ignore[return-value]

        backends = detect_backends(
            facts=_facts(),
            run_fn=_run_fail,
            helper_paths=("/usr/local/libexec/recursion-spawn-as",),
            stat_fn=stat_fn,
            env={},
        )
        helper = next(b for b in backends if b.name == "setuid_helper")
        self.assertFalse(helper.available)
        self.assertFalse(helper.changes_host_uid)
        self.assertEqual(helper.helper_path, "/usr/local/libexec/recursion-spawn-as")
        self.assertIn("not executed", helper.reason)


class LiveHostTests(unittest.TestCase):
    """Real probes and timings for backends that already work on this host."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.evaluation = evaluate_host(n=DEFAULT_N)
        cls.control = run_same_uid_control_probes(n=DEFAULT_N)

    def test_host_facts_match_what_was_read(self) -> None:
        facts = self.evaluation.facts
        live = collect_host_facts()
        self.assertEqual(facts.uid, os.getuid())
        self.assertEqual(facts.uid, live.uid)
        self.assertEqual(facts.username, live.username)
        self.assertEqual(facts.subuid_lines, live.subuid_lines)
        self.assertEqual(facts.subgid_lines, live.subgid_lines)
        self.assertGreaterEqual(facts.uid, 0)
        self.assertTrue(facts.username)

    def test_unmeasured_dict_is_all_none(self) -> None:
        self.assertEqual(
            set(self.evaluation.unmeasured),
            {
                "engine_user_created_at_install",
                "claude_code_signin_survives_reboot",
                "codex_signin_survives_reboot",
                "headless_secret_service_live",
            },
        )
        self.assertTrue(all(value is None for value in self.evaluation.unmeasured.values()))

    def test_unavailable_backends_are_not_faked(self) -> None:
        by_name = {b.name: b for b in self.evaluation.backends}
        self.assertIn("setuid_helper", by_name)
        self.assertIn("systemd_run", by_name)
        self.assertIn("subuid_userns", by_name)
        helper = by_name["setuid_helper"]
        if helper.helper_path is None:
            self.assertFalse(helper.available)
            self.assertIn("not installing a setuid helper", helper.reason)
        systemd = by_name["systemd_run"]
        if not systemd.available:
            self.assertFalse(systemd.changes_host_uid)
            self.assertIn("not editing polkit", systemd.reason)
        subuid = by_name["subuid_userns"]
        if not live_newuidmap_ready(self.evaluation.facts):
            self.assertFalse(subuid.available)
            self.assertFalse(subuid.changes_host_uid)

    def test_same_uid_control_channels_stay_open(self) -> None:
        by_name = {probe.name: probe for probe in self.control}
        for name in ("proc_pid_root", "ptrace", "abs_store_read"):
            probe = by_name[name]
            self.assertEqual(probe.n, DEFAULT_N)
            self.assertEqual(
                probe.blocked_count,
                0,
                f"{name} should stay open under the same uid; detail={probe.detail}",
            )
            self.assertFalse(probe.blocked)

    def test_measured_backends_block_f21_channels_and_report_percentiles(self) -> None:
        evaluation = self.evaluation
        measured = [
            backend
            for backend in evaluation.backends
            if backend.available and backend.changes_host_uid
        ]
        if not measured:
            self.assertEqual(evaluation.selected.name, "managed_login_folder")
            self.assertTrue(evaluation.selected.beta)
            self.assertEqual(evaluation.overhead, {})
            self.assertEqual(evaluation.probes, {})
            return
        self.assertIn(evaluation.selected.name, {b.name for b in measured})
        self.assertFalse(evaluation.selected.beta)
        for backend in measured:
            overhead = evaluation.overhead[backend.name]
            self.assertGreaterEqual(overhead.n, 10)
            self.assertEqual(overhead.n, DEFAULT_N)
            self.assertLessEqual(overhead.min_s, overhead.p50_s)
            self.assertLessEqual(overhead.p50_s, overhead.p95_s)
            self.assertLessEqual(overhead.p95_s, overhead.max_s)
            self.assertGreater(overhead.p50_s, 0.0)
            self.assertEqual(overhead.command[0], "sudo" if backend.name == "sudo" else overhead.command[0])
            probes = {probe.name: probe for probe in evaluation.probes[backend.name]}
            for name in ("proc_pid_root", "ptrace", "abs_store_read"):
                probe = probes[name]
                self.assertEqual(probe.n, DEFAULT_N)
                self.assertEqual(
                    probe.blocked_count,
                    DEFAULT_N,
                    f"{backend.name} {name} should drop to 0/n open; detail={probe.detail}",
                )
                self.assertTrue(probe.blocked)


def live_newuidmap_ready(facts: HostFacts) -> bool:
    return bool(facts.newuidmap_path and facts.newuidmap_is_setuid_root and facts.subuid_lines)


if __name__ == "__main__":
    unittest.main()
