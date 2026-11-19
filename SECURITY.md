# Security policy

> **Draft.** Three things in it are left to decide and are marked as placeholders:
> the security mailbox's address, the response times, and whether the scope below
> stands. The private vulnerability reporting switch is turned on in the settings
> of each public repository before the first release; until then the first way of
> reporting below may not be open. The section on what the code defends is written
> from merged code before the first release. Until then this file is a draft and
> the repository has no release.

## Reporting a vulnerability

Please do not report a suspected vulnerability in a public issue, pull request or
discussion. Report it privately, in one of two ways.

First, on GitHub: open this repository's **Security** tab and choose **Report a
vulnerability**. That is GitHub's private vulnerability reporting, which keeps the
report out of public view. If the button is not shown, private reporting is not
switched on for this repository yet: use the second way.

Second, if you have no GitHub account, or the button is not shown, write to the
project's security mailbox, a role address on the project's own domain.
Report it privately to <role mailbox address on the project's domain: to be filled in before the first release>.

Include what you can of:

- the commit you tested (`git rev-parse HEAD`), or the release once there are releases;
- the operating system and the Python build (`python -VV`);
- the steps that reproduce it, ideally as a script or a failing test;
- what an attacker gains, and what they need first (which user, which access).

We will acknowledge a report within <days: to be set before the first release>
and tell you what we decided within <days: to be set before the first release>. If you want credit, we name you when the fix
is released.

Please do not test against other people's machines or accounts, and do not use real
credentials, keys or tokens in a report: made-up values reproduce the same code paths.

## Supported versions

Recursion has no release yet. Until the first release, only the `master` branch
receives security fixes. After it, fixes go to the latest release.

| Version  | Security fixes |
|----------|----------------|
| `master` | yes            |

## Scope

Proposed; to be confirmed before the first release.

In scope: the code in this repository, which runs as one user's own daemon on that
user's computer: the daemon and its control socket, its loopback page, the run
launcher and Stop, the plugin signature and grant checks, the environment given to
the engines it drives, and the command-line and terminal clients.

Out of scope:

- the engines Recursion drives (for example Claude Code and Codex) and their vendors'
  services: please report those to their vendors;
- plugins that are not published in this repository;
- an attacker who already runs code as the same user as the daemon, unless the report
  shows them crossing a boundary this repository's documents claim, such as a run
  outliving Stop or a plugin acting beyond its grants.

## What the code defends, and what it does not yet

To be written from merged code before the first release, each statement checked
against the code at a named commit and each gap marked "not yet".
