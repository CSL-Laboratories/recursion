# Agent install

An agent follows `llms.txt` at the repository root. Each step there is one `recursiond` command. The text after the arrow is the JSON that command prints on success, or the name of that JSON.

These steps run on Linux, once `recursiond` is the command. This page does not install an interpreter, and Recursion installs no engine. The root contributor guide is not this page, and these steps do not edit it.

## Steps

1. `recursiond doctor --json`

   Prints one `doctor_report` object (`generated_at` and `checks`). Each check has a `state` of `ok`, `warn`, `fail` or `unknown`. Exit 0 when no check is `fail`. Exit 1 when any check is `fail`. Unknown is not a failure. Stop on a `fail`, and do the next step that check names, before `provision`.

2. `recursiond provision`

   Prints one JSON object. Success is `"code": "ok"` (exit 0). That run writes the per-user unit, the stores and the route homes' custody root. It does not start the daemon, and it does not run the `systemctl --user` commands it lists under `next`. A second run, when those are already in place, changes nothing and prints `"code": "ok"` again.

3. `recursiond up`

   Prints one JSON object. Success is `"code": "ok"` and `"gui"` is the address to open, `http://127.0.0.1:8765` (exit 0). Open that address. Any other `code` means the GUI is not open.

4. One of `recursiond mcp-config --client codex`, `recursiond mcp-config --client claude` or `recursiond mcp-config --client cursor`

   Prints the Control MCP snippet for that client and does not write it. Codex prints TOML. Claude and Cursor each print one JSON object whose only key is `mcpServers`. Paste the snippet into the client yourself. The command does not write under `.codex`, `.claude` or `.cursor`.

   The snippet names server id `recursion-control` and launches `python -m recursion_core.control.mcp` with no arguments, using the same interpreter that ran `recursiond`.

## Answers

Every step's success answer is one JSON object, except the Codex snippet, which is the TOML `mcp-config` prints. A usage error exits 2 and prints no JSON. Off Linux every command exits 1 with `{"code": "platform_unsupported"}`.
