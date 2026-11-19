"""The doctor report: a fake host, a probe that raises, and the paths that stay unread.

The report is the object at ``methods.v1.json#/$defs/doctor_report``. A probe that raises,
or that names a state outside ok, warn, fail and unknown, is reported unknown. Unknown is
never turned into ok. Nothing in a run opens a path ``deny.py`` names.
"""

from __future__ import annotations

import contextlib
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
# A real Windows host: its path module is ntpath. The simulated-Windows step keeps this host's POSIX
# paths and file modes (only os.name and sys.platform say Windows there), so it takes the POSIX branch.
_WINDOWS_HOST = os.sep == "\\"

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


def _local_schema(uri):
    """A ``file:`` schema URI read from disk, and only from the schema folder.

    The schemas sit beside ``methods.v1.json``. Left to urllib, a ``file:`` URI goes through
    an opener whose https handler loads the host's certificate store first; under the
    simulated-Windows step that is Windows' store, which this host's ssl module does not
    have (``enum_certificates``). Reading the file keeps the full schema check there too.

    On a Windows host the URI's path becomes a drive path through ``url2pathname``. Elsewhere
    it is unquoted as before: under the simulated-Windows step ``url2pathname`` reads
    ``os.name`` and would turn this host's POSIX path into a backslash one. A URI that names a
    host, or a path that lands outside the schema folder, is refused before anything is read.
    """

    from urllib.parse import unquote, urlsplit
    from urllib.request import url2pathname

    parts = urlsplit(uri)
    if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
        raise ValueError(f"schema reference is not a local file: {uri}")
    local = url2pathname(parts.path) if _WINDOWS_HOST else unquote(parts.path)
    path = Path(os.path.realpath(local))
    folder = Path(os.path.realpath(SCHEMA_DIR))
    if folder not in path.parents:
        raise ValueError(f"schema reference outside the schema folder: {uri}")
    return json.loads(path.read_text(encoding="utf-8"))


def _remote_schema(uri):
    """An ``http`` or ``https`` schema reference: refused, never fetched."""

    raise ValueError(f"remote schema reference refused, not fetched: {uri}")


_SCHEMA_HANDLERS = {"file": _local_schema, "http": _remote_schema, "https": _remote_schema}


def _resolver(methods):
    """A resolver for ``methods.v1.json`` that reads only the schema folder (needs jsonschema)."""

    import warnings

    import jsonschema

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return jsonschema.RefResolver(base_uri=METHODS_PATH.as_uri(), referrer=methods,
                                      handlers=dict(_SCHEMA_HANDLERS))


def _validate(report):
    _plain_report(report)
    try:
        import jsonschema
    except ImportError:
        return  # free-threaded 3.14 does not install jsonschema; the plain check above is what runs there
    import warnings
    methods = json.loads(METHODS_PATH.read_text(encoding="utf-8"))
    resolver = _resolver(methods)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
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


@contextlib.contextmanager
def account_untouched(asked):
    """Record, and refuse, each way a doctor run asks this account.

    The unit probe asks the password database for the home, the linger probe asks
    loginctl and the user-manager probe asks systemctl. A run given a root made by the
    test asks none of them. Each such call is appended to ``asked`` and raises.

    What it covers: ``subprocess.run`` and ``subprocess.Popen`` (so ``check_output``,
    ``call`` and a direct ``Popen`` too) given loginctl or systemctl as the first item of
    an argument list; ``recursion_core.daemon.check_operator_linger``; ``pwd.getpwuid``
    where ``pwd`` imports. What it does not see: a command given as one string,
    ``os.system``, ``os.posix_spawn``, ``pwd.getpwnam`` and ``getpass``. The doctor's probes
    call none of those today; a probe that starts to needs a guard here first.
    """

    import subprocess

    import recursion_core.daemon as daemon_module

    real_subprocess_run = subprocess.run
    real_popen = subprocess.Popen

    def refuse(name):
        def call(*_args, **_kwargs):
            asked.append(name)
            raise AssertionError(f"{name} was called")
        return call

    def account_command(args, kwargs):
        command = args[0] if args else kwargs.get("args", ())
        if isinstance(command, (list, tuple)) and command:
            name = os.path.basename(str(command[0]))
            if name in ("loginctl", "systemctl"):
                return name
        return None

    def guarded(*args, **kwargs):
        name = account_command(args, kwargs)
        if name is not None:
            return refuse(name)()
        return real_subprocess_run(*args, **kwargs)

    class GuardedPopen(real_popen):
        def __init__(self, *args, **kwargs):
            name = account_command(args, kwargs)
            if name is not None:
                refuse(name)()
            super().__init__(*args, **kwargs)

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch("subprocess.run", guarded))
        stack.enter_context(mock.patch("subprocess.Popen", GuardedPopen))
        stack.enter_context(mock.patch.object(daemon_module, "check_operator_linger", refuse("check_operator_linger")))
        try:
            import pwd
        except ImportError:
            pwd = None
        if pwd is not None:
            stack.enter_context(mock.patch.object(pwd, "getpwuid", refuse("pwd.getpwuid")))
        yield asked


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
        if _WINDOWS_HOST:
            # deny.denied_paths places every path under os.path.abspath(home); on Windows that
            # path starts with the current drive and uses backslashes.
            home = os.path.abspath(home)
        paths = set(deny.denied_paths(home))
        self.assertIn(os.path.join(home, ".claude", ".credentials.json"), paths)
        for entry in ENGINE_STORE_ENTRIES:
            self.assertIn(os.path.abspath(os.path.join(home, entry)), paths, entry)
        for row in SIGNIN_STORES.values():
            store = row.get("store_path") or ""
            if store.startswith("~/"):
                if _WINDOWS_HOST:
                    # "~/.codex/auth.json" keeps its slashes in a join; the product gives the absolute path.
                    self.assertIn(os.path.abspath(os.path.join(home, store[2:])), paths, store)
                else:
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
            scratch = os.path.join(home, "tmp")
            os.makedirs(scratch, 0o700)
            empty_path = os.path.join(home, "bin")  # no engine binary is found, so none is run
            os.makedirs(empty_path, 0o700)
            env = {
                "HOME": home,
                "XDG_CONFIG_HOME": os.path.join(home, ".config"),
                "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
                "XDG_RUNTIME_DIR": runtime,
                "PATH": empty_path,
                "TMPDIR": scratch,
            }
            before = len(_OPENED)
            asked = []
            # The root is the home this test made: the unit, linger and user-manager
            # probes read it and do not ask this account.
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(tempfile, "tempdir", scratch), account_untouched(asked):
                report = doctor.run(None, root=home)
            self.assertEqual(asked, [])
            for raw in _OPENED[before:]:
                self.assertFalse(deny.is_denied(raw, home), raw)
            self.assertNotIn(_CANARY, json.dumps(report))
            _validate(report)
            socket = _check(report, "daemon.socket")
            self.assertIn(socket["state"], ("fail", "unknown"))
            self.assertNotEqual(socket["state"], "ok")



class SchemaLoaderTests(unittest.TestCase):
    """The schema loader reads only the schema folder and fetches nothing."""

    def test_a_file_uri_of_this_host_is_read_from_the_schema_folder(self):
        target = SCHEMA_DIR / "common.v1.json"
        uri = target.as_uri()
        if _WINDOWS_HOST:
            # The shape a Windows host gives: a drive letter after the third slash.
            self.assertRegex(uri, r"^file:///[A-Za-z]:/")
        self.assertEqual(_local_schema(uri), json.loads(target.read_text(encoding="utf-8")))

    def test_a_reference_leaving_the_schema_folder_is_refused_and_not_read(self):
        outside = Path(os.path.realpath(__file__))
        for uri in (outside.as_uri(),
                    SCHEMA_DIR.as_uri() + "/../../../" + outside.relative_to(REPO).as_posix(),
                    SCHEMA_DIR.parent.as_uri() + "/v1-other/common.v1.json"):
            with self.subTest(uri=uri), mock.patch.object(Path, "read_text") as read:
                with self.assertRaisesRegex(ValueError, "outside"):
                    _local_schema(uri)
                read.assert_not_called()

    def test_an_http_or_https_reference_is_refused_and_not_fetched(self):
        with mock.patch("urllib.request.urlopen") as fetched:
            for uri in ("http://example.invalid/common.v1.json", "https://example.invalid/common.v1.json"):
                with self.subTest(uri=uri):
                    with self.assertRaisesRegex(ValueError, "refused"):
                        _SCHEMA_HANDLERS[uri.split(":", 1)[0]](uri)
                    with self.assertRaisesRegex(ValueError, "not a local file"):
                        _local_schema(uri)
            try:
                import jsonschema
            except ImportError:
                fetched.assert_not_called()
                return  # no jsonschema on this interpreter: the handlers above are what _validate gives it
            resolver = _resolver(json.loads(METHODS_PATH.read_text(encoding="utf-8")))
            import warnings
            with self.assertRaises(Exception) as caught, warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                jsonschema.validate(instance={}, schema={"$ref": "https://example.invalid/common.v1.json"},
                                    resolver=resolver)
        fetched.assert_not_called()
        self.assertIn("refused", f"{caught.exception} {caught.exception.__cause__}")


if __name__ == "__main__":
    unittest.main()
