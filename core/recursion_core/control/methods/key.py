"""``key.list`` (seam S1): the saved keys of the user the daemon runs as, never a value.

A read (no ``command_id``, no receipt, no service). Its published answer (``schemas/control/v1/methods.v1.json``,
``answer.key.list``) is ``{"code": "ok", "keys": [...]}``, each key exactly ``key_id``, ``provider``, ``env_name``,
``created_at`` and ``store``; or a refusal with ``request_invalid``, ``scope_refused``, ``unavailable`` or
``unsupported``. The spend opt-in and cap are not in the published answer (lane KEYS asked SPINE whether they
should be; until then they stay out, and ``keys.store.spend_policy`` is how lane QUOTA reads them).

The handler reads the key store's index only: no backend is asked, so no keychain prompts and no value is ever
in memory for it. A store it cannot read is ``unavailable`` with the store's code in ``detail``.
"""
from __future__ import annotations

from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.store import default_store

ANSWER_FIELDS = ('key_id', 'provider', 'env_name', 'created_at', 'store')


def key_list(ctx, params) -> dict:
    if not isinstance(params, dict):
        return {'code': 'request_invalid', 'detail': 'params'}
    if params:
        return {'code': 'request_invalid', 'detail': min(params)}
    try:
        index = default_store().list()
    except KeyStoreError as exc:
        return {'code': 'unavailable', 'detail': exc.code}
    records = [index.keys[key_id] for key_id in sorted(index.keys)]
    return {'code': 'ok', 'keys': [{field: getattr(record, field) for field in ANSWER_FIELDS} for record in records]}
