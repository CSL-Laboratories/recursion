"""Pick an enabled provider that has the required capabilities.

Order is the config preference list. This is deterministic and does not call
a model. It is not the frozen M1B route admission.
"""

from __future__ import annotations

from scripts.v3.harness.errors import HarnessError


def capable_providers(config: dict) -> list[str]:
    """Enabled providers that advertise every required capability, in preference order."""

    required = set(config["routing"]["required_capabilities"])
    by_name = {item["name"]: item for item in config["providers"]}
    chosen = []
    for name in config["routing"]["preference"]:
        item = by_name[name]
        if item["enabled"] and required.issubset(item["capabilities"]):
            chosen.append(name)
    return chosen


def select_route(config: dict, provider: str | None = None) -> tuple[str, list[str]]:
    """Return ``(worker, reviewers)``.

    Reviewers are the other capable providers, so a review is not the same
    process that did the work.
    """

    capable = capable_providers(config)
    if not capable:
        raise HarnessError("no enabled provider has the required capabilities")
    if provider is None:
        worker = capable[0]
    elif provider not in capable:
        raise HarnessError(f"provider {provider!r} is not an enabled capable provider")
    else:
        worker = provider
    reviewers = [name for name in capable if name != worker]
    return worker, reviewers
