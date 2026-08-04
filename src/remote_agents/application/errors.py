"""Typed errors exposed by the sealed application use-case surface."""


class DuplicateCommandError(ValueError):
    """A replayed idempotent command performed no new side effect."""


class SessionNotFoundError(LookupError):
    """The requested managed session is not known to the durable store."""


class ProjectCreationError(ValueError):
    """A project could not be created and catalogued, leaving no partial registration."""
