"""The fixed navigation bar every bot screen closes with.

`_message` is the one place a screen's closing row is built, so these drive real screens
through the boundary rather than asserting on that helper directly: a screen that stopped
routing through it would still pass a direct test of it, and that screen is exactly the
regression worth catching.
"""

from __future__ import annotations

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
    for row in message.reply_markup.inline_keyboard:
        for button in row:
            text = button.text.removeprefix("• ")
            if text == label or text.startswith(label):
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
        # Reached through Home, which is still where Add Project lives at this stage; Task
        # 2.2 moves it onto the launch list and this route goes with it.
        await boundary.start(chat.message_update("/start"), None)
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


@pytest.mark.asyncio
async def test_the_active_tab_marks_nothing_on_a_screen_that_belongs_to_no_flow() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary()

    await boundary.help_command(chat.message_update("/help"), None)
    anchor = chat.bot_messages[0].message_id

    assert _rows(chat.messages[anchor])[-1] == ["Sessions", "Launch", "Resume"]
