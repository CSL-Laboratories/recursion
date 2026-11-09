"""Linux user-unit install, upgrade and uninstall (CSL-758, F61).

Candidate code. It writes files under caller-supplied directories and returns
``systemctl --user`` commands. It does not run those commands.
"""

from .errors import InstallRefused
from .install import GRANT_SCHEMA, Plan, install, leftovers, plan_commands, uninstall, upgrade
from .layout import Owned, Roots
from .unit import UNIT_NAME, crash_restart, render_unit, service_fields

__all__ = [
    "GRANT_SCHEMA",
    "InstallRefused",
    "Owned",
    "Plan",
    "Roots",
    "UNIT_NAME",
    "crash_restart",
    "install",
    "leftovers",
    "plan_commands",
    "render_unit",
    "service_fields",
    "uninstall",
    "upgrade",
]
