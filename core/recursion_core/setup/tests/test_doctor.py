"""The doctor report: a fake host, a probe that raises, and the paths that stay unread.

The report is the object at ``methods.v1.json#/$defs/doctor_report``. A probe that raises,
or that names a state outside ok, warn, fail and unknown, is reported unknown. Unknown is
never turned into ok. Nothing in a run opens a path ``deny.py`` names.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[4]
SCHEMA_DIR = REPO / "schemas" / "control" / "v1"
METHODS_PATH = SCHEMA_DIR / "methods.v1.json"
FIXTURE_PATH = SCHEMA_DIR / "fixtures" / "good" / "methods.v1" / "h15-doctor.json"

_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z$"
)
_STATES = {"ok", "warn", "fail", "unknown"}
_FIELDS = {"id", "subject", "state", "detail", "fix"}
_CANARY = "CANARY-DOCTOR-9f3a2c1b-SHOULD-NOT-APPEAR"
_OPENED: list = []
_HOOKED = False


def _remember_open(event, args):
    if event == "open" and args:
        try:
            _OPENED.append(args[0])
        except Exception:
            return


def _watch_opens():
    global _HOOKED
    if not _HOOKED:
        sys.addaudithook(_remember_open)
        _HOOKED = True


def _plain_report(report):
    """The doctor_report rules, with no jsonschema (the free-threaded build has none)."""

    if type(report) is not dict or set(report) != {"generated_at", "checks"}:
        raise AssertionError(f"report fields {sorted(report) if isinstance(report, dict) else type(report)}")
    stamp = report["generated_at"]
    if type(stamp) is not str or _TIMESTAMP.match(stamp) is None:
        raise AssertionError(f"generated_at {stamp!r}")
    try:
        dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise AssertionError(f"generated_at is not a calendar instant: {stamp}") from exc
    checks = report["checks"]
    if type(checks) is not list:
        raise AssertionError("checks is not a list")
    for check in checks:
        if type(check) is not dict or set(check) != _FIELDS:
            raise AssertionError(f"check fields {check!r}")
        if type(check["id"]) is not str or _ID.match(check["id"]) is None:
            raise AssertionError(f"id {check.get('id')!r}")
        if not (check["subject"] is None or type(check["subject"]) is str):
            raise AssertionError("subject")
        if check["state"] not in _STATES:
            raise AssertionError(f"state {check.get('state')!r}")
        if type(check["detail"]) is not str:
            raise AssertionError("detail")
        if not (check["fix"] is None or type(check["fix"]) is str):
            raise AssertionError("mend field")


def _validate(report):
    _plain_report(report)
    try:
        import jsonschema
    except ImportError:
        return  # free-threaded 3.14 does not install jsonschema; the plain check above is what runs there
    import warnings
    methods = json.loads(METHODS_PATH.read_text(encoding="utf-8"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        resolver = jsonschema.RefResolver(base_uri=METHODS_PATH.as_uri(), referrer=methods)
        jsonschema.validate(instance=report, schema=methods["$defs"]["doctor_report"], resolver=resolver)


def _body(state, detail, fix=None, subject=None):
    return {"state": state, "detail": detail, "fix": fix, "subject": subject}


def _fake(**overrides):
    probes = {
        "platform.linux": lambda: _body("ok", "Linux"),
        "interpreter.free_threaded": lambda: _body("ok", "CPython 3.14, GIL off"),
        "systemd.user_manager": lambda: _body("ok", "running"),
        "systemd.linger": lambda: _body("ok", "Linger=yes"),
        "daemon.unit": lambda: _body("fail", "unit is not there", "run recursiond install"),
        "daemon.socket": lambda: _body("fail", "socket is not there", "the daemon is not running"),
        "store.ledger": lambda: _body("unknown", "store was not checked"),
        "store.breaker": lambda: _body("ok", "breaker store"),
        "engine.binary": lambda: [
            _body("unknown", "binary not found on PATH",
                  "install the engine the operator configured; Recursion installs no engine", "codex"),
            _body("ok", "claude 0.0.0", None, "claude_code"),
        ],
    }
    probes.update(overrides)
    return probes


def _check(report, check_id, subject=None):
    found = [row for row in report["checks"] if row["id"] == check_id and row["subject"] == subject]
    if len(found) != 1:
        raise AssertionError(f"{check_id} subject {subject!r}: {found!r}")
    return found[0]


class DoctorReportTests(unittest.TestCase):
    def test_a_report_from_a_fake_host_matches_the_schema(self):
        # The module is absent on the base: importing it is the red this entry names.
        import recursion_core.setup.doctor as doctor

        with mock.patch("subprocess.run") as spawned, mock.patch("os.lstat") as lstat:
            report = doctor.run(_fake())
        spawned.assert_not_called()
        lstat.assert_not_called()
        _validate(report)
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        sample = fixture["answer"]["report"]["checks"][0]
        self.assertEqual(set(sample), _FIELDS)
        for check in report["checks"]:
            self.assertEqual(set(check), set(sample))
        self.assertEqual(_check(report, "engine.binary", "codex")["state"], "unknown")
        self.assertEqual(_check(report, "daemon.socket")["state"], "fail")
        self.assertNotEqual(_check(report, "store.ledger")["state"], "ok")

    def test_a_probe_that_raises_is_unknown_and_never_ok(self):
        import recursion_core.setup.doctor as doctor

        def boom():
            raise RuntimeError("probe failed")

        report = doctor.run(_fake(**{"systemd.linger": boom}))
        check = _check(report, "systemd.linger")
        self.assertEqual(check["state"], "unknown")
        self.assertNotEqual(check["state"], "ok")
        self.assertIsNone(check["subject"])
        self.assertIn("RuntimeError", check["detail"])
        _validate(report)

    def test_an_unlisted_state_is_unknown_and_never_ok(self):
        import recursion_core.setup.doctor as doctor

        report = doctor.run(_fake(**{"store.ledger": lambda: _body("passed", "looks fine")}))
        self.assertEqual(_check(report, "store.ledger")["state"], "unknown")

    def test_unknown_stays_unknown(self):
        import recursion_core.setup.doctor as doctor

        report = doctor.run(_fake(**{"store.breaker": lambda: _body("unknown", "could not be read")}))
        self.assertEqual(_check(report, "store.breaker")["state"], "unknown")

    def test_interruption_propagates_and_a_second_caller_is_separate(self):
        import recursion_core.setup.doctor as doctor

        def stop():
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            doctor.run(_fake(**{"daemon.socket": stop}))
        first = doctor.run(_fake())
        second = doctor.run(_fake())
        first["checks"][0]["detail"] = "changed by the first caller"
        self.assertNotEqual(second["checks"][0]["detail"], "changed by the first caller")
        self.assertEqual(
            [(row["id"], row["subject"], row["state"]) for row in first["checks"]],
            [(row["id"], row["subject"], row["state"]) for row in second["checks"]],
        )

    def test_exit_code_is_1_only_when_a_check_is_fail(self):
        import recursion_core.setup.doctor as doctor

        self.assertEqual(doctor.exit_code(doctor.run(_fake())), 1)
        clear = _fake(**{
            "daemon.unit": lambda: _body("ok", "unit"),
            "daemon.socket": lambda: _body("warn", "not confirmed"),
        })
        report = doctor.run(clear)
        self.assertTrue(all(row["state"] != "fail" for row in report["checks"]))
        self.assertEqual(doctor.exit_code(report), 0)
        text = doctor.text(report)
        self.assertIn("daemon.socket", text)
        self.assertIn("warn", text)
        self.assertFalse(text.lstrip().startswith("{"))

    def test_denied_paths_include_the_seeds(self):
        import recursion_core.setup.deny as deny
        from recursion_core.daemon.engine_user import SIGNIN_STORES
        from recursion_core.subscription_routes.policy import ENGINE_STORE_ENTRIES

        home = "/home/operator"
        paths = set(deny.denied_paths(home))
        self.assertIn(os.path.join(home, ".claude", ".credentials.json"), paths)
        for entry in ENGINE_STORE_ENTRIES:
            self.assertIn(os.path.abspath(os.path.join(home, entry)), paths, entry)
        for row in SIGNIN_STORES.values():
            store = row.get("store_path") or ""
            if store.startswith("~/"):
                self.assertIn(os.path.join(home, store[2:]), paths, store)
            elif store.startswith("unconfirmed"):
                self.assertFalse(any(store in path for path in paths))
        self.assertTrue(deny.is_denied(os.path.join(home, ".pi", "agent", "auth.json"), home))
        self.assertFalse(deny.is_denied(os.path.join(home, ".config", "systemd", "user", "recursion-daemon.service"), home))
        self.assertFalse(deny.is_denied(os.path.join(home + "-other", ".pi"), home))

    def test_a_live_run_opens_no_denied_path(self):
        import recursion_core.setup.deny as deny
        import recursion_core.setup.doctor as doctor

        _watch_opens()
        with tempfile.TemporaryDirectory() as home:
            os.chmod(home, 0o700)
            for path in deny.denied_paths(home):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                leaf = os.path.basename(path)
                if leaf.startswith(".") or path.endswith(".json"):
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(_CANARY)
                else:
                    os.makedirs(path, exist_ok=True)
                    with open(os.path.join(path, "secret.txt"), "w", encoding="utf-8") as handle:
                        handle.write(_CANARY)
            runtime = os.path.join(home, "run")
            os.makedirs(runtime, 0o700)
            env = {
                "HOME": home,
                "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
                "XDG_RUNTIME_DIR": runtime,
                "PATH": os.environ.get("PATH", ""),
            }
            before = len(_OPENED)
            with mock.patch.dict(os.environ, env, clear=False):
                report = doctor.run(None)
            for raw in _OPENED[before:]:
                self.assertFalse(deny.is_denied(raw, home), raw)
            self.assertNotIn(_CANARY, json.dumps(report))
            _validate(report)
            socket = _check(report, "daemon.socket")
            self.assertIn(socket["state"], ("fail", "unknown"))
            self.assertNotEqual(socket["state"], "ok")


if __name__ == "__main__":
    unittest.main()
