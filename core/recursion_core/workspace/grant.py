"""Per-run access to a folder: the two functions lane STOP builds on, and the events that record a grant.

THE PUBLISHED FUNCTIONS (lane STOP's slice 7 calls them for the engine user):

``grant(path, uid) -> grant_id``
    Gives the user ``uid`` access to the folder ``path`` for one run, and answers the grant's id: ``wsg-``
    and 32 hex digits, made from the folder as it really is and the uid. The same folder and uid give the
    same id, so a grant asked again (a launch retried under its run id) is the same grant, applied once.
``revoke(grant_id) -> None``
    Takes that access away. Revoking a grant that was revoked already, or that changed nothing, does nothing.

Both are idempotent and neither waits on a lock of the daemon, the registry or a run; call them with none
held. A refusal is ``GrantRefused`` with a typed ``code`` (and, for ``path_invalid``, the ``reason``):

``path_invalid``       ``path`` is not a folder the operator owns and may enter (``validate``'s rules, apart
                       from the state directory: a run's worktree lives under it). ``reason`` says which.
``uid_invalid``        ``uid`` is not a whole number from 1: root is never granted anything.
``grant_unsupported``  ``uid`` is another user than the operator. Changing a folder's access for another
                       user (a POSIX access control list on Linux) is NOT BUILT in this pull request: what it
                       does to the mask, to default entries, to files the other user creates and to the
                       operator's own access after a revoke is measured first (the vault's F22 test plan,
                       whose result record has not landed). Until then a grant for another user is refused,
                       never pretended.
``grant_unknown``      ``revoke`` was given something that is not a grant id.

For the operator's own uid there is nothing to change: the operator owns the validated folder, and today
every run runs as the operator. ``grant`` answers the id and changes nothing, and so does ``revoke``.

THE PER-RUN RECORD. A run's access to its folder is recorded in the event log as ``workspace.grant`` when
the run is launched and ``workspace.revoke`` when it has ended, each ``{path, run_id}``
(``event.v1.json#/$defs/workspace.grant``), from ``source`` core, in the run's log: its session's, or
``run:<run_id>`` for a run with no session, never another run's. ``Ledger`` writes them through the event
store's transaction helper, in the caller's transaction, with one row per run in the lane's own table
``workspace_grant`` (``run_id`` its key; ``log_id`` and ``path``; ``granted_seq``, the seq of the grant that
stands or stood last; ``revoked_seq``, the seq of its revoke, or null while it stands):

- a grant recorded again while the run's grant stands (a launch asked twice) appends nothing;
- a grant recorded after the run's revoke is a NEW grant: a start that failed after its grant and gave it
  back is retried under the same run id (the seam relaunches the same run), so the grant is appended again
  and the run counts as unrevoked again, which is what lets a startup after a crash find it;
- a revoke ends the grant that stands, and is appended once for it; with none standing it appends nothing;
- every grant of one run names the same folder and the same log: another path or log for a run already
  recorded is refused (``ValueError('grant_conflict')``) before anything is written.

``unrevoked(tx)`` lists the runs whose grant stands, which is what a startup reads after a crash. A revoke ends
whichever grant of the run stands, so the caller that wires these (the next pull request) revokes an
attempt's grant before that attempt's failure is answered; a retry's grant then always comes after it.
Nothing here opens a transaction: the caller owns it, passes it, and calls this outside every daemon,
registry and run lock. Nothing calls the ledger in a running daemon yet.
"""
from __future__ import annotations

import hashlib
import os
import re

from recursion_core.workspace.validate import WorkspaceInvalid, validate

GRANT_ID = re.compile(r'wsg-[0-9a-f]{32}')
CODES = ('path_invalid', 'uid_invalid', 'grant_unsupported', 'grant_unknown')
_RUN_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
_LOG_ID = re.compile(r'(ses-[A-Za-z0-9_-]{1,60}|run:[A-Za-z0-9_-]{1,64})')


class GrantRefused(Exception):
    """A grant or a revoke that was refused. ``code`` is one of ``CODES``; ``reason`` goes with ``path_invalid``."""

    def __init__(self, code: str, reason: str = '') -> None:
        if code not in CODES:
            raise ValueError(f'no code named {code!r}')
        super().__init__(f'{code}: {reason}' if reason else code)
        self.code, self.reason = code, reason


def grant_id_of(folder, uid: int) -> str:
    """The id of the grant of ``folder`` (resolved) to ``uid``."""
    digest = hashlib.sha256(f'{int(uid)}\x00{os.fspath(folder)}'.encode('utf-8', 'surrogateescape')).hexdigest()
    return 'wsg-' + digest[:32]


def grant(path, uid) -> str:
    """Give ``uid`` access to the folder ``path`` for one run; the grant's id. See the module's text."""
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 1:
        raise GrantRefused('uid_invalid')
    try:
        folder = validate(os.fspath(path) if isinstance(path, os.PathLike) else path, state_dir=None)
    except WorkspaceInvalid as refused:
        raise GrantRefused('path_invalid', refused.reason) from None
    if uid != os.geteuid():
        raise GrantRefused('grant_unsupported')
    return grant_id_of(folder, uid)


def revoke(grant_id) -> None:
    """Take a grant away. Idempotent. See the module's text."""
    if not isinstance(grant_id, str) or not GRANT_ID.fullmatch(grant_id):
        raise GrantRefused('grant_unknown')
    # Only the operator's own grants exist yet, and they changed nothing: there is nothing to take away.


# ------------------------------------------------------------------------------------------- the record
TABLE = 'workspace_grant'
VERSION = 1
DDL = ('CREATE TABLE workspace_grant (run_id TEXT PRIMARY KEY, log_id TEXT NOT NULL, path TEXT NOT NULL, '
       'granted_seq INTEGER NOT NULL, revoked_seq INTEGER)')


def _ids(run_id, log_id) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError('run_id_invalid')
    log = f'run:{run_id}' if log_id is None else log_id
    if not isinstance(log, str) or not _LOG_ID.fullmatch(log) or (log.startswith('run:') and log != f'run:{run_id}'):
        raise ValueError('log_id_invalid')
    return log


def access_event(kind: str, run_id: str, path: str, *, log_id: str | None = None) -> dict:
    """The ``workspace.grant`` or ``workspace.revoke`` event of a run, as the helper's ``append`` takes it: an
    event.v1 without ``seq`` and ``time``, which the helper assigns. ``log_id`` is the run's session, or
    ``run:<run_id>`` for a run with no session (the default); another run's log raises ``ValueError``."""
    if kind not in ('workspace.grant', 'workspace.revoke'):
        raise ValueError('kind')
    log = _ids(run_id, log_id)
    if not isinstance(path, str) or not path:
        raise ValueError('path_invalid')
    return {'session_id': log, 'run_id': run_id, 'turn_id': None, 'type': kind, 'source': 'core',
            'trust': 'trusted', 'payload': {'path': path, 'run_id': run_id}}


class Ledger:
    """The per-run grant record, in the lane's table, through the event store's transaction helper."""

    @staticmethod
    def declare(store) -> None:
        """Declare the lane's table, once, at composition: ``store.register_tables('workspace', 1, DDL)``."""
        store.register_tables('workspace', VERSION, DDL)

    @staticmethod
    def granted(tx, run_id: str, path: str, *, log_id: str | None = None) -> int | None:
        """Record that ``run_id`` was given ``path``, in ``tx``: the new event's seq, or None when the run's
        grant stands already (nothing is appended again). After the run's revoke it is a new grant. Another path
        or log for a run already recorded raises ``ValueError('grant_conflict')``. See the module's text."""
        event = access_event('workspace.grant', run_id, path, log_id=log_id)
        rows = tx.execute('SELECT log_id, path, revoked_seq FROM workspace_grant WHERE run_id = ?', (run_id,))
        if not rows:
            seq = tx.append(event)
            tx.execute('INSERT INTO workspace_grant (run_id, log_id, path, granted_seq, revoked_seq) '
                       'VALUES (?, ?, ?, ?, NULL)', (run_id, event['session_id'], path, seq))
            return seq
        log, held, revoked = rows[0]
        if (log, held) != (event['session_id'], path):
            raise ValueError('grant_conflict')
        if revoked is None:
            return None
        seq = tx.append(event)
        tx.execute('UPDATE workspace_grant SET granted_seq = ?, revoked_seq = NULL WHERE run_id = ?', (seq, run_id))
        return seq

    @staticmethod
    def revoked(tx, run_id: str) -> int | None:
        """Record that ``run_id`` lost its folder, in ``tx``: the new event's seq, or None when no grant of the
        run stands (none was recorded, or its revoke was)."""
        _ids(run_id, None)
        rows = tx.execute('SELECT log_id, path FROM workspace_grant WHERE run_id = ? AND revoked_seq IS NULL', (run_id,))
        if not rows:
            return None
        log_id, path = rows[0]
        seq = tx.append(access_event('workspace.revoke', run_id, path, log_id=log_id))
        tx.execute('UPDATE workspace_grant SET revoked_seq = ? WHERE run_id = ?', (seq, run_id))
        return seq

    @staticmethod
    def unrevoked(tx) -> list:
        """``[(run_id, log_id, path), ...]`` for every run whose grant stands (no revoke after it), by run id."""
        return [tuple(row) for row in
                tx.execute('SELECT run_id, log_id, path FROM workspace_grant WHERE revoked_seq IS NULL ORDER BY run_id')]
