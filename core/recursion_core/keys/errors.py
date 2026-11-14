"""The key store's refusals: a typed code and a detail, and never a key's value.

Two kinds:

``KeyStoreError(code, detail)``
    What the store, its backends and its commands raise. ``code`` is a word from ``CODES``; ``detail``
    says more in words a user can act on. Neither ever carries a stored value: a value is only ever
    held in a local variable on its way into or out of a backend.

``KeyRefused(connection_id, code)``
    What ``credentials()`` raises when it KNOWS the id and refuses to hand the key out (the keychain is
    locked, the pinned backend changed, the key has no spend opt-in). It is a ``KeyError`` whose only
    argument is the connection id, so every caller that treats ``KeyError`` as "no credential" (seam
    S7's wrapper, ``RouteAdapter.credential``) refuses the launch exactly as it does for an unknown id,
    and the typed code rides beside it for a reader that wants it. An unknown id, or a known id asked
    for under another variable name, is a plain ``KeyError(connection_id)``, as S7's default raises.
"""
from __future__ import annotations

CODES = (
    # what a command was given
    'id_invalid', 'provider_invalid', 'env_name_invalid', 'value_invalid', 'cap_invalid',
    'value_from_argv', 'no_terminal', 'stdin_is_terminal', 'key_exists', 'key_unknown', 'not_confirmed',
    # the owner-only folder and its files
    'folder_invalid', 'file_invalid', 'index_invalid', 'owner_file_invalid', 'store_failed',
    'platform_unsupported',
    # the backend, its pin and the keychain
    'backend_changed', 'backend_not_built', 'keychain_unavailable', 'keychain_locked', 'keychain_failed',
    'keychain_timeout', 'keychain_tool_missing', 'keychain_undetermined', 'secret_missing',
    # spend
    'spend_not_allowed',
)


class KeyStoreError(Exception):
    """A refusal with a typed code. Its text is the code and the detail; it never carries a value."""

    def __init__(self, code: str, detail: str = '') -> None:
        if code not in CODES:
            raise ValueError(f'unknown key store code {code!r}')
        super().__init__(code, detail)
        self.code, self.detail = code, detail

    def __str__(self) -> str:
        return f'{self.code}: {self.detail}' if self.detail else self.code


class KeyRefused(KeyError):
    """A known connection whose key the store will not hand out. A ``KeyError(connection_id)`` with a code."""

    def __init__(self, connection_id: str, code: str) -> None:
        if code not in CODES:
            raise ValueError(f'unknown key store code {code!r}')
        super().__init__(connection_id)
        self.code = code
