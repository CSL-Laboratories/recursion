"""User-entered model-provider API keys and the credential source for API-key routes (lane KEYS). See README.md.

The public entry for other lanes::

    from recursion_core.keys import credentials
    credentials(connection_id: str, env_name: str) -> str     # raises KeyError(connection_id), or KeyRefused

Importing this package reads no store and creates nothing; the user's store is opened when ``credentials`` is
first called.
"""
from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.store import credentials

__all__ = ('KeyRefused', 'KeyStoreError', 'credentials')
