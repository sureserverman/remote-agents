"""The palette offers three ways to *go* somewhere and no way to *do* anything.

DEC-007 rests the safety of the destructive path on a confirmation whose cursor sits on the
abort — a force stop's confirmation, since DEC-018 leaves graceful stop and cleanup
unconfirmed. A command palette is a second route to whatever it exposes — type a fragment,
press enter — so an entry naming any session action would be a route around that
confirmation, and around the re-read and re-check standing in front of the unconfirmed two. These
tests are the check that no such entry exists, and that the declared table which the gate
sweeps is the table the palette actually serves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from backends import SessionUseCaseDouble, tui_context_for
from textual.command import DiscoveryHit, Hit

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.screens.palette import NAVIGATION_COMMANDS, NavigationCommands
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import ACTION_LABELS
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ProfileResumeCapability,
)
from remote_agents.domain.models import ProfileId, SessionRecord

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


@dataclass(slots=True)
class _Listing(SessionUseCaseDouble):
    records: tuple[SessionRecord, ...] = ()

    async def refresh_readiness(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def list_sessions(self) -> tuple[SessionRecord, ...]:
        return self.records

    async def copy_attach(self, _session_id: object) -> str | None:
        return None


class _Conversations:
    async def capabilities(self) -> tuple[ProfileResumeCapability, ...]:
        return (ProfileResumeCapability(ProfileId("claude"), True, True),)

    async def catalogue(self, _query: object) -> ConversationCataloguePage:
        return ConversationCataloguePage(
            conversations=(), page=1, page_count=1, unavailable_reason=None
        )


def _context(**overrides: object) -> TuiContext:
    arguments: dict[str, object] = {
        "sessions": _Listing(),
        "projects": object(),
        "profiles": (ProfileAvailability("claude", True),),
        "refresh_catalogue": lambda: (_PROJECT,),
        "attach_argv": lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        "catalogue": (_PROJECT,),
    }
    arguments.update(overrides)
    return tui_context_for(**arguments)


async def _discovered(app: RemoteAgentsTui) -> list[str]:
    provider = NavigationCommands(app.screen)
    return [str(hit.text) async for hit in provider.discover()]


def test_no_palette_entry_is_named_after_a_session_action() -> None:
    """The DEC-007 rule, asserted against the policy's own labels rather than a copied list."""
    names = {name for name, _help, _action in NAVIGATION_COMMANDS}

    assert names & set(ACTION_LABELS.values()) == set()


def test_every_entry_names_an_action_the_app_actually_has() -> None:
    """A palette entry that dispatches to nothing is a dead row nobody would notice."""
    for _name, _help, action in NAVIGATION_COMMANDS:
        assert hasattr(RemoteAgentsTui, f"action_{action}"), action


def test_no_entry_dispatches_to_an_action_rather_than_a_position() -> None:
    """Checked at the *action* as well as the label, because the label is only the caption.

    A future entry could be called "Clean up" and dispatch to `action_force_stop`. The name
    sweep above would not see that; this does.

    The set below is deliberately wider than "destructive", which DEC-018 narrowed to force
    stop and cleanup. `stop` is the graceful spelling and `resume_confirm` creates a session
    rather than ending one — neither is destructive, and both are still refused here, because
    what the palette must not do is *act* at all (DEC-007). Shrinking this set to the
    destructive two would open the route the file exists to keep closed.
    """
    acts_rather_than_navigates = {
        "stop",
        "force",
        "force_stop",
        "cleanup",
        "kill",
        "resume_confirm",
    }
    dispatched = {action for _name, _help, action in NAVIGATION_COMMANDS}

    assert dispatched & acts_rather_than_navigates == set()


async def test_the_palette_serves_exactly_the_declared_table() -> None:
    """Pins the constant to the behaviour, so the gate's sweep is not sweeping a fiction."""
    app = RemoteAgentsTui(_context(conversations=_Conversations()))

    async with app.run_test():
        offered = await _discovered(app)

    assert offered == [name for name, _help, _action in NAVIGATION_COMMANDS]
    assert offered == ["Sessions", "Resume", "Add project"]


async def test_resume_is_not_offered_on_a_host_that_wired_no_conversation_service() -> None:
    """The same answer the footer gives. An entry that does nothing is the complaint
    sub-plan 3 removed from the footer; the palette must not reintroduce it one key away."""
    app = RemoteAgentsTui(_context(conversations=None))

    async with app.run_test():
        offered = await _discovered(app)

    assert "Resume" not in offered
    assert offered == ["Sessions", "Add project"]


async def test_searching_narrows_to_matching_entries() -> None:
    app = RemoteAgentsTui(_context(conversations=_Conversations()))

    async with app.run_test():
        provider = NavigationCommands(app.screen)
        hits = [hit async for hit in provider.search("sess")]

    assert [str(hit.text) for hit in hits] == ["Sessions"]
    assert all(isinstance(hit, Hit) for hit in hits)


async def test_no_search_query_surfaces_a_session_action_either() -> None:
    """The search path is a second entry point and gets the same assertion as discovery."""
    app = RemoteAgentsTui(_context(conversations=_Conversations()))
    labels = set(ACTION_LABELS.values())

    async with app.run_test():
        provider = NavigationCommands(app.screen)
        for query in ("stop", "force", "kill", "clean", "e"):
            found = {str(hit.text) async for hit in provider.search(query)}
            assert found & labels == set(), f"query {query!r} surfaced {found & labels}"


async def test_choosing_an_entry_navigates_to_that_position() -> None:
    """The entry runs the app's own `action_*`, so the palette and the key cannot disagree."""
    record_time = datetime.now(UTC)
    assert record_time.tzinfo is UTC
    app = RemoteAgentsTui(_context(conversations=_Conversations()))

    async with app.run_test() as pilot:
        provider = NavigationCommands(app.screen)
        hits = [hit async for hit in provider.discover()]
        sessions = next(hit for hit in hits if str(hit.text) == "Sessions")
        assert isinstance(sessions, DiscoveryHit)

        sessions.command()
        await pilot.pause()
        await pilot.pause()
        landed = app.screen.position

    assert landed == "SESSIONS"


async def test_the_palette_is_reachable_at_all() -> None:
    """`ENABLE_COMMAND_PALETTE` defaults true, but a class could have turned it off."""
    app = RemoteAgentsTui(_context())

    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await pilot.pause()
        open_now = app.screen_stack[-1].__class__.__name__

    assert "Palette" in open_now or "CommandPalette" in open_now, open_now


class _Creator:
    def available_areas(self) -> tuple[str, ...]:
        return ("infra",)


async def test_the_palette_withholds_a_flow_jump_that_would_discard_typed_work() -> None:
    """The palette must not become the second route around a guard the key already honours.

    `ChoiceScreen.check_action` answers `None` — drawn but refused — for a flow jump while a
    value is being typed, which is how sub-plan 3 stopped `ctrl+s` throwing away a
    half-finished project name. This provider filtered on `is not False`, promoting every
    `None` to "available", and dispatched by reaching for `action_*` directly, which consults
    no guard at all. Reproduced before the fix: `ctrl+s` on `NAME` kept `half-typed-name`,
    and the palette's "Sessions" entry landed on the sessions list with the name gone.
    """
    from textual.widgets import Input

    app = RemoteAgentsTui(_context(projects=_Creator(), conversations=_Conversations()))

    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        await app.screen.choose("infra")
        await pilot.pause()
        app.screen.query_one("#filter", Input).value = "half-typed-name"
        await pilot.pause()

        assert app.screen.work_in_flight, "the fixture is not in the state under test"
        assert app.check_action("sessions", ()) is None, "expected drawn-but-refused"

        offered = await _discovered(app)

        # The key's own behaviour, as the control: refused, and the value survives.
        await pilot.press("ctrl+s")
        await pilot.pause()
        by_key = app.screen.position
        survived = app.screen.query_one("#filter", Input).value

    assert offered == [], f"the palette offered a jump the key refuses: {offered}"
    assert by_key == "NAME"
    assert survived == "half-typed-name"


async def test_an_entry_is_re_checked_when_it_is_chosen_not_only_when_it_is_listed() -> None:
    """The palette is a list read at one moment and acted on at another.

    Routing through `App.run_action` — which gates on `check_action` itself — is what makes
    that gap safe structurally, rather than by a filter that happens to be correct.
    """
    from textual.widgets import Input

    app = RemoteAgentsTui(_context(projects=_Creator(), conversations=_Conversations()))

    async with app.run_test() as pilot:
        hits = [hit async for hit in NavigationCommands(app.screen).discover()]
        sessions = next(hit for hit in hits if str(hit.text) == "Sessions")

        # Listed from the resting position, then chosen after the surface has moved into a
        # state that refuses it — the interleaving a list-time-only check cannot see.
        await pilot.press("ctrl+n")
        await pilot.pause()
        await app.screen.choose("infra")
        await pilot.pause()
        app.screen.query_one("#filter", Input).value = "typed-after-listing"
        await pilot.pause()

        sessions.command()
        await pilot.pause()
        await pilot.pause()
        landed = app.screen.position
        survived = app.screen.query_one("#filter", Input).value

    assert landed == "NAME", "a stale palette entry fired against a refusing screen"
    assert survived == "typed-after-listing"
