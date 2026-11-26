"""Recursion's own model loop, run as an engine process (lane LOOP). See README.md.

The loop is an engine like the CLIs the core drives: it runs in its own process (never inside the daemon),
reads its inputs as JSON lines on standard input, writes typed JSON lines on standard output, and takes its
provider key and base URL only from its environment. Nothing in this package imports the daemon, the control
plane or a vendor SDK.
"""
