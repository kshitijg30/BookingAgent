"""Stable core errors; adapters translate vendor failures into these types."""


class ProviderError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProviderTimeout(Exception):
    """The operation may have taken effect; never infer failure from this exception."""


class VersionConflict(Exception):
    """Optimistic persistence version no longer matches."""


class RequestConflict(ValueError):
    """An idempotency key or proposal reference conflicts with current state."""


class AccessDenied(PermissionError):
    """The authenticated caller does not own the requested resource."""


class EnqueueFailed(RuntimeError):
    """Confirming a quote could not be saved together with its job. Nothing was queued; the user can retry."""

    def __init__(self, quote_id: str) -> None:
        super().__init__(f"could not queue the write for quote {quote_id}")
        self.quote_id = quote_id
