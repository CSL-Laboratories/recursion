# core/recursion_core/daemon_install (CSL-758, F61, candidate)

Linux user-unit install, upgrade and uninstall for the per-user daemon. This is
candidate code. It does not run `systemctl`, does not listen on a socket, and
does not issue an engine-user privilege. Windows and macOS are not in this
package.

Spec cite: CSLR-D-011, PRODUCT.md `055cc7a5c793b7135e5d0565dc4198cf96230531`
(SHA-256 `e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf`).
Paper-only is PRODUCT.md 5.2 / CP0-v2. This package places no order.

## What it writes

Under directories the caller passes (never the process home, never
`os.environ`):

| path | action |
| --- | --- |
| `$home/.config/systemd/user/recursion-daemon.service` | install creates it, upgrade replaces it, uninstall removes it |
| `$runtime_dir/recursion-daemon.sock` | uninstall removes it if present; install does not bind it |
| `$state_dir/grants/engine-user.json` | a pointer whose `engine_user_grant` is `absent` |
| `$state_dir/registry/` | created if missing; upgrade and uninstall do not open or delete it |

The unit's `WantedBy=default.target` is what `systemctl --user enable` attaches
to login (systemd.unit(5)). `Restart=on-failure` is what brings it back after a
crash (systemd.service(5)). No `ExecStart` line looks at a token file.

`scripts/v3` serve-fake still refuses to start when its token file already
exists (`scripts/v3/control_api/__main__.py`, `token_file_exists`). That file
is not modified. `crash_restart` reports `restarted` for this unit whether or
not a token file remains.

`install` and `upgrade` return the `systemctl --user` commands. The caller
runs them. `enable` is without `--now`: the pass bar here is login start, and
this package does not start a process.

## What it does not do

- It does not install the daemon program, a venv, or an engine.
- It does not create an F21 engine-user grant. The JSON pointer is the path
  uninstall must clear.
- It does not keep a live run registry. That directory is only reserved.
- It does not execute a command, open a network socket, or read secrets.

## Tests

Python 3.11+ and the standard library. From the repository root:

```
PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/daemon_install/tests -t core -v
```
