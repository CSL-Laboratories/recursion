"""The launch check's registrant (lane LAUNCH-CHECK). It registers nothing.

``recursion_core.launch_check.hooks`` is the registrant name on the seams' fixed import
list (#369, ``control/hooks.py``). Composition imports this module and calls
``register(hooks)`` once, before the daemon serves. ``register`` touches nothing on the
object it is given: no table row, no factory, no command, no hook a running daemon would
call (the Liaison's ruling for this lane's first slice, Nov 15, 2026). With nothing
registered, every launch is refused ``launch_check_absent``.

What it will register, as a design and not as code (the seams' S10 contract at #370 ``3d1480e1``):

* ``HOOK``, ``launch_check``, is a single-valued hook, set with ``hooks.set('launch_check', fn)``.
  ``fn(ctx, executable)`` is called by the launcher on the run's thread after the executable is resolved
  and before the run directory's FIFOs and files are made. It returns ``None`` to let the launch go on,
  or a refusal code, which refuses the launch ``launch_check:<code>`` with no unit started; a raise is
  ``launch_check_failed``. It is parent side only: nothing is added to the child's environment.
* The check it will run there is this package's re-verification of the route's grant (path, realpath,
  size, full SHA-256, the floor, pin and refusal table versions) against ``executable``, which SPINE's
  reworded contract (the Liaison, 4:19 PM Nov 14) binds as the adapter's ``argv[0]``: the binary option
  when composition gave one, else the engine registration's ``default_executable``. That keyword is in
  neither #369 nor #370 yet; nothing here assumes another field.
* A refusal at this hook means "no unit started" (the parent refused). The child-side re-check, whose
  design note lives in csl-research, can only mean "no engine executed": the helper is already inside its unit.

The grants this hook will read come from route load (the lane's next slice), not from here.
"""

from __future__ import annotations

HOOK = "launch_check"  # the single-valued seam this registrant will set (control/hooks.py ``SINGLE``)


def register(hooks: object) -> None:
    """Register nothing. Composition already imports this module; no check is set, so the seam refuses every launch ``launch_check_absent``."""
    return None
