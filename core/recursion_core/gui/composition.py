"""The composition boundary: plugins provide plain registry() values, never core imports."""
from .surface import Application


def compose(client, door, hosts, origins, *, plugins=()):
    required, panels = {}, []
    for plugin in plugins:
        for cls, subjects in plugin.get("required", {}).items():
            required.setdefault(cls, []).extend(subjects)
        panels.extend(plugin.get("panels", ()))
    return Application(client, door, hosts, origins, required=required, panels=panels)
