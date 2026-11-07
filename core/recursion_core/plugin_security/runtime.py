"""Free-threaded runtime checks (Lucas, Nov 7: CPython 3.14t, ``-X gil=0``, refuse a GIL build).

``require_production_runtime`` runs once at daemon start and refuses anything other
than CPython 3.14 built free-threaded, started with ``-X gil=0``, with the GIL off.

``GilProbe`` is what the loader calls after every plugin load, including a load that
raised. Measured on this box with CPython 3.14.10t (locally observed, Nov 7):

* started *without* ``-X gil=0``, importing a C extension that does not declare
  ``Py_mod_gil`` switches the GIL on, and ``sys._is_gil_enabled()`` reports it;
* started *with* ``-X gil=0``, the same import keeps the GIL off and only the
  extension's own thread safety is at risk; ``sys._is_gil_enabled()`` stays ``False``.

So the post-load check catches the first case and cannot see the second. The loader
therefore also reports every extension module a load added (``doctor`` finding
``extension_module_loaded``), because under ``-X gil=0`` that is the only signal left.
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
