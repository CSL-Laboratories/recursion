"""Failures a caller can report without a traceback."""


class HarnessError(Exception):
    """A refused or failed harness operation. The message is safe to print."""


class LiveProviderDisabled(HarnessError):
    """A headless CLI was requested without ``--allow-live``."""


class ProviderTimeout(HarnessError):
    """A provider process did not finish within its timeout."""
