"""``recursiond up``: start the per-user unit and wait for its control socket.

The systemctl runner is injected. The default runner is ``systemctl`` with one of the two
argument lists below and no others; a test passes its own runner, and this module does not
have to be the process that starts the user manager. ``up`` never reports ``ok`` unless the
socket accepts a connection within the bound. A doctor check in state ``fail`` refuses
before any call, except ``daemon.socket``: that check is fail until the unit is listening,
which is the condition the wait exists for. Unknown is not a fail.

Success prints ``{"code": "ok", "gui": "http://127.0.0.1:8765"}``. ``8765`` is the daemon's
default ``--gui-port`` (``daemon/__main__.py``) on ``127.0.0.1``. How a GUI token reaches a
fresh user is not decided here, so the address is the URL only.
"""

from __future__ import annotations

import subprocess
import sys
import time

from recursion_core.daemon_install import UNIT_NAME

EXIT_OK, EXIT_REFUSED = 0, 1
# The daemon's default loopback GUI (daemon/__main__.py ``--gui-port``, gui.LoopbackGUI).
GUI = "http://127.0.0.1:8765"
SOCKET_BOUND_S = 5.0
RELOAD = ("--user", "daemon-reload")
ENABLE = ("--user", "enable", "--now", UNIT_NAME)
_COMMANDS = (RELOAD, ENABLE)
# ``daemon.socket`` is fail while nothing is listening. ``up`` is what makes it listen.
_SOCKET_CHECK = "daemon.socket"


def command(*, out=None, runner=None, report=None, bound_s=SOCKET_BOUND_S, socket_wait=None,
            environ=None) -> int:
    """Print one JSON object and return 0 only when the socket answered.

    ``runner`` receives each argument list in ``_COMMANDS`` and returns 0 (or None) on
    success. ``report`` is a doctor report; None runs ``doctor.run(None)``. ``socket_wait``
    is ``wait_for_socket`` unless a test passes its own. ``KeyboardInterrupt`` and
    ``SystemExit`` propagate. A second call does the same work again and returns its own
    answer.
    """

    out = sys.stdout if out is None else out
    if not sys.platform.startswith("linux"):
        _emit(out, {"code": "platform_unsupported",
                    "detail": f"recursiond up runs on Linux (a systemd user service); this system is {sys.platform}"})
        return EXIT_REFUSED
    runner = run_systemctl if runner is None else runner
    socket_wait = wait_for_socket if socket_wait is None else socket_wait
    environ = _environ(environ)
    if report is None:
        import recursion_core.setup.doctor as doctor
        report = doctor.run(None)
    failed = blocking_fails(report)
    if failed is None:
        _emit(out, {"code": "doctor_failed", "detail": "doctor returned no report"})
        return EXIT_REFUSED
    if failed:
        _emit(out, {"code": "doctor_failed", "detail": "doctor has a fail", "failed": failed})
        return EXIT_REFUSED
    for args in _COMMANDS:
        try:
            result = runner(list(args))
        except (KeyboardInterrupt, SystemExit):
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            _emit(out, {"code": "systemctl_failed", "command": list(args),
                        "detail": f"{type(exc).__name__}: {exc}"})
            return EXIT_REFUSED
        if result not in (None, 0):
            _emit(out, {"code": "systemctl_failed", "command": list(args),
                        "detail": f"systemctl exited {result}"})
            return EXIT_REFUSED
    try:
        path = socket_path(environ)
        answered = bool(socket_wait(path, bound_s))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # naming the socket, or the wait itself, did not answer
        _emit(out, {"code": "unconfirmed", "detail": f"{type(exc).__name__}: {exc}"})
        return EXIT_REFUSED
    if not answered:
        _emit(out, {"code": "unconfirmed", "socket": path,
                    "detail": f"the control socket did not answer within {bound_s:g} seconds"})
        return EXIT_REFUSED
    _emit(out, {"code": "ok", "gui": GUI})
    return EXIT_OK


def blocking_fails(report):
    """Check ids in state ``fail``, other than ``daemon.socket``. None when ``report`` has no check list."""

    if type(report) is not dict:
        return None
    checks = report.get("checks")
    if type(checks) is not list:
        return None
    failed = []
    for check in checks:
        if type(check) is not dict:
            return None
        if check.get("state") == "fail" and check.get("id") != _SOCKET_CHECK:
            failed.append(check.get("id"))
    return failed


def run_systemctl(args) -> int:
    """Run ``systemctl`` with one of the two lists ``up`` uses. Any other list is refused."""

    argv = tuple(args)
    if argv not in _COMMANDS:
        raise ValueError("up runs only daemon-reload and enable --now")
    done = subprocess.run(["systemctl", *argv], check=False, capture_output=True, text=True, timeout=60)
    return int(done.returncode)


def socket_path(environ=None) -> str:
    """The daemon's control socket (``control.client.socket_path``)."""

    from recursion_core.control.client import socket_path as control_socket_path

    return control_socket_path(_environ(environ))


def wait_for_socket(path: str, bound_s: float) -> bool:
    """True when a connection to ``path`` is accepted before ``bound_s`` seconds have passed."""

    import socket

    deadline = time.monotonic() + (bound_s if bound_s > 0 else 0.0)
    while True:
        if _try_connect(socket, path):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _try_connect(socket_module, path: str) -> bool:
    sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    try:
        sock.settimeout(0.2)
        sock.connect(path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _environ(environ):
    import os
    return os.environ if environ is None else environ


def _emit(out, answer: dict) -> None:
    import json
    print(json.dumps(answer, sort_keys=True), file=out)
