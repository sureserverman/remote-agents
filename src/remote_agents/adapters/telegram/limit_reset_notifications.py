"""The one sentence the owner reads when a provider wiped its meters ahead of schedule.

`Claude limits were reset early — 5h 91% → 2%, week 64% → 0%` — one message per provider per
event (DEC-097), naming every window that moved and what it moved from.

**Nothing here decides whether a reset happened.** `application/limit_resets.py` holds that
rule and holds it once (DEC-043, DEC-007); this module is handed the verdict and writes it down.
The division matters because the figure cannot be checked afterwards — by the time the owner
reads this, the window that was wiped is simply a window that has been wiped — so the sentence
and the judgement must not be able to disagree.

**The provider's display name is an argument, not a lookup, and that is load-bearing.** This
project already names providers in one place; spelling "Claude" here would make a second, free
to drift from the first. Passing it in makes the property structural rather than remembered —
the module *cannot* name a provider — and
`test_limit_reset_present_spells_no_provider_name_of_its_own` asserts exactly that over this
file's own source.

**Escaping happens here, at the boundary that decides the markup (DEC-014).** The window labels
are the provider's own text and the display name is resolved elsewhere, so neither is assumed
safe: an unbalanced `<` in either reaches a surface that parses HTML, which is a rendering fault
this project has already paid for once. `application/session_views.py` deliberately takes no
view on either surface's markup, which is why it is not done there.
"""

from __future__ import annotations

from html import escape

from remote_agents.application.limit_resets import EarlyReset
from remote_agents.application.session_views import whole_percent


def limit_reset_message(provider: str, resets: tuple[EarlyReset, ...]) -> str:
    """The sentence for one provider's early reset, or nothing when nothing reset.

    The empty answer is a real one rather than a guard against a caller mistake: the notifier
    asks per provider on every pass and most passes have nothing to say, and a headline with no
    windows under it would be an interruption reporting that nothing happened — which is the
    half of DEC-031 this kind was admitted under, not an exception to it.

    Percentages go through `whole_percent`, the same rule the limits block rounds with. A
    provider is free to publish `90.6`, and a figure rendered one way in a message and another
    way two screens along is two opinions about one number (DEC-043).
    """
    if not resets:
        return ""
    moved = ", ".join(
        f"{escape(reset.label)} {whole_percent(reset.previous_percent)}%"
        f" → {whole_percent(reset.current_percent)}%"
        for reset in resets
    )
    return f"{escape(provider)} limits were reset early — {moved}"
