"""The fixed navigation bar every bot screen closes with.

`_message` is the one place a screen's closing row is built, so these drive real screens
through the boundary rather than asserting on that helper directly: a screen that stopped
routing through it would still pass a direct test of it, and that screen is exactly the
regression worth catching.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fake_telegram import FakeChat

from remote_agents.adapters.telegram.notifications import SessionGroup, render_activity
from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.application.conversations import ConversationService
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
from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind, AgentActivity

OWNER = 7
CHAT = 11
PROJECT = CatalogProject("a" * 24, "Demo", "tests", "Registered")


class _Catalogue:
    def __init__(self, resolved: ResolvedConversation) -> None:
        self.resolved = resolved

    async def list_conversations(self, **_kwargs) -> ConversationCataloguePage:
        return ConversationCataloguePage((self.resolved.summary,), 1, 1)

    async def resolve_conversation(self, reference: ConversationReference):
        return self.resolved if reference == self.resolved.summary.reference else None

    async def resume_capabilities(self):
        return (ProfileResumeCapability(ProfileId("claude"), True, True),)


class _Launcher:
    """One RUNNING session, so the list and the detail both have something to draw."""

    def __init__(self, record: SessionRecord) -> None:
        self.record = record

    async def list_sessions(self):
        return [self.record]

    async def refresh_readiness(self) -> None:
        return None


def _record() -> SessionRecord:
    return SessionRecord(
        SessionId(UUID(int=1)),
        ProjectId(PROJECT.opaque_id),
        ProfileId("claude"),
        SessionDisplayIdentity("Demo", "Claude", "regular", 1),
        SessionState.RUNNING,
        datetime(2026, 8, 17, tzinfo=UTC),
    )


def _activity_group() -> SessionGroup:
    session = "0191f2c2-0000-7000-8000-00000000abcd"
    activity = AgentActivity(
        session_id=session,
        kind=ActivityKind.COMPLETED,
        detail="wrote the parser",
        observed_at=datetime(2026, 8, 17, 12, tzinfo=UTC),
        confidence=ActivityConfidence.REPORTED,
    )
    return SessionGroup(session, (activity,))


def _resolved() -> ResolvedConversation:
    return ResolvedConversation(
        ConversationSummary(
            ConversationReference("c-0123456789abcdef"),
            ProfileId("claude"),
            ProjectId(PROJECT.opaque_id),
            ConversationState.RESUMABLE,
            datetime(2026, 8, 2, tzinfo=UTC),
        ),
        ProviderConversationId("provider-id"),
    )


def _boundary(*, with_resume: bool = True) -> PrivateBotBoundary:
    return PrivateBotBoundary(
        OWNER,
        CHAT,
        catalogue=(PROJECT,),
        profiles=(ProfileAvailability("claude", True, None),),
        launcher=_Launcher(_record()),
        conversations=ConversationService(_Catalogue(_resolved())) if with_resume else None,
    )


def _rows(message) -> list[list[str]]:
    return [[button.text for button in row] for row in message.reply_markup.inline_keyboard]


def _button(message, label: str) -> str:
    """The token behind a button, found by its label and tolerant of the active-tab marker.

    A bar button carries the marker exactly when the owner is already inside that flow, so a
    lookup that matched only the bare label would silently fail on the half of the cases
    where the tab is the one being stood in — which is most of them.
    """
    buttons = [button for row in message.reply_markup.inline_keyboard for button in row]
    # Exact first, across the whole keyboard: a screen carrying both a body "Launch another"
    # and the bar's "Launch" would otherwise answer "Launch" with the body button, and the
    # test would pass having pressed the wrong thing.
    for button in buttons:
        if button.text.removeprefix("• ") == label:
            return button.callback_data
    for button in buttons:
        if button.text.removeprefix("• ").startswith(label):
            return button.callback_data
    raise AssertionError(f"no {label!r} button in {message.text!r}")


async def _every_screen(
    chat: FakeChat, boundary: PrivateBotBoundary
) -> list[tuple[str, list[list[str]]]]:
    """Drive a screen from each family the bot draws, snapshotting each as it is drawn.

    Snapshots rather than message objects: the chat holds **one** bot message that every
    render mutates in place, so collecting the object four times collects the last screen
    four times — a test that reads as covering the bot and covers whichever screen happened
    to be drawn last.
    """
    seen: list[tuple[str, list[list[str]]]] = []

    def snapshot(anchor: int) -> None:
        seen.append((chat.messages[anchor].text, _rows(chat.messages[anchor])))

    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    snapshot(anchor)

    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)
    snapshot(anchor)

    await boundary.callback(chat.press(_button(chat.messages[anchor], "Inspect")), None)
    snapshot(anchor)

    await boundary.launch_command(chat.message_update("/launch"), None)
    snapshot(anchor)

    await boundary.help_command(chat.message_update("/help"), None)
    snapshot(anchor)
    return seen


def _unmarked(row: list[str]) -> list[str]:
    """The bar's labels without the active-tab marker, for assertions about its shape."""
    return [label.removeprefix("• ") for label in row]


@pytest.mark.asyncio
async def test_the_navigation_bar_closes_every_screen_that_carries_a_keyboard() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary()

    for text, rows in await _every_screen(chat, boundary):
        assert _unmarked(rows[-1]) == ["Sessions", "Launch", "Resume"], (
            f"screen {text!r} does not close with the navigation bar"
        )


@pytest.mark.asyncio
async def test_the_navigation_bar_omits_resume_when_no_conversation_service_is_wired() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(with_resume=False)

    for text, rows in await _every_screen(chat, boundary):
        assert _unmarked(rows[-1]) == ["Sessions", "Launch"], (
            f"screen {text!r} offers Resume with no conversation service wired"
        )


@pytest.mark.asyncio
async def test_the_navigation_bar_keeps_back_on_its_own_row_above_it() -> None:
    """Back is context-dependent and the bar is not; merging them would make the bar mean
    something different on every screen, which is the whole property it exists to have."""
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary()

    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)

    rows = _rows(chat.messages[anchor])
    assert _unmarked(rows[-1]) == ["Sessions", "Launch", "Resume"]
    assert rows[-2] == ["Back"]


@pytest.mark.asyncio
async def test_the_navigation_bar_replaced_the_home_button_on_every_screen() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary()

    for text, rows in await _every_screen(chat, boundary):
        labels = {label for row in rows for label in row}
        assert "Home" not in labels, f"screen {text!r} still offers Home"


class _LaunchingLauncher(_Launcher):
    """Answers a launch, so the pending screen this test is about is actually drawn."""

    async def launch(self, _command):
        return self.record


def _recording(chat: FakeChat) -> list[tuple[str, object]]:
    """Every render this chat receives, in order, including ones later replaced.

    The chat holds one bot message that each render overwrites, so a screen that exists
    only until the next one lands — which is exactly what a pending screen is — leaves no
    trace in the final state. Recording the edits is the only way to assert about it.
    """
    seen: list[tuple[str, object]] = []
    original = chat.bot.edit_message_text

    async def recording(**kwargs):
        seen.append((str(kwargs.get("text", "")), kwargs.get("reply_markup")))
        await original(**kwargs)

    chat.bot.edit_message_text = recording
    return seen


@pytest.mark.asyncio
async def test_the_pending_screen_stays_barless_while_it_waits() -> None:
    """A wait must not be pressable into a second launch, so it drops the whole keyboard —
    and the bar is not exempt from that. `callback` renders it through `render_message`
    directly rather than through `_message`, which is the mechanism, not an oversight."""
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = PrivateBotBoundary(
        OWNER,
        CHAT,
        catalogue=(PROJECT,),
        profiles=(ProfileAvailability("claude", True, None),),
        launcher=_LaunchingLauncher(_record()),
    )
    rendered = _recording(chat)

    await boundary.launch_command(chat.message_update("/launch"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Claude")), None)

    pending = [markup for text, markup in rendered if "Launching" in text]
    assert pending, f"no pending screen was drawn; saw {[text for text, _ in rendered]}"
    for markup in pending:
        # No *buttons*, rather than no markup: an empty keyboard still arrives as an empty
        # `InlineKeyboardMarkup`, and what must not be pressable is a button.
        buttons = [button for row in getattr(markup, "inline_keyboard", ()) for button in row]
        assert buttons == [], f"the pending screen offered {[b.text for b in buttons]}"


def test_an_activity_notification_stays_barless_and_carries_only_its_one_button() -> None:
    """A notification is a message, not a screen. It is not somewhere the owner navigates
    from, and it is deleted when its one button is pressed."""
    group = _activity_group()

    rendered = render_activity(
        group, display="Demo · Claude · regular · #1", open_session="c1_open"
    )

    keyboard = [[button.text for button in row] for row in rendered.keyboard]
    assert keyboard == [["Open session"]]


class _Creator:
    """Enough of a project creator that the Add Project step can be opened."""

    def available_areas(self) -> tuple[str, ...]:
        return ("infra",)


def _open_boxes(chat: FakeChat) -> list[object]:
    """The input boxes still standing in the chat, found by their ForceReply markup."""
    return [
        message
        for message in chat.messages.values()
        if type(getattr(message, "reply_markup", None)).__name__ == "ForceReply"
    ]


async def _open_entry(chat: FakeChat, boundary: PrivateBotBoundary, entry: str) -> int:
    """Leave one guided text step open, and answer with the live view's anchor."""
    if entry == "session.rename":
        await boundary.sessions_command(chat.message_update("/sessions"), None)
        anchor = chat.bot_messages[0].message_id
        await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)
        await boundary.callback(chat.press(_button(chat.messages[anchor], "Rename")), None)
        return anchor
    if entry == "project.area":
        # Reached from the launch list, where Task 2.2 moved it -- the screen that can tell
        # you the project is missing is the screen that offers to create it.
        await boundary.launch_command(chat.message_update("/launch"), None)
        anchor = chat.bot_messages[0].message_id
        await boundary.callback(chat.press(_button(chat.messages[anchor], "Add Project")), None)
        await boundary.callback(chat.press(_button(chat.messages[anchor], "infra")), None)
        return anchor
    await boundary.launch_command(chat.message_update("/launch"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Search")), None)
    return anchor


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["launch.search", "session.rename", "project.area"])
@pytest.mark.parametrize("tab", ["Sessions", "Launch", "Resume"])
async def test_the_bar_abandons_entry_rather_than_stranding_its_input_box(
    entry: str, tab: str
) -> None:
    """The TUI greys the keys that leave a flow holding unsaved work; Telegram has no such
    state, so the bar has to *do* something coherent instead. Abandoning matches what every
    other button here already does, and a stranded input box — one the owner can still type
    into, attached to a step nothing is waiting on — is the worse of the two answers."""
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = PrivateBotBoundary(
        OWNER,
        CHAT,
        catalogue=(PROJECT,),
        profiles=(ProfileAvailability("claude", True, None),),
        launcher=_Launcher(_record()),
        conversations=ConversationService(_Catalogue(_resolved())),
        creator=_Creator(),
    )

    anchor = await _open_entry(chat, boundary, entry)
    assert _open_boxes(chat), f"{entry} did not leave an input box open"

    await boundary.callback(chat.press(_button(chat.messages[anchor], tab)), None)

    assert _open_boxes(chat) == [], f"pressing {tab} stranded the {entry} input box"
    assert _unmarked(_rows(chat.messages[anchor])[-1])[0] == "Sessions"


@pytest.mark.asyncio
async def test_the_active_tab_is_marked_on_a_screen_inside_that_flow() -> None:
    """Telegram will not style a pressed tab, so three identical rows on a dozen screens
    stop saying where you are. The marker is the only orienting signal available."""
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary()

    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    assert _rows(chat.messages[anchor])[-1] == ["• Sessions", "Launch", "Resume"]

    await boundary.launch_command(chat.message_update("/launch"), None)
    assert _rows(chat.messages[anchor])[-1] == ["Sessions", "• Launch", "Resume"]


@pytest.mark.asyncio
async def test_the_active_tab_follows_a_screen_deeper_into_its_flow() -> None:
    """A session detail is still the sessions flow: the marker tracks where the owner is,
    not which button they last pressed."""
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary()

    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)

    assert _rows(chat.messages[anchor])[-1] == ["• Sessions", "Launch", "Resume"]

    await boundary.callback(chat.press(_button(chat.messages[anchor], "Inspect")), None)
    assert _rows(chat.messages[anchor])[-1] == ["• Sessions", "Launch", "Resume"]


@pytest.mark.asyncio
async def test_the_active_tab_is_marked_on_a_resume_flow_screen_too() -> None:
    """The third flow, which the other two cases cannot cover for it: `_flow_of`'s resume
    mapping gates a real button label and nothing else asserts it."""
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary()

    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Resume")), None)

    assert _rows(chat.messages[anchor])[-1] == ["Sessions", "Launch", "• Resume"]

    # And deeper in: choosing a project stays inside the flow it was chosen from.
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)
    assert _rows(chat.messages[anchor])[-1] == ["Sessions", "Launch", "• Resume"]


class _ManyLauncher(_Launcher):
    """A mixed list, so the two counts are distinguishable from each other and from len()."""

    def __init__(self, records: list[SessionRecord]) -> None:
        self.records = records

    async def list_sessions(self):
        return self.records


def _sessions_counts_boundary(states: list[SessionState]) -> PrivateBotBoundary:
    records = [
        SessionRecord(
            SessionId(UUID(int=index + 1)),
            ProjectId(PROJECT.opaque_id),
            ProfileId("claude"),
            SessionDisplayIdentity("Demo", "Claude", "regular", index + 1),
            state,
            datetime(2026, 8, 17, tzinfo=UTC),
        )
        for index, state in enumerate(states)
    ]
    return PrivateBotBoundary(
        OWNER, CHAT, catalogue=(PROJECT,), launcher=_ManyLauncher(records)
    )


@pytest.mark.asyncio
async def test_the_sessions_counts_lead_the_list_that_owns_them() -> None:
    """Home's whole content was these two numbers, and they are counts *of sessions* — so
    they belong on the list rather than on a screen in front of it."""
    boundary = _sessions_counts_boundary(
        [SessionState.RUNNING, SessionState.RUNNING, SessionState.PRESERVED]
    )

    rendered = await boundary._sessions_reply()

    assert "2 active" in rendered.text
    assert "1 preserved" in rendered.text


@pytest.mark.asyncio
async def test_the_sessions_counts_are_still_rendered_when_nothing_is_running() -> None:
    """Both zero rather than absent: a line that appears and disappears is a line the owner
    has to re-find, and the empty list is exactly when they are looking for it."""
    boundary = _sessions_counts_boundary([])

    rendered = await boundary._sessions_reply()

    assert "0 active" in rendered.text
    assert "0 preserved" in rendered.text
    assert "Nothing is running." in rendered.text


@pytest.mark.asyncio
async def test_the_sessions_counts_come_from_the_records_the_list_pages() -> None:
    """One read, not two. A second read could disagree with the rows underneath it, and a
    header that contradicts its own list is worse than no header."""
    boundary = _sessions_counts_boundary([SessionState.RUNNING] * 12)
    boundary.session_page_size = 5

    second_page = await boundary._sessions_reply(2)

    # Counts are of the whole list, not of the page — the page shows 5 of them.
    assert "12 active" in second_page.text
    assert "Sessions 2/3" in second_page.text


def _picker_boundary(*, creator: object | None) -> PrivateBotBoundary:
    return PrivateBotBoundary(
        OWNER,
        CHAT,
        catalogue=(PROJECT,),
        profiles=(ProfileAvailability("claude", True, None),),
        launcher=_Launcher(_record()),
        conversations=ConversationService(_Catalogue(_resolved())),
        creator=creator,
    )


def test_add_project_on_launch_sits_beside_search_where_the_project_is_missing() -> None:
    """You want a new project at the moment you cannot find the one you wanted, which is
    this screen — not a dashboard in front of it."""
    boundary = _picker_boundary(creator=_Creator())

    rendered = boundary._projects_reply(boundary.catalogue, view_id="all")

    rows = [[button.text for button in row] for row in rendered.keyboard]
    assert ["Search", "Add Project"] in rows


def test_add_project_on_launch_is_absent_when_no_creator_is_wired() -> None:
    boundary = _picker_boundary(creator=None)

    rendered = boundary._projects_reply(boundary.catalogue, view_id="all")

    labels = {label for row in rendered.keyboard for label in [b.text for b in row]}
    assert "Add Project" not in labels


def test_add_project_on_launch_never_appears_in_the_resume_picker() -> None:
    """Both flows share one renderer, so this is the regression that renderer invites: you
    cannot resume a conversation in a project that does not exist yet."""
    boundary = _picker_boundary(creator=_Creator())

    rendered = boundary._projects_reply(boundary.catalogue, view_id="all", flow="resume")

    labels = {label for row in rendered.keyboard for label in [b.text for b in row]}
    assert "Add Project" not in labels


@pytest.mark.asyncio
async def test_start_lands_on_sessions_whatever_is_running() -> None:
    """Unconditionally, not "sessions if anything is running, else launch". A landing screen
    that moves with state is one the owner cannot build muscle memory for, and the bar puts
    Launch one press away from an empty list anyway."""
    for states in ([SessionState.RUNNING], []):
        chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
        boundary = _sessions_counts_boundary(states)

        await boundary.start(chat.message_update("/start"), None)

        shown = chat.messages[chat.bot_messages[0].message_id]
        assert "Sessions" in shown.text and "active" in shown.text, shown.text
        assert _rows(shown)[-1][0] == "• Sessions"


@pytest.mark.asyncio
async def test_start_lands_on_sessions_for_a_token_minted_before_the_upgrade() -> None:
    """A `nav.home` token outlives the deploy that stopped drawing it — tokens live in SQLite
    and are valid for their message, not for a clock (DEC-011). Re-pointing it is what keeps
    it from becoming the dead button the callback store exists to prevent."""
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _sessions_counts_boundary([SessionState.RUNNING])

    await boundary.start(chat.message_update("/start"), None)
    anchor = chat.bot_messages[0].message_id
    legacy = boundary._callback("nav.home", "home")
    boundary.callbacks.bind_pending(CHAT, anchor)

    await boundary.callback(chat.press(legacy, on=anchor), None)

    assert "Sessions" in chat.messages[anchor].text
    assert "no longer available" not in chat.messages[anchor].text


class _OutcomeLauncher(_Launcher):
    """Answers a launch with whatever state the case under test needs."""

    def __init__(self, record: SessionRecord) -> None:
        self.record = record

    async def list_sessions(self):
        return []

    async def launch(self, _command):
        return self.record


class _RealCreator(_Creator):
    def create(self, command):
        from remote_agents.application.project_admin import CreatedProject
        from remote_agents.domain.projects import ProjectIdentity

        identity = ProjectIdentity(area=command.area, name=command.name)
        return CreatedProject(identity, pathlib.Path("/dev") / command.area / command.name)


def _outcome_boundary(state: SessionState) -> PrivateBotBoundary:
    record = SessionRecord(
        SessionId(UUID(int=9)),
        ProjectId(PROJECT.opaque_id),
        ProfileId("claude"),
        SessionDisplayIdentity("Demo", "Claude", "regular", 1),
        state,
        datetime(2026, 8, 17, tzinfo=UTC),
    )
    return PrivateBotBoundary(
        OWNER,
        CHAT,
        catalogue=(PROJECT,),
        profiles=(ProfileAvailability("claude", True, None),),
        launcher=_OutcomeLauncher(record),
        conversations=ConversationService(_Catalogue(_resolved())),
        creator=_RealCreator(),
    )


async def _outcome_screens() -> dict[str, object]:
    """The four screens the Stage 1 reviews named, rendered by the code that really draws
    them rather than reconstructed here."""
    empty = _sessions_counts_boundary([])  # genuinely empty, not "a boundary I hope is empty"
    failed = _outcome_boundary(SessionState.FAILED)
    created = _outcome_boundary(SessionState.RUNNING)
    project = _outcome_boundary(SessionState.RUNNING)
    launch_id = f"{PROJECT.opaque_id}|claude"

    def claimed(boundary: PrivateBotBoundary, action: str, entity: str) -> str:
        token = boundary._callback(action, entity, mutation=True)
        boundary.callbacks.bind_pending(CHAT, 1)
        return token

    return {
        "empty sessions": await empty._sessions_reply(),
        "launch failed": (
            await failed._launch_reply(launch_id, claimed(failed, "launch.profile", launch_id), 1)
        )["reply_markup"],
        "session created": (
            await created._launch_reply(launch_id, claimed(created, "launch.profile", launch_id), 1)
        )["reply_markup"],
        "project created": (
            await project._project_reply(
                "infra|thing", claimed(project, "project.confirm", "infra|thing"), 1
            )
        )["reply_markup"],
    }


@pytest.mark.asyncio
async def test_no_screen_offers_a_body_button_that_duplicates_the_bar() -> None:
    """The class the Stage 1 reviews enumerated: buttons that were the only way out before a
    permanent way out existed. A body `Launch` directly above the bar's `Launch` reads as a
    bug, and on the launch-failure screen it sat above a *marked* `• Launch`.

    Swept over every screen in the set rather than the one the plan happened to name.
    """
    bar = {"Sessions", "Launch", "Resume", "Launch another"}

    for name, rendered in (await _outcome_screens()).items():
        keyboard = getattr(rendered, "keyboard", None) or rendered.inline_keyboard
        rows = [[button.text for button in row] for row in keyboard]
        body = [label for row in rows[:-1] for label in row]
        assert not (set(body) & bar), f"{name} duplicates the bar: {body}"


@pytest.mark.asyncio
async def test_the_bar_never_sits_directly_under_an_irreversible_button() -> None:
    """The bar is the one row the owner builds muscle memory for, so it is also the worst
    thing to put a kill button immediately above. Cancel buffers it.

    This is the rule `_force_confirm_reply` already stated — the destructive button must not
    be where the thumb rests — applied to a bottom row that changed underneath it.
    """
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary()

    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Force stop")), None)

    rows = _rows(chat.messages[anchor])
    assert "Force stop" in rows[-3], f"expected the confirm screen, got {rows}"
    assert rows[-2] == ["Cancel"], "nothing harmless separates the kill button from the bar"
    assert _unmarked(rows[-1]) == ["Sessions", "Launch", "Resume"]


@pytest.mark.asyncio
async def test_the_add_project_wizard_is_marked_as_the_launch_flow() -> None:
    """Pinned because it is the one mapping in `_FLOW_OF_PREFIX` whose truth is scheduled:
    the wizard is entered from Home today and moves onto the launch list in Task 2.2."""
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = PrivateBotBoundary(
        OWNER,
        CHAT,
        catalogue=(PROJECT,),
        profiles=(ProfileAvailability("claude", True, None),),
        launcher=_Launcher(_record()),
        conversations=ConversationService(_Catalogue(_resolved())),
        creator=_Creator(),
    )

    await boundary.launch_command(chat.message_update("/launch"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Add Project")), None)

    assert _rows(chat.messages[anchor])[-1] == ["Sessions", "• Launch", "Resume"]


@pytest.mark.asyncio
async def test_the_active_tab_marks_nothing_on_a_screen_that_belongs_to_no_flow() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary()

    await boundary.help_command(chat.message_update("/help"), None)
    anchor = chat.bot_messages[0].message_id

    assert _rows(chat.messages[anchor])[-1] == ["Sessions", "Launch", "Resume"]
