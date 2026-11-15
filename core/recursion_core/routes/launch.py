"""Launch boundary for route adapters (CSL-797, candidate). See README.md.

Adapters never start processes themselves. They call an injected ``launcher(argv, *, env, shell=False)``
that returns an ``EngineProcess``. The real launcher (not in this module) places the child in its own
cgroup v2 leaf and holds a pidfd; ``EngineProcess.fence()`` asks that cleanup layer to end the run's
whole tree. Nothing here uses ``PR_SET_PDEATHSIG`` or ``preexec_fn``. Tests use fake doubles only.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from typing import Protocol


class ReadTimeout(Exception):
    """``EngineProcess.read_line`` waited ``timeout_s`` and got no line."""


class EngineProcess(Protocol):
    def send(self, line: str) -> None: ...  # write one JSONL line to the child's stdin

    def close_stdin(self) -> None: ...

    def read_line(self, timeout_s: float) -> str | None: ...  # one stdout line, None at EOF, ReadTimeout

    def fence(self, reason: str) -> None: ...  # ask the cgroup+pidfd cleanup layer to end this run's tree


Launcher = Callable[..., EngineProcess]


class GilBuildRefused(RuntimeError):
    code = "gil_build_refused"


def require_free_threaded(is_gil_enabled: Callable[[], bool] | None = None) -> None:
    """Refuse to run on a GIL build (CSL-706: the core runs on free-threaded CPython 3.14t, ``-X gil=0``).

    The daemon calls this before it starts any process, and again after every plugin load (a C
    extension that is not marked free-threaded switches the GIL back on). ``is_gil_enabled`` is
    injectable so tests run on any interpreter; by default it is ``sys._is_gil_enabled``, and a build
    without that function is a GIL build.
    """

    check = is_gil_enabled if is_gil_enabled is not None else getattr(sys, "_is_gil_enabled", None)
    if check is None:
        raise GilBuildRefused("interpreter has no sys._is_gil_enabled: not a free-threaded build")
    if check():
        raise GilBuildRefused("the GIL is enabled: start the core on python3.14t with -X gil=0")


# Names copied from the parent env into an engine child. Everything else is dropped; credentials are
# added only when the route selects them. A deliberately small set: the full child-env rules (values,
# PATH, NO_PROXY; B1, CSL-785) are a separate module, not this one.
BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TMPDIR")


def child_env(
    parent_env: Mapping[str, str],
    *,
    extra_names: tuple[str, ...] = (),
    never: tuple[str, ...] = (),
    credential: tuple[str, str] | None = None,
) -> dict[str, str]:
    """Build an explicit child env: allowlisted names from ``parent_env`` plus the route's one credential.

    ``parent_env`` is passed in by the caller; this never reads or copies ``os.environ`` itself.
    Names in ``never`` are dropped even if allowlisted, unless they are the selected credential.
    """

    env = {name: parent_env[name] for name in BASE_ENV + extra_names if name in parent_env and name not in never}
    if credential is not None:
        name, value = credential
        env[name] = value
    return env
