"""Typed errors exposed by the sealed application use-case surface."""


class DuplicateCommandError(ValueError):
    """A replayed idempotent command performed no new side effect."""


class SessionNotFoundError(LookupError):
    """The requested managed session is not known to the durable store."""


class ExternalSessionStillRunningError(RuntimeError):
    """The owner must exit the external source locally before safe handoff can begin."""


class ExternalSessionUnavailableError(LookupError):
    """Previously observed external evidence no longer matches on recheck."""
