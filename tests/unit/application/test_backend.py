"""One backend, composed once per process, handed to both frontends.

`bootstrap` used to compose the bot and the local surface separately — two `SessionService`
instances over one database, two catalogues, two profile probes — sharing only helper
functions. The bot then typed its half `object | None` and reached into it by name, so a
capability the composition root forgot to wire was not a type error anywhere; it was a row
that silently stopped being offered.

`Backend` is what both frontends receive instead. It carries only application, domain and
port types (ARCH-B1): `application/` may not import an adapter (ARCH-02, DEC-015), and the
checker enforces that, but the rule is easy to break here by reaching for whichever
presentation type happened to be nearest — which is exactly how `LocalRuntime` came to be
typed against the Telegram wizard's `ProfileAvailability`, before sub-plan 4 moved that type
into `application/` and gave both surfaces the one narrowing.
"""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from schedule_fakes import MemoryScheduleStore

from remote_agents.application.backend import Backend
from remote_agents.application.schedule_book import ScheduleBook, ScheduleRefusal, ScheduleRefused
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ComposerScreen
from remote_agents.ports.schedules import Once, Repeat, Schedule

_SOURCE = Path(__file__).resolve().parents[3] / "src" / "remote_agents" / "application"


def test_a_backend_is_frozen(backend: Backend) -> None:
    """Composed once per process and read everywhere; nothing reassigns a field."""
    with pytest.raises(FrozenInstanceError):
        backend.max_label_length = 5  # type: ignore[misc]


def test_a_backend_carries_the_use_cases_both_surfaces_drive(backend: Backend) -> None:
    assert backend.sessions is not None
    assert backend.projects is not None
    assert backend.max_label_length == 40


def test_the_optional_capabilities_default_to_absent() -> None:
    """A host that wires neither offers neither affordance, rather than failing to start.

    The same widening `TuiContext` already documents: `capture`, `conversations` and
    `activity_feed` are capabilities, not requirements.
    """
    bare = Backend(sessions=object(), projects=object())
    assert bare.conversations is None
    assert bare.capture is None
    assert bare.activity_feed is None
    assert bare.catalogue == ()
    assert bare.profiles == ()
    assert bare.limits is None


def test_the_backend_module_imports_no_adapter() -> None:
    """ARCH-B1, checked here as well as globally, because this is the module at risk.

    `check_imports.py` sweeps the whole tree and would catch this too. The reason it is
    also pinned here is that the global sweep reports a violation *somewhere*, while this
    names the rule at the place a future field is most likely to break it: the temptation
    is always to type a field against the adapter that consumes it.
    """
    tree = ast.parse((_SOURCE / "backend.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)

    internal = [name for name in imported if name.startswith("remote_agents.")]
    assert internal, "no internal imports found — the parse is looking at the wrong file"
    offenders = [
        name
        for name in internal
        if not name.startswith(
            ("remote_agents.application", "remote_agents.domain", "remote_agents.ports")
        )
    ]
    assert offenders == [], (
        "application/backend.py may import only application, domain and ports — "
        f"found {offenders}. A field typed against an adapter makes the backend depend on "
        "the frontend it exists to serve (ARCH-02, DEC-015)."
    )


def test_a_host_that_wires_no_limits_reader_renders_no_limits() -> None:
    """The account-limits read is a capability like `capture`, not a requirement.

    Both surfaces guard on it, so a host whose providers publish nothing -- or one composed
    without the reader at all -- shows no Limits block rather than failing to start. Distinct
    from the reader answering an empty tuple, which means the providers were asked and had
    nothing to say; that one is rendered, this one is not reached.
    """
    assert Backend(sessions=object(), projects=object()).limits is None


@pytest.fixture
def backend() -> Backend:
    return Backend(sessions=object(), projects=object(), max_label_length=40)


# --- schedules (Task 1.5) ---------------------------------------------------------------------

_BERLIN = ZoneInfo("Europe/Berlin")
_NOW = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)  # Friday, 12:00 in Berlin
_WITH_MENU = ComposerScreen(composer=r"(?P<draft>.*)\Z", command_menu=r"(?P<first>\S+)")
_NO_MENU = ComposerScreen(composer=r"(?P<draft>.*)\Z")


def _book(store: MemoryScheduleStore | None = None, *, now: datetime = _NOW) -> ScheduleBook:
    return ScheduleBook(
        store if store is not None else MemoryScheduleStore(),
        composers={"claude": _WITH_MENU, "opencode": _NO_MENU, "silent": None},
        projects=lambda: (ProjectId("remote-agents"),),
        zone=lambda: _BERLIN,
        now=lambda: now,
    )


async def _add(book: ScheduleBook, prompt: str, *, profile: str = "claude", when=None):
    return await book.add(
        ProjectId("remote-agents"),
        ProfileId(profile),
        prompt,
        when if when is not None else Repeat.weekdays(time(9, 0)),
    )


async def test_a_schedule_is_added_with_its_next_local_fire_time() -> None:
    book = _book()
    added = await _add(book, "summarise the open PRs")

    assert isinstance(added, Schedule)
    # Friday noon in Berlin: the next weekday 09:00 is Monday.
    assert added.next_fire_at == datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
    assert not added.paused
    assert added.created_at == _NOW
    assert await book.list() == (added,)


async def test_a_schedule_whose_prompt_would_run_a_shell_command_is_refused() -> None:
    book = _book()
    refused = await _add(book, "!rm -rf build")

    assert refused == ScheduleRefused(ScheduleRefusal.SHELL)
    assert await book.list() == ()


async def test_a_schedule_slash_command_is_refused_without_a_menu_and_kept_with_one() -> None:
    book = _book()

    assert await _add(book, "/review", profile="opencode") == ScheduleRefused(ScheduleRefusal.MENU)
    assert isinstance(await _add(book, "/review", profile="claude"), Schedule)


async def test_a_schedule_for_an_agent_with_no_composer_is_refused() -> None:
    assert await _add(_book(), "hello", profile="silent") == ScheduleRefused(
        ScheduleRefusal.NO_COMPOSER
    )


async def test_a_schedule_for_an_unknown_project_or_profile_is_refused() -> None:
    book = _book()
    assert await book.add(
        ProjectId("elsewhere"), ProfileId("claude"), "hi", Repeat.daily(time(9, 0))
    ) == ScheduleRefused(ScheduleRefusal.UNKNOWN_PROJECT)
    assert await _add(book, "hi", profile="codex") == ScheduleRefused(
        ScheduleRefusal.UNKNOWN_PROFILE
    )


async def test_a_schedule_one_shot_in_the_past_is_refused() -> None:
    past = Once(datetime(2026, 10, 2, 11, 59))  # a minute before noon, Berlin
    assert await _add(_book(), "hi", when=past) == ScheduleRefused(ScheduleRefusal.PAST)


async def test_schedules_are_listed_soonest_first() -> None:
    book = _book()
    later = await _add(book, "later", when=Repeat.daily(time(18, 0)))
    sooner = await _add(book, "sooner", when=Repeat.daily(time(13, 0)))

    assert [schedule.id for schedule in await book.list()] == [sooner.id, later.id]


async def test_a_resumed_schedule_after_a_missed_time_sets_the_next_future_time() -> None:
    store = MemoryScheduleStore()
    added = await _add(_book(store), "hi", when=Repeat.daily(time(13, 0)))
    assert added.next_fire_at == datetime(2026, 10, 2, 11, 0, tzinfo=UTC)
    paused = await _book(store).pause(added.id)
    assert paused is not None and paused.paused

    # Resumed a day and a half later: today's and yesterday's 13:00 have both passed, and
    # neither is fired as a backlog.
    later = datetime(2026, 10, 3, 22, 0, tzinfo=UTC)
    resumed = await _book(store, now=later).resume(added.id)

    assert resumed is not None and not resumed.paused
    assert resumed.next_fire_at == datetime(2026, 10, 4, 11, 0, tzinfo=UTC)


async def test_a_resumed_one_shot_schedule_whose_time_passed_is_deleted() -> None:
    store = MemoryScheduleStore()
    added = await _add(_book(store), "hi", when=Once(datetime(2026, 10, 2, 13, 0)))
    await _book(store).pause(added.id)

    later = datetime(2026, 10, 3, 22, 0, tzinfo=UTC)
    assert await _book(store, now=later).resume(added.id) == ScheduleRefused(ScheduleRefusal.PAST)
    assert await store.get(added.id) is None


async def test_a_schedule_is_deleted() -> None:
    book = _book()
    added = await _add(book, "hi")

    assert await book.delete(added.id) is True
    assert await book.list() == ()
    assert await book.delete(added.id) is False


async def test_pausing_or_resuming_an_unknown_schedule_answers_none() -> None:
    book = _book()
    assert await book.pause("missing") is None
    assert await book.resume("missing") is None


def test_a_backend_carries_its_schedule_book() -> None:
    book = _book()
    assert Backend(sessions=object(), projects=object(), schedules=book).schedules is book
    assert Backend(sessions=object(), projects=object()).schedules is None


async def test_resuming_a_schedule_that_is_not_paused_changes_nothing() -> None:
    """A stale Resume tap must not skip a fire that is due, nor delete a pending one-shot."""
    store = MemoryScheduleStore()
    one_shot = await _add(_book(store), "hi", when=Once(datetime(2026, 10, 2, 13, 0)))
    repeat = await _add(_book(store), "hi", when=Repeat.daily(time(13, 0)))

    overdue = datetime(2026, 10, 2, 11, 30, tzinfo=UTC)  # both are due and not yet fired
    assert await _book(store, now=overdue).resume(one_shot.id) == one_shot
    assert await _book(store, now=overdue).resume(repeat.id) == repeat
    assert await store.get(one_shot.id) == one_shot
    assert await store.get(repeat.id) == repeat


async def test_paused_schedules_are_listed_after_the_active_ones() -> None:
    book = _book()
    paused = await _add(book, "first", when=Repeat.daily(time(12, 30)))
    active = await _add(book, "second", when=Repeat.daily(time(18, 0)))
    await book.pause(paused.id)

    assert [schedule.id for schedule in await book.list()] == [active.id, paused.id]


async def test_the_schedule_zone_is_read_per_call() -> None:
    zones = [_BERLIN]
    book = ScheduleBook(
        MemoryScheduleStore(),
        composers={"claude": _WITH_MENU},
        projects=lambda: (ProjectId("remote-agents"),),
        zone=lambda: zones[0],
        now=lambda: _NOW,
    )
    assert book.zone == _BERLIN
    zones[0] = ZoneInfo("America/New_York")
    added = await _add(book, "hi", when=Repeat.daily(time(9, 0)))
    # 09:00 in New York (13:00Z) is still ahead today; 09:00 Berlin passed at 07:00Z.
    assert added.next_fire_at == datetime(2026, 10, 2, 13, 0, tzinfo=UTC)


@pytest.mark.parametrize("reason", ["empty", "shell", "menu", "no_composer"])
def test_every_pre_paste_refusal_has_a_schedule_refusal(reason: str) -> None:
    from remote_agents.ports.terminal import PromptReason

    assert ScheduleRefusal(PromptReason(reason).value).value == reason
