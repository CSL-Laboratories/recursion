# Scheduled runs

How to run a routine on a schedule today, with your operating system's own timer
calling Recursion's own control command. It needs nothing from Recursion beyond the
daemon and the `start` command, and it is safe to repeat: if the timer fires twice for
the same period, one run starts.

A routines plugin is the intended way to run routines. This page is the way to do it
without one, and it says plainly at the end what it does not do.

## What you need

- A Linux machine where the Recursion daemon is running for your user and reachable at
  its control socket (`$XDG_RUNTIME_DIR/recursion/control.sock`). The daemon and that
  socket are Linux only at this commit. How to install and start the daemon is in
  [core/recursion_core/daemon/README.md](../core/recursion_core/daemon/README.md).
- An engine set up for the daemon. A `start` can only succeed when the engine it names
  (`claude_code` or `codex`) is set up, so check that by hand once before you schedule it.
- The control command. It is reached as `python -m recursion_core.control`. Where the
  package ships a `recursion-control` command, `recursion-control` takes the same
  arguments; that command is pending, so the examples below use the module form.

The command this page calls is:

```
python -m recursion_core.control start --tenant TENANT --engine ENGINE --prompt PROMPT --client-key KEY
```

`--tenant`, `--engine` and `--prompt` are required; `--client-key` is optional. `ENGINE`
is one of the engines the daemon wires: `claude_code` or `codex`. It prints the daemon's
answer as one line of JSON and exits 0 when the run started, 1 on any other answer
(including the daemon being down), 2 on a usage error.

## Why a repeat starts one run

`--client-key` is what makes the call safe to repeat. The daemon remembers the key for
each tenant. A retry never starts a second run: when the first `start` launched, a second
`start` with the same tenant and key answers the first run, with `"duplicate": true`. (If
the first `start` failed to launch, a retry with the same key relaunches under the same
run id and may answer `"duplicate": false`. However, the launcher creates `stdin.fifo`,
`stdout.jsonl` and `stderr.log` in the run's directory without looking for an earlier
attempt, and `Daemon.start` inserts the run's row the same way; only `env.fifo` is removed
on failure, so a retry of an attempt that got that far can fail again on the files or row
it left; when that happens, start the run by hand with a new `--client-key` or without
one, or wait for the next period's key.) The key is tied to the engine and to
the prompt (kept as a digest), so the same key with a different engine or prompt is
refused with `request_invalid` / `client_key_reused` and nothing is launched. The key
survives a daemon restart.

So the key must name the routine and the period, not the moment. A key made from the
routine's name and the date (for a daily routine) is the same for every firing that day.
A key made from the current second would be different every time and would defeat the
purpose.

## The routine as a small file

A routine is a small file at `~/.config/recursion/routines/NAME.env` with four required lines. For a
routine called `nightly-review`:

```
ROUTINE_TENANT=demo
ROUTINE_ENGINE=claude_code
ROUTINE_PROMPT='Review the changes merged yesterday and list anything that needs a follow-up.'
ROUTINE_SLOT=%Y%m%d
```

`ROUTINE_SLOT` is a `date` format. It says how long one period is: `%Y%m%d` is one run
per calendar day, `%Y%m%d%H` one per hour, `%G%V` one per week. It is read in the
machine's local time, like the timer's `OnCalendar=`.

Two optional lines control how long the script retries when the daemon's control socket is
not yet answering (`control_api_unavailable`), such as during a catch-up firing at boot:

```
ROUTINE_WAIT=0
ROUTINE_PAUSE=0
```

`ROUTINE_WAIT` is a whole number of seconds to keep retrying `start` on
`control_api_unavailable`; `0` (the default when omitted) means no wait. `ROUTINE_PAUSE`
is a whole number of seconds to sleep between tries (default `0`). When you set a
non-zero `ROUTINE_WAIT`, set a non-zero `ROUTINE_PAUSE` too so the script sleeps between
tries; pick figures that fit your host's daemon startup sweep (no fixed figures are
written into the script).

The file is read by the shell, so put the prompt on one line inside single quotes (a
prompt that holds a single quote needs it written as `'\''`). Put only text you trust in
it: the file is run as shell text with your rights.

The script's default command, `python3 -m recursion_core.control`, finds the package
only when `PYTHONPATH` points at it. The systemd service below sets it. Any other way
of calling the script (launchd, Task Scheduler, a shell) must set it in the routine file,
which the script reads, for example with these lines at the end of the file:

```
export PYTHONPATH="$HOME/.local/lib/recursion/core"
export RECURSION_CONTROL="$HOME/.local/lib/recursion/venv/bin/python3.14t -X gil=0 -m recursion_core.control"
```

With the documented install (`core/` copied to `~/.local/lib/recursion/core` and a bare
venv with nothing installed), set `PYTHONPATH` in every case; add `RECURSION_CONTROL`
only to name the interpreter. Without `PYTHONPATH`, the command fails with
`No module named 'recursion_core'` before it reaches the daemon.

## The script

One short script turns a routine name into the `start` call. Save it as
`~/.local/bin/recursion-run-routine` and make it executable. The three forms below all
call it.

```sh
#!/bin/sh
# recursion-run-routine NAME: start routine NAME once for the current period.
set -eu
name="${1:?usage: recursion-run-routine NAME}"
case "$name" in *[!A-Za-z0-9._-]*|"") echo "bad routine name" >&2; exit 2;; esac
. "${RECURSION_ROUTINES_DIR:-$HOME/.config/recursion/routines}/$name.env"
wait_s="${ROUTINE_WAIT:-0}"
pause_s="${ROUTINE_PAUSE:-0}"
case "$wait_s" in ""|*[!0-9]*|0[0-9]*) echo "bad ROUTINE_WAIT" >&2; exit 2;; esac
case "$pause_s" in ""|*[!0-9]*|0[0-9]*) echo "bad ROUTINE_PAUSE" >&2; exit 2;; esac
slot=$(date +"$ROUTINE_SLOT")
deadline=0
if [ "$wait_s" -gt 0 ]; then
  deadline=$(( $(date +%s) + wait_s ))
fi
while :; do
  rc=0
  # RECURSION_CONTROL is left unquoted on purpose: it is a command and its arguments.
  out=$(${RECURSION_CONTROL:-python3 -m recursion_core.control} start \
    --tenant "$ROUTINE_TENANT" --engine "$ROUTINE_ENGINE" \
    --prompt "$ROUTINE_PROMPT" --client-key "$name-$slot") || rc=$?
  if [ "$rc" -ne 1 ]; then
    if [ -n "$out" ]; then printf '%s\n' "$out"; fi
    exit "$rc"
  fi
  case "$out" in
    *'"code": "control_api_unavailable"'*|*'"code":"control_api_unavailable"'*) ;;
    *)
      if [ -n "$out" ]; then printf '%s\n' "$out"; fi
      exit "$rc"
      ;;
  esac
  if [ "$wait_s" -eq 0 ]; then
    printf '%s\n' "$out"
    exit "$rc"
  fi
  now=$(date +%s)
  if [ "$now" -ge "$deadline" ]; then
    printf '%s\n' "$out"
    exit "$rc"
  fi
  if [ "$pause_s" -gt 0 ]; then
    rem=$(( deadline - now ))
    if [ "$pause_s" -lt "$rem" ]; then sleep "$pause_s"; else sleep "$rem"; fi
  fi
done
```

The key is `NAME-SLOT`, for example `nightly-review-20261120`. Run it twice by hand
and, when the first `start` launched, the second answer carries `"duplicate": true`.

The script retries `start` only when the command exits 1 with `control_api_unavailable`,
for up to `ROUTINE_WAIT` seconds with `ROUTINE_PAUSE` seconds between tries (both default
to `0`, so with `ROUTINE_WAIT=0` or unset it tries once and returns). Any other answer,
and exit 2 on a usage error, are returned at once.

## Linux: a systemd user timer and service

Put these two files in `~/.config/systemd/user/`.

`recursion-routine-nightly-review.service`:

```
[Unit]
Description=Recursion routine nightly-review
Wants=recursion-daemon.service
After=recursion-daemon.service

[Service]
Type=oneshot
Environment=PYTHONPATH=%h/.local/lib/recursion/core
Environment="RECURSION_CONTROL=%h/.local/lib/recursion/venv/bin/python3.14t -X gil=0 -m recursion_core.control"
ExecStart=%h/.local/bin/recursion-run-routine nightly-review
```

`recursion-routine-nightly-review.timer`:

```
[Unit]
Description=Fire Recursion routine nightly-review every day at 03:00

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

The paths in the service match the daemon's own unit file (`core/systemd/recursion-daemon.service`):
change them if you installed Recursion somewhere else. `Persistent=true` means that if
the machine was off at 03:00, the timer fires once when it is next up. The key names the
period in which the firing happens, not the period it was meant for; a catch-up and the
next regular firing in the same period make one run.

A catch-up firing at boot can reach the daemon before its control socket answers:
`recursion-daemon.service` is `Type=simple`, so systemd considers
`After=recursion-daemon.service` satisfied as soon as the daemon process starts, while
the daemon runs its startup sweep before creating `$XDG_RUNTIME_DIR/recursion/control.sock`.
To wait through that startup window, set `ROUTINE_WAIT` and `ROUTINE_PAUSE` in the routine
file so the script retries `control_api_unavailable` until the socket answers; otherwise
start the service by hand once the daemon is up, or let the next period's firing try again.

Check the files, then turn the timer on:

```
systemd-analyze --user verify recursion-routine-nightly-review.service recursion-routine-nightly-review.timer
systemctl --user daemon-reload
systemctl --user enable --now recursion-routine-nightly-review.timer
systemctl --user list-timers recursion-routine-nightly-review.timer
```

To try it without waiting for 03:00, start the service by hand, twice:
`systemctl --user start recursion-routine-nightly-review.service`. The second start
makes no second run. If the daemon is down (and stays down past `ROUTINE_WAIT`), the service fails and
`systemctl --user status recursion-routine-nightly-review.service` shows the
`control_api_unavailable` answer; nothing is queued, and the next firing tries again.

The service ends when `start` returns. The daemon starts the run in its own unit, so
the service ending does not stop the run.

## macOS: launchd (beta, not run)

This form is written from launchd's documented format and has not been run. At this
commit the daemon and its control socket are Linux only, so on a Mac the command answers
`control_api_unavailable` unless it reaches a daemon on a Linux machine by some route of
your own. The form below is the scheduling half only. Set `PYTHONPATH` (and
`RECURSION_CONTROL` only to name the interpreter) in the routine file, as described under
the routine file above.

`~/Library/LaunchAgents/recursion.routine.nightly-review.plist`:

```
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>recursion.routine.nightly-review</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>"$HOME/.local/bin/recursion-run-routine" nightly-review</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>3</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
</dict>
</plist>
```

Load it with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/recursion.routine.nightly-review.plist`.
launchd runs a job that was missed while the Mac slept at the next wake, and a repeat
carries the same key, so it makes one run.

## Windows: Task Scheduler (beta, not run)

This form has not been run. The control socket needs POSIX; run natively on Windows the
command answers `control_api_unavailable` with the detail `platform_unsupported`. So the
task calls into a Linux distribution under WSL where the daemon runs and the script is
installed. Set `PYTHONPATH` (and `RECURSION_CONTROL` only to name the interpreter) in the
routine file, as described under the routine file above. Under WSL the daemon may not be
running when the task fires (the
distribution can be idle, and its user session may not be up); in that case the firing
answers `control_api_unavailable`.

```
schtasks /Create /TN "recursion-routine-nightly-review" /SC DAILY /ST 03:00 /TR "wsl.exe -d Ubuntu -- /home/YOU/.local/bin/recursion-run-routine nightly-review"
```

Replace `Ubuntu` with your distribution's name and `YOU` with your user in it. `schtasks /Create` has no
flag for it, so for the catch-up that `Persistent=true` gives on Linux set "Run task as
soon as possible after a scheduled start is missed" in the task's settings or its XML;
the repeat carries the same key.

## What this does not do

- It does not check whether the last run is still running. If a run takes longer than
  the period, the next firing starts another one beside it.
- It has no stalled state. If a run waits for an approval that nobody answers, nothing
  here notices, reports it or ends it.
- It keeps no record of firings. The only record is the runs themselves, which you can
  read with `python -m recursion_core.control list`. A firing that was refused or never
  reached the daemon leaves only the timer's own log (`journalctl --user -u
  recursion-routine-nightly-review.service`).
- It has no webhooks and no way to start a run from an outside event.
- Outside a firing's `ROUTINE_WAIT` window for `control_api_unavailable`, it does not
  retry a failed firing itself; nothing is queued, and the next period's firing is the
  next try.

A routines plugin is the intended way to do all of these. Until it is available, this
page is a floor: small, built from the operating system's own parts, and safe to repeat.
