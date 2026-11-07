"""User-service entry point. Control API attaches to Daemon separately."""
import getpass
import logging
import os
from pathlib import Path
import pwd
import signal
import threading
from . import Daemon, Registry, SystemdUserManager, UserLock, check_linger, require_free_threaded

log = logging.getLogger('recursion_core.daemon')


def linger_users(environ=os.environ, lookup=pwd.getpwnam):
    """The operator, plus the engine user when one is configured and present on this host."""
    users = [getpass.getuser()]
    engine = environ.get('RECURSION_ENGINE_USER', '').strip()
    if engine:
        try:
            lookup(engine)
            users.append(engine)
        except KeyError:
            log.warning('engine user %s not present; its Linger is not checked', engine)
    return users


def main():
    require_free_threaded()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    check_linger(linger_users())
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}')
    state = Path(os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')) / 'recursion'
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    with UserLock(runtime):
        daemon = Daemon(Registry(state), SystemdUserManager())
        try:
            daemon.sweep()
            while not stopped.wait(5):
                require_free_threaded()
                daemon.sweep()
        finally:
            daemon.close()  # Deliberately do not stop independent run services.


if __name__ == '__main__':
    main()
