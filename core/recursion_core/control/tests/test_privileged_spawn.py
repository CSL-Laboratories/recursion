"""CSL-734 B7 / F21: privileged spawn detection, measured backends, beta fallback.

The default run is pure unit tests with injected host facts and commands. The
live host checks (real sudo, systemd-run, unshare and process probes) run only
when ``RECURSION_LIVE_HOST_PROBES=1`` is set. They do not install helpers, edit
sudoers or polkit, create a system user, copy credentials, or launch Claude
Code or Codex.
"""

from __future__ import annotations

import ast
import os
import stat
import tempfile
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
    Overhead,
    ProbeSummary,
    ProcResult,
    byok_storage_when_secret_service_fails_closed,
    can_spawn,
    collect_host_facts,
    detect_backends,
    evaluate_host,
    find_target_uid_pid,
    percentile,
    probe_abs_store_read,
    run_same_uid_control_probes,
    run_same_uid_probes,
    select_path,
    SLEEP_PROBE_ARGV,
    store_writer_script,
    SUDO_REQUIRED_COMMANDS,
    systemd_unit_main_pid,
)
from recursion_core.control.privileged_spawn import _Tally, _launch_failure

LIVE_ENV = "RECURSION_LIVE_HOST_PROBES"
LIVE = os.environ.get(LIVE_ENV) == "1"


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


def _sudo_policy(allowed: dict[tuple[str, ...], str]):
    """Fake runner: ``allowed`` maps an exact argv to its stdout; all else is denied.

    A trailing ``*`` after ``python3 -c`` matches any store-writer script body,
    so tests can grant the real writer without copying a temp path.
    """

    def run(cmd, timeout: float) -> ProcResult:
        key = tuple(cmd)
        if key in allowed:
            return ProcResult(0, allowed[key], "")
        if len(key) >= 3 and key[-3:-1] == ("python3", "-c"):
            wildcard = key[:-1] + ("*",)
            if wildcard in allowed:
                return ProcResult(0, allowed[wildcard], "")
        return ProcResult(1, "", f"denied:{' '.join(cmd)}")

    return run


def _nobody_allows(*argvs: tuple[str, ...]) -> dict[tuple[str, ...], str]:
    out = {("sudo", "-n", "-u", "nobody", "--", "id", "-u"): "65534\n"}
    for argv in argvs:
        out[("sudo", "-n", "-u", "nobody", "--", *argv)] = ""
    return out


ALL_REQUIRED = (("true",), ("sleep", "8"), ("python3", "-c", "*"))


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

    def test_required_sudo_commands_match_the_probe_launches(self) -> None:
        self.assertEqual(SLEEP_PROBE_ARGV, ("sleep", "8"))
        self.assertIn(("true",), SUDO_REQUIRED_COMMANDS)
        self.assertIn(SLEEP_PROBE_ARGV, SUDO_REQUIRED_COMMANDS)
        self.assertNotIn(("sleep", "0"), SUDO_REQUIRED_COMMANDS)
        script = store_writer_script("/tmp/example.secret")
        self.assertIn("store-bytes", script)
        self.assertIn("/tmp/example.secret", script)


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

    def test_prefers_sudo_then_systemd_among_spawnable_backends(self) -> None:
        # Devin r4506565344: these assertions replaced an earlier helper-first
        # selection check after setuid_helper and subuid_userns stopped being
        # selectable (spawn_command cannot launch them). A named human reviewer
        # must submit their own GitHub review of the changed assertions.
        # Agents do not approve, dismiss, or treat this comment as that review.
        sudo = Backend("sudo", True, True, "ok", target_user="nobody", target_uid=65534)
        systemd = Backend("systemd_run", True, True, "ok", target_user="nobody", target_uid=65534)
        subuid = Backend("subuid_userns", True, True, "ok", target_uid=100000)
        helper = Backend("setuid_helper", True, True, "ok", helper_path="/helper", target_uid=123)
        selected_sudo = select_path((sudo, systemd, subuid))
        self.assertEqual(
            selected_sudo.name,
            "sudo",
            "sudo ranks above systemd_run among spawnable backends",
        )
        self.assertFalse(
            selected_sudo.beta,
            "a spawnable other-uid path is not the beta fallback",
        )
        selected_systemd = select_path((systemd, subuid))
        self.assertEqual(
            selected_systemd.name,
            "systemd_run",
            "systemd_run ranks above detection-only backends",
        )
        self.assertFalse(selected_systemd.beta)
        selected_sudo_only = select_path((sudo,))
        self.assertEqual(selected_sudo_only.name, "sudo")
        self.assertFalse(selected_sudo_only.beta)
        self.assertFalse(can_spawn(helper))
        self.assertFalse(can_spawn(subuid))

    def test_backends_spawn_command_rejects_are_never_selected(self) -> None:
        # Devin r4506181267: a working subuid map, or a setuid helper without a
        # recorded protocol, cannot launch an engine, so selecting it is wrong.
        subuid = Backend("subuid_userns", True, True, "ok", target_uid=100001)
        helper = Backend("setuid_helper", True, True, "ok", helper_path="/helper", target_uid=123)
        self.assertFalse(can_spawn(subuid))
        self.assertFalse(can_spawn(helper))
        selected = select_path((helper, subuid))
        self.assertEqual(selected.name, "managed_login_folder")
        self.assertTrue(selected.beta)
        sudo = Backend("sudo", True, True, "ok", target_user="nobody", target_uid=65534)
        self.assertTrue(can_spawn(sudo))
        self.assertEqual(select_path((helper, sudo, subuid)).name, "sudo")


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


class SudoPolicyDetectionTests(unittest.TestCase):
    def _sudo(self, allowed) -> Backend:
        backends = detect_backends(
            facts=_facts(sudo_n_true=False),
            run_fn=_sudo_policy(allowed),
            helper_paths=("/tmp/csl734-no-such-helper",),
            env={},
        )
        return next(b for b in backends if b.name == "sudo")

    def test_target_only_sudo_is_detected_without_root(self) -> None:
        # Devin r4506181429: sudoers grants nobody but not root.
        sudo = self._sudo(_nobody_allows(*ALL_REQUIRED))
        self.assertTrue(sudo.available)
        self.assertTrue(sudo.changes_host_uid)
        self.assertEqual(sudo.target_user, "nobody")
        self.assertEqual(sudo.target_uid, 65534)
        self.assertIn("sudo -n true failed", sudo.reason)
        backends = (Backend("setuid_helper", False, False, "absent"), sudo)
        selected = select_path(backends)
        self.assertEqual(selected.name, "sudo")
        self.assertFalse(selected.beta)

    def test_target_sudo_missing_a_required_command_is_unavailable(self) -> None:
        # Devin r4506181499: id -u allowed as nobody, true denied.
        sudo = self._sudo(_nobody_allows(("sleep", "8"), ("python3", "-c", "*")))
        self.assertFalse(sudo.available)
        self.assertIsNone(sudo.target_user)
        self.assertIn("-- true rc=1", sudo.reason)
        sudo = self._sudo(_nobody_allows(("true",), ("sleep", "8")))
        self.assertFalse(sudo.available)
        self.assertIn("python3 -c rc=1", sudo.reason)

    def test_argument_specific_sleep_0_and_pass_policy_is_unavailable(self) -> None:
        # Devin r4506565205: sudoers allows id -u, true, sleep 0 and
        # python3 -c pass, but not the actual probe launches (sleep 8 and
        # the store-writer script).
        sudo = self._sudo(
            _nobody_allows(("true",), ("sleep", "0"), ("python3", "-c", "pass"))
        )
        self.assertFalse(sudo.available)
        self.assertIsNone(sudo.target_user)
        self.assertIn("sleep 8", sudo.reason)

    def test_sudo_target_that_is_root_or_our_uid_is_rejected(self) -> None:
        allowed = {("sudo", "-n", "-u", "nobody", "--", "id", "-u"): "0\n"}
        allowed.update({("sudo", "-n", "-u", "nobody", "--", *a): "" for a in ALL_REQUIRED})
        allowed[("sudo", "-n", "-u", "daemon", "--", "id", "-u")] = "1000\n"
        self.assertFalse(self._sudo(allowed).available)


class EvaluateHostInjectionTests(unittest.TestCase):
    def _backends(self) -> tuple[Backend, ...]:
        return (
            Backend("setuid_helper", False, False, "absent"),
            Backend("sudo", True, True, "ok", target_user="nobody", target_uid=65534),
            Backend("systemd_run", False, False, "no systemd"),
            Backend("subuid_userns", False, True, "detection only", target_uid=100001),
        )

    def test_measurement_failure_is_recorded_and_falls_back(self) -> None:
        def measure_fn(backend: Backend, n: int) -> Overhead:
            raise RuntimeError("overhead command failed rc=1 err=sudo: a password is required")

        def probe_fn(backend: Backend, n: int):
            raise AssertionError("probes must not run after a failed measurement")

        evaluation = evaluate_host(
            n=DEFAULT_N,
            facts=_facts(sudo_n_true=True),
            backends=self._backends(),
            measure_fn=measure_fn,
            probe_fn=probe_fn,
        )
        self.assertIn("sudo", evaluation.errors)
        self.assertIn("password is required", evaluation.errors["sudo"])
        sudo = next(b for b in evaluation.backends if b.name == "sudo")
        self.assertFalse(sudo.available)
        self.assertIn("measurement failed", sudo.reason)
        self.assertEqual(evaluation.overhead, {})
        self.assertEqual(evaluation.probes, {})
        self.assertEqual(evaluation.selected.name, "managed_login_folder")
        self.assertTrue(evaluation.selected.beta)

    def test_measured_backend_is_selected_and_subuid_is_not_measured(self) -> None:
        seen: list[str] = []
        overhead = Overhead(DEFAULT_N, 0.01, 0.02, 0.005, 0.03, ("sudo",))
        summaries = tuple(
            ProbeSummary(name, DEFAULT_N, DEFAULT_N, "denied", 0, "sleep uid=65534 x20")
            for name in ("proc_pid_root", "ptrace", "abs_store_read")
        )

        def measure_fn(backend: Backend, n: int) -> Overhead:
            seen.append(backend.name)
            return overhead

        evaluation = evaluate_host(
            n=DEFAULT_N,
            facts=_facts(),
            backends=self._backends(),
            measure_fn=measure_fn,
            probe_fn=lambda backend, n: summaries,
        )
        self.assertEqual(seen, ["sudo"])
        self.assertEqual(evaluation.errors, {})
        self.assertEqual(evaluation.selected.name, "sudo")
        self.assertEqual(evaluation.probes["sudo"], summaries)

    def test_invalid_sample_count_raises_instead_of_selecting_fallback(self) -> None:
        # Devin r4506565268: n<10 used to be swallowed as a backend failure.
        def measure_fn(backend: Backend, n: int) -> Overhead:
            raise AssertionError("must not measure when n is invalid")

        def probe_fn(backend: Backend, n: int):
            raise AssertionError("must not probe when n is invalid")

        with self.assertRaises(ValueError) as ctx:
            evaluate_host(
                n=9,
                facts=_facts(sudo_n_true=True),
                backends=self._backends(),
                measure_fn=measure_fn,
                probe_fn=probe_fn,
            )
        self.assertIn("n must be >= 10", str(ctx.exception))
        self.assertIn("got 9", str(ctx.exception))
        with self.assertRaises(ValueError):
            evaluate_host(
                n=0,
                facts=_facts(),
                backends=self._backends(),
                measure_fn=measure_fn,
                probe_fn=probe_fn,
            )

    def test_launch_failures_in_probes_demote_the_backend(self) -> None:
        # Devin r4506565205: process-not-found and store-writer errors must
        # not leave sudo selected.
        overhead = Overhead(DEFAULT_N, 0.01, 0.02, 0.005, 0.03, ("sudo",))
        failed = (
            ProbeSummary(
                "proc_pid_root",
                DEFAULT_N,
                0,
                "no process with uid 65534 under launcher pid 1",
                DEFAULT_N,
                "not found x20",
            ),
            ProbeSummary(
                "ptrace",
                DEFAULT_N,
                0,
                "no process with uid 65534 under launcher pid 1",
                DEFAULT_N,
                "not found x20",
            ),
            ProbeSummary(
                "abs_store_read",
                DEFAULT_N,
                0,
                "writer_failed rc=1 err=denied",
                DEFAULT_N,
                "0600 store owner uid=65534 x20",
            ),
        )

        evaluation = evaluate_host(
            n=DEFAULT_N,
            facts=_facts(sudo_n_true=True),
            backends=self._backends(),
            measure_fn=lambda backend, n: overhead,
            probe_fn=lambda backend, n: failed,
        )
        self.assertIn("sudo", evaluation.errors)
        self.assertIn("writer failed", evaluation.errors["sudo"])
        self.assertIn("engine process did not appear", evaluation.errors["sudo"])
        sudo = next(b for b in evaluation.backends if b.name == "sudo")
        self.assertFalse(sudo.available)
        self.assertIn("launch failed", sudo.reason)
        self.assertEqual(evaluation.overhead, {})
        self.assertNotIn("sudo", evaluation.probes)
        self.assertEqual(evaluation.selected.name, "managed_login_folder")
        self.assertTrue(evaluation.selected.beta)


class ProbeTargetTests(unittest.TestCase):
    def test_walk_skips_the_sudo_launcher_and_finds_the_engine(self) -> None:
        # Devin r4506181329: sudo (ruid=caller, euid=0) -> sleep (all 65534).
        uids = {100: (1000, 0, 0, 0), 101: (0, 0, 0, 0), 102: (65534,) * 4}
        children = {100: [101], 101: [102], 102: []}
        pid = find_target_uid_pid(
            100, 65534, timeout_s=0.0, uids_fn=uids.get, children_fn=lambda p: children[p]
        )
        self.assertEqual(pid, 102)

    def test_walk_never_returns_a_non_target_process(self) -> None:
        uids = {100: (1000, 0, 0, 0), 101: (0, 0, 0, 0), 102: (65534, 65534, 65534, 0)}
        children = {100: [101], 101: [102], 102: []}
        self.assertIsNone(
            find_target_uid_pid(
                100, 65534, timeout_s=0.05, uids_fn=uids.get, children_fn=lambda p: children[p]
            )
        )
        # Root as the target never matches the sudo launcher (ruid = caller).
        self.assertIsNone(
            find_target_uid_pid(
                100, 0, timeout_s=0.0, uids_fn={100: (1000, 0, 0, 0)}.get, children_fn=lambda p: []
            )
        )

    def test_systemd_unit_main_pid_checks_the_unit_uid(self) -> None:
        calls: list[tuple[str, ...]] = []

        def run(cmd, timeout: float) -> ProcResult:
            calls.append(tuple(cmd))
            return ProcResult(0, "4242\n", "")

        pid = systemd_unit_main_pid(
            "csl734-probe-x", 65534, run_fn=run, timeout_s=0.0, uids_fn={4242: (65534,) * 4}.get
        )
        self.assertEqual(pid, 4242)
        self.assertEqual(
            calls[0], ("systemctl", "show", "-p", "MainPID", "--value", "csl734-probe-x")
        )
        self.assertIsNone(
            systemd_unit_main_pid(
                "u", 65534, run_fn=run, timeout_s=0.0, uids_fn={4242: (1000,) * 4}.get
            )
        )
        self.assertIsNone(
            systemd_unit_main_pid(
                "u",
                65534,
                run_fn=lambda cmd, t: ProcResult(0, "0\n", ""),
                timeout_s=0.0,
                uids_fn={0: (65534,) * 4}.get,
            )
        )

    def test_probes_require_a_target_uid(self) -> None:
        with self.assertRaises(ValueError):
            run_same_uid_probes(Backend("sudo", True, True, "ok", target_user="nobody"))


class StoreProbeErrorTests(unittest.TestCase):
    def test_failed_writer_is_an_error_not_a_blocked_read(self) -> None:
        # Devin r4506181376.
        with tempfile.TemporaryDirectory() as tmp:
            result, detail = probe_abs_store_read(
                ("sudo", "-n", "-u", "nobody", "--"),
                tmp,
                expected_owner=65534,
                run_fn=lambda cmd, t: ProcResult(1, "", "sudo: a password is required"),
            )
        self.assertIsNone(result)
        self.assertIn("writer_failed", detail)

    def test_writer_that_left_no_store_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, detail = probe_abs_store_read(
                (), tmp, expected_owner=65534, run_fn=lambda cmd, t: ProcResult(0, "", "")
            )
        self.assertIsNone(result)
        self.assertIn("writer_left_no_store", detail)

    def test_store_written_by_the_wrong_uid_is_an_error(self) -> None:
        def run(cmd, timeout: float) -> ProcResult:
            # Executes nothing: the fake writer plants the file as our own uid.
            script = cmd[-1]
            path = script.split("p=", 1)[1].split(";", 1)[0]
            with open(ast.literal_eval(path), "wb") as handle:
                handle.write(b"x")
            return ProcResult(0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            result, detail = probe_abs_store_read((), tmp, expected_owner=os.getuid() + 1, run_fn=run)
            self.assertEqual(os.listdir(tmp), [])
        self.assertIsNone(result)
        self.assertIn("writer_wrong_uid", detail)

    def test_summary_with_errors_is_never_blocked(self) -> None:
        summary = ProbeSummary("abs_store_read", 20, 19, "writer_failed", 1)
        self.assertFalse(summary.blocked)
        self.assertEqual(summary.open_count, 0)
        self.assertTrue(ProbeSummary("ptrace", 20, 20, "denied", 0).blocked)
        self.assertFalse(ProbeSummary("ptrace", 20, 0, "x", 20).blocked)


class DevinReview5818620711Tests(unittest.TestCase):
    """Devin reviews 5818620711 on c46a151f (r4506887696, r4506887763) and r4507075865 on 782094d5."""

    def test_sudo_sleep_launch_gets_a_timeout_longer_than_the_sleep(self) -> None:
        # r4506887696: a runner that honours its timeout returns 124 when the
        # command needs longer than the timeout; sleep 8 needs more than 8 s.
        allowed = _nobody_allows(*ALL_REQUIRED)
        policy = _sudo_policy(allowed)
        seen: dict[tuple[str, ...], float] = {}

        def run(cmd, timeout: float) -> ProcResult:
            key = tuple(cmd)
            seen[key] = timeout
            if key[-2:] == SLEEP_PROBE_ARGV and timeout <= float(SLEEP_PROBE_ARGV[1]):
                return ProcResult(124, "", "timeout")
            return policy(cmd, timeout)

        backends = detect_backends(
            facts=_facts(sudo_n_true=False),
            run_fn=run,
            helper_paths=("/tmp/csl734-no-such-helper",),
            env={},
        )
        sudo = next(b for b in backends if b.name == "sudo")
        self.assertTrue(sudo.available, sudo.reason)
        self.assertEqual(sudo.target_user, "nobody")
        sleep_key = ("sudo", "-n", "-u", "nobody", "--", *SLEEP_PROBE_ARGV)
        self.assertGreater(seen[sleep_key], float(SLEEP_PROBE_ARGV[1]))
        # The probe command itself is unchanged: exactly the measured launch.
        self.assertIn(sleep_key, seen)

    def test_earlier_writer_failure_is_kept_when_the_last_attempt_succeeds(self) -> None:
        # r4506887763: first of 20 writer calls fails, the other 19 block.
        tally = _Tally()
        tally.add("abs_store_read", (None, "writer_failed rc=1 err=denied"), "0600 store owner uid=65534")
        for _ in range(DEFAULT_N - 1):
            tally.add("abs_store_read", (True, "PermissionError"), "0600 store owner uid=65534")
        summary = next(s for s in tally.summaries(DEFAULT_N) if s.name == "abs_store_read")
        self.assertEqual(summary.error_count, 1)
        self.assertEqual(summary.detail, "PermissionError")  # last detail, as before
        self.assertEqual(summary.launch_failure_count, 1)
        self.assertIn("writer_failed", summary.launch_failure_detail)
        reason = _launch_failure((summary,))
        self.assertIsNotNone(reason)
        self.assertIn("writer failed on 1/20", reason)

    def test_earlier_missing_engine_process_is_kept(self) -> None:
        tally = _Tally()
        tally.add("ptrace", (None, "no process with uid 65534 under launcher pid 7"), "not found")
        for _ in range(DEFAULT_N - 1):
            tally.add("ptrace", (True, "EPERM"), "sleep uid=65534")
        summary = next(s for s in tally.summaries(DEFAULT_N) if s.name == "ptrace")
        self.assertEqual(summary.launch_failure_count, 1)
        self.assertIn("engine process did not appear on 1/20", _launch_failure((summary,)))

    def test_other_probe_errors_are_not_launch_failures(self) -> None:
        tally = _Tally()
        tally.add("ptrace", (None, "ptrace unavailable: ENOSYS"), "sleep uid=65534")
        for _ in range(DEFAULT_N - 1):
            tally.add("ptrace", (True, "EPERM"), "sleep uid=65534")
        summary = next(s for s in tally.summaries(DEFAULT_N) if s.name == "ptrace")
        self.assertEqual(summary.launch_failure_count, 0)
        self.assertIsNone(_launch_failure((summary,)))

    def test_evaluate_host_demotes_on_one_early_launch_failure(self) -> None:
        tally = _Tally()
        tally.add("abs_store_read", (None, "writer_failed rc=1 err=denied"), "0600 store owner uid=65534")
        for _ in range(DEFAULT_N - 1):
            tally.add("abs_store_read", (True, "PermissionError"), "0600 store owner uid=65534")
            tally.add("proc_pid_root", (True, "EACCES"), "sleep uid=65534")
            tally.add("ptrace", (True, "EPERM"), "sleep uid=65534")
        tally.add("proc_pid_root", (True, "EACCES"), "sleep uid=65534")
        tally.add("ptrace", (True, "EPERM"), "sleep uid=65534")
        probes = tally.summaries(DEFAULT_N)
        overhead = Overhead(DEFAULT_N, 0.01, 0.02, 0.005, 0.03, ("sudo",))
        evaluation = evaluate_host(
            n=DEFAULT_N,
            facts=_facts(sudo_n_true=True),
            backends=EvaluateHostInjectionTests._backends(self),
            measure_fn=lambda backend, n: overhead,
            probe_fn=lambda backend, n: probes,
        )
        self.assertIn("writer failed on 1/20", evaluation.errors["sudo"])
        self.assertEqual(evaluation.selected.name, "managed_login_folder")
        self.assertTrue(evaluation.selected.beta)


    def test_sudo_detection_writer_can_create_its_store_as_another_user(self) -> None:
        # r4507075865: the writer runs as the target user, so the detection
        # directory must let another uid create a file (1777, like the probe
        # store directory), not a caller-owned 0700 temp dir.
        import ast
        import re

        policy = _sudo_policy(_nobody_allows(*ALL_REQUIRED))
        modes: list[int] = []

        def run(cmd, timeout: float) -> ProcResult:
            key = tuple(cmd)
            if len(key) >= 3 and key[-3:-1] == ("python3", "-c"):
                match = re.search(r"p=('[^']*')", key[-1])
                directory = os.path.dirname(ast.literal_eval(match.group(1)))
                mode = stat.S_IMODE(os.stat(directory).st_mode)
                modes.append(mode)
                if mode & 0o1777 != 0o1777:  # another uid cannot create here
                    return ProcResult(1, "", "PermissionError: [Errno 13]")
            return policy(cmd, timeout)

        backends = detect_backends(
            facts=_facts(sudo_n_true=False),
            run_fn=run,
            helper_paths=("/tmp/csl734-no-such-helper",),
            env={},
        )
        sudo = next(b for b in backends if b.name == "sudo")
        self.assertEqual(len(modes), 1)
        self.assertTrue(sudo.available, sudo.reason)
        self.assertEqual(sudo.target_user, "nobody")

    def test_sudo_detection_ignores_a_precreated_probe_secret(self) -> None:
        # r4507085798 / r4507255229: a local user can plant probe.secret
        # once the detection directory is 1777. The earlier test planted
        # that name during sleep 8, before chmod, as the directory owner,
        # and the fake runner never created the target-user file, so it
        # only checked the changed filename. Plant after the directory is
        # world-writable, then create the writer file the same way
        # production does (store_writer_script). This process cannot
        # create a file as another uid without host or sudoers changes,
        # so this does not simulate a second uid.
        import re

        policy = _sudo_policy(_nobody_allows(*ALL_REQUIRED))
        planted: list[str] = []
        writer_paths: list[str] = []
        created_payloads: list[bytes] = []
        created_modes: list[int] = []
        sleep_modes: list[int] = []

        def run(cmd, timeout: float) -> ProcResult:
            key = tuple(cmd)
            if key[-2:] == ("sleep", "8"):
                tmp = tempfile.gettempdir()
                for name in os.listdir(tmp):
                    if name.startswith("csl734-sudo-probe-"):
                        directory = os.path.join(tmp, name)
                        sleep_modes.append(stat.S_IMODE(os.stat(directory).st_mode))
            if len(key) >= 3 and key[-3:-1] == ("python3", "-c"):
                match = re.search(r"p=('[^']*')", key[-1])
                path = ast.literal_eval(match.group(1))
                writer_paths.append(path)
                directory = os.path.dirname(path)
                mode = stat.S_IMODE(os.stat(directory).st_mode)
                if mode & 0o1777 != 0o1777:
                    return ProcResult(1, "", "detection directory is not world-writable")
                secret = os.path.join(directory, "probe.secret")
                fd = os.open(secret, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                planted.append(secret)
                exec(store_writer_script(path), {})
                with open(path, "rb") as fh:
                    created_payloads.append(fh.read())
                created_modes.append(stat.S_IMODE(os.stat(path).st_mode))
            return policy(cmd, timeout)

        backends = detect_backends(
            facts=_facts(sudo_n_true=False),
            run_fn=run,
            helper_paths=("/tmp/csl734-no-such-helper",),
            env={},
        )
        sudo = next(b for b in backends if b.name == "sudo")
        self.assertEqual(len(sleep_modes), 1, sleep_modes)
        self.assertNotEqual(sleep_modes[0] & 0o1777, 0o1777)
        self.assertEqual(len(planted), 1, planted)
        self.assertEqual(len(writer_paths), 1, writer_paths)
        self.assertNotEqual(writer_paths[0], planted[0])
        self.assertEqual(created_payloads, [b"store-bytes"])
        self.assertEqual(created_modes, [0o600])
        self.assertTrue(sudo.available, sudo.reason)
        self.assertEqual(sudo.target_user, "nobody")


@unittest.skipUnless(LIVE, f"live host probes run only with {LIVE_ENV}=1")
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
            self.assertEqual(probe.error_count, 0, f"{name} errored: {probe.detail}")
            self.assertEqual(
                probe.blocked_count,
                0,
                f"{name} should stay open under the same uid; detail={probe.detail}",
            )
            self.assertFalse(probe.blocked)
            self.assertIn(f"uid={os.getuid()}", probe.targets)

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
                self.assertEqual(probe.error_count, 0, f"{name} errored: {probe.detail}")
                # Every attempt probed the engine uid, never the launcher.
                self.assertEqual(
                    probe.targets.count("uid="), 1, f"{name} targets={probe.targets}"
                )
                self.assertIn(f"uid={backend.target_uid} x{DEFAULT_N}", probe.targets)
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
