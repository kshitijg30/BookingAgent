"""Background jobs: a small queue worker that runs provider writes, slot research and memory updates.

``outbox`` writes the jobs a turn queued, ``registry`` maps a kind to its handler, ``worker`` claims due jobs and
runs them. Handlers: ``book``, ``change`` (cancel, reschedule), ``check`` (settle a write whose job died),
``research`` (find other slots), ``memory`` (update remembered facts). ``write`` holds the shared outcome rules
of the write jobs and ``dead`` what happens when a job runs out of attempts.
"""
