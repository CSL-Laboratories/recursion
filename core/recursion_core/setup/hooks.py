"""The setup lane's registrant (seam S1).

``register(hooks)`` adds the doctor handler and nothing else. It opens no file,
starts no thread and reads no state. The handler runs only when a caller
dispatches ``doctor``. The running daemon loads this module only after #370
composes the registrant; until then a direct ``compose()`` is what reaches it.
"""

from __future__ import annotations


def register(hooks) -> None:
    """Add the doctor handler. This call opens nothing."""

    hooks.methods.register("doctor", _doctor)


def _doctor(_ctx, params):
    if type(params) is not dict or len(params) != 0:
        return {"code": "request_invalid", "detail": "params"}
    import recursion_core.setup.doctor as doctor

    return {"code": "ok", "report": doctor.run(None)}
