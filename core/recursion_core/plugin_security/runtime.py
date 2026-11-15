"""Free-threaded runtime checks (Lucas, Nov 7: CPython 3.14t, ``-X gil=0``, refuse a GIL build).

``require_production_runtime`` refuses anything other than CPython 3.14 built
free-threaded, started with ``-X gil=0``, with the GIL off. The daemon calls it at start
(``daemon/__main__.py``, through its ``require_free_threaded``; decision D2, Lucas, Nov 12
2026: "The daemon refuses to start without ``-X gil=0``"), and again on every sweep. On the
composed path a refusal there no longer stops the daemon: it poisons the plugin core and Stop and
reconciliation go on. (A daemon given an injected client, which composes no plugins, still stops.) ``-X gil=0`` matters because without it importing an extension that does not declare
``Py_mod_gil`` turns the GIL on for the whole process (measured below). ``PYTHON_GIL=0``
in the environment is not accepted in its place: the check reads ``sys._xoptions``.

``GilProbe`` is what the loader calls after every plugin load, including a load that
raised. Measured on this box with CPython 3.14.10t (locally observed, Nov 7):

* started *without* ``-X gil=0``, importing a C extension that does not declare
  ``Py_mod_gil`` switches the GIL on, and ``sys._is_gil_enabled()`` reports it;
* started *with* ``-X gil=0``, the same import keeps the GIL off and only the
  extension's own thread safety is at risk; ``sys._is_gil_enabled()`` stays ``False``.

So the post-load check catches the first case and cannot see the second. The loader
therefore also reports every extension module a plugin's import added (the load finding
``extension_module_loaded``; ``doctor`` runs no plugin code, so it cannot report it, and
an extension imported later inside a plugin call is not seen), because under ``-X gil=0``
that is the only signal left.
"""

from __future__ import annotations

import sys
import sysconfig

from .errors import Refused

__all__ = ["GilProbe", "require_production_runtime", "runtime_report"]


def runtime_report() -> dict:
    """What this interpreter is, as plain values (for doctor output and tests)."""
    probe = getattr(sys, "_is_gil_enabled", None)
    return {
        "implementation": sys.implementation.name,
        "version": list(sys.version_info[:3]),
        "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "gil_xoption": sys._xoptions.get("gil"),
        "gil_enabled": True if probe is None else bool(probe()),
    }


def require_production_runtime(report: dict | None = None) -> None:
    """Refuse to start unless this is CPython 3.14t started with ``-X gil=0`` and GIL off."""
    info = runtime_report() if report is None else report
    if info["implementation"] != "cpython" or tuple(info["version"][:2]) != (3, 14):
        raise Refused("runtime_requires_cpython_314t",
                      "found %s %s" % (info["implementation"], ".".join(map(str, info["version"]))))
    if not info["free_threaded_build"]:
        raise Refused("runtime_gil_build_refused")
    if info["gil_xoption"] != "0":
        raise Refused("runtime_requires_X_gil_0")
    if info["gil_enabled"]:
        raise Refused("runtime_gil_enabled")


class GilProbe:
    """Reads ``sys._is_gil_enabled()``. A build without the function counts as GIL on."""

    def gil_enabled(self) -> bool:
        probe = getattr(sys, "_is_gil_enabled", None)
        return True if probe is None else bool(probe())
