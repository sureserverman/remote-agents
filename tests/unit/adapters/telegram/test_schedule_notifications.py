"""The schedule notice: one message per fire, its reason in words (DEC-031 as amended, DEC-010)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from remote_agents.adapters.telegram.flood import FloodGate
from remote_agents.adapters.telegram.schedule_notifications import (
    ScheduleNotifier,
    schedule_message,
)
from remote_agents.application.notification_policy import schedule_told
from remote_agents.application.schedules import FireOutcome, FireReport
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.schedules import Repeat, Schedule

FIRE = datetime(2026, 10, 2, 7, 0, tzinfo=UTC)


def _report(outcome: FireOutcome, **fields) -> FireReport:
    schedule = Schedule(
        id="s1",
        project_id=ProjectId("p-opaque"),
        profile_id=ProfileId("claude"),
        prompt="reply with OK",
        when=Repeat.daily(time(9, 0)),
        paused=False,
        next_fire_at=FIRE,
        created_at=FIRE - timedelta(days=1),
    )
    return FireReport(schedule, outcome, FIRE, **fields)


def _message(report: FireReport, project: str = "remote-agents") -> str:
    return schedule_message(report, project_name=project)


_SAMPLE = {
    FireOutcome.STARTED: {"session_id": "x"},
    FireOutcome.NOT_TYPED: {"session_id": "x", "reason": "dialog"},
    FireOutcome.LAUNCH_FAILED: {"reason": "launch_error"},
    FireOutcome.MISSED: {"late_by": timedelta(minutes=47)},
    FireOutcome.SKIPPED_LIMIT: {},
    FireOutcome.SKIPPED_PREVIOUS: {"session_id": "prev"},
    FireOutcome.DUPLICATE: {},
}


@pytest.mark.parametrize("outcome", list(FireOutcome))
def test_every_outcome_the_policy_tells_has_wording(outcome: FireOutcome) -> None:
    """A new outcome without a sentence fails here, whichever way the policy goes."""
    assert outcome in _SAMPLE, f"no sample report for {outcome}; add one and its wording"
    text = _message(_report(outcome, **_SAMPLE[outcome]))
    if schedule_told(outcome):
        assert text.startswith("Scheduled: claude in remote-agents "), text
    else:
        assert text == ""


def test_started_reads_as_the_owner_asked() -> None:
    assert _message(_report(FireOutcome.STARTED)) == "Scheduled: claude in remote-agents started"


@pytest.mark.parametrize(
    ("reason", "words"),
    [
        ("dialog", "a dialog is open"),
        ("composing", "already held text"),
        ("unconfirmed", "was not seen to land"),
        ("not_ready", "not ready within 90 seconds"),
        ("shell", "shell command"),
        ("no_composer", "cannot be read"),
        ("empty", "empty"),
        ("unrecognised", "not recognised"),
    ],
)
def test_a_prompt_not_typed_says_why_and_that_the_session_is_open(reason, words) -> None:
    text = _message(_report(FireOutcome.NOT_TYPED, session_id="x", reason=reason))
    assert text.startswith("Scheduled: claude in remote-agents started, but its message was not")
    assert words in text
    assert "the session is open" in text


def test_a_skip_says_why_in_words() -> None:
    limit = _message(_report(FireOutcome.SKIPPED_LIMIT))
    previous = _message(_report(FireOutcome.SKIPPED_PREVIOUS, session_id="prev"))
    assert limit == "Scheduled: claude in remote-agents skipped — claude is at its usage limit"
    assert previous == (
        "Scheduled: claude in remote-agents skipped — its previous run is still working"
    )


def test_a_missed_run_says_how_late() -> None:
    text = _message(_report(FireOutcome.MISSED, late_by=timedelta(minutes=47)))
    assert text == (
        "Scheduled: claude in remote-agents missed — the service was down at its time (47m late)"
    )


def test_a_failed_launch_says_so() -> None:
    assert "did not start" in _message(_report(FireOutcome.LAUNCH_FAILED, reason="launch_error"))
    assert "did not become ready" in _message(
        _report(FireOutcome.LAUNCH_FAILED, reason="not_ready")
    )


def test_an_html_special_project_name_is_escaped_once() -> None:
    text = _message(_report(FireOutcome.STARTED), project="a<b>&c")
    assert "a&lt;b&gt;&amp;c" in text
    assert "&amp;lt;" not in text


class _View:
    def __init__(self, refuse: int = 0) -> None:
        self.sent: list[dict[str, object]] = []
        self._refuse = refuse

    async def send_apart(self, bot: object, arguments: dict[str, object]) -> int:
        if self._refuse:
            self._refuse -= 1
            raise RuntimeError("chat unavailable")
        self.sent.append(arguments)
        return len(self.sent)


def _notifier(view: _View) -> ScheduleNotifier:
    notifier = ScheduleNotifier(
        view=view, project_name=lambda project_id: "remote-agents", flood=FloodGate()
    )
    notifier.attach(object())
    return notifier


async def test_each_fire_is_one_message() -> None:
    view = _View()
    notifier = _notifier(view)

    await notifier.notify(_report(FireOutcome.STARTED, session_id="x"))
    await notifier.notify(_report(FireOutcome.SKIPPED_LIMIT))

    assert [sent["text"] for sent in view.sent] == [
        "Scheduled: claude in remote-agents started",
        "Scheduled: claude in remote-agents skipped — claude is at its usage limit",
    ]
    assert all(sent["parse_mode"] == "HTML" for sent in view.sent)


def test_a_session_that_ended_before_its_message_is_not_called_open() -> None:
    text = _message(_report(FireOutcome.NOT_TYPED, session_id="x", reason="not_running"))
    assert text == (
        "Scheduled: claude in remote-agents started, but its session ended before its message "
        "could be typed"
    )


def test_a_launch_error_is_named_and_escaped() -> None:
    text = _message(
        _report(FireOutcome.LAUNCH_FAILED, reason="launch_error", detail="RuntimeError: <tmux>")
    )
    assert text == (
        "Scheduled: claude in remote-agents did not start — the launch failed: "
        "RuntimeError: &lt;tmux&gt;"
    )


def test_a_fire_a_restart_found_already_launched_is_told() -> None:
    text = _message(_report(FireOutcome.DUPLICATE))
    assert "may have started before a restart" in text
    assert "check its sessions" in text


def test_the_package_word_for_what_is_typed_is_message() -> None:
    """DEC-075: `check_telegram_actions` forbids the other word in this package."""
    for outcome, fields in _SAMPLE.items():
        for reason in ("dialog", "not_running", "shell", None):
            text = _message(_report(outcome, **{**fields, "reason": reason}))
            assert "prompt" not in text.casefold()


async def test_a_refused_message_is_sent_on_the_next_pass_and_not_twice() -> None:
    view = _View(refuse=1)
    notifier = _notifier(view)

    await notifier.notify(_report(FireOutcome.STARTED, session_id="x"))
    assert view.sent == []
    await notifier.pass_once()
    await notifier.pass_once()

    assert [sent["text"] for sent in view.sent] == ["Scheduled: claude in remote-agents started"]


async def test_a_refused_message_is_held_for_ten_minutes_then_given_up() -> None:
    view = _View(refuse=1000)
    clock = [FIRE]
    notifier = ScheduleNotifier(
        view=view, project_name=lambda _: "remote-agents", flood=FloodGate(), now=lambda: clock[0]
    )
    notifier.attach(object())

    await notifier.notify(_report(FireOutcome.STARTED, session_id="x"))
    for minutes in range(1, 10):
        clock[0] = FIRE + timedelta(minutes=minutes)
        await notifier.pass_once()
    assert notifier.pending == ("Scheduled: claude in remote-agents started",)
    clock[0] = FIRE + timedelta(minutes=10)
    await notifier.pass_once()
    assert notifier.pending == ()


async def test_fires_told_together_are_each_sent_exactly_once() -> None:
    """Typed side by side, fires finish together; two drains must not share one head."""
    import asyncio

    class _Slow(_View):
        async def send_apart(self, bot, arguments) -> int:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return await super().send_apart(bot, arguments)

    view = _Slow()
    notifier = _notifier(view)
    reports = [
        _report(FireOutcome.STARTED, session_id="x"),
        _report(FireOutcome.SKIPPED_LIMIT),
        _report(FireOutcome.MISSED, late_by=timedelta(minutes=20)),
    ]

    await asyncio.gather(*(notifier.notify(report) for report in reports))

    texts = [sent["text"] for sent in view.sent]
    assert len(texts) == 3 and len(set(texts)) == 3
    assert notifier.pending == ()


def test_the_bot_names_the_project_from_its_catalogue_and_wires_the_notifier() -> None:
    from backends import backend_for

    from remote_agents.adapters.telegram.service import build_private_bot
    from remote_agents.application.project_catalog import CatalogProject

    with_book = build_private_bot(
        1,
        2,
        backend=backend_for(
            schedules=object(),
            catalogue=(CatalogProject("p-opaque", "remote-agents", "infra", "infra"),),
        ),
    )
    without = build_private_bot(1, 2, backend=backend_for())

    assert with_book.schedule_notifier is not None
    assert without.schedule_notifier is None
    assert (
        schedule_message(
            _report(FireOutcome.STARTED),
            project_name=with_book._project_name("p-opaque"),  # noqa: SLF001
        )
        == "Scheduled: claude in remote-agents started"
    )
    assert with_book._project_name("gone") == "gone"  # noqa: SLF001
