# Security policy

> **Draft.** Three things in it are Lucas's to decide and are left as placeholders:
> the reporting contact, the response times, and whether the scope below stands.
> The section on what the code defends is written from merged code before the
> first release. Until then this file is a draft and the repository has no release.

## Reporting a vulnerability

Please do not report a suspected vulnerability in a public issue, pull request or
discussion.

Report it privately to <contact: Lucas to name>.

Include what you can of:

- the commit you tested (`git rev-parse HEAD`), or the release once there are releases;
- the operating system and the Python build (`python -VV`);
- the steps that reproduce it, ideally as a script or a failing test;
- what an attacker gains, and what they need first (which user, which access).

We will acknowledge a report within <days: Lucas to set> and tell you what we
decided within <days: Lucas to set>. If you want credit, we name you when the fix
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

Proposed, for Lucas to confirm.

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
