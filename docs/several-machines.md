# Run Recursion on several machines

Each machine runs its own Recursion daemon (Linux only at this head).
Each machine keeps its own record of runs.
No code at this head connects one machine's daemon to another's.

## List runs over SSH

Make sure the daemon is running on each machine you want to check. The
control command requires Python 3.11 or newer because it imports `tomllib`
from the standard library through the terminal package (`recursion_core.tui`).

No package installs `recursion_core` at this head. From a checkout, run the
control command from the repository root with `PYTHONPATH=core` set. On a
machine set up as in the daemon's README, the code is in
`~/.local/lib/recursion/core`: run the venv's python,
`~/.local/lib/recursion/venv/bin/python`, with
`PYTHONPATH=$HOME/.local/lib/recursion/core` set instead.

1. Open an SSH session to the first machine.
   Sign in as the user who runs the daemon.
2. Run the control command in that session (from a checkout at the repository
   root; for the daemon README layout, use the venv python and `PYTHONPATH`
   above).

   ```sh
   PYTHONPATH=core python -m recursion_core.control list
   ```

3. Read the JSON reply for that machine.
4. Open an SSH session to each other machine and repeat the command there.

The command talks to the daemon on the machine where you run it.
It connects through an owner-only Unix socket.

If the daemon is down, the command returns `control_api_unavailable`
and exits with status 1.

The control parser names itself `recursion-control` and has the same
subcommands (`list`, `status RUN`, `stop RUN`, `start`, `conditions`). No
console command by that name is defined at this head. Whether a built package
installs it as a command is pending. Use the `python -m` form shown above.

## Limits

- There is no shared record of runs across machines.
- The product does not place a run on a machine.
- There is no lease on a run.
- There is no way to enroll one machine with another.

A single view over several machines is not part of this release.
No date is given for it.
