"""The services and areas the agent offers; consumers can provide their own.

The flows read the catalog from ``Deps.catalog`` and pass it on to the tool schemas and to
``render.question``. ``DEFAULT_CATALOG`` is only the default used by constructors and configuration.
"""

from __future__ import annotations

from pydantic import Field

from booking_agent.domain.models import Frozen


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
