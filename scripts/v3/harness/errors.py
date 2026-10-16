"""Failures a caller can report without a traceback."""


class HarnessError(Exception):
    """A refused or failed harness operation. The message is safe to print."""


class LiveProviderDisabled(HarnessError):
    """A headless CLI was requested without ``--allow-live``."""


class ProviderTimeout(HarnessError):
    """A provider process did not finish within its timeout."""


class ProviderAvailability(HarnessError):
    """The provider cannot take work until a cooldown ends.

    ``reason`` is ``rate-limited``, ``quota-exhausted``, or ``auth-expired``.
    The process output is not stored on the exception.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        until: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.until = until
        self.retry_after = retry_after


class ProviderRateLimited(ProviderAvailability):
    """The provider asked the harness to slow down."""

    def __init__(self, message: str, *, until: str | None = None, retry_after: float | None = None) -> None:
        super().__init__(message, reason="rate-limited", until=until, retry_after=retry_after)


class ProviderQuotaExhausted(ProviderAvailability):
    """The provider reported that its quota is exhausted."""

    def __init__(self, message: str, *, until: str | None = None, retry_after: float | None = None) -> None:
        super().__init__(message, reason="quota-exhausted", until=until, retry_after=retry_after)


class ProviderAuthExpired(ProviderAvailability):
    """The provider rejected the attempt because sign-in is no longer valid."""

    def __init__(self, message: str, *, until: str | None = None, retry_after: float | None = None) -> None:
        super().__init__(message, reason="auth-expired", until=until, retry_after=retry_after)
