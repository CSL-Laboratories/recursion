# Architecture decision records

This directory is the durable, one-file-per-choice record of Recursion
architecture and direction. An ADR is written from a closed Linear issue
labelled `decision`. It does not replace or edit `state/DECISIONS.md`
(protected).

## When to write one

The Linear assignee writes the ADR when a Linear issue labelled `decision`
closes. The issue is the working record while it is open. Do not author an
ADR for an unresolved decision.

## File name

Copy [0000-template.md](0000-template.md) to `NNNN-short-title.md`.

- `NNNN` is the next unused four-digit number. `0000` is the template, not a
  decision.
- `short-title` is kebab-case.

Example: `0001-linux-keyring-backend.md`

## Link it from Linear

The ADR and the Linear issue must point at each other. Put the ADR path (or
the merged GitHub URL) on the closed `decision` issue. Put the Linear issue
URL in the ADR.

## Index

| ADR | Title | Linear | Status |
| --- | --- | --- | --- |
| [0000](0000-template.md) | Template | — | template |
