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

    **The gap between the two is older and wider than DEC-020**, and an earlier version of
    this docstring said otherwise — that availability had only ever narrowed the domain on
    `SessionState`, so anything the policy refused the matrix refused too. That was false when
    written, and the repository's own architecture test asserts it is false:
    `tests/architecture/test_policy_matches_domain.py::test_the_policy_is_a_subset_not_a_restatement_of_the_domain`
    requires the narrower-than-the-domain set to be **non-empty**. It holds three pairs:

        running        -> cleanup    (domain-legal, policy refuses)
        stop_requested -> cleanup    (domain-legal, policy refuses)
        orphaned       -> force      (the pair DEC-020 introduced)

    So this exception is not naming a gap DEC-020 opened; it is naming the first place the
    long-standing gap was closed. `SessionService.force_stop` and `SessionService.cleanup`
    both raise it now, each asking `available_actions` rather than restating the rule.

    What DEC-020 did change is *why* the domain cannot make the refusal for the ORPHANED
    pair: provenance lives on the record, and the matrix is a pure function of state, so
    there is no version of the transition table that could express it.
    """
