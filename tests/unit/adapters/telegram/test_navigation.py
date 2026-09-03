"""The fixed navigation bar every bot screen closes with.

`_message` is the one place a screen's closing row is built, so these drive real screens
through the boundary rather than asserting on that helper directly: a screen that stopped
routing through it would still pass a direct test of it, and that screen is exactly the
regression worth catching.
"""

from __future__ import annotations

import pathlib
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from backends import SessionUseCaseDouble, backend_for
from fake_telegram import FakeChat

from remote_agents.adapters.telegram.notifications import render_activity
from remote_agents.adapters.telegram.presenters import unpadded
from remote_agents.adapters.telegram.service import PrivateBotBoundary, build_private_bot
from remote_agents.application.conversations import ConversationService
from remote_agents.application.notification_policy import SessionGroup
from remote_agents.application.profiles import ProfileAvailability
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


class _Launcher(SessionUseCaseDouble):
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
    """The wired bot these thirty-two tests drive.

    One backend, handed over whole. It was briefly two — a `backend_for(...)` assembled and
    then taken apart into the boundary's separate fields — which was Task 3.1's scaffolding
    while the boundary still declared them individually. Task 3.2 removed that need, and
    leaving the copy in would have made this helper quietly lossy: a `capture=` or
    `profiles=` added to the factory call would never have reached the bot, because only
    four field names were being copied across.

    `profiles` is routed through the backend, as of sub-plan 4. It was not, for as long as
    `Backend.profiles` held the domain `ProfileCompatibility` and each surface narrowed it
    separately -- handing that tuple straight over was the line that once took the local
    surface down on a version probe that merely timed out. `compose_backend` narrows once
    now, so the boundary and the surface are given the same one.
    """
    return build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(
            sessions=_Launcher(_record()),
            projects=_Creator(),
            conversations=ConversationService(_Catalogue(_resolved())) if with_resume else None,
            catalogue=(PROJECT,),
        ),
        profiles=(ProfileAvailability("claude", True, None),),
    )


def _rows(message) -> list[list[str]]:
    return [
        [unpadded(button.text) for button in row] for row in message.reply_markup.inline_keyboard
    ]


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
        if unpadded(button.text).removeprefix("• ") == label:
            return button.callback_data
    for button in buttons:
        if unpadded(button.text).removeprefix("• ").startswith(label):
            return button.callback_data
    # A session picker reads `🟢 #7 Demo` and an action button `📄 Inspect` since the redesign:
    # the label a test names is the *last* token, behind a mark it did not write.
    for button in buttons:
        if unpadded(button.text).endswith(f" {label}"):
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

    **Breadth is the point, and it has been wrong before.** The invariants built on this
    helper read as claims about every screen, so a family missing from here is a family
    those invariants silently exempt — which is how a resume-flow screen with no Back
    survived a stage. The families are: the sessions list, a session detail, a capture, the
    launch picker, the resume picker, the Add Project wizard, a destructive confirmation,
    and help. Screens still outside it: the launch/resume outcome screens and Copy Attach,
    both of which need a launcher that answers, and the empty list, which needs a boundary
    with no sessions — all three are covered by their own tests instead.
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

    # Back to the detail first: the capture screen offers no session actions of its own.
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Back")), None)
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Force stop")), None)
    snapshot(anchor)

    await boundary.launch_command(chat.message_update("/launch"), None)
    snapshot(anchor)

    if boundary.backend.projects is not None:
        await boundary.callback(chat.press(_button(chat.messages[anchor], "Add Project")), None)
        snapshot(anchor)

    if boundary.backend.conversations is not None:
        await boundary.callback(chat.press(_button(chat.messages[anchor], "Resume")), None)
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
    assert rows[-2] == ["‹ Back to sessions"]


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
    boundary = build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(catalogue=(PROJECT,), sessions=_LaunchingLauncher(_record())),
        profiles=(ProfileAvailability("claude", True, None),),
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
    boundary = build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(
            catalogue=(PROJECT,),
            sessions=_Launcher(_record()),
            conversations=ConversationService(_Catalogue(_resolved())),
            projects=_Creator(),
        ),
        profiles=(ProfileAvailability("claude", True, None),),
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
    return build_private_bot(
        OWNER, CHAT, backend=backend_for(catalogue=(PROJECT,), sessions=_ManyLauncher(records))
    )


@pytest.mark.asyncio
async def test_the_sessions_counts_lead_the_list_that_owns_them() -> None:
    """Home's whole content was these two numbers, and they are counts *of sessions* — so
    they belong on the list rather than on a screen in front of it."""
    boundary = _sessions_counts_boundary(
        [SessionState.RUNNING, SessionState.RUNNING, SessionState.PRESERVED]
    )

    rendered = await boundary._sessions_reply()

    # The emoji legend is the count: two active, one preserved, and no bucket in between.
    assert "<b>Sessions</b> · 3  🟢 2 · ⚪ 1" in rendered.text


@pytest.mark.asyncio
async def test_the_sessions_counts_are_still_rendered_when_nothing_is_running() -> None:
    """The total stays even at zero: a line that appears and disappears is a line the owner
    has to re-find, and the empty list is exactly when they are looking for it. The emoji
    legend is the per-bucket count and an empty bucket is left out of it, so at zero the
    header is the total alone."""
    boundary = _sessions_counts_boundary([])

    rendered = await boundary._sessions_reply()

    assert rendered.text.startswith("<b>Sessions</b> · 0\n")
    assert "Nothing is running." in rendered.text


@pytest.mark.asyncio
async def test_the_sessions_counts_reconcile_with_the_rows_they_sit_above() -> None:
    """Every listed state lands in one of four buckets, and the legend counts each bucket.

    The old two-word header ("0 active · 0 preserved") had no place for STARTING,
    STOP_REQUESTED, FAILED or ORPHANED, so four such rows sat under a header that said nothing
    was happening. The redesign's buckets cover every listed state -- these four are two in
    transition and two needing attention -- and the rows are headed by the same buckets, so the
    arithmetic is checkable against the sections themselves.
    """
    boundary = _sessions_counts_boundary(
        [
            SessionState.ORPHANED,
            SessionState.FAILED,
            SessionState.STARTING,
            SessionState.STOP_REQUESTED,
        ]
    )

    rendered = await boundary._sessions_reply()

    assert "<b>Sessions</b> · 4  🟡 2 · 🔴 2" in rendered.text
    assert "🟢" not in rendered.text.split("\n")[0], "an empty bucket is not in the legend"
    assert "<b>IN TRANSITION</b>" in rendered.text and "<b>NEEDS ATTENTION</b>" in rendered.text
    # Two pickers to a row, plus the bar.
    assert sum(len(row) for row in rendered.keyboard[:-1]) == 4, "one picker per session"


@pytest.mark.asyncio
async def test_the_sessions_counts_come_from_the_records_the_list_pages() -> None:
    """One read, not two. A second read could disagree with the rows underneath it, and a
    header that contradicts its own list is worse than no header."""
    boundary = _sessions_counts_boundary([SessionState.RUNNING] * 12)
    boundary.session_page_size = 5

    second_page = await boundary._sessions_reply(2)

    # Counts are of the whole list, not of the page — the page shows 5 of them.
    assert "<b>Sessions 2/3</b> · 12  🟢 12" in second_page.text
    assert second_page.text.count("<code>running") == 5


def _picker_boundary(*, creator: object | None) -> PrivateBotBoundary:
    return build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(
            catalogue=(PROJECT,),
            sessions=_Launcher(_record()),
            conversations=ConversationService(_Catalogue(_resolved())),
            projects=creator,
        ),
        profiles=(ProfileAvailability("claude", True, None),),
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
        assert shown.text.startswith("<b>Sessions</b> · "), shown.text
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
    return build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(
            catalogue=(PROJECT,),
            sessions=_OutcomeLauncher(record),
            conversations=ConversationService(_Catalogue(_resolved())),
            projects=_RealCreator(),
        ),
        profiles=(ProfileAvailability("claude", True, None),),
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


def test_owner_commands_mirror_the_navigation_bar() -> None:
    """The menu and the bar should name the same places. `/resume` had never been listed at
    all, and `/start` is unlisted now that it lands where `/sessions` does — it stays
    *registered* because Telegram requires it of every bot."""
    from remote_agents.adapters.telegram.service import _OWNER_COMMANDS

    assert [command.command for command in _OWNER_COMMANDS] == [
        "launch",
        "resume",
        "sessions",
        "help",
    ]


def test_owner_commands_no_longer_have_a_home_to_render() -> None:
    import remote_agents.adapters.telegram.presenters as presenters
    import remote_agents.adapters.telegram.service as service

    assert not hasattr(presenters, "render_home")
    assert not hasattr(service.PrivateBotBoundary, "_home_reply")


@pytest.mark.asyncio
async def test_owner_commands_reach_the_resume_picker_when_resume_is_wired() -> None:
    """`/resume` is new dispatch wiring, not a rename, so its success path needs its own
    proof: nothing else shows that the *command* reaches the picker the Resume button does."""
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary()

    await boundary.resume_command(chat.message_update("/resume"), None)

    shown = chat.messages[chat.bot_messages[0].message_id]
    assert shown.text.startswith("<b>Resume 1/1</b>")
    assert _rows(shown)[0] == ["Demo"]
    assert _rows(shown)[-1] == ["Sessions", "Launch", "• Resume"]


@pytest.mark.asyncio
async def test_owner_commands_answer_resume_even_where_it_is_unavailable() -> None:
    """The menu is set once for the chat and cannot vary per screen the way the bar does,
    so `/resume` is listed on a composition that wires no conversation service. It answers
    with a sentence rather than nothing."""
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(with_resume=False)

    await boundary.resume_command(chat.message_update("/resume"), None)

    shown = chat.messages[chat.bot_messages[0].message_id]
    assert "Resume is unavailable." in shown.text
    assert _unmarked(_rows(shown)[-1]) == ["Sessions", "Launch"]


class _ResumingLauncher(_Launcher):
    def __init__(self, record: SessionRecord) -> None:
        self.record = record
        self.commands: list[object] = []

    async def list_sessions(self):
        return []

    async def resume(self, command):
        self.commands.append(command)
        return ResumeOutcome(self.record, created=True)


def _resume_boundary() -> tuple[PrivateBotBoundary, _ResumingLauncher]:
    launcher = _ResumingLauncher(_record())
    return (
        build_private_bot(
            OWNER,
            CHAT,
            backend=backend_for(
                catalogue=(PROJECT,),
                sessions=launcher,
                conversations=ConversationService(_Catalogue(_resolved())),
            ),
            profiles=(ProfileAvailability("claude", True, None),),
        ),
        launcher,
    )


@pytest.mark.asyncio
async def test_resume_without_review_makes_the_conversation_choice_the_act() -> None:
    """Launch stopped asking for a review when choosing the agent became the act. Choosing a
    named conversation is a *more* specific choice than choosing an agent, so resume was
    charging an extra press for less ambiguity.

    **Renamed from `..._reaches_a_session_at_launch_s_depth`, which claimed a comparison
    nothing here makes** — and one that is false as stated: resume is three presses
    (project, profile, conversation) against launch's two, because it has one more thing to
    choose. The plan's own residual records that. What is actually asserted, and what the
    stage was about, is that no *review* stands between the terminal choice and the act; a
    name that promises a tap count invites the next reader to trust an assertion that was
    never written, which is the over-claiming DEC-019 exists to police.
    """
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary, launcher = _resume_boundary()

    await boundary.resume_command(chat.message_update("/resume"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Claude")), None)
    conversation = _rows(chat.messages[anchor])[0][0]
    await boundary.callback(chat.press(_button(chat.messages[anchor], conversation)), None)

    assert len(launcher.commands) == 1, "choosing the conversation is the act"
    assert "Session resumed" in chat.messages[anchor].text
    assert "Review resume" not in chat.messages[anchor].text


@pytest.mark.asyncio
async def test_resume_without_review_drops_a_repeated_press(monkeypatch) -> None:
    """DEC-008 makes the *repeat* safe: the one-shot claim drops the second press rather than
    servicing it into a second session. It says nothing about the first, unintended press —
    the phrasing this docstring used to carry ("what makes one press safe") is the exact
    error that let a one-press resume ship and become a gate escalation, and what actually
    bounds the first press is migration 8, which frees the conversation once its session
    ends."""
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary, launcher = _resume_boundary()

    await boundary.resume_command(chat.message_update("/resume"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Claude")), None)
    conversation = _rows(chat.messages[anchor])[0][0]
    token = _button(chat.messages[anchor], conversation)

    await boundary.callback(chat.press(token, on=anchor), None)
    await boundary.callback(chat.press(token, on=anchor), None)

    assert len(launcher.commands) == 1, "the repeat was dropped, not serviced twice"


@pytest.mark.asyncio
async def test_resume_without_review_still_refuses_a_project_that_left_the_catalogue() -> None:
    """The check the deleted review screen carried. It has to move to the act rather than
    leave with the screen — and before the claim, so a stale one-shot is not burned on a
    project that has gone, which is the reasoning `_launch_reply` already records."""
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary, launcher = _resume_boundary()

    await boundary.resume_command(chat.message_update("/resume"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Claude")), None)
    conversation = _rows(chat.messages[anchor])[0][0]
    token = _button(chat.messages[anchor], conversation)

    boundary.catalogue = ()
    await boundary.callback(chat.press(token, on=anchor), None)

    assert launcher.commands == []
    assert "no longer available" in chat.messages[anchor].text


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
    boundary = build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(
            catalogue=(PROJECT,),
            sessions=_Launcher(_record()),
            conversations=ConversationService(_Catalogue(_resolved())),
            projects=_Creator(),
        ),
        profiles=(ProfileAvailability("claude", True, None),),
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


class _BoundLauncher(_Launcher):
    """Answers resume with an existing record and, by default, `created=False` — which is what
    the service does for a conversation already attached to a session: it starts nothing and
    hands back what it found.

    `created` is a parameter rather than a constant because the two halves are different
    screens and one of them had no double at all. A FAILED record with `created=True` is a
    resume that really was created and whose pane did not come up; with `created=False` it is
    a conversation bound to a session that failed earlier, and this press started nothing.

    *An earlier version of this docstring asserted that difference while the code checked
    FAILED first and rendered both identically, and added that the FAILED guard could have
    been deleted with the suite still green — also false, since deleting it drops FAILED into
    the "Not resumed" branch and fails the parametrized row. Both sentences were taken from a
    review's premise rather than read off the code, which is the very habit that review was
    reporting elsewhere. They are true now because close-out moved the guard behind `created`,
    which is what makes the two screens actually differ.*
    """

    def __init__(self, record: SessionRecord, *, created: bool = False) -> None:
        self.record = record
        self.created = created
        self.commands: list[object] = []

    async def list_sessions(self):
        return []

    async def resume(self, command):
        self.commands.append(command)
        return ResumeOutcome(self.record, created=self.created)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        # FAILED answers "Not resumed" *here* because this whole parametrization runs against
        # `_BoundLauncher`'s default `created=False` — a conversation already bound to a
        # session that failed. The "Resume did not become ready" screen belongs to the press
        # that actually created the failed session, and is pinned separately below.
        (SessionState.FAILED, "Not resumed"),
        (SessionState.PRESERVED, "Not resumed"),
        (SessionState.STOP_REQUESTED, "Not resumed"),
        (SessionState.ORPHANED, "Not resumed"),
        # The two the list omitted, and the two that matter most: RUNNING and STARTING are
        # the *commonest* things `_resume_locked` hands back for an already-bound
        # conversation, and they are the states a genuine resume also returns. Branching on
        # the state alone therefore cannot tell "I started this" from "this was already
        # attached and I started nothing" — which is why the bot claimed "Session resumed"
        # over a live session it had not touched, contradicting both the README and step 12
        # of the acceptance checklist.
        (SessionState.RUNNING, "Not resumed"),
        (SessionState.STARTING, "Not resumed"),
    ],
)
async def test_resume_without_review_answers_every_state_it_can_return(
    state: SessionState, expected: str
) -> None:
    """Every state `_resume_locked` can hand back, not just the one that was easy to build.

    The gap this closes is specific: the FAILED branch was shadowed by a duplicated guard and
    nothing noticed, because no test asserted the FAILED-on-resume message. A resume that
    really did create a session which failed to come up was being told its conversation was
    attached to something it could not recover — when `refresh_readiness` and a force stop are
    both real recoveries.
    """
    record = replace(_record(), state=state)
    boundary = build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(
            catalogue=(PROJECT,),
            sessions=_BoundLauncher(record),
            conversations=ConversationService(_Catalogue(_resolved())),
        ),
        profiles=(ProfileAvailability("claude", True, None),),
    )
    token = boundary._callback("resume.confirm", "c-0123456789abcdef", mutation=True)
    boundary.callbacks.bind_pending(CHAT, 1)

    result = await boundary._resume_reply("c-0123456789abcdef", token, 1)

    assert expected in str(result["text"])
    assert "Session resumed" not in str(result["text"])


@pytest.mark.asyncio
async def test_resume_without_review_does_not_claim_an_attachment_is_final() -> None:
    """Migration 8 releases the conversation once its session ends, so the message must not
    say recovery is impossible — it was false for PRESERVED, which one Clean up releases."""
    record = replace(_record(), state=SessionState.PRESERVED)
    boundary = build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(
            catalogue=(PROJECT,),
            sessions=_BoundLauncher(record),
            conversations=ConversationService(_Catalogue(_resolved())),
        ),
        profiles=(ProfileAvailability("claude", True, None),),
    )
    token = boundary._callback("resume.confirm", "c-0123456789abcdef", mutation=True)
    boundary.callbacks.bind_pending(CHAT, 1)

    text = str((await boundary._resume_reply("c-0123456789abcdef", token, 1))["text"])

    assert "not something this tool can do" not in text
    assert "once that session has ended" in text


@pytest.mark.asyncio
async def test_resume_without_review_says_when_nothing_was_started() -> None:
    """`_resume_locked` returns the *existing* record for a bound conversation rather than
    starting anything, and reporting that as "Session resumed" described a session that had
    not moved.

    Stood up with STOP_REQUESTED, **not** ENDED, and the distinction is the test's whole
    honesty: since Task 3.4, `get_by_resume_source` filters on `state <> 'ended'`
    (`session_store.py:89`), so an ENDED record with `created=False` is a pair the service can
    no longer produce. This test used to pin exactly that pair, and its docstring said
    "whatever state it is in" — a sentence Task 3.4 had already made false. A test standing up
    an impossible input proves nothing while reading as coverage.
    """
    stopping = replace(_record(), state=SessionState.STOP_REQUESTED)
    boundary = build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(
            catalogue=(PROJECT,),
            sessions=_BoundLauncher(stopping),
            conversations=ConversationService(_Catalogue(_resolved())),
        ),
        profiles=(ProfileAvailability("claude", True, None),),
    )
    token = boundary._callback("resume.confirm", "c-0123456789abcdef", mutation=True)
    boundary.callbacks.bind_pending(CHAT, 1)

    result = await boundary._resume_reply("c-0123456789abcdef", token, 1)

    assert "Not resumed" in str(result["text"])
    assert "Session resumed" not in str(result["text"])


@pytest.mark.asyncio
async def test_a_created_resume_that_failed_to_come_up_keeps_its_own_message() -> None:
    """The half of FAILED that no double stood up, and the one the guard exists for.

    `_resume_reply` answers "Resume did not become ready" only for `created and FAILED` — a
    session this press created whose pane did not come up. The parametrized row above covers
    the other half, `created=False` and FAILED, which is a conversation bound to a session
    that failed earlier and gets the attachment screen instead.

    The two rows together are what pin the guard: deleting `outcome.created` from the
    condition makes this test's session report an attachment it did not gain, and deleting the
    FAILED branch entirely makes the row above report an attempt this press never made.
    """
    failed = replace(_record(), state=SessionState.FAILED)
    boundary = build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(
            catalogue=(PROJECT,),
            sessions=_BoundLauncher(failed, created=True),
            conversations=ConversationService(_Catalogue(_resolved())),
        ),
        profiles=(ProfileAvailability("claude", True, None),),
    )
    token = boundary._callback("resume.confirm", "c-0123456789abcdef", mutation=True)
    boundary.callbacks.bind_pending(CHAT, 1)

    text = str((await boundary._resume_reply("c-0123456789abcdef", token, 1))["text"])

    assert "Resume did not become ready" in text
    assert "Not resumed" not in text, "a session this press created is not 'attached elsewhere'"
    assert "Session resumed" not in text


@pytest.mark.asyncio
async def test_a_launch_survives_a_notification_arriving_while_it_waits() -> None:
    """The race, end to end and in the order it really happens.

    A launch resolves its token, draws the pending screen, and only then claims. If an agent
    activity notification is delivered inside that gap, `LiveView.move_to_bottom` re-sends the
    screen below it and rebinds its tokens — and the launch used to answer "That action has
    already run" for a session it had not started.
    """
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    launcher = _LaunchingLauncher(_record())
    boundary = build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(catalogue=(PROJECT,), sessions=launcher),
        profiles=(ProfileAvailability("claude", True, None),),
    )
    launcher.launched: list[object] = []

    async def launch(command):
        launcher.launched.append(command)
        return launcher.record

    launcher.launch = launch

    await boundary.launch_command(chat.message_update("/launch"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)

    # The notification lands exactly where it hurts: after the token resolved, during the
    # pending render, before the claim.
    original = boundary.view.render
    moved: list[int] = []

    async def render_then_a_notification_arrives(*args, **kwargs):
        result = await original(*args, **kwargs)
        if not moved:
            moved.append(1)
            await boundary.view.move_to_bottom(chat.bot)
        return result

    boundary.view.render = render_then_a_notification_arrives
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Claude")), None)

    assert len(launcher.launched) == 1, "the launch ran"
    shown = chat.messages[boundary.view.anchor()].text
    assert "already run" not in shown, shown


@pytest.mark.parametrize("action", ["graceful", "cleanup", "force.confirmed"])
def test_a_stop_survives_a_notification_arriving_while_it_waits(action: str) -> None:
    """The same race as the launch above, on the path that fix did not reach.

    `ded49ab` removed the `message_id` match from `claim_mutation`, on the reasoning that
    `resolve` had already enforced message binding "before any caller reaches here -- all six
    call sites resolve first". That is true of five. `StopController.claim` performs its
    **own** `resolve` (`stops.py:144`), and it runs on the far side of the pending-screen
    round trip -- inside the very window the fix was written for -- so all three stops still
    answered "That action has already run" for a stop that never ran.

    Parametrized over the three stop actions rather than asserted for one, because
    `_PENDING_NOTICES` carries all three and the defect is a property of the path, not of
    the action travelling it. The force case is why this is a Critical rather than a nuisance:
    the owner has just passed a second confirmation on an irreversible kill, is told it
    already happened, and the pane survives.
    """
    boundary = build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(catalogue=(PROJECT,), sessions=_Launcher(_record())),
        profiles=(ProfileAvailability("claude", True, None),),
    )
    # `cleanup` is offered for PRESERVED and `graceful` for RUNNING (`available_actions`), so
    # each action is offered from the state that actually permits it rather than from one
    # state that would silently mint nothing for two of the three.
    record = _record()
    if action == "cleanup":
        record = replace(record, state=SessionState.PRESERVED)
    if action == "force.confirmed":
        token = boundary.stops.offer_confirmed_force(
            record.session_id, record.profile_id, record.state, None, OWNER, CHAT
        )
    else:
        token = boundary.stops.offer(
            record.session_id, record.profile_id, record.state, None, action, OWNER, CHAT
        )
    assert token is not None, f"{action} was not offered for a {record.state.name} session"

    anchor = 500
    boundary.callbacks.bind_pending(CHAT, anchor)
    # Exactly what `LiveView.move_to_bottom` does when a notification pushes the screen down.
    boundary.callbacks.rebind(CHAT, anchor, anchor + 1)

    request = boundary.stops.claim(token, OWNER, CHAT, anchor)

    assert request is not None, (
        f"{action} was refused after a rebind: the stop would answer "
        '"That action has already run" for a stop that never ran'
    )


@pytest.mark.asyncio
async def test_the_force_confirmation_survives_a_notification_arriving_while_it_opens() -> None:
    """The second `reread` call site, which the stop test above does not reach.

    `_force_confirm_reply` is the other caller that used to re-`resolve` downstream of the
    dispatcher. Its window is narrower than a stop's — a bare `force` carries no pending
    notice, so there is no Telegram round trip — but it is not zero: `_release_attachment` and
    `_abandon_entry` both await ahead of it, and a notification delivered there rebinds the
    token just the same. Pinned separately because a fix verified only through
    `StopController.claim` would leave this one to be rediscovered.
    """
    boundary = build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(catalogue=(PROJECT,), sessions=_Launcher(_record())),
        profiles=(ProfileAvailability("claude", True, None),),
    )
    record = _record()
    token = boundary.stops.offer(
        record.session_id, record.profile_id, record.state, None, "force", OWNER, CHAT
    )
    assert token is not None

    anchor = 600
    boundary.callbacks.bind_pending(CHAT, anchor)
    boundary.callbacks.rebind(CHAT, anchor, anchor + 1)

    # Through the real entry point. An earlier version of this test called
    # `callbacks.reread` directly and asserted its semantics — which are covered elsewhere —
    # while its docstring claimed to pin this call site. It would have stayed green through a
    # "defence in depth" edit restoring `resolve(..., message_id=...)` at service.py:1621,
    # which is the one change it exists to catch.
    rendered = await boundary._force_confirm_reply(token, anchor)

    assert "no longer available" not in rendered.text, (
        "the force confirmation refused a force stop that is still perfectly legal, because a "
        "notification rebound its token between the dispatcher's resolve and this screen"
    )
    assert "Force stop" in rendered.text
    labels = [button.text for row in rendered.keyboard for button in row]
    assert "Force stop" in labels and "Cancel" in labels
