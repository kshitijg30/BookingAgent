"""Optional demo-control port, separate from the operational booking provider."""

from __future__ import annotations

from collections import deque
from typing import Protocol

from booking_agent.provider.scenario import BookOutcome, Scenario


class DemoControls(Protocol):
    scenario: Scenario
    forced_book: deque[BookOutcome]
    forced_status_timeouts: int

    def provider_cancel(self, booking_id: str) -> None: ...
