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

    def __init__(self, refuse: int = 0) -> None:
        self.sent: list[str] = []
        self._refuse = refuse

    async def send_apart(self, bot: object, arguments: dict[str, object]) -> int:
        if self._refuse:
            self._refuse -= 1
            raise _Refused("chat unavailable")
        self.sent.append(str(arguments["text"]))
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
    """DEC-049's three strikes, and the retry that makes them necessary.

    The baseline moves on the pass that detects, so the wipe can never be found again — the
    message is therefore held and retried rather than re-derived. Three consecutive refusals
    give up on it, once, and a fourth pass must not still be trying.
    """
    view = _View(refuse=99)
    steady = [
        (_limits("claude", ("5h", 91, 4), at=_START),),
        (_limits("claude", ("5h", 2, 4), at=_START + timedelta(minutes=10)),),
        (_limits("claude", ("5h", 2, 4), at=_START + timedelta(minutes=20)),),
        (_limits("claude", ("5h", 2, 4), at=_START + timedelta(minutes=30)),),
        (_limits("claude", ("5h", 2, 4), at=_START + timedelta(minutes=40)),),
        (_limits("claude", ("5h", 2, 4), at=_START + timedelta(minutes=50)),),
    ]
    notifier = _notifier(view, steady)

    with caplog.at_level(logging.WARNING):
        for _ in steady:
            await notifier.pass_once()

    given_up = [r for r in caplog.records if "giving up" in r.getMessage()]
    assert len(given_up) == 1, f"expected exactly one journal line, got {len(given_up)}"
    assert "claude" in given_up[0].getMessage()


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
