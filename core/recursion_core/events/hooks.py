"""Lane EVENTS' registrant: the module composition imports.

``register(hooks)`` sets the event store's factory and nothing else. It opens
nothing, starts no thread and reads no state. Session, approval and workspace
methods stay with their own lanes: this module does not register their
handlers. The SESSIONS registrant registers nothing on purpose and is not
touched here.

The change reader is not in this tree. When that module is shipped, this
``register`` is the place that brings its service in, and that call must not
set ``event_store`` again: a second registration of one hook fails composition.
"""

from __future__ import annotations

import os

from recursion_core.events.db import EventStore, StoreError

STORE_FILE = 'events.sqlite3'


def open_store(state_dir) -> EventStore:
    """``factory(state_dir) ->`` the store in that directory.

    A state directory that is not a folder is ``StoreError('state_dir_not_a_folder')``.
    Anything the helper refuses (a damaged file, a second opener) is its own error,
    unchanged. An interruption is not turned into another error.
    """
    try:
        folder = os.fspath(state_dir)
    except TypeError:
        raise StoreError('state_dir_not_a_folder') from None
    if not isinstance(folder, str) or not os.path.isdir(folder):
        raise StoreError('state_dir_not_a_folder')
    return EventStore(os.path.join(folder, STORE_FILE))


def register(hooks) -> None:
    """Set the event store's factory. Called once by ``hooks.compose()``."""
    if not callable(getattr(hooks, 'set', None)):
        raise TypeError('hooks')
    hooks.set('event_store', open_store)
