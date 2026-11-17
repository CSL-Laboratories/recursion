"""The workspace: the folder a run works in (lane WORKSPACE).

A run is given a validated folder: the one its caller named (the command line sends the folder it was
launched from), checked before anything is launched. In ``direct`` mode the engine works in that folder;
in ``worktree`` mode it works in a git worktree of its own, cut for that run. Diffs, checkpoints, restore,
fork and the publish of a review branch are built on the same record.

What is here now (pull request 1, new files only, nothing registered with a running daemon):

``validate``  the checks on a folder (``validate.py``).
``start``     the start registrant's pure part (S6): a start request's ``workspace`` turned into the run's
              launch snapshot, or a typed refusal; it creates and writes nothing.
``place``     the directory a run's worktree is put in, chosen at spawn from what the machine measures.
              The default is outside the repository and does not write its git common directory. A linked
              worktree is only an explicit setting. The engines' own worktree switching is off, and the
              identity returned is frozen.
``grant``     ``grant(path, uid)`` and ``revoke(grant_id)``, the per-run access to a folder for another user
              that lane STOP builds on, and the ``workspace.grant`` and ``workspace.revoke`` events.

Nothing in this package registers itself: ``workspace/hooks.py``, the module the composition imports, is not
written yet, so a composed daemon has no workspace registrant and starts every run as it does today.
"""
