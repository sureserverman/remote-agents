"""Resume walks project → profile → catalogue → confirm, and never shows a provider ID."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from textual.widgets import OptionList
from tui_feedback import announcements
from tui_feedback import status as _status
from tui_positions import position

from remote_agents.adapters.tui.app import AttachRequest, RemoteAgentsTui
from remote_agents.adapters.tui.context import ProfileChoice, TuiContext
from remote_agents.application.commands import ResumeCommand
from remote_agents.application.project_catalog import CatalogProject
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


def _summary(index: int, profile: str = "claude") -> ConversationSummary:
    return ConversationSummary(
        ConversationReference(f"c-{'0' * 14}{index:02d}"),
        ProfileId(profile),
        ProjectId("opaque-existing"),
        ConversationState.RESUMABLE,
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

    async def resume(self, command: ResumeCommand) -> SessionRecord:
        self.resumed.append(command)
        return self.record


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

    assert step == "PROJECTS"


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


async def test_resume_requires_a_confirm_step_and_issues_a_tui_key() -> None:
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
        assert position(app) == "RESUME_CONFIRM"
        assert launcher.resumed == [], "the selection alone must not resume"

        await app.screen.choose("resume-confirm")
        await pilot.pause()

    assert len(launcher.resumed) == 1
    assert launcher.resumed[0].idempotency_key.startswith("tui-")
    assert launcher.resumed[0].profile_id == ProfileId("claude")


async def test_aborting_the_confirm_resumes_nothing() -> None:
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
        await app.screen.choose("\x00cancel")
        await pilot.pause()

    assert launcher.resumed == []


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
        await app.screen.choose("resume-confirm")
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

    **Two deliberate navigation changes, matching the pairs Tasks 2.1 and 2.2 removed.**
    Escape at the agent choice used to jump straight to the project list, skipping the resume
    project choice. And the confirm's Cancel row used to restart the entire flow while Escape
    from that same position went back exactly one — so the two rows disagreed about what
    leaving meant. On a real stack both are one level, which is what makes them agree.

    Asserted as the whole walk, so reinstating either shortcut fails here rather than passing
    on a single destination.
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
        await app.screen.choose(str(_summary(1).reference))
        await pilot.pause()
        assert position(app) == "RESUME_CONFIRM"

        # Cancel, not Escape: the row is the half that used to restart the flow.
        await app.screen.choose("\x00cancel")
        await pilot.pause()
        assert position(app) == "RESUME_CONVERSATIONS", (
            "Cancel at the confirm must go back one position, as Escape always did"
        )

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
        assert position(app) == "PROJECTS"

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


async def test_the_navigation_guard_spans_the_conversation_resolve_and_its_push() -> None:
    """The third fetch of the resume flow is held like its two siblings (DEC-024).

    `ResumeConversationsScreen.choose` resolves a reference and pushes the confirmation, and
    it was the one of the three fetches not under the guard. Nothing chose that: the two
    siblings were guarded when the flow was hand-rolled, this one was extracted afterwards
    and did not inherit it. An unexplained exception is how the next reader concludes the
    guard is optional, which is the whole of DEC-024's reasoning.

    Asserted with the same shape the sibling case above uses — the stack depth recorded at
    each flip, rather than a race — for the same reason: a race reproduces it only sometimes,
    while "was the confirmation already pushed when the guard was released" is the property
    itself and fails deterministically if the `finally` is ever narrowed back to the resolve.
    """
    flips: list[tuple[bool, int]] = []

    class _Watching(RemoteAgentsTui):
        def set_busy(self, busy: bool) -> None:
            flips.append((busy, len(self.screen_stack)))
            super().set_busy(busy)

    summary = _summary(1)
    conversations = _Conversations({1: (summary,)}, caps=_capable("claude"))
    app = _Watching(_context(conversations, _Launcher()))

    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        await app.screen.choose("opaque-existing")
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        assert position(app) == "RESUME_CONVERSATIONS", "the flow did not reach the page"
        flips.clear()

        await app.screen.choose(str(summary.reference))
        await pilot.pause()

        assert position(app) == "RESUME_CONFIRM", "choosing a conversation must advance"

    assert flips, "resolving a chosen conversation must take the navigation guard"
    taken, depth_when_taken = flips[0]
    released, depth_when_released = flips[-1]
    assert taken is True and released is False
    assert depth_when_released > depth_when_taken, (
        "the guard was released at stack depth "
        f"{depth_when_released}, the same depth it was taken at — the confirmation had not "
        "been pushed yet, so a global binding firing here would discard the resolved "
        "conversation with no error at all"
    )


async def _reenter_during(app) -> None:
    import asyncio

    await asyncio.sleep(0.005)
    await app.action_resume()
