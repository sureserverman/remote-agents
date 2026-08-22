"""How much of a conversation catalogue one page shows, and which agents may be offered.

Two values the resume flow needs before either surface can draw anything, and both were
written more than once. The page size existed three times — `_RESUME_PAGE_SIZE = 10` in
`adapters/tui/screens/resume.py`, an unread second copy of the same constant in
`adapters/tui/app.py`, and a bare positional `10` inside the bot's
`ConversationCatalogueQuery(page, 10, ...)`. The capability filter existed three times too:
inside the bot's per-profile condition in `_resume_profiles_reply`, and as the same anonymous
comprehension in both of the local surface's capability reads.

**Neither of these is a step in the flow, and this module deliberately does not contain one.**
The flow itself stays where it is: the bot composes its own unavailability reasons and mints
its own callback tokens, and the local surface keeps its reads inside the navigation guard
(DEC-024) with `advance_to` called from inside that block. What moves here is only what both
surfaces were answering separately and had no mechanism keeping in agreement — ARCH-B4's
shape, an already-decided input in and a value to render out, with no callback into a screen
and nothing imported from either frontend.

`resume_available` — whether a *conversation* may be reopened — is the third value in this
flow and it already lives beside `ConversationService` in `application/conversations.py`,
where it was centralized by BL-004. It stays there: it is a policy question about one record,
which is the division that entry's docstring already draws.
"""

from __future__ import annotations

from collections.abc import Iterable

from remote_agents.domain.conversations import ProfileResumeCapability

#: How many saved conversations one page of the resume catalogue holds, on every surface.
#:
#: One number rather than two because a surface is not the thing that decides it: the owner
#: pages through the same provider catalogue whichever way they reached it, and two surfaces
#: that page differently give the same conversation two different addresses. It stays inside
#: `ConversationCatalogueQuery`'s accepted 1..50 bound, which is checked by a test rather than
#: by inspection — a shared constant the shared query refuses would fail both surfaces at once.
RESUME_PAGE_SIZE = 10


def resume_capable(capability: ProfileResumeCapability) -> bool:
    """Whether an agent may be offered as somewhere to reopen a saved conversation.

    Both halves are required and the second is the one that is easy to lose:
    `catalogue_available` says the provider can *list* what it has saved, and
    `selected_resume_available` says a chosen one can actually be reopened. A provider that
    answers yes to the first and no to the second is a list of conversations none of which
    can be resumed, which is worse than an empty list because the owner presses one to find
    out.

    Truthful per profile, never a version allowlist — DEC-002, which is why this reads flags
    the provider answered rather than a table of what each agent is believed to support. This
    is the predicate only; the bot's unavailability wording composes `reason` around it and
    stays the bot's, because nothing on the local surface renders a refused agent at all.
    """
    return capability.catalogue_available and capability.selected_resume_available


def resume_capable_profiles(
    capabilities: Iterable[ProfileResumeCapability],
) -> tuple[ProfileResumeCapability, ...]:
    """The offerable subset of one capability reading, in the order the provider gave it.

    The form the local surface's two reads both wanted, written once. Order is the caller's:
    the agent list is rendered straight from this tuple, so sorting here would move rows under
    the owner's cursor between the fetch that opens the screen and the re-read on every way
    back to it.

    An empty answer is an ordinary state, not a failure — DEC-002 has the question asked of
    the host rather than tabled, so "no agent here can resume today" is a real reading, and
    both screens carry their own sentence for it.
    """
    return tuple(capability for capability in capabilities if resume_capable(capability))
