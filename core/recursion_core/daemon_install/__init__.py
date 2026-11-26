"""The per-user daemon's systemd user unit: install, upgrade, uninstall (lane INSTALL).

Ported from branch cursor/csl-758-daemon-install-b4bd (59b96a7b, CSL-758), not merged; see README.md
for what changed. It writes files under the roots the caller passes and returns the
``systemctl --user`` commands that apply them. It runs none of them.
"""

from .errors import InstallRefused
from .install import Plan, install, leftovers, plan_commands, uninstall, upgrade
from .layout import Owned, Roots, owned_paths
from .unit import DAEMON_MODULE, MARKER, STOP_TIMEOUT_S, UNIT_NAME, daemon_exec_args, render_unit, service_fields

__all__ = [
    "DAEMON_MODULE",
    "InstallRefused",
    "MARKER",
    "Owned",
    "Plan",
    "Roots",
    "STOP_TIMEOUT_S",
    "UNIT_NAME",
    "daemon_exec_args",
    "install",
    "leftovers",
    "owned_paths",
    "plan_commands",
    "render_unit",
    "service_fields",
    "uninstall",
    "upgrade",
]
