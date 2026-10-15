"""Load and write the version-1 harness config."""

from __future__ import annotations

import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from scripts.v3.harness.errors import HarnessError
from scripts.v3.harness.providers import adapters
from scripts.v3.harness.usage import default_budgets


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = ROOT / "config" / "harness.json"
SCHEMA_PATH = ROOT / "config" / "harness.schema.json"
_SECRET_KEY = re.compile(
    r"(?i)^(api[_-]?key|token|secret|password|authorization|credential|xai_api_key)$"
)


def default_config_path() -> Path:
    return DEFAULT_CONFIG


def load_config(path: Path | None = None) -> dict:
    target = DEFAULT_CONFIG if path is None else Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"cannot read config {target}") from exc
    return validate_config(payload)


def validate_config(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise HarnessError("config must be a JSON object")
    _reject_secret_keys(payload)
    _check_schema(payload)
    known = adapters()
    names = []
    providers = []
    for item in payload["providers"]:
        name = item["name"]
        if name in names:
            raise HarnessError(f"duplicate provider {name!r}")
        names.append(name)
        cls = known.get(name)
        if cls is None:
            raise HarnessError(f"unknown provider {name!r}")
        if tuple(item["capabilities"]) != cls.CAPABILITIES:
            raise HarnessError(f"{name} capabilities do not match the adapter")
        if tuple(item["argv"]) != cls.STATIC_ARGV:
            raise HarnessError(f"{name} argv does not match the adapter")
        providers.append(
            {
                "argv": list(cls.STATIC_ARGV),
                "capabilities": list(cls.CAPABILITIES),
                "enabled": item["enabled"],
                "name": name,
                "timeout_seconds": item["timeout_seconds"],
            }
        )
    preference = list(payload["routing"]["preference"])
    if sorted(preference) != sorted(names):
        raise HarnessError("routing.preference must list each provider once")
    budgets = payload["budgets"]
    return {
        "budgets": {
            "max_cost": budgets["max_cost"],
            "max_input_tokens": budgets["max_input_tokens"],
            "max_output_tokens": budgets["max_output_tokens"],
        },
        "providers": providers,
        "review": {
            "accept_unreviewed": payload["review"]["accept_unreviewed"],
            "max_rounds": payload["review"]["max_rounds"],
        },
        "routing": {
            "preference": preference,
            "required_capabilities": list(payload["routing"]["required_capabilities"]),
        },
        "version": 1,
    }


def default_enabled() -> set[str]:
    """Providers a new config turns on. Live CLIs stay off."""

    return {name for name, cls in adapters().items() if getattr(cls, "OFFLINE", False)}


def starter_config(enabled: set[str]) -> dict:
    """Build a version-1 config. ``enabled`` names the providers that start on."""

    known = adapters()
    unknown = sorted(enabled - set(known))
    if unknown:
        raise HarnessError(f"unknown providers: {unknown}")
    budgets = default_budgets()
    return {
        "version": 1,
        "review": {"accept_unreviewed": False, "max_rounds": 2},
        "budgets": {
            "max_input_tokens": budgets["max_input_tokens"],
            "max_output_tokens": budgets["max_output_tokens"],
            "max_cost": budgets["max_cost"],
        },
        "routing": {
            "required_capabilities": ["execute"],
            "preference": list(known),
        },
        "providers": [
            {
                "name": name,
                "enabled": name in enabled,
                "capabilities": list(cls.CAPABILITIES),
                "argv": list(cls.STATIC_ARGV),
                "timeout_seconds": 120,
            }
            for name, cls in known.items()
        ],
    }


def write_starter(path: Path, enabled: set[str], *, force: bool = False) -> dict:
    """Write a starter config. An existing file is left in place without ``force``."""

    target = Path(path)
    if target.is_dir():
        raise HarnessError(f"{target} is a directory")
    if target.exists() and not force:
        raise HarnessError(f"refusing to overwrite {target}; pass --force")
    payload = starter_config(enabled)
    validate_config(payload)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise HarnessError(f"cannot write {target}: {exc.strerror or 'filesystem error'}") from exc
    return payload


def _check_schema(payload: dict) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        Draft202012Validator(schema).validate(payload)
    except ValidationError as exc:
        loc = ".".join(str(part) for part in exc.absolute_path) or "config"
        raise HarnessError(f"config {loc}: {exc.message}") from exc


def _reject_secret_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _SECRET_KEY.fullmatch(str(key)):
                raise HarnessError("config must not contain credential fields")
            _reject_secret_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_keys(item)
