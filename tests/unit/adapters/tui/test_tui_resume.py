"""Resume walks project → profile → catalogue → confirm, and never shows a provider ID."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

import pytest
from textual.widgets import OptionList
from tui_feedback import announcements
from tui_feedback import status as _status
from tui_positions import position

from remote_agents.adapters.tui.app import AttachRequest, RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.commands import ResumeCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.services import ResumeOutcome
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")
_SECRET_PATH = "/home/user/.claude/projects/opaque/abc123def456.jsonl"
_PROVIDER_ID = "abc123def456"


class _FutureConversationState(StrEnum):
    """A second conversation state, which the domain does not have yet.

    Borrowed from `tests/contract/test_resume_offer_parity.py`, which explains it at length:
    `ConversationState` is a `StrEnum` with one member, so there is no non-resumable value to
    hand a surface, and Python refuses to *extend* a populated enum — hence a sibling rather
    than a subclass. Being a `StrEnum` too, its member satisfies the same `str` contract
    `ConversationSummary.state` is carried as, while `is ConversationState.RESUMABLE` is false
    for it. That is what makes the refusal path reachable at all.
    """

    ARCHIVED = "archived"


def _summary(
    index: int,
    profile: str = "claude",
    state: ConversationState | _FutureConversationState = ConversationState.RESUMABLE,
) -> ConversationSummary:
    return ConversationSummary(
        ConversationReference(f"c-{'0' * 14}{index:02d}"),
        ProfileId(profile),
        ProjectId("opaque-existing"),
        state,
        datetime.now(UTC),
        description=f"conversation {index}",
    )


def _record() -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "resumed", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _Conversations:
    pages: dict[int, tuple[ConversationSummary, ...]] = field(default_factory=dict)
    page_count: int = 1
    caps: tuple[ProfileResumeCapability, ...] = ()
    queries: list[object] = field(default_factory=list)

    async def catalogue(self, query) -> ConversationCataloguePage:
        self.queries.append(query)
        return ConversationCataloguePage(
            self.pages.get(query.page, ()), query.page, self.page_count
        )

    async def resolve_for_resume(self, reference: ConversationReference):
        for page in self.pages.values():
            for summary in page:
                if summary.reference == reference:
                    return ResolvedConversation(summary, ProviderConversationId(_PROVIDER_ID))
        return None

    async def capabilities(self) -> tuple[ProfileResumeCapability, ...]:
        return self.caps


@dataclass(slots=True)
class _Launcher:
    resumed: list[ResumeCommand] = field(default_factory=list)
    record: SessionRecord = field(default_factory=_record)

    async def refresh_readiness(self):
        return ()

    async def list_sessions(self):
        return ()

    async def copy_attach(self, _session_id):
        return None

    async def resume(self, command: ResumeCommand) -> ResumeOutcome:
        self.resumed.append(command)
        return ResumeOutcome(self.record, created=True)


def _context(conversations: _Conversations, launcher: _Launcher) -> TuiContext:
    return TuiContext(
        launcher=launcher,  # type: ignore[arg-type]
        creator=object(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True), ProfileChoice("codex", True)),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_PROJECT,),
        conversations=conversations,  # type: ignore[arg-type]
    )


def _capable(*profiles: str) -> tuple[ProfileResumeCapability, ...]:
    return tuple(ProfileResumeCapability(ProfileId(profile), True, True) for profile in profiles)


def _rows(app: RemoteAgentsTui) -> list[str]:
    return [str(option.prompt) for option in app.screen.query_one("#choices", OptionList).options]


def _keys(app: RemoteAgentsTui) -> list[str]:
    return [option.id for option in app.screen.query_one("#choices", OptionList).options]


async def test_resume_is_offered_when_a_conversation_service_is_wired() -> None:
    conversations = _Conversations({1: (_summary(1),)}, caps=_capable("claude"))
    app = RemoteAgentsTui(_context(conversations, _Launcher()))

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        step = position(app)

    assert step == "RESUME_PROJECTS"


async def test_a_context_without_conversations_offers_no_resume() -> None:
    context = TuiContext(
        launcher=_Launcher(),  # type: ignore[arg-type]
        creator=object(),  # type: ignore[arg-type]
        profiles=(ProfileChoice("claude", True),),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux",),
        catalogue=(_PROJECT,),
    )
    app = RemoteAgentsTui(context)

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        step = position(app)

    assert step == "DASHBOARD"


async def test_only_resume_capable_profiles_are_offered() -> None:
    """From capabilities(), not from a version allowlist (DEC-002)."""
    conversations = _Conversations({1: (_summary(1),)}, caps=_capable("claude"))
    app = RemoteAgentsTui(_context(conversations, _Launcher()))

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        keys = _keys(app)

    assert "claude" in keys
    assert "codex" not in keys, "codex is not resume-capable here and must not be offered"


async def test_the_catalogue_is_paginated() -> None:
    conversations = _Conversations(
        {1: tuple(_summary(index) for index in range(1, 4)), 2: (_summary(9),)},
        page_count=2,
        caps=_capable("claude"),
    )
    app = RemoteAgentsTui(_context(conversations, _Launcher()))

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        first = _rows(app)
        assert any("conversation 1" in row for row in first)

        await app.screen.choose("\x00next")
        await pilot.pause()
        second = _rows(app)

    assert any("conversation 9" in row for row in second)


async def test_no_provider_id_or_path_is_ever_rendered() -> None:
    conversations = _Conversations({1: (_summary(1),)}, caps=_capable("claude"))
    app = RemoteAgentsTui(_context(conversations, _Launcher()))

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        rendered = " ".join(_rows(app)) + _status(app) + " ".join(str(k) for k in _keys(app))

    assert _PROVIDER_ID not in rendered
    assert ".jsonl" not in rendered
    assert "/home/" not in rendered
    for fragment in _SECRET_PATH.split("/"):
        if len(fragment) > 6:
            assert fragment not in rendered


async def test_choosing_a_conversation_starts_the_resume_with_no_step_in_between() -> None:
    """Three positions, and the third is the act.

    **This asserted the opposite until now, and the change is DEC-018's rule applied rather
    than a preference.** That entry decides confirmations by "applied to both surfaces or
    neither", because DEC-007's rendered-row parity is what makes a second surface safe to hold
    destructive power — and the bot retired its own resume review step, answering a press on it
    with "Resuming no longer has a review step — choosing a conversation starts it." A
    confirmation on this surface alone was exactly the one-surface-only case that rule forbids.

    Resuming is also not destructive: it starts a session, and the session it starts can be
    stopped. DEC-018's other half is the friction argument — a confirmation on a routine action
    trains the owner to dismiss confirmations, which makes the ones guarding force stop and
    Remote Control worth less.
    """
    conversations = _Conversations({1: (_summary(1),)}, caps=_capable("claude"))
    launcher = _Launcher()
    app = RemoteAgentsTui(_context(conversations, launcher))

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        assert position(app) == "RESUME_CONVERSATIONS"

        await app.screen.choose(str(_summary(1).reference))
        await pilot.pause()

    assert len(launcher.resumed) == 1, (
        f"choosing a conversation must start it; the launcher saw {launcher.resumed}"
    )
    assert launcher.resumed[0].idempotency_key.startswith("tui-")
    assert launcher.resumed[0].profile_id == ProfileId("claude")


async def test_a_conversation_that_resolves_but_cannot_be_resumed_starts_nothing() -> None:
    """The one substantive check the confirmation carried, kept at the act rather than lost.

    The removed screen re-asked `resume_available` before issuing, and its comment is precise
    about what that catches: a `resolve_for_resume` that disagrees with the `catalogue` listing,
    since the two are independent reads of the provider and only the first was ever filtered. It
    is equally precise about what it does *not* catch — staleness while the owner deliberates —
    because `resolved` is a frozen snapshot, so a pure function of it cannot see anything move.

    Removing the screen removes the deliberation window entirely, so the half that was never
    covered stops existing; the half that was covered has to survive, and this is it.
    """
    unresumable = _summary(1, state=_FutureConversationState.ARCHIVED)  # type: ignore[arg-type]
    conversations = _Conversations({1: (unresumable,)}, caps=_capable("claude"))
    launcher = _Launcher()
    app = RemoteAgentsTui(_context(conversations, launcher))

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        await app.screen.choose(str(unresumable.reference))
        await pilot.pause()
        warned = " ".join(announcements(app, severity="warning"))
        step = position(app)

    assert launcher.resumed == [], "an unresumable conversation was started anyway"
    assert "no longer be resumed" in warned or "not valid" in warned, warned
    assert step == "RESUME_CONVERSATIONS", "a refusal must leave the owner on the list"


async def test_a_ready_resume_ends_in_the_same_attach_handoff_as_a_launch() -> None:
    conversations = _Conversations({1: (_summary(1),)}, caps=_capable("claude"))
    launcher = _Launcher()
    app = RemoteAgentsTui(_context(conversations, launcher))

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        await app.screen.choose(str(_summary(1).reference))
        await pilot.pause()

    assert isinstance(app.return_value, AttachRequest)
    assert app.return_value.session_id == str(launcher.record.session_id)


async def test_a_reference_that_no_longer_resolves_does_not_resume() -> None:
    conversations = _Conversations({1: (_summary(1),)}, caps=_capable("claude"))
    launcher = _Launcher()
    app = RemoteAgentsTui(_context(conversations, launcher))

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        conversations.pages = {}
        await app.screen.choose(str(_summary(1).reference))
        await pilot.pause()
        reported = " ".join(announcements(app)).casefold()

    assert launcher.resumed == []
    assert "no longer" in reported or "not available" in reported


@pytest.mark.parametrize("forged", ["../../etc/passwd", _SECRET_PATH, "c-notreal", ""])
async def test_a_forged_reference_is_refused(forged: str) -> None:
    """Only a reference the server issued may be resumed; nothing is accepted as input."""
    conversations = _Conversations({1: (_summary(1),)}, caps=_capable("claude"))
    launcher = _Launcher()
    app = RemoteAgentsTui(_context(conversations, launcher))

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        await app.screen.choose(forged)
        await pilot.pause()

    assert launcher.resumed == []


async def test_back_out_of_the_resume_flow_stops_at_every_position() -> None:
    """Back walks the resume flow out one position at a time, and Cancel agrees with it.

    **One deliberate navigation change survives here; the other left with its screen.**
    Escape at the agent choice used to jump straight to the project list, skipping the resume
    project choice — that is the leg still asserted below. The second was the confirm's Cancel
    row restarting the whole flow while Escape from the same position went back exactly one, so
    the two rows disagreed about what leaving meant; with the confirmation gone there are no
    two rows left to disagree, and the walk is one position shorter.

    Asserted as the whole walk, so reinstating the surviving shortcut fails here rather than
    passing on a single destination.
    """
    conversations = _Conversations({1: (_summary(1),)}, caps=_capable("claude"))
    launcher = _Launcher()
    app = RemoteAgentsTui(_context(conversations, launcher))

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        assert position(app) == "RESUME_CONVERSATIONS"

        await app.action_back()
        await pilot.pause()
        assert position(app) == "RESUME_PROFILES"

        await app.action_back()
        await pilot.pause()
        assert position(app) == "RESUME_PROJECTS", (
            "escape from the agent choice must return to the resume project list"
        )

        await app.action_back()
        await pilot.pause()
        assert position(app) == "DASHBOARD"

    assert launcher.resumed == [], "walking back out must resume nothing"


async def test_reopening_resume_mid_navigation_does_not_strand_the_owner() -> None:
    """Double-tapping Ctrl+E while a catalogue read is in flight must not dead-end.

    The Stage 3 discipline applies to the resume wizard's read navigation too: a second
    entry point firing during an await used to reset the chosen project, after which
    selecting a profile silently did nothing and only Escape recovered.
    """
    import asyncio

    @dataclass(slots=True)
    class _SlowConversations:
        pages: dict[int, tuple[ConversationSummary, ...]] = field(default_factory=dict)
        caps: tuple[ProfileResumeCapability, ...] = ()

        async def catalogue(self, query) -> ConversationCataloguePage:
            return ConversationCataloguePage(self.pages.get(query.page, ()), query.page, 1)

        async def resolve_for_resume(self, reference):
            return None

        async def capabilities(self):
            await asyncio.sleep(0.02)
            return self.caps

    conversations = _SlowConversations({1: (_summary(1),)}, caps=_capable("claude"))
    app = RemoteAgentsTui(_context(conversations, _Launcher()))  # type: ignore[arg-type]

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        await asyncio.gather(
            app.screen.choose("opaque-existing"),
            _reenter_during(app),
        )
        await pilot.pause()

        # Whatever screen the owner lands on, selecting the offered profile must go
        # somewhere rather than silently doing nothing.
        if position(app) == "RESUME_PROFILES":
            await app.screen.choose("claude")
            await pilot.pause()
            assert position(app) == "RESUME_CONVERSATIONS", (
                "selecting a profile did nothing: the chosen project was lost mid-navigation"
            )


async def test_the_navigation_guard_is_held_until_the_next_screen_is_pushed() -> None:
    """The guard must span the push, not just the read that precedes it.

    `push_screen` yields while the new screen mounts. A guard cleared before that await
    leaves a window in which a second global binding pops the screen being mounted, and the
    work that was just fetched is discarded with no error at all — the same "second entry
    point mid-navigation" class the guard exists for, failing silently rather than stranding.

    Asserted by recording the stack depth at each flip rather than by racing a second action:
    a race would reproduce it only sometimes, while "was the screen already pushed when the
    guard was released" is the property itself, and it fails deterministically if the
    `finally` is ever narrowed back to the fetch alone.
    """
    flips: list[tuple[bool, int]] = []

    class _Watching(RemoteAgentsTui):
        def set_busy(self, busy: bool) -> None:
            flips.append((busy, len(self.screen_stack)))
            super().set_busy(busy)

    conversations = _Conversations({1: (_summary(1),)}, caps=_capable("claude"))
    app = _Watching(_context(conversations, _Launcher()))

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        flips.clear()

        await app.screen.choose("opaque-existing")
        await pilot.pause()

    assert flips, "choosing a resume project must take the navigation guard"
    taken, depth_when_taken = flips[0]
    released, depth_when_released = flips[-1]
    assert taken is True and released is False
    assert depth_when_released > depth_when_taken, (
        "the guard was released at stack depth "
        f"{depth_when_released}, the same depth it was taken at — the next screen had not "
        "been pushed yet, so a global binding firing here would discard the fetched work"
    )


async def test_the_navigation_guard_still_spans_the_conversation_resolve() -> None:
    """The third fetch of the resume flow is held like its two siblings (DEC-024).

    **The property survives the confirmation's removal; the way it was asserted does not.**
    This used to record the stack depth at each guard flip and require the release to happen
    *deeper* than the take — a precise way of saying "the confirmation was already pushed", so
    a `finally` narrowed back to the resolve alone failed deterministically rather than racily.
    With no screen pushed there is no depth change left to observe, and keeping that shape
    would have been a test passing on its own vacuity.

    What replaces it asks the property directly instead of inferring it from the stack: the
    fake records whether the guard was held *at the moment `resolve_for_resume` was called*.
    That is what DEC-024 actually says, it cannot be satisfied by accident, and it fails the
    moment the `async with` is narrowed to exclude the fetch.
    """
    held_during_resolve: list[bool] = []

    class _WatchingConversations(_Conversations):
        async def resolve_for_resume(self, reference: ConversationReference):
            held_during_resolve.append(app.busy)
            return await super().resolve_for_resume(reference)

    summary = _summary(1)
    conversations = _WatchingConversations({1: (summary,)}, caps=_capable("claude"))
    launcher = _Launcher()
    app = RemoteAgentsTui(_context(conversations, launcher))

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        assert position(app) == "RESUME_CONVERSATIONS", "the flow did not reach the page"

        await app.screen.choose(str(summary.reference))
        await pilot.pause()

    assert held_during_resolve == [True], (
        "resolve_for_resume ran outside the navigation guard, which is exactly the exception "
        "DEC-024 removed: a second entry point arriving mid-resolve would reset the chosen "
        "project and the selection would silently do nothing"
    )
    assert len(launcher.resumed) == 1, (
        "the resume was never issued, so the guard assertion above proves nothing about the "
        "path that matters"
    )


async def _reenter_during(app) -> None:
    import asyncio

    await asyncio.sleep(0.005)
    await app.action_resume()
