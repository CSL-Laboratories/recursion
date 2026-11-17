"""The workspace's part of a start (seam S6): the pure normaliser and the function that registers it.

``Normalizer(state_dir=...)`` is ``normalize(principal, request)`` for ``hooks.start_registrant``. It reads
the request's ``workspace`` (``methods.v1.json#/$defs/workspace_request``: ``path``, and ``mode`` and
``base_ref`` when given) and answers one of three things:

- ``{}`` when the request names no workspace (absent or null). The run then starts in today's folder, the
  engine's private home, exactly as before; the launch snapshot's ``workspace`` stays null.
- ``{'workspace': {mode, source_path, base_ref, worktree_path}}``, the run's launch workspace as
  ``run.record.v1.json#/$defs/launch_workspace`` gives it: ``source_path`` is the folder RESOLVED (see
  ``validate``), ``base_ref`` is the request's or null, ``worktree_path`` is null, because no worktree
  exists until the provider makes one at launch (seam S4, the next pull request).
- a ``Refusal`` (the start method's own code, before any record exists):
  ``request_invalid`` / ``workspace`` when the value is not a workspace request (not an object, no
  ``path``, a member the schema does not have, a ``mode`` outside ``direct`` and ``worktree``, a
  ``base_ref`` that is not a non-empty string); ``workspace_invalid`` / ``<reason>`` when the folder fails
  a rule of ``validate.REASONS``, or ``base_ref_invalid`` when the base is not one this lane passes to git,
  or ``not_a_repository`` when worktree mode is asked of a folder in no git work tree, or ``not_unit_safe``
  when the folder the run would WORK in could not be named in a unit (see below).

THE MODE when the request gives none: ``worktree`` when the folder is inside a git work tree, ``direct``
otherwise. That is the brief's recommended default (a), a question for Lucas that is still open; the
normaliser takes it as ``default_mode='auto'`` and can be told ``'direct'`` or ``'worktree'``.

THE FOLDER A RUN WORKS IN must hold only the characters the launcher can put in a unit
(``validate.UNIT_PATH_SAFE``). In direct mode that is the folder asked for. In worktree mode it is the run's
worktree under the state directory, ``<state_dir>/worktrees/<run_id>``, and in it the asked folder's place in
its repository (a run asked to work in ``<top>/pkg`` works in ``<worktree>/pkg``); a run id is always a
unit-safe word. So a repository named ``my project`` may be worked on in worktree mode, and not in direct mode,
where the launcher would refuse it after the record exists.

THE PLACE of that worktree is chosen at spawn, not here. ``place.choose`` measures the machine: whether a
candidate is on the same filesystem as the repository, whether a reflink or a hardlink can be made there, and
how much space is free. It picks the cheapest mechanism that filesystem can carry out, in a directory outside
the repository, and it does not write the repository's git common directory. A linked worktree, which does
write that directory, is used only when the caller passes ``linked=True``. The engines' own worktree switching
is off, and the ``Place`` returned is frozen, so the identity bound at spawn stays. This normaliser still
judges unit-safety of ``<state_dir>/worktrees/<run_id>`` above, the only spelling it can see before a run id
exists. The launcher's unit-path check applies to the directory ``choose`` returns.

IT IS PURE. It creates no folder, worktree or branch, writes no file and appends no event: it reads the file
system (``stat``, ``realpath``, and in a mode other than direct the ``.git`` files and folders and the
repository's own config files ``validate.git_work_tree`` reads, the way git's own discovery reads them) and
runs no git. A repository's ``core.worktree`` names the top the run's worktree is made from, so a folder's place
in its repository is its place under that top. A retried start
never reaches it: the seam matches a retry on the client's raw request before any normaliser runs, so a
worktree made for a failed start is found again by the provider, not here.

REGISTERING. ``register(hooks)`` declares the request field ``workspace`` and the snapshot key ``workspace``
and nothing else. Nothing calls it in a running daemon yet: the composition imports a lane's
``workspace/hooks.py``, which is not written, so this pull request registers nothing live.

THE STATE DIRECTORY. A normaliser is given the daemon's state directory so that it can refuse it. Left out,
it is the one the daemon itself uses (``daemon/__main__.py``): ``$XDG_STATE_HOME/recursion``, else
``~/.local/state/recursion``, read when a start is normalised.
"""
from __future__ import annotations

import os
from pathlib import Path

from recursion_core.control.hooks import Refusal
from recursion_core.workspace.validate import WorkspaceInvalid, git_work_tree, unit_safe, valid_base_ref, validate

NAME = 'workspace'
FIELDS = ('workspace',)
KEYS = ('workspace',)
MODES = ('direct', 'worktree')
REQUEST_MEMBERS = frozenset({'path', 'mode', 'base_ref'})


def default_state_dir() -> Path:
    """The daemon's state directory, as ``daemon/__main__.py`` makes it."""
    return Path(os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')) / 'recursion'


class Normalizer:
    """``normalize(principal, request)`` for the workspace's start registrant. See the module's text."""

    def __init__(self, *, state_dir=None, protected=(), default_mode: str = 'auto', uid: int | None = None) -> None:
        if default_mode not in ('auto', *MODES):
            raise ValueError(f'default mode {default_mode!r}')
        self._state_dir = None if state_dir is None else Path(state_dir)
        self._protected = tuple(protected)
        self._default_mode = default_mode
        self._uid = uid

    def __call__(self, principal, request) -> dict | Refusal:
        value = request.get('workspace') if isinstance(request, dict) else None
        if value is None:
            return {}
        if not isinstance(value, dict) or not set(value) <= REQUEST_MEMBERS or 'path' not in value:
            return Refusal('request_invalid', 'workspace')
        path, mode, base_ref = value['path'], value.get('mode'), value.get('base_ref')
        if not isinstance(path, str) or not path:
            return Refusal('request_invalid', 'workspace')
        if 'mode' in value and mode not in MODES:
            return Refusal('request_invalid', 'workspace')
        if 'base_ref' in value and (not isinstance(base_ref, str) or not base_ref):
            return Refusal('request_invalid', 'workspace')
        state_dir = self._state_dir if self._state_dir is not None else default_state_dir()
        try:
            folder = validate(path, state_dir=state_dir, protected=self._protected, uid=self._uid)
        except WorkspaceInvalid as refused:
            return Refusal('workspace_invalid', refused.reason)
        if base_ref is not None and not valid_base_ref(base_ref):
            return Refusal('workspace_invalid', 'base_ref_invalid')
        if mode is None and self._default_mode != 'auto':
            mode = self._default_mode
        works_in = folder
        if mode != 'direct':
            top = git_work_tree(folder)
            if top is None and mode == 'worktree':
                return Refusal('workspace_invalid', 'not_a_repository')
            if top is None:
                mode = 'direct'
            else:
                mode, works_in = 'worktree', state_dir / 'worktrees' / folder.relative_to(top)
        if not unit_safe(works_in):
            return Refusal('workspace_invalid', 'not_unit_safe')
        return {'workspace': {'mode': mode, 'source_path': str(folder), 'base_ref': base_ref, 'worktree_path': None}}


def register(hooks, *, normalizer: Normalizer | None = None) -> None:
    """Register the workspace's start registrant on ``hooks``: the field ``workspace``, the key ``workspace``."""
    hooks.start_registrant(NAME, FIELDS, KEYS, normalizer if normalizer is not None else Normalizer())
