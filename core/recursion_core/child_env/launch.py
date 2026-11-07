"""Child-launch boundary for engine children in the new core (CSL-785, candidate).

``spawn`` is the one place a new-core engine child is started. It takes only a
``ValidatedEnv`` from ``policy.build_child_env`` and always passes it to Popen as an
explicit ``env=``. Popen with ``env=None`` would inherit the parent environment, which
is the bypass the old transport has (``os.environ.copy()``); here that cannot happen.

The executable is resolved on the validated PATH (or the absolute argv[0]) and must pass
the same file rule as any other value, so a bare name never resolves through the parent's
PATH. Refusals raise before Popen is called.

Not wired into a running core: no daemon, run store or engine adapter calls this yet.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence

from recursion_core.child_env.policy import EnvRefused, ValidatedEnv, _unsafe_file

__all__ = ["spawn"]

# Keyword arguments a caller may not pass: each would replace or widen what the child gets.
_FORBIDDEN = frozenset({"env", "shell", "executable", "preexec_fn", "close_fds", "pass_fds", "user", "group", "extra_groups"})


def spawn(
    argv: Sequence[str],
    env: ValidatedEnv,
    *,
    cwd: str | None = None,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    **popen_kwargs: object,
) -> subprocess.Popen:
    """Start one engine child with exactly ``env``. Raises ``TypeError`` or ``EnvRefused`` before any spawn."""

    if type(env) is not ValidatedEnv:
        raise TypeError("spawn needs the ValidatedEnv build_child_env returned, not a plain mapping")
    if isinstance(argv, (str, bytes)) or not argv or not all(isinstance(item, str) for item in argv):
        raise EnvRefused(None, "argv_malformed")
    bad = sorted(_FORBIDDEN.intersection(popen_kwargs))
    if bad:
        raise EnvRefused(None, "launch_option_denied:" + ",".join(bad))
    executable = _resolve_executable(argv[0], env)
    child_env = env.to_dict()
    kwargs: dict[str, object] = dict(popen_kwargs)
    kwargs.update(env=child_env, cwd=cwd, close_fds=True, shell=False)
    if os.name != "nt":
        kwargs.setdefault("start_new_session", True)
    return popen([executable, *argv[1:]], **kwargs)


def _resolve_executable(name: str, env: ValidatedEnv) -> str:
    if os.path.isabs(name):
        located = name
    elif os.sep in name:
        raise EnvRefused(None, "argv0_relative")
    else:
        located = shutil.which(name, path=env.get("PATH", os.defpath))
        if located is None:
            raise EnvRefused(None, "argv0_not_found")
    cause = _unsafe_file(located)
    if cause is not None:
        raise EnvRefused(None, "argv0_" + cause)
    if not os.access(located, os.X_OK):
        raise EnvRefused(None, "argv0_not_executable")
    return located
