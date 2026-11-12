"""User-service composition root: lifecycle, loopback GUI and injected control adapter."""
import argparse
import importlib
import getpass
import logging
import os
from pathlib import Path
import signal
import threading
from . import (Daemon, Registry, SystemdUserManager, UserLock, check_operator_linger,
               refuse_engine_user, require_free_threaded)

from .gui import LoopbackGUI

log = logging.getLogger('recursion_core.daemon')


def main(*, client=None, door=None, plugins=(), port=8765):
    require_free_threaded()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    refuse_engine_user(os.environ)
    check_operator_linger(getpass.getuser())
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}')
    state = Path(os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')) / 'recursion'
    stopped = threading.Event()
    previous_handlers = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, lambda *_: stopped.set())
        with UserLock(runtime):
            daemon = Daemon(Registry(state), SystemdUserManager())
            gui = None
            try:
                daemon.sweep()
                gui = _start_gui(client, door, plugins, port, announced=False)
                announced = gui is None
                while not stopped.wait(5):
                    require_free_threaded()
                    daemon.sweep()
                    if gui is None:
                        gui = _start_gui(client, door, plugins, port, announced=announced)
                        announced = gui is None
            finally:
                try:
                    if gui is not None:
                        gui.__exit__(None, None, None)
                finally:
                    daemon.close()  # Deliberately do not stop independent run services.
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


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
    parser.add_argument("--plugin", action="append", default=[], metavar="MODULE:REGISTRY",
                        help="explicit trusted plugin registry factory, e.g. cockpit.gui.panels:registry")
    args = parser.parse_args()
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
    main(plugins=plugins, port=args.gui_port)


if __name__ == '__main__':
    cli()
