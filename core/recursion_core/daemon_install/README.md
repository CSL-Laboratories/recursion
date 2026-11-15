# core/recursion_core/daemon_install (lane INSTALL)

The per-user daemon's systemd user unit: `recursiond install`, `upgrade` and `uninstall`
(`control/commands/setup.py` is the command family; `recursion_core._entrypoints` is the
`recursiond` command). Linux only; `recursiond` refuses every command elsewhere with
`platform_unsupported`.

## What it writes

Under the home folder the caller passes (the password database's, which is what the user manager
calls `%h`; never `$HOME`, never `os.environ`):

| path | install | upgrade | uninstall |
| --- | --- | --- | --- |
| `~/.config/systemd/user/recursion-daemon.service` | creates it (0644); the same bytes already there change nothing; other bytes refuse `unit_differs`; a unit another user owns or that group or world can write refuses `unit_untrusted` (owner and mode bits of the file read; POSIX ACLs are not read); a unit removed while it is read refuses `unit_missing`, and one replaced then `unit_changed` (run the command again) | rewrites it (also when its bytes are current but another user could change it); missing refuses `unit_missing` | removes it if its first line is this installer's marker (`unit_not_ours` otherwise); refuses `unit_enabled` while `default.target.wants/` links it |
| `~/.config`, `~/.config/systemd`, `~/.config/systemd/user` | creates the missing ones (0700); existing ones stay as they are | - | stay: other units share them |
| the daemon's state folder (`$XDG_STATE_HOME/recursion`: the run registry, and from slice 2 the stores) | - | - | stays |

A link at any folder or file under the home is refused (`symlink_refused`, `not_regular`), never
followed, and so is a folder there that another user owns or that group or world can write
(`folder_untrusted`: another user could put a unit in it or take ours away; `chmod go-w` it). A
link above the home (`/home` -> `/var/home`) is resolved. Every write goes to a fresh
temporary name, is fsynced, and is linked (new file) or renamed (replacement) into place; the
folder is fsynced after it. The unit is read with a non-blocking open of at most 1 MiB, so a FIFO
or a device in its place is refused (`not_regular`) instead of holding the command. A file-system
error on the way (a folder the user cannot write, a full disk) is the command's refusal
`filesystem_error`, naming the error and the path (the unit's folder when the failing call, an
`fsync` or a `read` on an open file, names none).

The unit runs `<interpreter> -I -X gil=0 -m recursion_core.daemon`, where `<interpreter>` is the
Python that ran `recursiond install`, exactly as given (a virtual environment's `bin/python` is not
resolved to its target; a `%` in its path is written `%%`, systemd's literal percent, so systemd
does not expand it as a specifier). Before writing, the command layer starts that interpreter as the unit
will (`-I`, from `/`) and refuses unless it is free-threaded CPython 3.14
(`interpreter_not_free_threaded`) and imports this same `recursion_core` without `PYTHONPATH`
(`not_installed`). The unit has `Restart=on-failure`, `WantedBy=default.target`, `UMask=0077`,
`KillMode=control-group`, `UnsetEnvironment=PYTHONPATH PYTHONHOME`, and a finite stop:
`TimeoutStopSec=90` with `SendSIGKILL=yes`. A stop that runs past it is a daemon crash: the run
units (their own transient `recursion-run-*.service` units) stay alive, and the daemon recovers at
its next start (seams S1 and S13 in SPINE's brief).

Each command returns the `systemctl --user` commands that apply it and runs none of them:
install `daemon-reload`, `enable` (no `--now`: `recursiond up`, slice 4, starts it); upgrade
`daemon-reload`, `try-restart`; uninstall `stop`, `daemon-reload`. This package starts no process,
opens no socket and touches no system unit.

## Ported from branch `cursor/csl-758-daemon-install-b4bd` (59b96a7b), not merged

Kept: the unit text and its reader, the closed `ExecStart` grammar, the user-manager-only plans,
install twice equals install once, refusing links, the no-process-or-network import rule.
Changed, because master is not the branch's base:

- No grant pointer (`grants/engine-user.json`). Nothing on master reads it; the engine user is lane
  STOP's.
- No `registry/` folder and no socket removal. On master the run registry is
  `$XDG_STATE_HOME/recursion/runs.sqlite3`, and the control socket is
  `$XDG_RUNTIME_DIR/recursion/control.sock`, which the daemon creates and removes itself. Install made
  neither, so uninstall touches neither.
- The unit points at the installed interpreter with `-I -X gil=0`, adds `KillMode=control-group`,
  `UnsetEnvironment` and the finite stop timeout, and starts with a marker line.
- `uninstall` refuses while the unit is enabled (systemd cannot `disable` a unit whose file is gone)
  and refuses a unit it did not write.
- Temporary files have fresh names (a leftover one no longer blocks), new files are linked into place
  exclusively, and writes fsync the file and its folder.
- A root's own links are resolved instead of refused; links under it are still refused.
- `crash_restart` (a model of `scripts/v3`'s serve-fake token check) is gone: this unit has no token
  gate, and the test that the unit has no `ExecStartPre` stands in for it.

## Tests

From the repository root, with the scratch folders from the lane preamble:

```
PYTHONPATH=core python -B -m unittest recursion_core.daemon_install.tests.test_daemon_install
```
