"""Pick an enabled provider that has the required capabilities.

Higher ``routing.weights`` run first. Equal weights keep preference order.
The weights are the user's explicit order. They are not a hidden score of
responsiveness, subscription efficiency, and completion. This is deterministic
and does not call a model. It is not the frozen M1B route admission.
"""

from __future__ import annotations

from scripts.v3.harness.errors import HarnessError


def capable_providers(config: dict) -> list[str]:
    """Enabled providers that advertise every required capability, highest weight first."""

    required = set(config["routing"]["required_capabilities"])
    by_name = {item["name"]: item for item in config["providers"]}
    preference = list(config["routing"]["preference"])
    chosen = []
    for name in preference:
        item = by_name[name]
        if item["enabled"] and required.issubset(item["capabilities"]):
            chosen.append(name)
    weights = config["routing"].get("weights") or {}
    return order_providers(chosen, preference, weights)


def order_providers(names: list[str], preference: list[str], weights: dict) -> list[str]:
    """Higher weight first. Equal weights keep preference order."""

    rank = {name: index for index, name in enumerate(preference)}
    return sorted(
        names,
        key=lambda name: (-int(weights.get(name, 1)), rank.get(name, len(rank))),
    )


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
