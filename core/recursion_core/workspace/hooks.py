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
- ``worktree``, and no worktree has been made yet: ``place.choose`` picks one directory outside the
  repository, ``spawn.make`` builds it by that place's mechanism, and the path is stored on the launch
  as ``worktree_path``. The same ask returns that directory. It does not work in the source checkout
  and it does not fall back to the engine home. A linked worktree, which writes the source git common
  directory, is made only when this provider was constructed with ``linked=True``.
- ``worktree`` with a ``worktree_path`` already stored: that folder, checked again.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import threading

from recursion_core.workspace import start
from recursion_core.workspace.place import choose
from recursion_core.workspace.spawn import SpawnFailed, make
from recursion_core.workspace.start import Normalizer, default_state_dir
from recursion_core.workspace.validate import WorkspaceInvalid, git_work_tree, unit_safe, validate

log = logging.getLogger(__name__)

STATE_ENV = 'RECURSION_STATE_DIR'
# A run id is one path component. The API's own ids match; anything else is not a slot we will create.
_RUN_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,200}$')


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
    """``working_directory(ctx)`` for seam S4. See the module text.

    ``linked`` is the explicit setting for a linked worktree. The default is false: the place is
    outside the repository and the source git common directory is not written.
    """

    def __init__(self, *, state_dir=None, linked: bool = False) -> None:
        if not isinstance(linked, bool):
            raise TypeError('linked is true or false')
        self._state_dir = None if state_dir is None else Path(state_dir)
        self._linked = linked
        self._lock = threading.Lock()
        self._folders: dict = {}
        self._places: dict = {}
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
            if run_id in self._refused:
                raise self._refused[run_id]
            try:
                folder = self._folder(ctx)
            except WorkspaceInvalid:
                raise
            except Exception as exc:
                # A place already chosen stays. The next ask makes that same place, and does not choose again.
                if run_id is None or run_id not in self._places:
                    if run_id is not None:
                        self._refused[run_id] = exc
                    raise
                raise
            if run_id is None:
                return folder
            self._folders.setdefault(run_id, folder)
            return self._folders[run_id]

    def _checked(self, path: str):
        folder = validate(path, state_dir=None)
        if not unit_safe(folder):
            raise WorkspaceInvalid('not_unit_safe')
        return folder

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
            return self._checked(made)
        if mode == 'worktree':
            return self._spawn(ctx, workspace, source)
        raise WorkspaceInvalid('not_a_directory')

    def _spawn(self, ctx, workspace, source):
        run_id = getattr(ctx, 'run_id', None)
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise WorkspaceInvalid('not_unit_safe')
        top = git_work_tree(source)
        if top is None:
            raise RuntimeError('not_a_repository')
        place = self._places.get(run_id)
        if place is None:
            work, objects = _sizes(top)
            candidate = self._state() / 'worktrees' / run_id
            place = choose(top, [candidate], work_bytes=work, object_bytes=objects, linked=self._linked)
            if place is None or place.engine_worktree_switching:
                raise RuntimeError('no_place_outside_the_repository')
            if place.writes_git_common_dir and not self._linked:
                raise RuntimeError('no_place_outside_the_repository')
            self._places[run_id] = place
        base_ref = workspace.get('base_ref') if isinstance(workspace.get('base_ref'), str) else None
        try:
            root = make(place, base_ref=base_ref)
        except SpawnFailed as exc:
            raise RuntimeError(exc.reason) from exc
        relative = Path(os.path.realpath(source)).relative_to(Path(os.path.realpath(top)))
        target = root if relative == Path('.') else root / relative
        folder = self._checked(str(target))
        if isinstance(workspace, dict):
            workspace['worktree_path'] = str(folder)
        log.info('worktree for %s made at %s by %s', run_id, folder, place.mechanism)
        return folder


def register(hooks) -> None:
    """Register the workspace start field and the working-directory provider. Opens nothing."""
    state = _configured_state()
    start.register(hooks, normalizer=Normalizer(state_dir=state))
    hooks.set('working_directory', Provider(state_dir=state))
