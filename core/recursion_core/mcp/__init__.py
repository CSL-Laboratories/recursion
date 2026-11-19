"""The core's own MCP code, standard library only.

``client/`` is the MCP client of tool servers (protocol core over a pair of byte streams). The Control MCP
server is ``recursion_core.control.mcp``; the client reads and writes lines with that server's
``framing`` and ``jsonrpc`` modules, by import.
"""
