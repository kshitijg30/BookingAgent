"""Reusable booking operations. Optional integrations live in booking_agent.adapters."""

from booking_agent.engine import (
    BookingEngine,
    BookingRequest,
    BookingResult,
    ConfirmQuote,
    EngineConfig,
    SelectSlot,
)
from booking_agent.errors import (
    AccessDenied,
    EnqueueFailed,
    ProviderError,
    ProviderTimeout,
    RequestConflict,
    VersionConflict,
)
from booking_agent.memory_repository import MemoryRepository
from booking_agent.ports import Repository
from booking_agent.provider.contract import (
    AvailabilityProvider,
    BookingProvider,
    ChangeProvider,
    ProviderAPI,
    ProviderCapabilities,
)

__version__ = "0.2.0"
__all__ = [
    "AccessDenied",
    "AvailabilityProvider",
    "BookingEngine",
    "BookingProvider",
    "BookingRequest",
    "BookingResult",
    "ChangeProvider",
    "ConfirmQuote",
    "EngineConfig",
    "EnqueueFailed",
    "MemoryRepository",
    "ProviderAPI",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderTimeout",
    "Repository",
    "RequestConflict",
    "SelectSlot",
    "VersionConflict",
]
