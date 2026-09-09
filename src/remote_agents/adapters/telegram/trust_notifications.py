"""The folder-trust question as a standing message, and what it becomes once answered.

**One renderer, two callers.** The launch reply and the notification sent on its own say the same
thing about the same session, and saying it twice would be two wordings to keep in step — so
`_launch_reply` renders through here rather than beside it.

Barless by construction (DEC-032). The navigation bar is appended at one choke point, and a
notification is a message rather than a screen: pressing "Sessions" on a message that outlives
the screen it was sent from is a different act from pressing it on the screen itself. These
call `render_message` directly, which is the mechanism and not an oversight.

**No filesystem path, which is a deliberate departure from the plan's sketch.** That asked for
the folder in `<code>` beneath the headline, and the bot cannot honour it: `CatalogProject`
carries `opaque_id`, `name`, `area` and `group` and no path, and this surface renders host
paths nowhere. The session's display identity is what every other message here names it by,
so it is what this names it by too.
"""

from __future__ import annotations

from remote_agents.adapters.telegram.presenters import (
    Button,
    RenderedMessage,
    _validate_callback,
    render_message,
)
from remote_agents.domain.models import SessionRecord, SessionState

TRUST_LABEL = "✅ Trust this project"
DECLINE_LABEL = "⛔ Don't trust — close it"
OPEN_LABEL = "Open session"

_HEADLINE = "🔒 <b>Waiting to be trusted</b>"


def render_trust_question(
    record: SessionRecord,
    *,
    answerable: bool,
    trust: str | None = None,
    decline: str,
) -> RenderedMessage:
    """The question, with the answers this project can actually give for that agent.

    `answerable` is the profile half of the policy, decided by the caller rather than here:
    saying *yes* means typing into the agent's own dialog and is confined to the agents whose
    dialog this project reads, while saying *no* ends a session that never started and is
    available for all of them.

    **One button per row.** DEC-032 gives the two-wide shape to the stop row, which is the one
    place on this surface where two buttons sit side by side; a pair of answers drawn the same
    way would read as a pair of stops.
    """
    if answerable and trust is None:
        raise ValueError("an answerable trust question needs a trust callback")
    rows: list[tuple[Button, ...]] = []
    if answerable and trust is not None:
        _validate_callback(trust)
        rows.append((Button(TRUST_LABEL, trust),))
    _validate_callback(decline)
    rows.append((Button(DECLINE_LABEL, decline),))
    return render_message(
        f"{_HEADLINE}\n{record.display.rendered}\n"
        "The agent is asking whether this folder can be trusted. "
        "Nothing runs until you answer.",
        tuple(rows),
    )


def render_trust_settled(record: SessionRecord, *, open_session: str) -> RenderedMessage:
    """What the standing question becomes once it has an answer.

    **An amendment carries its keyboard** (DEC-034), so a trusted session's message keeps a way
    into the session it is about rather than becoming a paragraph the owner can do nothing
    with. A declined one carries none, and that is the same rule rather than an exception to
    it: there is no longer a session to open, and a button that answers "no longer available"
    is worse than no button.
    """
    if record.state is SessionState.ENDED:
        return render_message(f"⛔ <b>Closed without trusting.</b>\n{record.display.rendered}")
    _validate_callback(open_session)
    return render_message(
        f"✅ <b>Trusted. The agent is running.</b>\n{record.display.rendered}",
        ((Button(OPEN_LABEL, open_session),),),
    )
