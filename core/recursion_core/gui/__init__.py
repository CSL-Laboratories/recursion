"""Recursion's daemon-served operator GUI (PRODUCT.md O68, Recursion bar 4); no Tauri shell.

``surface.Application`` is a WSGI app over ``client.ControlClient``; ``inventory`` holds
the route rubric and the O50 beta tag. No order path, no own Stop, no socket binding.
Cockpit operator screens are plugin panels in this GUI and do not live here.
"""

from .surface import Application
from .composition import compose

__all__ = ["Application", "compose"]
