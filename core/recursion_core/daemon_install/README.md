# core/recursion_core/daemon_install (lane INSTALL)

The per-user daemon's systemd user unit and its stores: `recursiond install`, `upgrade` and
`uninstall` (`control/commands/setup.py` is the command family; `recursion_core._entrypoints` is
the `recursiond` command). Linux only; `recursiond` refuses every command elsewhere with
`platform_unsupported`.

## What it writes

Under the home folder the caller passes (the password database's, which is what the user manager
calls `%h`; never `$HOME`; a password database that gives no entry for this user, none or a failed
lookup, refuses `home_unknown`). The command layer reads `XDG_CONFIG_HOME` from its environment only to
refuse, before anything is read, a value naming another folder than `~/.config` (`config_home_elsewhere`:
a user manager started with it reads its units from there; the refusal says how to run the command
without it). `XDG_STATE_HOME` is the person's choice of the state folder, and one resolver names it
(`layout.state_home`): the variable's value when it is set, else `~/.local/state`, the standard's
default, with `recursion` under it. Empty is the default; a relative value is refused
`state_home_relative` (install, upgrade and the daemon's start alike), its text naming the value and the
way out (an absolute path, or unset the variable). A chosen place that is there must be the person's and
not writable by group or world, with every folder and link on the way root's or the person's
(`folder_untrusted`, the home's own rule); one that is not there is made 0700 under its nearest folder
that is there, which must meet the same rule. Below, `<state>` is `$XDG_STATE_HOME/recursion`, or
`~/.local/state/recursion` by default:

| path | install | upgrade | uninstall |
| --- | --- | --- | --- |
| `<state>/plugin-security/breaker.sqlite3` and `revoked.sqlite3` (the breaker store and the revocation store, where the daemon opens them) | creates each missing one through `plugin_security.stores` (0600), before the unit; one already there must pass `stores.open_store` and is left as it is; a refusal keeps the store's code (`breaker_store_untrusted`, `revocation_store_invalid`, ...), and a store that cannot be made or put in place (a full disk, an I/O error at its final link) is `store_not_created` | the same | stay (question 3: deleting them would reset every runtime revocation) |
| `~/.config/systemd/user/recursion-daemon.service` | creates it (0644); the same bytes already there change nothing; other bytes refuse `unit_differs`, and a unit another user owns or that group or world can write refuses `unit_untrusted` (owner and mode bits of the file read; POSIX ACLs are not read); a unit removed while it is read refuses `unit_missing`, and one replaced then `unit_changed` (run the command again; the path is checked while the file read is still open); all before any store is made; and a unit found that is removed, replaced or rewritten in place while the stores are made refuses `unit_missing` or `unit_changed` at the answer, with the stores already made | rewrites it (also when its bytes are current but another user could change it); missing refuses `unit_missing`, before any store is made; a unit whose `[Service]` section sets `XDG_STATE_HOME` to another folder, reads an `EnvironmentFile`, passes the user manager's `XDG_STATE_HOME`, or cannot be read so, refuses `unit_state_elsewhere`, before anything is made and with the unit left as it is (replacing it would point the daemon at another state folder, without its revocations and breaker trips); a current unit it keeps that is removed, replaced or rewritten in place while the stores are made refuses `unit_missing` or `unit_changed` at the answer | removes it if its first line is this installer's marker (`unit_not_ours` otherwise), while it holds the file it read and after the path was checked to name it (`unit_changed` if it was replaced meanwhile; the removal itself is by name, so a file put there in the few statements after that check is the one removed; one already gone counts as removed by the other caller); refuses `unit_enabled` while `default.target.wants/` links it |
| `<state>/homes` (the route homes' custody root: the launcher makes each route home in it, and `setup.route_home.check` refuses a root that is missing or not owner-only) | creates it (0700) if missing, after the stores and before the unit; one already there must be ours, open to nobody else at all, and readable, writable and searchable by its owner (`folder_untrusted`, before anything is made); the root as made, or as another caller made it meanwhile, is held to the same rule before the unit is written (a umask that takes the owner's bits, or another caller's 0755, is `folder_untrusted` and leaves no new unit) | the same | stays, with the route homes and their logins in it |
| `~/.config`, `~/.config/systemd`, `~/.config/systemd/user`, and `~/.local`, `~/.local/state` (by default) or the chosen place, `<state>`, `.../plugin-security` | creates the missing ones (0700); existing ones stay as they are | the same | stay: other units share them, and the state folder holds the run registry |
| `~/.config/recursion/authority/anchor.key` (the install's own authority key) and its folder | makes the key (0600, folder 0700) on a profile with no key and no ledger, then the ledger bound to it; a key, ledger and `authority_anchor` that agree are left as they are; a disagreement is `authority_anchor_disagrees`, before anything is written (below) | the same | stay |
| anything else in the daemon's state folder (the run registry) | - | - | stays |

The unit sets `XDG_STATE_HOME=%h/.local/state` by default, or `"XDG_STATE_HOME=<the chosen place>"`
(absolute, links resolved, in systemd's quoting), so the daemon's state folder (`daemon/__main__.py`
takes it from the same resolver) is the one install made the stores in, whatever the user manager's own
environment says. A unit `upgrade` would replace whose daemon may keep its state in another folder than
the chosen one (or the default) is refused `unit_state_elsewhere`, its text naming the folder in use and,
with a chosen place or a unit the installer wrote, the two ways out that work (run the command again with
`XDG_STATE_HOME` naming the unit's folder, or unset where it names the default; or the daemon stopped,
`recursiond uninstall`, the state folder the unit names moved to the new place, then `recursiond install` there: the ledger moves
with the stores, since the install's key with no ledger beside it is refused); a
value only in the user manager's own environment (`environment.d`, `systemctl --user set-environment`) is
not seen, since this package starts no process to ask it. With a chosen place the answer adds `state`
(the folder in use) and, when the default's `plugin-security` folder holds a store, `stores_left`: one
sentence saying those stores are left where they are and not used. Nothing is ever moved or removed:
a store made in the default place before stays there. The daemon's installed-profile check counts a
chosen folder as installed when the installer's unit names it. The ledger step runs in the same `install` and `upgrade`:
the command binds the authority ledger to the install's own key (below). A caller of `install()` that passes
neither `anchor_public_key` nor `make_anchor` gets `needs the anchor key, which recursiond install makes` and no `ledger.sqlite3`; one
that passes a key creates the ledger through `plugin_security.stores.create_ledger`.

### The install's own authority key

The owner answered that the ledger's anchor is the install's own key. On a profile with no key and no
ledger, `recursiond install` (and `upgrade`, for a profile installed before) makes one Ed25519 key pair
through `cryptography`, writes its private half to `~/.config/recursion/authority/anchor.key` (PKCS#8
PEM, unencrypted; file 0600 in a folder 0700, under the same link and owner checks as every folder here;
a fresh temporary name made with `O_EXCL` and `O_NOFOLLOW`, linked into place exclusively), creates
`ledger.sqlite3` bound to its public half, and prints one line on stderr: "Made this install's own
authority key (owner-only, <path>); it signs your order permissions on this machine and never leaves it.
Losing it means making new permissions; it is not recoverable." The key is never printed, logged or
answered, and no copy is made anywhere. The line is printed as soon as the key and its ledger are both in
place, before the later steps, so a later step that fails does not hide it. A failure between the key and
the ledger leaves the two agreeing or neither: a ledger already bound to the key just made keeps the key
with it (the line is printed), and with no ledger there the key is removed again, only while the path
still names the file read (a key moved onto the path after that read is kept; a file moved there between
the last comparison and the unlink is not seen). Only a process ended between the two (a power cut, a kill)
can leave the key alone. The folders made on the way to the key are synced in their parents before the
key is written. A key found with its ledger never gets a new ledger: a ledger gone between the look and
the ledger step is refused `authority_anchor_disagrees`, and the key is left as it is.

Run again with the key, the ledger and `install.json`'s `authority_anchor` (null, or the same key) agreeing,
it writes nothing (`created: false`). Before anything is written, a disagreement is refused
`authority_anchor_disagrees`, its detail naming which part: a key without a ledger, a ledger without a
key (a new key would orphan the ledger's spend history, so none is made), a ledger bound to another key,
an `authority_anchor` without a key or naming another key. The first two say what is there and what can
be done: for a key alone, run again with `XDG_STATE_HOME` naming the folder its ledger was made in, or move
the key out of its folder and run again, which makes a new key and ledger (permissions the old key signed
are then not accepted); for a ledger alone, put its key back, since a new key is made only when no ledger
is there. A key or an `authority_anchor` on CSL's
published list (`install.CSL_KEY_IDS`; none is published yet) is `anchor_is_csl_key`. A link at the key is
`not_regular` and its target is not touched; a link at its folder is `symlink_refused`; a folder another
user could change, or not exactly owner-only, is `folder_untrusted`; a key file open to anyone else is
`anchor_key_untrusted`; one that is not an unencrypted Ed25519 PKCS#8 PEM key is `anchor_key_invalid`.
`install.json` is read through the daemon's own reader and never written here: its `authority_anchor` is
written by the plugin installer's commit path, which is not on this branch, so it stays null for now.
Where the interpreter cannot import `cryptography` (the optional `plugins` extra), no key is made and the
ledger step's detail says so; the daemon then refuses to start an installed profile, as it does without a
ledger. `uninstall` leaves the key, as it leaves the stores.

A link at any folder or file under the home is refused (`symlink_refused`, `not_regular`), never
followed, and so is a folder there that another user owns or that group or world can write
(`folder_untrusted`: another user could put a unit in it or take ours away; `chmod go-w` it). A
link above the home (`/home` -> `/var/home`) is resolved. Every write goes to a fresh
temporary name, is fsynced, and is linked (new file) or renamed (replacement) into place; the
folder is fsynced after it. The unit is read with a non-blocking open of at most 1 MiB, so a FIFO
or a device in its place is refused (`not_regular`) instead of holding the command, and so is a socket. A
file-system error on the way (a folder the user cannot write, a full disk, a unit that cannot be
opened for want of a descriptor or for an I/O error) is the command's refusal
`filesystem_error`, naming the error and the path (the unit's folder when the failing call, an
`fsync` or a `read` on an open file, names none).

The unit runs `<interpreter> -I -X gil=0 -m recursion_core.daemon`, where `<interpreter>` is the
Python that ran `recursiond install`, exactly as given (a virtual environment's `bin/python` is not
resolved to its target; a `%` in its path is written `%%`, systemd's literal percent, so systemd
does not expand it as a specifier). Before writing, the command layer starts that interpreter as the unit
will (`-I`, from `/`) and refuses unless it is free-threaded CPython 3.14
(`interpreter_not_free_threaded`) and imports this same `recursion_core` without `PYTHONPATH`
(`not_installed`). The unit has `Restart=on-failure`, `WantedBy=default.target`, `UMask=0077`,
`KillMode=control-group`, `UnsetEnvironment=PYTHONPATH PYTHONHOME`,
`Environment=XDG_STATE_HOME=%h/.local/state` (or the chosen place, above), and a finite stop:
`TimeoutStopSec=90` with `SendSIGKILL=yes`. A stop that runs past it is a daemon crash: the run
units (their own transient `recursion-run-*.service` units) stay alive, and the daemon recovers at
its next start (seams S1 and S13 in SPINE's brief).

Each command returns the `systemctl --user` commands that apply it and runs none of them:
install `daemon-reload`, `enable` (no `--now`: `recursiond up`, slice 4, starts it); upgrade
`daemon-reload`, `try-restart`; uninstall `stop`, `daemon-reload`. This package starts no process,
opens no socket and touches no system unit.

### The disk under the stores

Before any folder, store, custody root or unit is made, `install` and `upgrade` read the file-system type
of the folder each store goes in (the breaker store's, the revocation store's and the ledger's, under the
state folder the one resolver names, chosen or default) from `/proc/self/mountinfo`: the longest mount
point that holds the folder's nearest folder that is there decides. On `9p`, `v9fs`, `drvfs`, `overlay`,
`nfs`, `nfs4`, `cifs` or `smb3` (`install.REFUSED_FILESYSTEMS`, append-only) they refuse
`store_filesystem_unsupported` and make nothing, with this one line, `<folder>` the folder whose type was
read and `<kind>` the type: "Recursion does not create its stores in <folder>: it is on <kind>, where
SQLite's write-ahead log can damage a database. Put a local disk or volume at <folder> (in a container:
mount a volume there) and run recursiond install again, or run it with XDG_STATE_HOME set to an absolute
folder on a local disk. Check a folder with: findmnt -no FSTYPE -T "<folder>"". The XDG_STATE_HOME way is
for `install`: an `upgrade` whose unit names another state folder is still refused `unit_state_elsewhere`,
with that refusal's own ways out. Where the table cannot be read (no `/proc/self/mountinfo`), nothing is
refused and the plan's `filesystem` lines say the type was not read. A person's setting to accept the
risk is not read yet; `check_store_ground` takes it as an argument (`accept_risk`), and no caller passes
it.

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
