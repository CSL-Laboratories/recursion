"""The workspace registrant (seam S4 and seam S6).

``register(hooks)`` is what ``control.hooks.compose`` imports. It registers the start normaliser from
``start.py`` and the working-directory provider. The normaliser reads the daemon's state directory from
``RECURSION_STATE_DIR`` at registration (``compose()`` takes no argument). Left unset, it is the daemon's
own default, read when a start is normalised.

The provider reads ``ctx.launch.workspace`` and makes no registry lookup.

- No workspace: ``None``. The launcher keeps today's folder, the engine's private home. HOME does not change.
- ``direct``: the source folder, checked again with ``validate`` (another uid, the state directory and
  everything inside it, a link followed to its real folder). The same folder is returned if the launch
  asks a second time.
- ``worktree``, and no worktree has been made yet: ``place.choose`` picks a directory outside the
  repository. This door does not make the worktree, does not work in the source checkout, and does not
  fall back to the engine home. The launch is refused. A second ask for the same run does not pick again.
- ``worktree`` with a ``worktree_path`` already stored: that folder, checked again. This door never
  writes one.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import threading

from recursion_core.workspace import start
from recursion_core.workspace.place import choose
from recursion_core.workspace.start import Normalizer, default_state_dir
from recursion_core.workspace.validate import WorkspaceInvalid, git_work_tree, unit_safe, validate

log = logging.getLogger(__name__)

STATE_ENV = 'RECURSION_STATE_DIR'


def _configured_state():
    raw = os.environ.get(STATE_ENV)
    if not raw:
        return None
    return Path(raw)


def _bytes_under(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def _sizes(repository: Path) -> tuple[int, int]:
    """Checkout bytes, and git object bytes. Counts the caller measured, not a stand-in pair of zeros."""
    work = 0
    for dirpath, dirnames, filenames in os.walk(repository, followlinks=False):
        if '.git' in dirnames:
            dirnames.remove('.git')
        for name in filenames:
            try:
                work += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    objects = _bytes_under(repository / '.git' / 'objects') if (repository / '.git').is_dir() else 0
    return work, objects


class Provider:
    """``working_directory(ctx)`` for seam S4. See the module text."""

    def __init__(self, *, state_dir=None) -> None:
        self._state_dir = None if state_dir is None else Path(state_dir)
        self._lock = threading.Lock()
        self._folders: dict = {}
        self._refused: dict = {}

    def _state(self) -> Path:
        if self._state_dir is not None:
            return self._state_dir
        return default_state_dir()

    def __call__(self, ctx):
        run_id = getattr(ctx, 'run_id', None)
        with self._lock:
            if run_id in self._folders:
                return self._folders[run_id]
            refused = self._refused.get(run_id)
        if refused is not None:
            raise refused
        try:
            folder = self._folder(ctx)
        except WorkspaceInvalid:
            raise
        except Exception as exc:
            # A worktree place was chosen, or the choice failed. Asking again must not choose a second place.
            if run_id is None:
                raise
            with self._lock:
                self._refused.setdefault(run_id, exc)
                raise self._refused[run_id]
        if run_id is None:
            return folder
        with self._lock:
            self._folders.setdefault(run_id, folder)
            return self._folders[run_id]

    def _folder(self, ctx):
        launch = getattr(ctx, 'launch', None)
        workspace = getattr(launch, 'workspace', None) if launch is not None else None
        if not workspace:
            return None
        mode = workspace.get('mode')
        source = workspace.get('source_path')
        made = workspace.get('worktree_path')
        if mode == 'direct':
            folder = validate(source, state_dir=self._state())
            if not unit_safe(folder):
                raise WorkspaceInvalid('not_unit_safe')
            return folder
        if mode == 'worktree' and isinstance(made, str) and made:
            folder = validate(made, state_dir=None)
            if not unit_safe(folder):
                raise WorkspaceInvalid('not_unit_safe')
            return folder
        if mode == 'worktree':
            self._choose_and_stop(source)
        raise WorkspaceInvalid('not_a_directory')

    def _choose_and_stop(self, source) -> None:
        top = git_work_tree(source)
        if top is None:
            raise RuntimeError('not_a_repository')
        work, objects = _sizes(top)
        candidate = self._state() / 'worktrees' / 'unbuilt'
        chosen = choose(top, [candidate], work_bytes=work, object_bytes=objects, linked=False)
        if chosen is None:
            raise RuntimeError('no_place_outside_the_repository')
        # The place is outside the repository. Making the worktree (reflink, hardlink or copy,
        # then the path the run would work in) is the spawn half, and it is not this door.
        log.error('place %s (%s) was chosen for %s and the worktree was not made',
                  chosen.path, chosen.mechanism, top)
        raise RuntimeError('worktree_not_made')


def register(hooks) -> None:
    """Register the workspace start field and the working-directory provider. Opens nothing."""
    state = _configured_state()
    start.register(hooks, normalizer=Normalizer(state_dir=state))
    hooks.set('working_directory', Provider(state_dir=state))
