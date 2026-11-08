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
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    with UserLock(runtime):
        daemon = Daemon(Registry(state), SystemdUserManager())
        try:
            daemon.sweep()
            with LoopbackGUI(client, door, plugins=plugins, port=port):
                while not stopped.wait(5):
                    require_free_threaded()
                    daemon.sweep()
        finally:
            daemon.close()  # Deliberately do not stop independent run services.


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui-port", type=int, default=8765)
    parser.add_argument("--plugin", action="append", default=[], metavar="MODULE:REGISTRY",
                        help="explicit trusted plugin registry factory, e.g. cockpit.gui:registry")
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
