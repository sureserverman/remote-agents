"""A binding across a trust answer and a restart, and what the owner is told when it never comes."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from remote_agents.adapters.agents.thread_binding import bound_session
from remote_agents.adapters.telegram.binding_notifications import BindingNotifier
from remote_agents.application.doctor import production_doctor
from remote_agents.application.thread_binding import (
    COMPOSER_PATIENCE_SECONDS,
    SEEN_PATIENCE_SECONDS,
    UnboundWatch,
    unbound_notice,
)
from remote_agents.domain.models import ProfileId, SessionState
from remote_agents.ports.terminal import PromptDelivery, PromptOutcome, PromptReason
from remote_agents.ports.thread_binding import PendingBinding

from .test_thread_binding_launch import _THREAD, World


@pytest.fixture
def world(tmp_path: Path) -> World:
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    return World(spool)


def _trusted(world: World, session) -> None:
    index = next(i for i, r in enumerate(world.records) if r.session_id == session)
    record = world.records[index]
    world.records[index] = type(record)(
        record.session_id,
        record.project_id,
        record.profile_id,
        record.display,
        SessionState.RUNNING,
        record.created_at,
    )


def test_bound_after_the_owner_answers_trust_however_long_that_took(world: World) -> None:
    session = world.session(state=SessionState.UNTRUSTED)
    world.run(2)
    world.elapse(COMPOSER_PATIENCE_SECONDS * 100)
    world.run()
    assert world.sent == [], "nothing is typed on the dialog, and its wait costs no patience"
    _trusted(world, session)
    world.run()
    assert len(world.sent) == 1
    assert bound_session(world.spool, _THREAD) == str(session)
    assert world.told == []


def test_a_service_restart_mid_bind_retries_once_and_binds(world: World) -> None:
    session = world.session()
    world.shell_runs = False
    world.run()
    assert world.bindings.state(str(session)) is PendingBinding.TRIED
    world.shell_runs = True
    world.binder = world.new_binder()
    world.run(2)
    assert len(world.sent) == 2
    assert bound_session(world.spool, _THREAD) == str(session)
    assert world.told == []


def test_one_notice_per_session_across_passes(world: World) -> None:
    first, second = world.session(), world.session()
    world.shell_runs = False
    world.run()
    world.elapse(SEEN_PATIENCE_SECONDS)
    world.run(4)
    assert sorted(map(str, world.told)) == sorted([str(first), str(second)])


class _View:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.refuse = False

    async def send_apart(self, bot, message) -> int:
        if self.refuse:
            raise RuntimeError("Telegram said no")
        self.sent.append(message["text"])
        return 1


def test_the_bot_says_it_once_and_holds_it_until_it_lands(world: World) -> None:
    world.session()
    view = _View()
    notifier = BindingNotifier(view=view, project_name=lambda project: "<orchard>")

    async def go() -> None:
        await notifier.notify(world.records[0])
        assert view.sent == [] and len(notifier.pending) == 1, "held until the bot attaches"
        notifier.attach(object())
        view.refuse = True
        await notifier.pass_once()
        assert len(notifier.pending) == 1
        view.refuse = False
        await notifier.pass_once()
        await notifier.pass_once()

    asyncio.run(go())
    assert len(view.sent) == 1
    assert "&lt;orchard&gt;" in view.sent[0]
    assert "handoffs will refuse" in view.sent[0]


def test_the_console_is_told_once_and_not_about_what_was_there_when_it_opened(
    world: World,
) -> None:
    old = world.session()
    world.bindings.give_up(str(old))

    async def sessions():
        return list(world.records)

    watch = UnboundWatch(world.bindings, sessions)
    assert asyncio.run(watch.newly_given_up()) == ()
    fresh = world.session()
    assert asyncio.run(watch.newly_given_up()) == ()
    world.bindings.give_up(str(fresh))
    assert [r.session_id for r in asyncio.run(watch.newly_given_up())] == [fresh]
    assert asyncio.run(watch.newly_given_up()) == ()


def test_both_surfaces_use_one_sentence(world: World) -> None:
    world.session()
    record = world.records[0]
    assert unbound_notice(record, "orchard") == (
        f"codex in orchard #{record.display.sequence} could not bind its thread. It keeps working, "
        "but its handoffs will refuse, so it cannot roll over."
    )


def _report(**fields) -> dict[str, object]:
    return production_doctor(
        core_ready=True,
        database_ready=True,
        tmux_ready=True,
        telegram_ready=True,
        service_ready=True,
        profiles=(),
        registered_projects=1,
        discovered_projects=1,
        catalogue_projects=1,
        **fields,
    )


def test_doctor_reports_the_count_and_it_never_moves_the_verdict() -> None:
    bare = _report()
    counted = _report(unbound_sessions=2)
    assert "unbound_sessions" not in bare
    assert counted["unbound_sessions"] == 2
    assert counted["healthy"] == bare["healthy"]
    assert _report(unbound_sessions=0)["unbound_sessions"] == 0


def test_a_busy_first_turn_does_not_use_up_the_patience(world: World) -> None:
    """The owner's first turn can run for an hour. The thread is the same when it ends."""
    session = world.session()
    busy = PromptDelivery(PromptOutcome.REFUSED, PromptReason.BUSY)
    world.deliveries = [busy] * 6
    for _ in range(6):
        world.run()
        world.elapse(COMPOSER_PATIENCE_SECONDS / 2)
    world.run()
    assert world.told == []
    assert bound_session(world.spool, _THREAD) == str(session)


def test_a_composer_holding_text_still_uses_up_the_patience(world: World) -> None:
    session = world.session()
    world.deliveries = [PromptDelivery(PromptOutcome.REFUSED, PromptReason.COMPOSING)] * 2
    world.run()
    world.elapse(COMPOSER_PATIENCE_SECONDS)
    world.run()
    assert world.told == [session]


def test_wait_bound_is_bounded_even_while_the_pane_stays_busy(world: World) -> None:
    session = world.session()
    world.deliveries = [PromptDelivery(PromptOutcome.REFUSED, PromptReason.BUSY)] * 500
    assert asyncio.run(world.binder.wait_bound(session, ProfileId("codex"))) is False


def test_wait_bound_can_tell_the_owner_for_a_caller_that_carries_on(world: World) -> None:
    session = world.session()
    world.shell_runs = False
    assert asyncio.run(world.binder.wait_bound(session, ProfileId("codex"), tell=True)) is False
    assert world.told == [session]


def test_a_session_gone_from_the_store_is_forgotten(world: World) -> None:
    session = world.session()
    pending = world.spool / "thread-binding" / "pending" / str(session)
    old = time.time() - 3600
    os.utime(pending, (old, old))
    world.records.clear()
    world.run()
    assert world.bindings.state(str(session)) is PendingBinding.NONE
