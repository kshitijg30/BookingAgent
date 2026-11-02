"""The only path from the agent to the provider.

- ``calls``: ``ProviderCalls`` runs one provider call at a time, charged to the turn budget and traced.
- ``verify``: asking the provider what happened to a write (``get_booking_status``, read-back through
  ``list_bookings``) and the cancel call with its retry on a transient 503.

The write protocol itself (which key, what to do with each answer) lives in ``jobs/``.
"""

from booking_agent.gateway.calls import ProviderCalls
from booking_agent.gateway.verify import BookStatus, ReadBack, cancel_with_retry, check_book_status, read_back

__all__ = ["BookStatus", "ProviderCalls", "ReadBack", "cancel_with_retry", "check_book_status", "read_back"]
