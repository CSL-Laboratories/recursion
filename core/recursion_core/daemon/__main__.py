"""User-service composition root: lifecycle, sweep, control socket and loopback GUI.

With no injected ``client``/``door`` the daemon composes its own control adapter
(``control_adapter.compose_control``): the control API over the durable registry, served
on the owner-only Unix socket under ``$XDG_RUNTIME_DIR/recursion/``, with the same object
behind the loopback GUI. An injected pair replaces that adapter (tests, embedding).
"""
import argparse
import importlib
import getpass
import logging
import os
from pathlib import Path
import signal
import threading
from . import (Daemon, Registry, SystemdUserManager, UserLock, check_operator_linger,
               refuse_engine_user, require_free_threaded, require_posix)

from .control_adapter import compose_control
from .gui import LoopbackGUI
from .launcher import EngineUnitManager

log = logging.getLogger('recursion_core.daemon')


def main(*, client=None, door=None, plugins=(), port=8765, sweep_seconds=5):
    require_posix()  # Windows: typed PlatformUnsupported before any lock, state, socket or GUI
    require_free_threaded()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    refuse_engine_user(os.environ)
    check_operator_linger(getpass.getuser())
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}')
    state = Path(os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')) / 'recursion'
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    with UserLock(runtime):
        injected = client is not None or door is not None
        daemon = Daemon(Registry(state), SystemdUserManager() if injected else EngineUnitManager())
        gui = composed = None
        try:
            daemon.sweep()
            if not injected:
                # After the startup sweep: recovered runs are re-observed before any surface can ask.
                composed = compose_control(daemon, state)
                client, door = composed.client, composed.door
                composed.control.reconcile()
                log.info('control API on %s', composed.server.path)
            gui = _start_gui(client, door, plugins, port, announced=False)
            announced = gui is None
            while not stopped.wait(sweep_seconds):
                require_free_threaded()
                daemon.sweep()
                if composed is not None:
                    composed.control.reconcile()
                if gui is None:
                    gui = _start_gui(client, door, plugins, port, announced=announced)
                    announced = gui is None
        finally:
            try:
                if gui is not None:
                    gui.__exit__(None, None, None)
            finally:
                try:
                    if composed is not None:
                        composed.close()
                finally:
                    daemon.close()  # Deliberately do not stop independent run services.


def _start_gui(client, door, plugins, port, *, announced):
    """Bind the loopback GUI, or return None so reconciliation keeps running.

    Only a bind failure (OSError, e.g. another listener on the port) is tolerated
    and retried on the next sweep; a bad plugin registry still raises.
    """
    gui = LoopbackGUI(client, door, plugins=plugins, port=port)
    try:
        gui.__enter__()
    except OSError as exc:
        if not announced:
            log.warning('GUI unavailable: cannot bind 127.0.0.1:%d (%s); '
                        'reconciliation continues and the bind is retried every sweep', port, exc)
        return None
    if announced:
        log.info('GUI available again on 127.0.0.1:%d', port)
    return gui


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui-port", type=int, default=8765)
    parser.add_argument("--sweep-seconds", type=float, default=None,
                        help="seconds between registry sweeps (default 5)")
    parser.add_argument("--plugin", action="append", default=[], metavar="MODULE:REGISTRY",
                        help="explicit trusted plugin registry factory, e.g. pkg.module:registry")
    args = parser.parse_args()
    extra = {}
    if args.sweep_seconds is not None:
        if not args.sweep_seconds > 0:
            parser.error("--sweep-seconds must be positive")
        extra["sweep_seconds"] = args.sweep_seconds
    require_free_threaded()
    plugins = []
    for value in args.plugin:
        module, sep, name = value.partition(":")
        if not sep or not module or not name.isidentifier():
            parser.error("plugin must name MODULE:REGISTRY")
        factory = getattr(importlib.import_module(module), name)
        require_free_threaded()  # a plugin import may enable the GIL
        plugins.append(factory())
        require_free_threaded()
    main(plugins=plugins, port=args.gui_port, **extra)


if __name__ == '__main__':
    cli()
