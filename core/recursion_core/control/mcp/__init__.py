"""The Control MCP server (lane MCP). See README.md in this folder.

First slice: a stdio server for the four run verbs (``run.start``, ``run.status``, ``run.list``,
``run.stop``) in MCP protocol revision 2025-06-18, forwarding each tool call to the daemon's owner-only
control socket. It refuses to start unless the socket check passes. There are no caller tokens, no scopes
and no Tasks yet, and nothing here is registered with the daemon.

Run it with ``python -m recursion_core.control.mcp``.
"""
