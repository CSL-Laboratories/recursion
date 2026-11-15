"""Recursion TUI: a full-screen client of the control API (CSL-799, candidate). See README.md."""

from recursion_core.tui.app import Loop, TuiApp, call_api
from recursion_core.tui.protocol import COMMANDS, METHODS, SURFACE, CommandError, parse_command
from recursion_core.tui.sanitize import OutputSanitizer

__all__ = ["COMMANDS", "METHODS", "SURFACE", "CommandError", "Loop", "OutputSanitizer", "TuiApp", "call_api",
           "parse_command"]
