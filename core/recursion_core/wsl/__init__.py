"""WSL host facts (CSL-238). ``probe.read`` returns one frozen record."""

from recursion_core.wsl.probe import Record, read

__all__ = ['Record', 'read']
