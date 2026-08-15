"""Typed errors exposed by the sealed application use-case surface."""


class DuplicateCommandError(ValueError):
    """A replayed idempotent command performed no new side effect."""


class SessionNotFoundError(LookupError):
    """The requested managed session is not known to the durable store."""


class ProjectCreationError(ValueError):
    """A project was never catalogued; any directory created for it is removed, or logged."""


class StopNotPermittedError(ValueError):
    """The action policy refuses this stop, though the lifecycle matrix would allow it.

    Distinct from `domain.state_machine.InvalidTransition`, which means the *domain* refuses.
    The two were the same thing until DEC-020: availability had always narrowed the domain on
    `SessionState` alone, so anything the policy refused the matrix refused too, and the
    service needed no check of its own. DEC-020 branches on `orphan_provenance`, which lives
    on the record and which the matrix — a pure function of state — cannot read. This names
    the gap that opened, rather than borrowing the domain's exception for a decision the
    domain did not make.
    """
