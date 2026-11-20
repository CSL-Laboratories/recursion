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
  socket are Linux only at this commit.
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
each tenant. A second `start` with the same tenant and key does not launch again: it
answers the first run, with `"duplicate": true`. The key is tied to the engine and to
the prompt (kept as a digest), so the same key with a different engine or prompt is
refused with `request_invalid` / `client_key_reused` and nothing is launched. The key
survives a daemon restart.

So the key must name the routine and the period, not the moment. A key made from the
routine's name and the date (for a daily routine) is the same for every firing that day.
A key made from the current second would be different every time and would defeat the
purpose.

## The routine as a small file

A routine is a file of four lines at `~/.config/recursion/routines/NAME.env`. For a
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

The file is read by the shell, so put the prompt on one line inside single quotes (a
prompt that holds a single quote needs it written as `'\''`). Put only text you trust in
it: the file is run as shell text with your rights.

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
slot=$(date +"$ROUTINE_SLOT")
# RECURSION_CONTROL is left unquoted on purpose: it is a command and its arguments.
exec ${RECURSION_CONTROL:-python3 -m recursion_core.control} start \
  --tenant "$ROUTINE_TENANT" --engine "$ROUTINE_ENGINE" \
  --prompt "$ROUTINE_PROMPT" --client-key "$name-$slot"
```

The key is `NAME-SLOT`, for example `nightly-review-20261120`. Run it twice by hand
and the second answer carries `"duplicate": true`.

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

Check the files, then turn the timer on:

```
systemd-analyze --user verify recursion-routine-nightly-review.service recursion-routine-nightly-review.timer
systemctl --user daemon-reload
systemctl --user enable --now recursion-routine-nightly-review.timer
systemctl --user list-timers recursion-routine-nightly-review.timer
```

To try it without waiting for 03:00, start the service by hand, twice:
`systemctl --user start recursion-routine-nightly-review.service`. The second start
makes no second run. If the daemon is down the service fails and
`systemctl --user status recursion-routine-nightly-review.service` shows the
`control_api_unavailable` answer; nothing is queued, and the next firing tries again.

The service ends when `start` returns. The daemon starts the run in its own unit, so
the service ending does not stop the run.

## macOS: launchd (beta, not run)

This form is written from launchd's documented format and has not been run. At this
commit the daemon and its control socket are Linux only, so on a Mac the command answers
`control_api_unavailable` unless it reaches a daemon on a Linux machine by some route of
your own. The form below is the scheduling half only.

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
installed:

```
schtasks /Create /TN "recursion-routine-nightly-review" /SC DAILY /ST 03:00 /TR "wsl.exe -d Ubuntu -- /home/YOU/.local/bin/recursion-run-routine nightly-review"
```

Replace `Ubuntu` with your distribution's name and `YOU` with your user in it. Use the
task's setting "Run task as soon as possible after a scheduled start is missed" for the
catch-up that `Persistent=true` gives on Linux; the repeat carries the same key.

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
- It does not retry a failed firing itself; the next period's firing is the next try.

A routines plugin is the intended way to do all of these. Until it is available, this
page is a floor: small, built from the operating system's own parts, and safe to repeat.
