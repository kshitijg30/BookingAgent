"""The services, areas, labels and timezone the agent works with. Needs only pydantic (the minimal install)."""

from __future__ import annotations

from pydantic import Field

from booking_agent.domain.entities import Frozen


class ServiceCatalog(Frozen):
    services: tuple[str, ...] = ("plumber", "electrician", "ac_service", "carpenter", "cleaning")
    areas: tuple[str, ...] = ("Koramangala", "Indiranagar", "HSR Layout", "Whitefield", "Jayanagar")
    labels: dict[str, str] = Field(
        default_factory=lambda: {
            "plumber": "Plumbing",
            "electrician": "Electrical",
            "ac_service": "AC servicing",
            "carpenter": "Carpentry",
            "cleaning": "Cleaning",
        }
    )
    timezone: str = "Asia/Kolkata"


DEFAULT_CATALOG = ServiceCatalog()
