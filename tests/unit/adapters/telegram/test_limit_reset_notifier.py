"""The fourth notifier: read limits, ask whether anything reset early, say it once — DEC-097.

It is the trust notifier's shape rather than the activity notifier's — a pass driven by a loop
of its own, holding what it could not deliver — and it is the first notifier whose subject is
**not a session**. DEC-031 required that; DEC-097 amends it for exactly this kind, on the
argument DEC-031 itself set: a wiped meter changes what the owner can do in the next hour.

**The baseline lives in memory and moves on every pass, including a pass whose send failed.**
That is what makes a reset news once. What is retried is the *message*, not the detection: once
the baseline has moved, the same wipe cannot be found again, so a refusal that lost the message
would lose it permanently. Three consecutive refusals abandon it with one journal line (DEC-049,
DEC-034), because a chat that cannot be reached is not evidence about this provider.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from remote_agents.adapters.telegram.flood import FloodGate
from remote_agents.adapters.telegram.limit_reset_notifications import LimitResetNotifier
from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import AgentLimits, UsageWindow

_START = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


class _Refused(Exception):
    """Stands in for telegram.error.TelegramError, which the notifier must treat as a refusal."""


class _View:
    """A recording `LiveView`; `send_apart` is the one method a notifier of this shape needs."""

    chat_id = 4242

    def __init__(self, refuse: int = 0, refuse_naming: str | None = None) -> None:
        self.sent: list[str] = []
        self._refuse = refuse
        #: Refuse only the messages naming this provider, so a pass can have one send succeed
        #: and another fail — the arrangement DEC-049's outage rule actually turns on.
        self._refuse_naming = refuse_naming

    async def send_apart(self, bot: object, arguments: dict[str, object]) -> int:
        text = str(arguments["text"])
        if self._refuse_naming is not None and text.startswith(self._refuse_naming):
            raise _Refused("this one is not deliverable")
        if self._refuse:
            self._refuse -= 1
            raise _Refused("chat unavailable")
        self.sent.append(text)
        return 100 + len(self.sent)


def _limits(profile: str, *windows: tuple[str, float, int], at: datetime) -> AgentLimits:
    """One provider's reading. Each window is (label, used_percent, hours until it resets)."""
    return AgentLimits(
        ProfileId(profile),
        tuple(
            UsageWindow(label, percent, at + timedelta(hours=hours))
            for label, percent, hours in windows
        ),
        observed_at=at,
        stale_source="usage API",
    )


def _notifier(view: _View, readings: list, *, flood: FloodGate | None = None):
    """A notifier over a scripted sequence of readings, one per pass."""
    ticks = iter(readings)
    moments = iter(_START + timedelta(minutes=n) for n in range(0, 600, 10))

    async def limits():
        reading = next(ticks)
        if isinstance(reading, Exception):
            raise reading
        return reading

    notifier = LimitResetNotifier(
        limits=limits,
        view=view,
        flood=flood,
        now=lambda: next(moments),
        name_for=lambda profile_id: profile_id.title(),
    )
    notifier.attach(object())
    return notifier


async def test_the_notifier_takes_a_baseline_on_its_first_pass_and_says_nothing() -> None:
    """There is a reading and nothing to compare it against; that is not news."""
    view = _View()
    notifier = _notifier(view, [(_limits("claude", ("5h", 91, 4), at=_START),)])

    await notifier.pass_once()

    assert view.sent == []


async def test_the_notifier_sends_one_message_per_provider_that_reset_early() -> None:
    view = _View()
    notifier = _notifier(
        view,
        [
            (
                _limits("claude", ("5h", 91, 4), ("week", 64, 40), at=_START),
                _limits("codex", ("5h", 30, 4), at=_START),
            ),
            (
                _limits("claude", ("5h", 2, 4), ("week", 0, 40), at=_START + timedelta(minutes=10)),
                _limits("codex", ("5h", 31, 4), at=_START + timedelta(minutes=10)),
            ),
        ],
    )

    await notifier.pass_once()
    await notifier.pass_once()

    assert view.sent == ["Claude limits were reset early — 5h 91% → 2%, week 64% → 0%"], (
        "codex did not reset, so it must not be spoken about"
    )


async def test_the_notifier_does_not_repeat_itself_once_the_baseline_has_moved() -> None:
    """A reset is news once. The third pass sees the same figures the second one left."""
    wiped = _limits("claude", ("5h", 2, 4), at=_START + timedelta(minutes=10))
    view = _View()
    notifier = _notifier(
        view,
        [
            (_limits("claude", ("5h", 91, 4), at=_START),),
            (wiped,),
            (_limits("claude", ("5h", 2, 4), at=_START + timedelta(minutes=20)),),
        ],
    )

    await notifier.pass_once()
    await notifier.pass_once()
    await notifier.pass_once()

    assert len(view.sent) == 1


async def test_the_notifier_keeps_its_baseline_when_a_read_fails_or_answers_empty() -> None:
    """A failed read is not a reading, and must not be able to manufacture a wipe.

    Both shapes are tested together because they fail the same way if the baseline is
    overwritten: the next real reading would be compared against nothing (silence, tolerable)
    or against an empty tuple read as 0% (a fabricated reset, not tolerable).
    """
    view = _View()
    notifier = _notifier(
        view,
        [
            (_limits("claude", ("5h", 91, 4), at=_START),),
            RuntimeError("the reader fell over"),
            (),
            (_limits("claude", ("5h", 2, 4), at=_START + timedelta(minutes=30)),),
        ],
    )

    await notifier.pass_once()
    await notifier.pass_once()
    await notifier.pass_once()
    assert view.sent == [], "a failed or empty read spoke"

    await notifier.pass_once()
    assert len(view.sent) == 1, "the baseline from before the failed reads was not kept"


async def test_the_notifier_abandons_a_message_after_three_refusals_with_one_journal_line(
    caplog,
) -> None:
    """DEC-049's three strikes — the half that fires, and the arrangement it needs to fire in.

    **This test used to have one provider refusing into the void, and it passed against a
    notifier that struck unconditionally — which was the defect.** A refusal is evidence about
    the message only when the channel is demonstrably working, so a lone pending provider can
    never be struck: there is nothing to tell an unsendable message apart from an unreachable
    chat. Codex therefore drops eleven points every pass, delivering each time, and Claude's
    refusals are then genuinely about Claude's message.

    What is retried is the message, not the detection: the baseline moves on the pass that
    detects, so the wipe can never be found again and a refusal that dropped the sentence would
    drop it for good. Three consecutive strikes give up on it, once, and a later pass must not
    still be trying.
    """
    view = _View(refuse_naming="Claude")
    moment = _START
    codex = [91, 80, 69, 58, 47, 36]
    claude = [91, 2, 2, 2, 2, 2]
    readings = [
        (
            _limits("claude", ("5h", claude[n], 4), at=moment + timedelta(minutes=10 * n)),
            _limits("codex", ("5h", codex[n], 4), at=moment + timedelta(minutes=10 * n)),
        )
        for n in range(6)
    ]
    notifier = _notifier(view, readings)

    with caplog.at_level(logging.WARNING):
        for _ in readings:
            await notifier.pass_once()

    given_up = [r for r in caplog.records if "giving up" in r.getMessage()]
    assert len(given_up) == 1, f"expected exactly one journal line, got {len(given_up)}"
    assert "claude" in given_up[0].getMessage()
    # The provider whose sends worked is untouched by the other's abandonment — `_abandoned` is
    # per provider, and a wipe of Codex's must still be reportable afterwards.
    assert len([t for t in view.sent if t.startswith("Codex")]) == len(readings) - 1


async def test_the_notifier_holds_its_send_while_the_chat_is_flood_banned() -> None:
    """The chat-wide "not yet" is shared, and spending a request during a ban extends it.

    The ban that made this rule cost this project nearly six hours on 2026-09-13, because
    three senders each discovered it separately and each kept going.
    """
    flood = FloodGate()
    flood.hold_off(60)
    view = _View()
    notifier = _notifier(
        view,
        [
            (_limits("claude", ("5h", 91, 4), at=_START),),
            (_limits("claude", ("5h", 2, 4), at=_START + timedelta(minutes=10)),),
        ],
        flood=flood,
    )

    await notifier.pass_once()
    await notifier.pass_once()

    assert view.sent == [], "the notifier sent into a flood ban"


def _two_providers(claude: float, codex: float, *, at: datetime):
    return (
        _limits("claude", ("5h", claude, 4), at=at),
        _limits("codex", ("5h", codex, 4), at=at),
    )


async def test_the_notifier_records_no_strike_on_a_pass_where_nothing_got_through(
    caplog,
) -> None:
    """DEC-049's actual clause, which the first version of this class cited and did not have.

    A refusal is evidence about *this message* only when the channel is demonstrably working.
    When nothing got through, the refusals are evidence about an outage instead — and counting
    them abandons a standing message over a Telegram hiccup. At a 300 s cadence three strikes
    is fifteen minutes from a wipe being reported to it being silently unreportable, and
    `_abandoned` is keyed on the **provider** and never cleared, so "silently" would have meant
    for the life of the process.

    Found by Stage 2's Tier-2 review, against `trust_notifications.py`, which implements the
    rule correctly and whose own comment spells out the fifteen-second version of this sum.
    """
    view = _View(refuse=99)
    moment = _START
    readings = [_two_providers(91, 30, at=moment)]
    for step in range(1, 8):
        readings.append(_two_providers(2, 30, at=moment + timedelta(minutes=10 * step)))
    notifier = _notifier(view, readings)

    with caplog.at_level(logging.WARNING):
        for _ in readings:
            await notifier.pass_once()

    assert not [r for r in caplog.records if "giving up" in r.getMessage()], (
        "a total outage was counted as evidence about the provider"
    )


async def test_a_fresh_detection_does_not_inherit_the_previous_events_refusals(caplog) -> None:
    """A new wipe replacing an undelivered one starts its own count, not the old one's.

    The design already accepts that a second detection *replaces* the first — the later sentence
    is the truer one. What it must not do is arrive two-thirds of the way through somebody
    else's three strikes and be abandoned on its first real attempt at delivery.

    **Codex is here to make strikes possible at all**, which is the DEC-049 rule from the test
    above doing its job: with only Claude pending, nothing else delivers, no refusal is ever
    evidence about the message, and the counter this test is about never moves. So Codex drops
    eleven points every pass — a detection and a successful send each time — and Claude's
    refusals are then genuinely about Claude's message.

    Claude's first wipe is refused once; its second wipe arrives on the next pass and replaces
    it. Counting from zero, the fourth pass is only the second strike against the new message,
    so nothing may have been abandoned yet. Inheriting the old count, it would be the third.
    """
    view = _View(refuse_naming="Claude")
    moment = _START
    codex = [91, 80, 69, 58, 47]
    claude = [91, 40, 2, 2, 2]
    readings = [
        (
            _limits("claude", ("5h", claude[n], 4), at=moment + timedelta(minutes=10 * n)),
            _limits("codex", ("5h", codex[n], 4), at=moment + timedelta(minutes=10 * n)),
        )
        for n in range(5)
    ]
    notifier = _notifier(view, readings)

    with caplog.at_level(logging.WARNING):
        for _ in range(4):
            await notifier.pass_once()

    assert [t for t in view.sent if t.startswith("Codex")], "codex never delivered"
    assert not [r for r in caplog.records if "giving up" in r.getMessage()], (
        "the second wipe inherited the first event's refusals and was abandoned early"
    )
